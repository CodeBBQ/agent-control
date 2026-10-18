from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_control.models import (
    AgentReport,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    OwnerAction,
    OwnerActionStatus,
    OwnerActionType,
    Priority,
    ReportPhase,
    TruthStatus,
    Workstream,
    WorkstreamPhase,
)


def evidence(**updates: object) -> EvidenceReference:
    values: dict[str, object] = {
        "id": "ev-1",
        "origin": EvidenceOrigin.AGENT_REPORTED,
        "truth_status": TruthStatus.REPORTED,
        "kind": EvidenceKind.ISSUE,
        "observed_at": datetime.now(UTC),
        "source_url": "https://github.com/CodeBBQ/example/issues/1",
        "summary": "Issue evidence",
    }
    values.update(updates)
    return EvidenceReference.model_validate(values)


def test_schema_rejects_naive_time_arbitrary_url_and_secret_fields() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        evidence(observed_at=datetime(2026, 10, 15))
    with pytest.raises(ValidationError, match="github.com"):
        evidence(source_url="https://evil.example/collect")
    with pytest.raises(ValidationError, match="secret-like field"):
        EvidenceReference.model_validate(
            {
                **evidence().model_dump(),
                "token": "not-allowed",
            }
        )


def test_workstream_dependency_contract() -> None:
    with pytest.raises(ValidationError, match="depend on itself"):
        Workstream(
            id="work-1",
            project_id="project-1",
            title="Work",
            phase=WorkstreamPhase.PLANNED,
            issue_numbers=(1,),
            dependency_ids=("work-1",),
            next_action="Choose another dependency",
            updated_at=datetime.now(UTC),
        )


def test_progress_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        AgentReport(
            id="report-1",
            agent_run_id="run-1",
            workstream_id="work-1",
            phase=ReportPhase.IMPLEMENTING,
            summary="Claim without evidence",
            next_action="Add evidence",
            reported_at=datetime.now(UTC),
            evidence=(),
        )


def test_owner_action_resolution_is_explicit() -> None:
    base = {
        "id": "action-1",
        "workstream_id": "work-1",
        "action_type": OwnerActionType.PRODUCT_DECISION,
        "priority": Priority.HIGH,
        "reason": "A real decision is required",
        "requested_action": "Choose the durable state location",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError, match="explicit decision"):
        OwnerAction(**base, status=OwnerActionStatus.RESOLVED)
    action = OwnerAction(**base)
    assert action.status is OwnerActionStatus.PENDING
