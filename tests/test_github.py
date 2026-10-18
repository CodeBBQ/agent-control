from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_control.github import (
    CIStatus,
    GitHubError,
    GitHubProjectSnapshot,
    GitHubReadAdapter,
    ReviewStatus,
    SnapshotCache,
    aggregate_checks,
)
from agent_control.models import Project


def test_check_aggregation_never_applies_old_green_to_new_head() -> None:
    current = "a" * 40
    old = "b" * 40
    status, checks, observed = aggregate_checks(
        current,
        [
            {
                "name": "tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": old,
                "details_url": "https://github.com/CodeBBQ/example/actions/runs/1",
                "completed_at": "2026-10-15T10:00:00Z",
            }
        ],
    )
    assert status is CIStatus.STALE
    assert observed == old
    assert checks[0].status is CIStatus.SUCCESS


def test_read_adapter_projects_issues_pulls_checks_reviews_and_branches(tmp_path: Path) -> None:
    current = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/issues"):
            body = [
                {
                    "number": 2,
                    "title": "State",
                    "state": "open",
                    "html_url": "https://github.com/CodeBBQ/example/issues/2",
                    "updated_at": "2026-10-15T10:00:00Z",
                    "body": "Depends on #1",
                }
            ]
        elif path.endswith("/pulls"):
            body = [
                {
                    "number": 7,
                    "title": "Implementation",
                    "state": "open",
                    "draft": True,
                    "html_url": "https://github.com/CodeBBQ/example/pull/7",
                    "updated_at": "2026-10-15T10:01:00Z",
                    "head": {"sha": current, "ref": "feature/example"},
                    "base": {"ref": "main"},
                    "requested_reviewers": [{"login": "reviewer"}],
                }
            ]
        elif path.endswith("/branches"):
            body = [{"name": "main", "commit": {"sha": current}}]
        elif path.endswith("/check-runs"):
            body = {
                "check_runs": [
                    {
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": current,
                        "details_url": "https://github.com/CodeBBQ/example/actions/runs/1",
                        "completed_at": "2026-10-15T10:02:00Z",
                    }
                ]
            }
        elif path.endswith("/reviews"):
            body = []
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=body, headers={"etag": '"fixture"'})

    adapter = GitHubReadAdapter(
        token=None,
        cache_directory=tmp_path,
        transport=httpx.MockTransport(handler),
        read_relationships=False,
    )
    snapshot = adapter.fetch_project(
        Project(
            id="example",
            repository="CodeBBQ/example",
            display_name="Example",
            source_url="https://github.com/CodeBBQ/example",
        )
    )
    assert snapshot.issues[0].dependency_numbers == (1,)
    assert snapshot.pull_requests[0].ci_status is CIStatus.SUCCESS
    assert snapshot.pull_requests[0].ci_observed_sha == current
    assert snapshot.pull_requests[0].review_status is ReviewStatus.REVIEW_REQUESTED
    assert snapshot.branch_heads == {"main": current}


def test_api_failure_classification_and_stale_snapshot_preservation(tmp_path: Path) -> None:
    project = Project(
        id="example",
        repository="CodeBBQ/example",
        display_name="Example",
        source_url="https://github.com/CodeBBQ/example",
    )
    cache = SnapshotCache(tmp_path)
    prior = GitHubProjectSnapshot(
        project_id=project.id,
        repository=project.repository,
        observed_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    cache.save(prior)
    stale = cache.stale(project, GitHubError("rate_limited", "GitHub API returned HTTP 429", 429))
    assert stale.stale is True
    assert stale.observed_at == prior.observed_at
    assert stale.sync_error == "rate_limited: GitHub API returned HTTP 429"


def test_conditional_cache_uses_304_body(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=[], headers={"etag": '"v1"'})

    adapter = GitHubReadAdapter(
        token=None,
        cache_directory=tmp_path,
        transport=httpx.MockTransport(handler),
        read_relationships=False,
    )
    first, _ = adapter._request("/repos/CodeBBQ/example/issues", {"page": 1})
    second, _ = adapter._request("/repos/CodeBBQ/example/issues", {"page": 1})
    assert first == second == []
    assert calls == 2


@pytest.mark.parametrize(
    ("status_code", "headers", "classification"),
    (
        (403, {}, "permission"),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (429, {}, "rate_limited"),
        (500, {}, "server"),
    ),
)
def test_api_errors_are_classified_without_response_body_leakage(
    tmp_path: Path,
    status_code: int,
    headers: dict[str, str],
    classification: str,
) -> None:
    adapter = GitHubReadAdapter(
        token=None,
        cache_directory=tmp_path,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status_code, headers=headers, json={"message": "untrusted secret detail"}
            )
        ),
    )
    with pytest.raises(GitHubError) as caught:
        adapter._request("/repos/CodeBBQ/example/issues")
    assert caught.value.classification == classification
    assert "untrusted secret detail" not in str(caught.value)


