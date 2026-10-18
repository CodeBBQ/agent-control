"""Conservative read-only workstream projection from ordinary GitHub evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from agent_control.github import CIStatus, GitHubProjectSnapshot, ReviewStatus
from agent_control.models import (
    AgentReport,
    AgentRun,
    Blocker,
    OwnerAction,
    Workstream,
    WorkstreamPhase,
)


def _belongs_to_issue(branch: str, number: int) -> bool:
    return re.search(rf"(?:^|[/_-])issue[-_/]?{number}(?![0-9])", branch, re.I) is not None


def _github_phase(
    issue: object, pull: object | None, branch: str | None
) -> tuple[WorkstreamPhase, str]:
    from agent_control.github import IssueObservation, PullRequestObservation

    assert isinstance(issue, IssueObservation)
    assert pull is None or isinstance(pull, PullRequestObservation)
    note = (issue.latest_comment or "").lower()
    if pull and pull.state == "open":
        if (
            pull.ci_status is CIStatus.FAILED
            or pull.review_status is ReviewStatus.CHANGES_REQUESTED
        ):
            return WorkstreamPhase.BLOCKED, "Review failed CI or changes requested"
        if pull.ci_status in {CIStatus.QUEUED, CIStatus.IN_PROGRESS}:
            return WorkstreamPhase.VERIFYING, "Await current-head CI"
        if pull.review_status is ReviewStatus.APPROVED and pull.ci_status is CIStatus.SUCCESS:
            return WorkstreamPhase.REVIEW, "Await explicit owner integration decision"
        if pull.draft and ("specification only" in note or "investigation/specification" in note):
            return WorkstreamPhase.INVESTIGATING, "Review proposed contract and owner decisions"
        if "ready for integration" in note:
            return WorkstreamPhase.REVIEW, "Reported ready; verify independently"
        if (
            pull.ci_status is CIStatus.SUCCESS
            or pull.review_status is ReviewStatus.REVIEW_REQUESTED
        ):
            return WorkstreamPhase.REVIEW, "Review PR and outstanding acceptance gates"
        return WorkstreamPhase.IMPLEMENTING, "Inspect PR and pending checks"
    if issue.state == "closed":
        return WorkstreamPhase.COMPLETE, "Issue closed; acceptance not independently verified"
    if "changes required" in note or "blocked" in note:
        return WorkstreamPhase.BLOCKED, "Review latest reported blocker"
    if "investigation" in note or "specification" in note or "contract" in note:
        return WorkstreamPhase.INVESTIGATING, "Review issue investigation and owner decisions"
    if branch:
        return WorkstreamPhase.IMPLEMENTING, "Inspect branch and acceptance gates"
    return WorkstreamPhase.PLANNED, "Review issue and next action"


@dataclass(frozen=True)
class IssueState:
    workstreams: tuple[Workstream, ...]
    agent_runs: tuple[AgentRun, ...]
    reports: tuple[AgentReport, ...]
    blockers: tuple[Blocker, ...]
    owner_actions: tuple[OwnerAction, ...]
    errors: tuple[str, ...]


def project_issues(
    snapshots: dict[str, GitHubProjectSnapshot], *, owner_login: str | None
) -> IssueState:
    # Owner login is retained for API compatibility, not used for machine event authorization.
    _ = owner_login
    streams: dict[str, Workstream] = {}
    by_ref: dict[str, str] = {}
    errors: list[str] = []

    for project_id, snapshot in snapshots.items():
        for issue in snapshot.issues:
            ref = f"{snapshot.repository}#{issue.number}"
            stream_id = f"{project_id}-issue-{issue.number}"
            if stream_id in streams or ref in by_ref:
                errors.append(f"{ref}: duplicate issue")
                continue
            try:
                pull = next(
                    (
                        p
                        for p in snapshot.pull_requests
                        if _belongs_to_issue(p.head_ref, issue.number)
                    ),
                    None,
                )
                branch = (
                    pull.head_ref
                    if pull
                    else next(
                        (
                            name
                            for name in snapshot.branch_heads
                            if _belongs_to_issue(name, issue.number)
                        ),
                        None,
                    )
                )
                phase, next_action = _github_phase(issue, pull, branch)
                streams[stream_id] = Workstream(
                    id=stream_id,
                    project_id=project_id,
                    title=issue.title[:240] or "Untitled workstream",
                    phase=phase,
                    issue_numbers=(issue.number,),
                    next_action=next_action,
                    owning_branch=branch,
                    pull_request_number=pull.number if pull else None,
                    expected_head_sha=(
                        pull.head_sha if pull else snapshot.branch_heads.get(branch or "")
                    ),
                    updated_at=max(issue.updated_at, issue.latest_comment_at or issue.updated_at),
                )
                by_ref[ref] = stream_id
            except (ValidationError, ValueError):
                errors.append(f"{ref}: invalid observed workstream")

    for snapshot in snapshots.values():
        for issue in snapshot.issues:
            ref = f"{snapshot.repository}#{issue.number}"
            if ref not in by_ref:
                continue
            stream_id = by_ref[ref]
            dependency_refs = tuple(
                f"{snapshot.repository}#{number}"
                for number in (*issue.native_blocked_by, *issue.dependency_numbers)
            )
            dependency_ids = tuple(
                dict.fromkeys(
                    by_ref[other]
                    for other in dependency_refs
                    if other in by_ref and by_ref[other] != stream_id
                )
            )
            try:
                updated = streams[stream_id].model_copy(update={"dependency_ids": dependency_ids})
                Workstream.model_validate(updated.model_dump())
                streams[stream_id] = updated
            except ValidationError:
                errors.append(f"{ref}: invalid dependency graph")

    return IssueState(tuple(streams.values()), (), (), (), (), tuple(errors))
