"""Deterministic-shape synthetic data for first-run exploration and tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_control.config import Settings
from agent_control.models import (
    AgentReport,
    AgentRun,
    AgentRunStatus,
    Blocker,
    BlockerClassification,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    OwnerAction,
    OwnerActionType,
    Priority,
    ReportPhase,
    TruthStatus,
    Workstream,
    WorkstreamPhase,
)
from agent_control.storage import JsonRecordStore, RecordNotFound


def _evidence(identifier: str, repository: str, issue: int, when: datetime) -> EvidenceReference:
    return EvidenceReference(
        id=identifier,
        origin=EvidenceOrigin.AGENT_REPORTED,
        truth_status=TruthStatus.REPORTED,
        kind=EvidenceKind.ISSUE,
        observed_at=when,
        source_url=f"https://github.com/{repository}/issues/{issue}",
        source_ref=f"issue:{issue}",
        summary=f"Agent report references canonical issue #{issue}",
    )


def initialize_projects(settings: Settings, store: JsonRecordStore) -> None:
    store.initialize()
    for item in settings.projects:
        project = item.project()
        try:
            store.get("projects", project.id)
        except RecordNotFound:
            store.save(
                "projects",
                project,
                expected_revision=0,
                idempotency_key=f"initialize-project:{project.id}",
            )


def seed_demo(settings: Settings, store: JsonRecordStore, *, now: datetime | None = None) -> None:
    if len(settings.projects) < 2:
        raise ValueError("demo state requires at least two configured projects")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    initialize_projects(settings, store)
    first, second = (item.project() for item in settings.projects[:2])
    workstreams = (
        Workstream(
            id=f"{first.id}-canvas",
            project_id=first.id,
            title="Initial control canvas",
            phase=WorkstreamPhase.VERIFYING,
            issue_numbers=(4, 9),
            next_action="Review exact-HEAD checks and perform browser acceptance",
            owning_branch="feature/initial-control-canvas",
            pull_request_number=10,
            expected_head_sha="a" * 40,
            updated_at=current - timedelta(minutes=8),
        ),
        Workstream(
            id=f"{first.id}-state",
            project_id=first.id,
            title="Conflict-safe coordination state",
            phase=WorkstreamPhase.BLOCKED,
            issue_numbers=(2, 5, 7),
            dependency_ids=(f"{first.id}-canvas",),
            next_action="Owner selects protected remote state location and writer scope",
            owning_branch="feature/initial-control-canvas",
            updated_at=current - timedelta(hours=2),
        ),
        Workstream(
            id=f"{second.id}-release",
            project_id=second.id,
            title="Independent release verification",
            phase=WorkstreamPhase.IMPLEMENTING,
            issue_numbers=(1,),
            next_action="Complete current implementation and publish evidence",
            owning_branch="feature/release-verification",
            pull_request_number=21,
            expected_head_sha="b" * 40,
            updated_at=current - timedelta(minutes=20),
        ),
    )
    for workstream in workstreams:
        store.save(
            "workstreams",
            workstream,
            expected_revision=0,
            idempotency_key=f"demo-workstream:{workstream.id}",
        )
    evidence = _evidence("ev-recent", first.repository, 4, current - timedelta(minutes=9))
    stale_evidence = _evidence("ev-stale", first.repository, 2, current - timedelta(hours=8))
    runs = (
        AgentRun(
            id="run-canvas-01",
            workstream_id=workstreams[0].id,
            agent_identity="Local implementation agent",
            adapter="explicit-report-cli",
            environment="local checkout",
            status=AgentRunStatus.REPORTED,
            started_at=current - timedelta(hours=1),
            last_report_at=current - timedelta(minutes=9),
            latest_report_id="report-canvas-01",
        ),
        AgentRun(
            id="run-state-01",
            workstream_id=workstreams[1].id,
            agent_identity="Security contract analyst",
            adapter="explicit-report-cli",
            environment="cloud agent",
            status=AgentRunStatus.REPORTED,
            started_at=current - timedelta(hours=10),
            last_report_at=current - timedelta(hours=8),
            latest_report_id="report-state-01",
        ),
        AgentRun(
            id="run-release-01",
            workstream_id=workstreams[2].id,
            agent_identity="Release agent",
            adapter="explicit-report-cli",
            environment="local worktree",
            status=AgentRunStatus.COMPLETED,
            started_at=current - timedelta(hours=3),
            last_report_at=current - timedelta(hours=1),
            completed_at=current - timedelta(minutes=50),
            latest_report_id="report-release-01",
            completion_summary=(
                "Implementation reported complete; review and CI remain separate gates"
            ),
        ),
    )
    for run in runs:
        store.save("agent-runs", run, expected_revision=0, idempotency_key=f"demo-run:{run.id}")
    reports = (
        AgentReport(
            id="report-canvas-01",
            agent_run_id=runs[0].id,
            workstream_id=workstreams[0].id,
            phase=ReportPhase.VERIFYING,
            summary="The agent reports that application and hermetic tests are ready for review.",
            next_action="Observe CI on the exact pull-request head",
            reported_at=current - timedelta(minutes=9),
            evidence=(evidence,),
        ),
        AgentReport(
            id="report-state-01",
            agent_run_id=runs[1].id,
            workstream_id=workstreams[1].id,
            phase=ReportPhase.WAITING,
            summary="Remote writes remain gated on a least-privilege writer decision.",
            next_action="Owner chooses state branch or separate private repository",
            reported_at=current - timedelta(hours=8),
            evidence=(stale_evidence,),
        ),
        AgentReport(
            id="report-release-01",
            agent_run_id=runs[2].id,
            workstream_id=workstreams[2].id,
            phase=ReportPhase.VERIFYING,
            summary=(
                "The agent reports a complete candidate revision; "
                "no merge authorization is implied."
            ),
            next_action="Independent review",
            reported_at=current - timedelta(hours=1),
            evidence=(_evidence("ev-release", second.repository, 1, current - timedelta(hours=1)),),
        ),
    )
    for report in reports:
        store.save(
            "agent-reports", report, expected_revision=0, idempotency_key=f"demo-report:{report.id}"
        )
    blocker = Blocker(
        id="blocker-remote-write-auth",
        workstream_id=workstreams[1].id,
        agent_run_id=runs[1].id,
        classification=BlockerClassification.SECURITY,
        summary="Remote writer authorization is unresolved",
        detail=(
            "No protected state branch or separate state repository and writer scope "
            "has been accepted."
        ),
        created_at=current - timedelta(hours=8),
        evidence=(stale_evidence,),
    )
    store.save(
        "blockers", blocker, expected_revision=0, idempotency_key="demo-blocker:remote-write"
    )
    actions = (
        OwnerAction(
            id="action-state-location",
            workstream_id=workstreams[1].id,
            agent_run_id=runs[1].id,
            action_type=OwnerActionType.SECURITY_DECISION,
            priority=Priority.HIGH,
            reason=(
                "Cross-agent writes cannot be enabled without an accepted repository/branch "
                "and credential scope."
            ),
            requested_action="Select the remote coordination-state location and writer policy",
            created_at=current - timedelta(hours=8),
            updated_at=current - timedelta(hours=8),
            evidence=(stale_evidence,),
        ),
        OwnerAction(
            id="action-mobile-check",
            workstream_id=workstreams[0].id,
            agent_run_id=runs[0].id,
            action_type=OwnerActionType.MANUAL_ACCEPTANCE,
            priority=Priority.MEDIUM,
            reason="Responsive CSS tests cannot establish real Samsung browser behavior.",
            requested_action="Perform the physical mobile-browser acceptance check",
            created_at=current - timedelta(minutes=8),
            updated_at=current - timedelta(minutes=8),
            evidence=(evidence,),
        ),
    )
    for action in actions:
        store.save(
            "owner-actions", action, expected_revision=0, idempotency_key=f"demo-action:{action.id}"
        )
