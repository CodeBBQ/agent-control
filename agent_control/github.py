"""Read-only GitHub projection with exact-HEAD CI semantics."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent_control.models import Project, Sha, validate_github_url
from agent_control.session_links import SessionReference, session_links_in_comment

MAX_GITHUB_PAGES = 20
MAX_HTTP_CACHE_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
PROJECT_FETCH_BUDGET_SECONDS = 20.0
MAX_ENRICHED_OPEN_PRS = 12
MAX_RELATIONSHIP_ISSUES_PER_POLL = 4


class GitHubError(RuntimeError):
    """A classified, safe GitHub observation failure."""

    def __init__(self, classification: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.classification = classification
        self.status_code = status_code


class CIStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    CANCELLED = "cancelled"
    MISSING = "missing"
    UNKNOWN = "unknown"
    STALE = "stale"


class ReviewStatus(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REVIEW_REQUESTED = "review_requested"
    NONE = "none"
    UNKNOWN = "unknown"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must contain timezone")
    return value.astimezone(UTC)


class GitHubModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CheckObservation(GitHubModel):
    name: str = Field(min_length=1, max_length=240)
    status: CIStatus
    head_sha: Sha
    source_url: str = Field(max_length=500)
    completed_at: datetime | None = None

    _url = field_validator("source_url")(validate_github_url)
    _timestamp = field_validator("completed_at")(lambda value: _aware(value) if value else value)


class IssueObservation(GitHubModel):
    number: int = Field(ge=1)
    title: str = Field(max_length=500)
    state: str = Field(pattern=r"^(open|closed)$")
    source_url: str = Field(max_length=500)
    updated_at: datetime
    dependency_numbers: tuple[int, ...] = Field(default=(), max_length=50)
    milestone_title: str | None = Field(default=None, max_length=200)
    milestone_number: int | None = Field(default=None, ge=1)
    native_blocked_by: tuple[int, ...] = Field(default=(), max_length=50)
    native_sub_issues: tuple[int, ...] = Field(default=(), max_length=50)
    native_links_observed: bool = False
    latest_comment: str | None = Field(default=None, max_length=2000)
    latest_comment_at: datetime | None = None
    session_links: tuple[SessionReference, ...] = Field(default=(), max_length=20)

    _url = field_validator("source_url")(validate_github_url)
    _timestamp = field_validator("updated_at", "latest_comment_at")(
        lambda value: _aware(value) if value else value
    )


class PullRequestObservation(GitHubModel):
    number: int = Field(ge=1)
    title: str = Field(max_length=500)
    state: str = Field(pattern=r"^(open|closed)$")
    draft: bool
    source_url: str = Field(max_length=500)
    head_ref: str = Field(min_length=1, max_length=240)
    base_ref: str = Field(min_length=1, max_length=240)
    head_sha: Sha
    updated_at: datetime
    ci_status: CIStatus
    ci_observed_sha: Sha | None = None
    review_status: ReviewStatus
    checks: tuple[CheckObservation, ...] = Field(default=(), max_length=200)

    _url = field_validator("source_url")(validate_github_url)
    _timestamp = field_validator("updated_at")(_aware)


class GitHubProjectSnapshot(GitHubModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    project_id: str
    repository: str
    observed_at: datetime
    stale: bool = False
    sync_error: str | None = Field(default=None, max_length=500)
    issues: tuple[IssueObservation, ...] = Field(default=(), max_length=2000)
    pull_requests: tuple[PullRequestObservation, ...] = Field(default=(), max_length=500)
    branch_heads: dict[str, Sha] = Field(default_factory=dict)

    _timestamp = field_validator("observed_at")(_aware)


class HttpResponseCache:
    """Small conditional-request cache; it stores public API JSON, never auth headers."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def get(self, key: str) -> tuple[str, Any] | None:
        path = self._path(key)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_HTTP_CACHE_BYTES:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("etag"), str):
                return None
            return payload["etag"], payload.get("body")
        except (OSError, ValueError):
            return None

    def put(self, key: str, etag: str, body: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        serialized = json.dumps({"etag": etag, "body": body}, separators=(",", ":"))
        if len(serialized.encode()) > MAX_HTTP_CACHE_BYTES:
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GitHubError("malformed_response", "GitHub response omitted a timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as error:
        raise GitHubError(
            "malformed_response", "GitHub response contained an invalid timestamp"
        ) from error


def _text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise GitHubError("malformed_response", "GitHub response contained a non-text field")
    return value[:maximum]


def _dependencies(body: Any) -> tuple[int, ...]:
    if not isinstance(body, str):
        return ()
    matches = re.findall(
        r"(?im)\b(?:depends on|blocked by|dependency)\s*:?[ \t]*(?:[^#\n]*?)#(\d+)\b", body
    )
    return tuple(dict.fromkeys(int(match) for match in matches))[:50]


def aggregate_checks(
    current_head: str, raw_checks: Iterable[dict[str, Any]]
) -> tuple[CIStatus, tuple[CheckObservation, ...], str | None]:
    checks: list[CheckObservation] = []
    stale = False
    for item in raw_checks:
        head_sha = _text(item.get("head_sha"), maximum=40).lower()
        if head_sha != current_head:
            stale = True
        raw_status = item.get("status")
        conclusion = item.get("conclusion")
        if raw_status == "queued":
            status = CIStatus.QUEUED
        elif raw_status == "in_progress":
            status = CIStatus.IN_PROGRESS
        elif raw_status == "completed" and conclusion == "success":
            status = CIStatus.SUCCESS
        elif raw_status == "completed" and conclusion in {
            "failure",
            "timed_out",
            "action_required",
            "startup_failure",
        }:
            status = CIStatus.FAILED
        elif raw_status == "completed" and conclusion in {"cancelled", "skipped"}:
            status = CIStatus.CANCELLED
        else:
            status = CIStatus.UNKNOWN
        details_url = _text(item.get("details_url") or item.get("html_url"), maximum=500)
        checks.append(
            CheckObservation(
                name=_text(item.get("name"), maximum=240),
                status=status,
                head_sha=head_sha,
                source_url=details_url,
                completed_at=_parse_time(item["completed_at"])
                if item.get("completed_at")
                else None,
            )
        )
    if stale:
        return CIStatus.STALE, tuple(checks), checks[0].head_sha if checks else None
    if not checks:
        return CIStatus.MISSING, (), current_head
    statuses = {check.status for check in checks}
    if CIStatus.FAILED in statuses:
        combined = CIStatus.FAILED
    elif CIStatus.IN_PROGRESS in statuses:
        combined = CIStatus.IN_PROGRESS
    elif CIStatus.QUEUED in statuses:
        combined = CIStatus.QUEUED
    elif CIStatus.CANCELLED in statuses:
        combined = CIStatus.CANCELLED
    elif statuses == {CIStatus.SUCCESS}:
        combined = CIStatus.SUCCESS
    else:
        combined = CIStatus.UNKNOWN
    return combined, tuple(checks), current_head


def aggregate_reviews(
    raw_reviews: Iterable[dict[str, Any]], requested: Iterable[Any]
) -> ReviewStatus:
    latest_by_user: dict[str, str] = {}
    for review in raw_reviews:
        user = review.get("user") or {}
        login = user.get("login") if isinstance(user, dict) else None
        state = review.get("state")
        if (
            isinstance(login, str)
            and isinstance(state, str)
            and state in {"APPROVED", "CHANGES_REQUESTED"}
        ):
            latest_by_user[login] = state
    states = set(latest_by_user.values())
    if "CHANGES_REQUESTED" in states:
        return ReviewStatus.CHANGES_REQUESTED
    if requested:
        return ReviewStatus.REVIEW_REQUESTED
    if "APPROVED" in states:
        return ReviewStatus.APPROVED
    return ReviewStatus.NONE


class GitHubReadAdapter:
    def __init__(
        self,
        *,
        token: str | None,
        cache_directory: Path,
        transport: httpx.BaseTransport | None = None,
        read_relationships: bool = True,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-control-service/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=5,
            transport=transport,
        )
        self.http_cache = HttpResponseCache(cache_directory / "http")
        self._deadline: float | None = None
        self.read_relationships = read_relationships
        self._relationship_cursor: dict[str, int] = {}
        self._relationships: dict[str, dict[int, tuple[tuple[int, ...], tuple[int, ...]]]] = {}

    def close(self) -> None:
        self.client.close()

    def _request(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, httpx.Response]:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise GitHubError("timeout", "GitHub repository fetch exceeded its time budget")
        request = self.client.build_request("GET", path, params=params)
        key = str(request.url)
        cached = self.http_cache.get(key)
        if cached:
            request.headers["If-None-Match"] = cached[0]
        try:
            response = self.client.send(request)
        except httpx.HTTPError as error:
            raise GitHubError("network", "GitHub is currently unreachable") from error
        if response.status_code == 304 and cached:
            return cached[1], response
        if response.status_code >= 400:
            classification = (
                "rate_limited"
                if response.status_code == 429
                or (
                    response.status_code == 403
                    and response.headers.get("x-ratelimit-remaining") == "0"
                )
                else "permission"
                if response.status_code in {401, 403}
                else "not_found"
                if response.status_code == 404
                else "server"
                if response.status_code >= 500
                else "api"
            )
            raise GitHubError(
                classification,
                f"GitHub API returned HTTP {response.status_code}",
                response.status_code,
            )
        try:
            body = response.json()
        except ValueError as error:
            raise GitHubError("malformed_response", "GitHub returned invalid JSON") from error
        etag = response.headers.get("etag")
        if etag:
            self.http_cache.put(key, etag, body)
        return body, response

    def _pages(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        items: list[Any] = []
        page = 1
        while page <= MAX_GITHUB_PAGES:
            query = {**(params or {}), "per_page": 100, "page": page}
            body, response = self._request(path, query)
            if not isinstance(body, list):
                raise GitHubError("malformed_response", "GitHub list endpoint returned a non-list")
            items.extend(body)
            if 'rel="next"' not in response.headers.get("link", "") and len(body) < 100:
                return items
            page += 1
        raise GitHubError("pagination_limit", f"GitHub response exceeded {MAX_GITHUB_PAGES} pages")

    def _check_runs(self, path: str) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for page in range(1, MAX_GITHUB_PAGES + 1):
            body, _response = self._request(path, {"per_page": 100, "page": page})
            if not isinstance(body, dict) or not isinstance(body.get("check_runs"), list):
                raise GitHubError("malformed_response", "check-runs response is malformed")
            page_checks = body["check_runs"]
            if not all(isinstance(item, dict) for item in page_checks):
                raise GitHubError(
                    "malformed_response", "check-runs response contains invalid items"
                )
            checks.extend(page_checks)
            if len(page_checks) < 100:
                return checks
        raise GitHubError("pagination_limit", "check runs exceeded the safe pagination limit")

    def fetch_project(self, project: Project) -> GitHubProjectSnapshot:
        self._deadline = time.monotonic() + PROJECT_FETCH_BUDGET_SECONDS
        try:
            return self._fetch_project(project)
        finally:
            self._deadline = None

    def _fetch_project(self, project: Project) -> GitHubProjectSnapshot:
        owner, repo = project.repository.split("/", 1)
        root = f"/repos/{owner}/{repo}"
        raw_issues = self._pages(f"{root}/issues", {"state": "all", "sort": "updated"})
        raw_pulls = self._pages(f"{root}/pulls", {"state": "all", "sort": "updated"})
        raw_branches = self._pages(f"{root}/branches")
        # One paginated comment feed per repository rather than a request for
        # each unmarked issue, keeping the 120-second poll within API budgets.
        latest_comments: dict[int, tuple[str, datetime]] = {}
        session_records: dict[int, list[SessionReference]] = {}
        if any(
            isinstance(item, dict) and "pull_request" not in item and item.get("comments", 0)
            for item in raw_issues
        ):
            prefix = f"https://api.github.com{root}/issues/"
            for comment in self._pages(
                f"{root}/issues/comments", {"sort": "updated", "direction": "desc"}
            ):
                if not isinstance(comment, dict):
                    continue
                url, body = comment.get("issue_url"), comment.get("body")
                if not isinstance(url, str) or not url.startswith(prefix):
                    continue
                suffix = url[len(prefix) :]
                if not suffix.isdecimal() or not isinstance(body, str):
                    continue
                try:
                    updated = _parse_time(comment.get("updated_at"))
                except (TypeError, ValueError):
                    continue
                comment_issue_number = int(suffix)
                comment_id = comment.get("id")
                comment_url = (
                    f"https://github.com/{project.repository}/issues/"
                    f"{comment_issue_number}#issuecomment-{comment_id}"
                    if isinstance(comment_id, int) and comment_id > 0
                    else None
                )
                session_records.setdefault(comment_issue_number, []).extend(
                    session_links_in_comment(body, observed_at=updated, comment_url=comment_url)
                )
                prior = latest_comments.get(comment_issue_number)
                if prior is None or updated >= prior[1]:
                    latest_comments[comment_issue_number] = (body[:2000], updated)
        # Bound native dependency/sub-issue reads to a small rotating sample.
        # An issue without a completed read is UNKNOWN, never "no dependencies".
        previous_links = self._relationships.setdefault(project.repository, {})
        candidates = [
            item["number"]
            for item in raw_issues
            if isinstance(item, dict)
            and "pull_request" not in item
            and isinstance(item.get("number"), int)
        ]
        if self.read_relationships and candidates:
            start = self._relationship_cursor.get(project.repository, 0) % len(candidates)
            ordered = candidates[start:] + candidates[:start]
            completed = 0
            for number in ordered[:MAX_RELATIONSHIP_ISSUES_PER_POLL]:
                try:
                    blocked = self._pages(f"{root}/issues/{number}/dependencies/blocked_by")
                    children = self._pages(f"{root}/issues/{number}/sub_issues")

                    def issue_numbers(rows: list[Any]) -> tuple[int, ...]:
                        numbers = [
                            entry["number"]
                            for entry in rows
                            if isinstance(entry, dict)
                            and isinstance(entry.get("number"), int)
                            and entry.get("repository_url", f"https://api.github.com{root}")
                            == f"https://api.github.com{root}"
                        ]
                        return tuple(dict.fromkeys(numbers[:50]))

                    previous_links[number] = (issue_numbers(blocked), issue_numbers(children))
                except GitHubError as error:
                    # Endpoint availability and permissions are independent of
                    # main issue access. Preserve prior verified links.
                    if error.classification in {"rate_limited", "timeout"}:
                        break
                completed += 1
            self._relationship_cursor[project.repository] = start + max(completed, 1)
        issues: list[IssueObservation] = []
        pulls: list[PullRequestObservation] = []
        for item in raw_issues:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            issue_number = item.get("number")
            latest = latest_comments.get(issue_number) if isinstance(issue_number, int) else None
            latest_comment: str | None = latest[0] if latest else None
            latest_comment_at: datetime | None = latest[1] if latest else None
            session_history: dict[str, SessionReference] = {}
            if isinstance(issue_number, int):
                for entry in session_records.get(issue_number, []):
                    existing = session_history.get(entry.url)
                    if existing is None or entry.observed_at >= existing.observed_at:
                        session_history[entry.url] = entry
            recent_sessions = tuple(
                sorted(session_history.values(), key=lambda entry: entry.observed_at, reverse=True)[
                    :20
                ]
            )
            milestone = item.get("milestone")
            milestone_data = milestone if isinstance(milestone, dict) else {}
            milestone_title = milestone_data.get("title")
            native_links = (
                previous_links.get(issue_number) if isinstance(issue_number, int) else None
            )
            issues.append(
                IssueObservation(
                    number=item.get("number"),
                    title=_text(item.get("title"), maximum=500),
                    state=item.get("state"),
                    source_url=_text(item.get("html_url"), maximum=500),
                    updated_at=_parse_time(item.get("updated_at")),
                    dependency_numbers=_dependencies(item.get("body")),
                    milestone_title=(
                        _text(milestone_title, maximum=200) if milestone_title is not None else None
                    ),
                    milestone_number=milestone_data.get("number"),
                    native_blocked_by=native_links[0] if native_links else (),
                    native_sub_issues=native_links[1] if native_links else (),
                    native_links_observed=native_links is not None,
                    latest_comment=latest_comment,
                    latest_comment_at=latest_comment_at,
                    session_links=recent_sessions,
                )
            )
        enriched = 0
        for item in raw_pulls:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            head = item.get("head") or {}
            base = item.get("base") or {}
            current_sha = _text(
                head.get("sha") if isinstance(head, dict) else None, maximum=40
            ).lower()
            # Do not request checks and reviews for every historical closed PR.
            # Older open PRs beyond this budget retain UNKNOWN, never a fake PASS.
            if item.get("state") == "open" and enriched < MAX_ENRICHED_OPEN_PRS:
                check_runs = self._check_runs(f"{root}/commits/{current_sha}/check-runs")
                raw_reviews = self._pages(f"{root}/pulls/{number}/reviews")
                ci_status, checks, observed_sha = aggregate_checks(current_sha, check_runs)
                review = aggregate_reviews(raw_reviews, item.get("requested_reviewers") or [])
                enriched += 1
            else:
                ci_status, checks, observed_sha = CIStatus.UNKNOWN, (), None
                review = ReviewStatus.UNKNOWN
            pulls.append(
                PullRequestObservation(
                    number=number,
                    title=_text(item.get("title"), maximum=500),
                    state=item.get("state"),
                    draft=bool(item.get("draft")),
                    source_url=_text(item.get("html_url"), maximum=500),
                    head_ref=_text(
                        head.get("ref") if isinstance(head, dict) else None, maximum=240
                    ),
                    base_ref=_text(
                        base.get("ref") if isinstance(base, dict) else None, maximum=240
                    ),
                    head_sha=current_sha,
                    updated_at=_parse_time(item.get("updated_at")),
                    ci_status=ci_status,
                    ci_observed_sha=observed_sha,
                    review_status=review,
                    checks=checks,
                )
            )
        branch_heads: dict[str, str] = {}
        for item in raw_branches:
            if not isinstance(item, dict) or not isinstance(item.get("commit"), dict):
                continue
            branch_heads[_text(item.get("name"), maximum=240)] = _text(
                item["commit"].get("sha"), maximum=40
            ).lower()
        try:
            return GitHubProjectSnapshot(
                project_id=project.id,
                repository=project.repository,
                observed_at=datetime.now(UTC),
                issues=tuple(issues),
                pull_requests=tuple(pulls),
                branch_heads=branch_heads,
            )
        except ValidationError as error:
            raise GitHubError(
                "malformed_response", "GitHub response failed schema validation"
            ) from error


class SnapshotCache:
    """Last-successful project snapshots survive API outages and service restarts."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", project_id):
            raise ValueError("invalid project id")
        return self.root / "projects" / f"{project_id}.json"

    def save(self, snapshot: GitHubProjectSnapshot) -> None:
        path = self._path(snapshot.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise GitHubError("unsafe_cache", "snapshot cache path is a symlink")
        payload = snapshot.model_dump_json(indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise GitHubError("snapshot_too_large", "GitHub snapshot exceeds the safe cache bound")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def load(self, project_id: str) -> GitHubProjectSnapshot | None:
        path = self._path(project_id)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        try:
            return GitHubProjectSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            return None

    def stale(self, project: Project, error: GitHubError) -> GitHubProjectSnapshot:
        prior = self.load(project.id)
        safe_error = f"{error.classification}: {error}"
        if prior:
            return prior.model_copy(update={"stale": True, "sync_error": safe_error})
        return GitHubProjectSnapshot(
            project_id=project.id,
            repository=project.repository,
            observed_at=datetime.now(UTC),
            stale=True,
            sync_error=safe_error,
        )
