"""Join coordination records with read-only GitHub observations for the UI."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from agent_control.config import Settings
from agent_control.github import GitHubError, GitHubProjectSnapshot, SnapshotCache
from agent_control.issue_projection import project_issues
from agent_control.models import (
    AgentReport,
    AgentRun,
    Blocker,
    OwnerAction,
    OwnerActionStatus,
    Priority,
    Project,
    Workstream,
)
from agent_control.storage import JsonRecordStore, StorageError


class ProjectSource(Protocol):
    def fetch_project(self, project: Project) -> GitHubProjectSnapshot: ...


@dataclass(frozen=True)
class CanvasData:
    projects: tuple[Project, ...]
    github: dict[str, GitHubProjectSnapshot]
    workstreams: tuple[Workstream, ...]
    agent_runs: tuple[AgentRun, ...]
    reports: tuple[AgentReport, ...]
    blockers: tuple[Blocker, ...]
    owner_actions: tuple[OwnerAction, ...]
    local_state_error: str | None
    generated_at: datetime
    coordination_warnings: tuple[str, ...] = ()

    def project(self, project_id: str) -> Project | None:
        return next((item for item in self.projects if item.id == project_id), None)

    def workstream(self, workstream_id: str) -> Workstream | None:
        return next((item for item in self.workstreams if item.id == workstream_id), None)

    def run(self, run_id: str) -> AgentRun | None:
        return next((item for item in self.agent_runs if item.id == run_id), None)


class CanvasProjection:
    def __init__(
        self,
        settings: Settings,
        store: JsonRecordStore,
        snapshot_cache: SnapshotCache,
        source: ProjectSource | None,
        *,
        source_error: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.snapshot_cache = snapshot_cache
        self.source = source
        self.source_error = source_error
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = 0
        self._github = self._cached_projects(settings)

    def _load_records(self, kind: str, expected: type[object]) -> tuple[object, ...]:
        return tuple(
            record for record, _revision in self.store.list(kind) if isinstance(record, expected)
        )

    def _cached_projects(self, settings: Settings) -> dict[str, GitHubProjectSnapshot]:
        return {
            config.id: self.snapshot_cache.stale(
                config.project(),
                GitHubError("pending", "GitHub synchronization has not completed"),
            )
            for config in settings.projects
        }

    def refresh_github(self) -> None:
        """Only the background worker uses GitHub; requests read snapshots."""
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                generation, settings = self._generation, self.settings
            for config in settings.projects:
                project = config.project()
                if self.source is None:
                    result = self.snapshot_cache.stale(
                        project,
                        GitHubError(
                            "configuration", self.source_error or "GitHub source unavailable"
                        ),
                    )
                else:
                    try:
                        result = self.source.fetch_project(project)
                        self.snapshot_cache.save(result)
                    except GitHubError as error:
                        result = self.snapshot_cache.stale(project, error)
                    except (OSError, ValueError):
                        result = self.snapshot_cache.stale(
                            project, GitHubError("storage", "Snapshot cache is unavailable")
                        )
                    except Exception:
                        result = self.snapshot_cache.stale(
                            project, GitHubError("internal", "GitHub synchronization failed")
                        )
                with self._lock:
                    if generation != self._generation:
                        return
                    self._github = {**self._github, project.id: result}
        finally:
            self._refresh_lock.release()

    def wait_for_refresh(self) -> None:
        """Called only at shutdown, before closing the GitHub HTTP client."""
        with self._refresh_lock:
            pass

    def reset_settings(self, settings: Settings) -> None:
        cached = self._cached_projects(settings)
        with self._lock:
            self.settings = settings
            self._generation += 1
            self._github = cached

    def data(self, *, force_refresh: bool = False) -> CanvasData:
        with self._lock:
            return self._data_locked(force_refresh=force_refresh)

    def _data_locked(self, *, force_refresh: bool = False) -> CanvasData:
        projects = tuple(item.project() for item in self.settings.projects)
        # A browser request must never perform network I/O or wait for a fetch.
        del force_refresh
        local_error: str | None = None
        warnings: tuple[str, ...] = ()
        if self.settings.coordination_source == "issues":
            state = project_issues(self._github, owner_login=self.settings.owner_login)
            workstreams, runs, reports = state.workstreams, state.agent_runs, state.reports
            blockers, actions = state.blockers, state.owner_actions
            # Per-issue integrity errors must not claim the whole local state is unavailable.
            warnings = state.errors[:50]
        else:
            try:
                workstreams = cast(
                    tuple[Workstream, ...], self._load_records("workstreams", Workstream)
                )
                runs = cast(tuple[AgentRun, ...], self._load_records("agent-runs", AgentRun))
                reports = cast(
                    tuple[AgentReport, ...], self._load_records("agent-reports", AgentReport)
                )
                blockers = cast(tuple[Blocker, ...], self._load_records("blockers", Blocker))
                actions = cast(
                    tuple[OwnerAction, ...], self._load_records("owner-actions", OwnerAction)
                )
            except StorageError as error:
                workstreams, runs, reports, blockers, actions = (), (), (), (), ()
                local_error = f"coordination state unavailable: {error}"
        return CanvasData(
            projects=projects,
            github=dict(self._github),
            workstreams=workstreams,
            agent_runs=runs,
            reports=reports,
            blockers=blockers,
            owner_actions=actions,
            local_state_error=local_error,
            generated_at=datetime.now(UTC),
            coordination_warnings=warnings,
        )

    def is_report_stale(self, run: AgentRun, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        anchor = run.last_report_at or run.started_at
        return (current - anchor).total_seconds() > self.settings.stale_report_seconds


def pending_actions(data: CanvasData) -> list[OwnerAction]:
    rank = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}
    return sorted(
        (item for item in data.owner_actions if item.status is OwnerActionStatus.PENDING),
        key=lambda item: (rank[item.priority], item.created_at),
    )


def current_project_ci(data: CanvasData, project_id: str) -> list[object]:
    snapshot = data.github.get(project_id)
    return list(snapshot.pull_requests) if snapshot else []
