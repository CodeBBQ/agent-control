from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_control.config import ProjectConfig, Settings
from agent_control.demo import seed_demo
from agent_control.github import (
    CIStatus,
    GitHubProjectSnapshot,
    IssueObservation,
    PullRequestObservation,
    ReviewStatus,
    SnapshotCache,
)
from agent_control.models import Project
from agent_control.projection import CanvasProjection
from agent_control.storage import JsonRecordStore


class FakeSource:
    def __init__(self, snapshots: dict[str, GitHubProjectSnapshot]) -> None:
        self.snapshots = snapshots

    def fetch_project(self, project: Project) -> GitHubProjectSnapshot:
        return self.snapshots[project.id]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        github_token_env=None,
        coordination_source="local",
        refresh_seconds=30,
        stale_report_seconds=1800,
        projects=(
            ProjectConfig(
                id="agent-control",
                repository="example-org/control-demo",
                display_name="Agent Control Service",
            ),
            ProjectConfig(
                id="project-two",
                repository="example-org/project-two",
                display_name="Example Project Two",
            ),
        ),
    )


@pytest.fixture
def populated_store(settings: Settings) -> JsonRecordStore:
    store = JsonRecordStore(settings.state_directory)
    seed_demo(settings, store, now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    return store


@pytest.fixture
def projection(settings: Settings, populated_store: JsonRecordStore) -> CanvasProjection:
    current = "a" * 40
    old = "c" * 40
    observed = datetime(2026, 10, 15, 12, 1, tzinfo=UTC)
    first = GitHubProjectSnapshot(
        project_id="agent-control",
        repository="example-org/control-demo",
        observed_at=observed,
        issues=(
            IssueObservation(
                number=4,
                title="<script>alert('no')</script> Responsive canvas",
                state="open",
                source_url="https://github.com/example-org/control-demo/issues/4",
                updated_at=observed,
            ),
            IssueObservation(
                number=2,
                title="Coordination state",
                state="open",
                source_url="https://github.com/example-org/control-demo/issues/2",
                updated_at=observed,
            ),
        ),
        pull_requests=(
            PullRequestObservation(
                number=10,
                title="Initial canvas",
                state="open",
                draft=True,
                source_url="https://github.com/example-org/control-demo/pull/10",
                head_ref="feature/initial-control-canvas",
                base_ref="main",
                head_sha=current,
                updated_at=observed,
                ci_status=CIStatus.SUCCESS,
                ci_observed_sha=current,
                review_status=ReviewStatus.REVIEW_REQUESTED,
            ),
        ),
        branch_heads={"feature/initial-control-canvas": current},
    )
    second = GitHubProjectSnapshot(
        project_id="project-two",
        repository="example-org/project-two",
        observed_at=observed,
        pull_requests=(
            PullRequestObservation(
                number=21,
                title="Release verification",
                state="open",
                draft=False,
                source_url="https://github.com/example-org/project-two/pull/21",
                head_ref="feature/release-verification",
                base_ref="main",
                head_sha="b" * 40,
                updated_at=observed,
                ci_status=CIStatus.STALE,
                ci_observed_sha=old,
                review_status=ReviewStatus.CHANGES_REQUESTED,
            ),
            PullRequestObservation(
                number=22,
                title="Failed exact-head verification",
                state="open",
                draft=False,
                source_url="https://github.com/example-org/project-two/pull/22",
                head_ref="feature/failing-verification",
                base_ref="main",
                head_sha="d" * 40,
                updated_at=observed,
                ci_status=CIStatus.FAILED,
                ci_observed_sha="d" * 40,
                review_status=ReviewStatus.NONE,
            ),
        ),
    )
    projection = CanvasProjection(
        settings,
        populated_store,
        SnapshotCache(settings.cache_directory),
        FakeSource({"agent-control": first, "project-two": second}),
    )
    projection.refresh_github()
    return projection
