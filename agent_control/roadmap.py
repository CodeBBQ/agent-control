"""Read-only project roadmap and workstream view models.

This module exclusively projects cached GitHub observations; it never makes HTTP
requests and never claims that reported agent runs are presently alive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent_control.github import CIStatus, IssueObservation, PullRequestObservation
from agent_control.models import AgentRunStatus, Project, Workstream, WorkstreamPhase
from agent_control.projection import CanvasData


@dataclass(frozen=True)
class RoadmapIssue:
    number: int
    title: str
    workstream_id: str | None
    bucket: str
    is_sub_issue: bool


@dataclass(frozen=True)
class MilestoneSummary:
    title: str
    total: int
    closed: int
    in_progress: int
    open: int
    issues: tuple[RoadmapIssue, ...]
    omitted: int


@dataclass(frozen=True)
class ProjectSummary:
    project: Project
    total: int
    closed: int
    in_progress: int
    open: int
    milestones: tuple[MilestoneSummary, ...]
    stale: bool
    sync_error: str | None


@dataclass(frozen=True)
class DependencyLink:
    number: int
    source_url: str
    native: bool


@dataclass(frozen=True)
class WorkstreamCard:
    stream: Workstream
    issue: IssueObservation
    pull: PullRequestObservation | None
    milestone: str
    reported_agents: int
    historical_agents: int
    last_activity: datetime
    bucket: str
    dependencies: tuple[DependencyLink, ...]
    native_links_observed: bool
    sub_issues: tuple[int, ...]
    ci_status: str
    priority: int


def _bucket(issue: IssueObservation, stream: Workstream | None) -> str:
    if issue.state == "closed":
        return "closed"
    if stream and stream.phase not in {WorkstreamPhase.PLANNED, WorkstreamPhase.UNKNOWN}:
        return "in_progress"
    return "open"


def _milestone_summary(
    title: str,
    issues: list[IssueObservation],
    streams: dict[int, Workstream],
    children: set[int],
) -> MilestoneSummary:
    buckets = [_bucket(issue, streams.get(issue.number)) for issue in issues]
    ordered = sorted(
        issues,
        key=lambda issue: (
            issue.state == "closed",
            issue.number in children,
            -issue.updated_at.timestamp(),
        ),
    )
    # Bounded DOM for large repositories; totals still count every issue.
    visible = tuple(
        RoadmapIssue(
            number=issue.number,
            title=issue.title,
            workstream_id=streams[issue.number].id if issue.number in streams else None,
            bucket=_bucket(issue, streams.get(issue.number)),
            is_sub_issue=issue.number in children,
        )
        for issue in ordered[:30]
    )
    return MilestoneSummary(
        title=title,
        total=len(issues),
        closed=buckets.count("closed"),
        in_progress=buckets.count("in_progress"),
        open=buckets.count("open"),
        issues=visible,
        omitted=max(0, len(issues) - len(visible)),
    )


def project_roadmaps(data: CanvasData) -> tuple[ProjectSummary, ...]:
    result: list[ProjectSummary] = []
    for project in data.projects:
        snapshot = data.github.get(project.id)
        issues = list(snapshot.issues) if snapshot else []
        streams = {
            issue_number: stream
            for stream in data.workstreams
            if stream.project_id == project.id
            for issue_number in stream.issue_numbers
        }
        children = {number for issue in issues for number in issue.native_sub_issues}
        groups: dict[str, list[IssueObservation]] = {}
        for issue in issues:
            groups.setdefault(issue.milestone_title or "Unassigned", []).append(issue)
        milestone_groups = tuple(
            _milestone_summary(title, group, streams, children)
            for title, group in sorted(
                groups.items(), key=lambda kv: (kv[0] == "Unassigned", kv[0])
            )
        )
        result.append(
            ProjectSummary(
                project=project,
                total=len(issues),
                closed=sum(group.closed for group in milestone_groups),
                in_progress=sum(group.in_progress for group in milestone_groups),
                open=sum(group.open for group in milestone_groups),
                milestones=milestone_groups,
                stale=snapshot is None or snapshot.stale,
                sync_error=snapshot.sync_error if snapshot else "GitHub snapshot pending",
            )
        )
    return tuple(result)


def _ci_status(pull: PullRequestObservation | None, stale: bool) -> str:
    if pull is None:
        return "unknown"
    if stale:
        return "stale"
    if pull.ci_status is CIStatus.SUCCESS and pull.ci_observed_sha != pull.head_sha:
        return "stale"
    return pull.ci_status.value


def workstream_cards(data: CanvasData, project_id: str) -> tuple[WorkstreamCard, ...]:
    snapshot = data.github.get(project_id)
    if snapshot is None:
        return ()
    streams = {
        number: stream
        for stream in data.workstreams
        if stream.project_id == project_id
        for number in stream.issue_numbers
    }
    cards: list[WorkstreamCard] = []
    for issue in snapshot.issues:
        stream = streams.get(issue.number)
        if stream is None:
            continue
        pull = next(
            (pr for pr in snapshot.pull_requests if pr.number == stream.pull_request_number),
            None,
        )
        runs = [run for run in data.agent_runs if run.workstream_id == stream.id]
        unfinished = sum(
            run.status not in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED} for run in runs
        )
        links = [
            DependencyLink(
                number=n,
                source_url=f"https://github.com/{snapshot.repository}/issues/{n}",
                native=True,
            )
            for n in issue.native_blocked_by
            if n != issue.number
        ]
        links.extend(
            DependencyLink(
                number=n,
                source_url=f"https://github.com/{snapshot.repository}/issues/{n}",
                native=False,
            )
            for n in issue.dependency_numbers
            if n != issue.number and n not in issue.native_blocked_by
        )
        phase = _bucket(issue, stream)
        ci = _ci_status(pull, snapshot.stale)
        if phase != "closed" and (stream.phase is WorkstreamPhase.BLOCKED or ci == "failed"):
            priority = 0
        elif phase != "closed" and any(
            item.workstream_id == stream.id and item.status.value == "pending"
            for item in data.owner_actions
        ):
            priority = 1
        elif phase != "closed" and stream.phase in {
            WorkstreamPhase.REVIEW,
            WorkstreamPhase.VERIFYING,
        }:
            priority = 2
        elif phase == "in_progress":
            priority = 3
        elif phase == "open":
            priority = 4
        else:
            priority = 5
        cards.append(
            WorkstreamCard(
                stream=stream,
                issue=issue,
                pull=pull,
                milestone=issue.milestone_title or "Unassigned",
                reported_agents=unfinished,
                historical_agents=len(runs),
                last_activity=max(
                    issue.updated_at,
                    issue.latest_comment_at or issue.updated_at,
                    pull.updated_at if pull else issue.updated_at,
                ),
                bucket=phase,
                dependencies=tuple(links[:50]),
                native_links_observed=issue.native_links_observed,
                sub_issues=issue.native_sub_issues,
                ci_status=ci,
                priority=priority,
            )
        )
    return tuple(sorted(cards, key=lambda card: (card.priority, -card.last_activity.timestamp())))
