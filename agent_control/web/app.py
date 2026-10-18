"""Read-only coordination canvas plus explicit local operator administration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.responses import Response

from agent_control.config import ConfigurationError, Settings, default_config_path
from agent_control.github import CIStatus, GitHubReadAdapter, SnapshotCache
from agent_control.models import AgentRunStatus, OwnerActionStatus
from agent_control.pr_actions import PRActionError, PRActions
from agent_control.projection import CanvasData, CanvasProjection, pending_actions
from agent_control.repository_picker import _project
from agent_control.roadmap import project_roadmaps, workstream_cards
from agent_control.self_update import SelfUpdater, UpdateError
from agent_control.session_links import (
    SessionLinkError,
    SessionLinkStore,
    SessionReference,
    recent_session_links,
    validate_session_url,
)
from agent_control.setup import save_config
from agent_control.storage import JsonRecordStore

PACKAGE_ROOT = Path(__file__).resolve().parent


def static_asset_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _workstream_context(data: CanvasData, workstream_id: str) -> dict[str, Any]:
    workstream = data.workstream(workstream_id)
    if workstream is None:
        raise HTTPException(status_code=404, detail="Workstream not found")
    project = data.project(workstream.project_id)
    snapshot = data.github.get(workstream.project_id)
    issues = [
        issue
        for issue in (snapshot.issues if snapshot else ())
        if issue.number in workstream.issue_numbers
    ]
    pull = next(
        (
            item
            for item in (snapshot.pull_requests if snapshot else ())
            if item.number == workstream.pull_request_number
        ),
        None,
    )
    branch_head = (
        snapshot.branch_heads.get(workstream.owning_branch)
        if snapshot and workstream.owning_branch
        else None
    )
    branch_missing = bool(
        snapshot
        and not snapshot.stale
        and workstream.owning_branch
        and workstream.owning_branch not in snapshot.branch_heads
    )
    pull_missing = bool(
        snapshot and not snapshot.stale and workstream.pull_request_number and pull is None
    )
    runs = [item for item in data.agent_runs if item.workstream_id == workstream.id]
    run_ids = {item.id for item in runs}
    return {
        "workstream": workstream,
        "project": project,
        "snapshot": snapshot,
        "issues": issues,
        "pull": pull,
        "branch_head": branch_head,
        "branch_missing": branch_missing,
        "pull_missing": pull_missing,
        "runs": sorted(runs, key=lambda item: item.started_at, reverse=True),
        "reports": sorted(
            (item for item in data.reports if item.agent_run_id in run_ids),
            key=lambda item: item.reported_at,
            reverse=True,
        ),
        "blockers": [item for item in data.blockers if item.workstream_id == workstream.id],
        "actions": [item for item in data.owner_actions if item.workstream_id == workstream.id],
    }


def create_app(
    settings: Settings | None = None,
    *,
    projection: CanvasProjection | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    active = settings or Settings.default()
    active_path = (
        config_path
        if config_path is not None
        else (default_config_path() if settings is None else None)
    )
    csrf_token = secrets.token_urlsafe(32)
    session_store = SessionLinkStore(active.state_directory)
    updater = SelfUpdater(active, active_path) if active_path is not None else None
    pr_actions = PRActions(updater) if updater is not None else None
    static_root = PACKAGE_ROOT / "static"
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    templates.env.globals["asset_version"] = lambda filename: static_asset_fingerprint(
        static_root / filename
    )
    templates.env.globals["OwnerActionStatus"] = OwnerActionStatus
    templates.env.globals["AgentRunStatus"] = AgentRunStatus
    templates.env.globals["CIStatus"] = CIStatus
    templates.env.globals["csrf_token"] = csrf_token
    templates.env.globals["running_code_commit"] = (
        updater.code_commit() if updater is not None else "unavailable"
    )

    owned_source: GitHubReadAdapter | None = None
    if projection is None:
        source_error: str | None = None
        try:
            token = active.github_token
            owned_source = GitHubReadAdapter(
                token=token,
                cache_directory=active.cache_directory,
            )
        except ConfigurationError as error:
            source_error = str(error)
        projection = CanvasProjection(
            active,
            JsonRecordStore(active.state_directory),
            SnapshotCache(active.cache_directory),
            owned_source,
            source_error=source_error,
        )

    refresh_requested = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def poll_github() -> None:
            while True:
                # Background errors must never disable subsequent polls.
                with suppress(Exception):
                    await asyncio.to_thread(projection.refresh_github)
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        refresh_requested.wait(), timeout=projection.settings.refresh_seconds
                    )
                refresh_requested.clear()

        task = asyncio.create_task(poll_github())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(projection.wait_for_refresh)
            if owned_source is not None:
                owned_source.close()

    app = FastAPI(title="Agent Control Service", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = active
    app.state.projection = projection
    app.state.github_source = owned_source
    app.state.updater = updater
    app.state.pr_actions = pr_actions
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    def session_index(
        data: CanvasData, project_id: str
    ) -> tuple[dict[str, tuple[SessionReference, ...]], str | None]:
        project = data.project(project_id)
        if project is None:
            return {}, None
        storage_error: str | None = None
        try:
            local = session_store.for_repository(project.repository)
        except SessionLinkError:
            local = {}
            storage_error = "Local session links could not be read; check local storage."
        else:
            storage_error = None
        snapshot = data.github.get(project_id)
        issues = {issue.number: issue for issue in snapshot.issues} if snapshot else {}
        index: dict[str, tuple[SessionReference, ...]] = {}
        for stream in data.workstreams:
            if stream.project_id != project_id:
                continue
            number = stream.issue_numbers[0]
            observed = issues.get(number)
            index[stream.id] = recent_session_links(
                local.get(number, ()), observed.session_links if observed else ()
            )
        return index, storage_error

    def render(
        request: Request, template: str, context: dict[str, Any], status: int = 200
    ) -> Response:
        """Render one cached snapshot into the persistent three-column shell."""
        data = context.get("data") or projection.data()
        chosen = context.get("selected_project") or context.get("project")
        if chosen is None:
            chosen_id = request.query_params.get("project") or request.cookies.get(
                "agent_control_project"
            )
            chosen = next((item for item in data.projects if item.id == chosen_id), None)
        if chosen is None and data.projects:
            chosen = data.projects[0]
        queue = pending_actions(data)
        attention = {
            "owner_actions": len(queue),
            "blockers": sum(item.resolved_at is None for item in data.blockers),
            "stale_projects": sum(item.stale for item in data.github.values()),
            "failed_ci": sum(
                pull.ci_status is CIStatus.FAILED
                for snapshot in data.github.values()
                for pull in snapshot.pull_requests
            ),
        }
        response = templates.TemplateResponse(
            request,
            template,
            {
                "data": data,
                "selected_project": chosen,
                "roadmaps": project_roadmaps(data),
                "attention": attention,
                "side_actions": queue[:15],
                "recent_runs": sorted(
                    data.agent_runs, key=lambda run: run.started_at, reverse=True
                )[:15],
                "is_report_stale": projection.is_report_stale,
                **context,
            },
            status_code=status,
        )
        if chosen is not None:
            response.set_cookie(
                "agent_control_project", chosen.id, httponly=True, samesite="strict"
            )
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def overview(request: Request) -> Response:
        data = projection.data()
        selected_id = request.query_params.get("project") or request.cookies.get(
            "agent_control_project"
        )
        selected = next(
            (item for item in data.projects if item.id == selected_id),
            data.projects[0] if data.projects else None,
        )
        if selected is None:
            raise HTTPException(status_code=503, detail="No configured projects")
        session_links, session_link_error = session_index(data, selected.id)
        return render(
            request,
            "overview.html",
            {
                "data": data,
                "selected_project": selected,
                "selected_snapshot": data.github.get(selected.id),
                "workstream_cards": workstream_cards(data, selected.id),
                "session_links": session_links,
                "session_link_error": session_link_error,
            },
        )

    @app.get("/projects/{project_id}", include_in_schema=False)
    def project_page(request: Request, project_id: str) -> Response:
        data = projection.data()
        project = data.project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        workstreams = [item for item in data.workstreams if item.project_id == project_id]
        session_links, session_link_error = session_index(data, project_id)
        return render(
            request,
            "project.html",
            {
                "data": data,
                "project": project,
                "snapshot": data.github.get(project_id),
                "workstreams": workstreams,
                "runs": data.agent_runs,
                "selected_project": project,
                "selected_snapshot": data.github.get(project_id),
                "workstream_cards": workstream_cards(data, project_id),
                "session_links": session_links,
                "session_link_error": session_link_error,
            },
        )

    @app.get("/workstreams/{workstream_id}", include_in_schema=False)
    def workstream_page(request: Request, workstream_id: str) -> Response:
        data = projection.data()
        stream = data.workstream(workstream_id)
        if stream is None:
            raise HTTPException(status_code=404, detail="Workstream not found")
        sessions, session_link_error = session_index(data, stream.project_id)
        return render(
            request,
            "workstream.html",
            {
                "data": data,
                **_workstream_context(data, workstream_id),
                "is_report_stale": projection.is_report_stale,
                "sessions": sessions.get(workstream_id, ()),
                "session_link_error": session_link_error,
            },
        )

    @app.get("/agents/{run_id}", include_in_schema=False)
    def agent_page(request: Request, run_id: str) -> Response:
        data = projection.data()
        run = data.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Agent run not found")
        workstream = data.workstream(run.workstream_id)
        reports = sorted(
            (item for item in data.reports if item.agent_run_id == run.id),
            key=lambda item: item.reported_at,
            reverse=True,
        )
        return render(
            request,
            "agent.html",
            {
                "data": data,
                "run": run,
                "workstream": workstream,
                "selected_project": data.project(workstream.project_id) if workstream else None,
                "reports": reports,
                "is_stale": projection.is_report_stale(run),
            },
        )

    @app.get("/owner-actions", include_in_schema=False)
    def owner_actions_page(request: Request) -> Response:
        data = projection.data()
        return render(
            request,
            "owner_actions.html",
            {"data": data, "actions": pending_actions(data)},
        )

    @app.get("/failures", include_in_schema=False)
    def failures_page(request: Request) -> Response:
        data = projection.data()
        failed_pulls = [
            (data.project(project_id), pull)
            for project_id, snapshot in data.github.items()
            for pull in snapshot.pull_requests
            if pull.ci_status in {CIStatus.FAILED, CIStatus.CANCELLED, CIStatus.STALE}
        ]
        return render(
            request,
            "failures.html",
            {
                "data": data,
                "blockers": [item for item in data.blockers if item.resolved_at is None],
                "failed_pulls": failed_pulls,
                "sync_failures": [item for item in data.github.values() if item.sync_error],
            },
        )

    def verify_operator(request: Request) -> None:
        """Require a direct local browser with CSRF, never proxy-forwarded writes."""
        host = f"127.0.0.1:{active.port}"
        if (
            request.headers.get("host") != host
            or request.headers.get("origin") != f"http://{host}"
            or any(
                header in request.headers
                for header in (
                    "forwarded",
                    "x-forwarded-for",
                    "x-forwarded-host",
                    "x-forwarded-proto",
                    "x-real-ip",
                )
            )
            or not secrets.compare_digest(
                request.headers.get("x-agent-control-csrf", ""), csrf_token
            )
        ):
            raise HTTPException(status_code=403, detail="Local operator authorization required")
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(status_code=415, detail="Expected JSON")

    async def operator_json(request: Request) -> dict[str, Any]:
        verify_operator(request)
        body = await request.body()
        if len(body) > 16384:
            raise HTTPException(status_code=413, detail="Request is too large")
        try:
            payload = json.loads(body)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        return payload

    @app.post("/workstreams/{workstream_id}/sessions", include_in_schema=False)
    async def update_workstream_session(request: Request, workstream_id: str) -> JSONResponse:
        payload = await operator_json(request)
        data = projection.data()
        stream = data.workstream(workstream_id)
        project = data.project(stream.project_id) if stream else None
        if stream is None or project is None:
            raise HTTPException(status_code=404, detail="Workstream not found")
        url = payload.get("url")
        action = payload.get("action", "add")
        if not isinstance(url, str) or action not in {"add", "remove"}:
            raise HTTPException(status_code=422, detail="Invalid session link request")
        try:
            clean_url = validate_session_url(url)
            if action == "add":
                session_store.add(project.repository, stream.issue_numbers[0], clean_url)
                result = "saved"
            else:
                removed = session_store.remove(
                    project.repository, stream.issue_numbers[0], clean_url
                )
                result = "removed" if removed else "not_found"
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except SessionLinkError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return JSONResponse({"status": result})

    @app.get("/settings", include_in_schema=False)
    def settings_page(request: Request) -> Response:
        revision = ""
        if active_path is not None and active_path.is_file() and not active_path.is_symlink():
            revision = hashlib.sha256(active_path.read_bytes()).hexdigest()
        return render(
            request,
            "settings.html",
            {
                "settings": active,
                "repositories": "\n".join(project.repository for project in active.projects),
                "revision": revision,
                "editable": bool(revision),
            },
        )

    @app.post("/settings", include_in_schema=False)
    async def save_settings(request: Request) -> JSONResponse:
        nonlocal active
        payload = await operator_json(request)
        if active_path is None:
            raise HTTPException(status_code=403, detail="Writable config path unavailable")
        try:
            current = Settings.from_file(active_path)
            revision = hashlib.sha256(active_path.read_bytes()).hexdigest()
        except ConfigurationError as error:
            raise HTTPException(
                status_code=409, detail="Configuration changed externally"
            ) from error
        if payload.get("revision") != revision:
            raise HTTPException(status_code=409, detail="Settings changed; reload before saving")
        repositories = payload.get("repositories")
        if not isinstance(repositories, str) or len(repositories) > 8000:
            raise HTTPException(status_code=422, detail="Invalid repository list")
        selections = [item.strip() for item in repositories.replace(",", "\n").splitlines()]
        selections = [item for item in selections if item]
        if not 1 <= len(selections) <= 50:
            raise HTTPException(status_code=422, detail="Select between 1 and 50 repositories")
        previous = {item.repository.casefold(): item for item in current.projects}
        try:
            projects = tuple(previous.get(item.casefold()) or _project(item) for item in selections)
            changed = Settings.model_validate(
                {
                    **current.model_dump(),
                    "owner_login": payload.get("owner_login") or None,
                    "projects": projects,
                }
            )
            save_config(active_path, changed)
        except (ValueError, ValidationError, ConfigurationError) as error:
            raise HTTPException(status_code=422, detail=f"Invalid settings: {error}") from error
        active = changed
        app.state.settings = changed
        projection.reset_settings(changed)
        refresh_requested.set()
        return JSONResponse({"status": "saved"})

    @app.get("/maintenance/branches", include_in_schema=False)
    def maintenance_branches() -> dict[str, Any]:
        if updater is None:
            raise HTTPException(status_code=409, detail="Config-backed managed update unavailable")
        try:
            return updater.branches()
        except UpdateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/maintenance/pr", include_in_schema=False)
    async def maintenance_pr_preview(branch: str) -> dict[str, Any]:
        if pr_actions is None:
            raise HTTPException(status_code=409, detail="Managed PR actions are unavailable")
        try:
            return await run_in_threadpool(pr_actions.preview, branch)
        except PRActionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/maintenance/pr", include_in_schema=False)
    async def maintenance_pr_action(request: Request) -> dict[str, Any]:
        payload = await operator_json(request)
        if pr_actions is None:
            raise HTTPException(status_code=409, detail="Managed PR actions are unavailable")
        branch = payload.get("branch")
        number = payload.get("number")
        expected_head = payload.get("head")
        merge = payload.get("merge")
        confirmation = payload.get("confirmation")
        if (
            not isinstance(branch, str)
            or type(number) is not int
            or not isinstance(expected_head, str)
            or type(merge) is not bool
            or not isinstance(confirmation, str)
        ):
            raise HTTPException(status_code=422, detail="Invalid PR action request")
        try:
            return await run_in_threadpool(
                pr_actions.perform, branch, number, expected_head, merge, confirmation
            )
        except PRActionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/maintenance/update-status", include_in_schema=False)
    def maintenance_update_status() -> dict[str, Any]:
        if updater is None:
            return {"status": "unavailable"}
        try:
            return updater.status()
        except UpdateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/maintenance/update", include_in_schema=False)
    async def maintenance_update(request: Request) -> dict[str, str]:
        body = await operator_json(request)
        if updater is None:
            raise HTTPException(status_code=409, detail="Config-backed managed update unavailable")
        branch = body.get("branch")
        if not isinstance(branch, str):
            raise HTTPException(status_code=422, detail="Select a Git branch")
        try:
            return await run_in_threadpool(updater.queue, branch)
        except UpdateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/refresh", include_in_schema=False)
    async def refresh() -> Response:
        refresh_requested.set()
        return RedirectResponse("/", status_code=303)

    @app.exception_handler(404)
    async def not_found(request: Request, _error: Exception) -> Response:
        return render(
            request,
            "error.html",
            {"message": "The requested control-canvas item was not found."},
            404,
        )

    @app.exception_handler(500)
    async def internal_error(request: Request, _error: Exception) -> Response:
        return render(
            request,
            "error.html",
            {"message": "The dashboard could not render this view safely."},
            500,
        )

    return app


class DeferredApp:
    """Import-safe ASGI app; configuration is evaluated on first ASGI use."""

    def __init__(self) -> None:
        self.instance: FastAPI | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.instance is None:
            self.instance = create_app()
        await self.instance(scope, receive, send)


app = DeferredApp()