def test_closed_prs_skip_expensive_ci_and_review_enrichment(tmp_path: Path) -> None:
    """Historical PR growth must not cause two extra network requests per PR."""
    calls: list[str] = []
    sha = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/issues") or path.endswith("/branches"):
            return httpx.Response(200, json=[])
        if path.endswith("/pulls"):
            pulls = [
                {
                    "number": number,
                    "title": f"PR {number}",
                    "state": "open" if number == 1 else "closed",
                    "draft": False,
                    "html_url": f"https://github.com/CodeBBQ/example/pull/{number}",
                    "updated_at": "2026-10-15T10:00:00Z",
                    "head": {"sha": sha, "ref": f"feature/issue-{number}-thing"},
                    "base": {"ref": "main"},
                }
                for number in range(1, 51)
            ]
            return httpx.Response(200, json=pulls)
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        raise AssertionError(path)

    adapter = GitHubReadAdapter(
        token=None,
        cache_directory=tmp_path,
        transport=httpx.MockTransport(handler),
        read_relationships=False,
    )
    snapshot = adapter.fetch_project(
        Project(
            id="example",
            repository="CodeBBQ/example",
            display_name="Example",
            source_url="https://github.com/CodeBBQ/example",
        )
    )
    assert len(snapshot.pull_requests) == 50
    assert snapshot.pull_requests[0].ci_status is CIStatus.MISSING
    assert all(p.ci_status is CIStatus.UNKNOWN for p in snapshot.pull_requests[1:])
    assert sum(path.endswith("/check-runs") for path in calls) == 1
    assert sum(path.endswith("/reviews") for path in calls) == 1


def test_repository_fetch_has_bounded_request_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow multi-page API must end with a timeout rather than infinite polling."""
    import time

    import agent_control.github as github

    monkeypatch.setattr(github, "PROJECT_FETCH_BUDGET_SECONDS", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.02)
        return httpx.Response(200, json=[{"ignored": True}] * 100)

    adapter = GitHubReadAdapter(
        token=None,
        cache_directory=tmp_path,
        transport=httpx.MockTransport(handler),
        read_relationships=False,
    )
    with pytest.raises(GitHubError, match="time budget") as caught:
        adapter.fetch_project(
            Project(
                id="example",
                repository="CodeBBQ/example",
                display_name="Example",
                source_url="https://github.com/CodeBBQ/example",
            )
        )
    assert caught.value.classification == "timeout"
    assert adapter._deadline is None


def test_bounded_native_issue_dependencies_and_milestones(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/issues"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": n,
                        "title": f"Work {n}",
                        "state": "open",
                        "html_url": f"https://github.com/CodeBBQ/example/issues/{n}",
                        "updated_at": "2026-10-15T10:00:00Z",
                        "comments": 0,
                        "milestone": {"number": 1, "title": "V1"},
                    }
                    for n in range(1, 8)
                ],
            )
        if path.endswith("/pulls") or path.endswith("/branches"):
            return httpx.Response(200, json=[])
        if path.endswith("/dependencies/blocked_by"):
            number = int(path.split("/")[-3])
            return httpx.Response(200, json=[{"number": number - 1}] if number > 1 else [])
        if path.endswith("/sub_issues"):
            return httpx.Response(200, json=[])
        raise AssertionError(path)

    adapter = GitHubReadAdapter(
        token=None, cache_directory=tmp_path, transport=httpx.MockTransport(handler)
    )
    project = Project(
        id="example",
        repository="CodeBBQ/example",
        display_name="Example",
        source_url="https://github.com/CodeBBQ/example",
    )
    first = adapter.fetch_project(project)
    assert all(issue.milestone_title == "V1" for issue in first.issues)
    assert sum(issue.native_links_observed for issue in first.issues) == 4
    assert first.issues[1].native_blocked_by == (1,)
    assert first.issues[-1].native_links_observed is False
    assert sum(p.endswith("/dependencies/blocked_by") for p in calls) == 4
    second = adapter.fetch_project(project)
    assert all(issue.native_links_observed for issue in second.issues)
    assert second.issues[-1].native_blocked_by == (6,)


def test_unread_native_dependencies_are_not_mislabeled_as_empty(tmp_path: Path) -> None:
    project = Project(
        id="example",
        repository="CodeBBQ/example",
        display_name="Example",
        source_url="https://github.com/CodeBBQ/example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/issues"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 9,
                        "title": "Feature",
                        "state": "open",
                        "html_url": "https://github.com/CodeBBQ/example/issues/9",
                        "updated_at": "2026-10-15T10:00:00Z",
                    }
                ],
            )
        if path.endswith("/pulls") or path.endswith("/branches"):
            return httpx.Response(200, json=[])
        return httpx.Response(403, json={"message": "forbidden"})

    adapter = GitHubReadAdapter(
        token=None, cache_directory=tmp_path, transport=httpx.MockTransport(handler)
    )
    snapshot = adapter.fetch_project(project)
    assert snapshot.issues[0].native_links_observed is False
