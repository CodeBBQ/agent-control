"""Regression coverage for passive issue projection without machine protocols."""

from datetime import UTC, datetime

from agent_control.github import GitHubProjectSnapshot, IssueObservation
from agent_control.issue_projection import project_issues
from agent_control.models import WorkstreamPhase


def test_ordinary_issues_project_without_markers_or_publisher() -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    snapshot = GitHubProjectSnapshot(
        project_id="example",
        repository="CodeBBQ/example",
        observed_at=now,
        issues=(
            IssueObservation(
                number=1,
                title="Design",
                state="open",
                source_url="https://github.com/CodeBBQ/example/issues/1",
                updated_at=now,
            ),
            IssueObservation(
                number=2,
                title="Implement",
                state="open",
                source_url="https://github.com/CodeBBQ/example/issues/2",
                updated_at=now,
                native_blocked_by=(1,),
            ),
        ),
    )
    result = project_issues({"example": snapshot}, owner_login=None)
    assert [item.id for item in result.workstreams] == ["example-issue-1", "example-issue-2"]
    assert result.workstreams[0].phase is WorkstreamPhase.PLANNED
    assert result.workstreams[1].dependency_ids == ("example-issue-1",)
    assert not result.agent_runs and not result.reports
    assert not result.blockers and not result.owner_actions and not result.errors
