"""Versioned, bounded coordination-domain records.

These records contain coordination facts only. GitHub remains authoritative for
issues, pull requests, branches, reviews, and check runs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    ),
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
RepositoryName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=200,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    ),
]
Sha = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[0-9a-f]{40}$")]

_SECRET_KEY = re.compile(r"(?:token|password|secret|credential|authorization|api[_-]?key)", re.I)
_SECRET_VALUE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._~-]{12,})",
    re.I,
)


class CoordinationModel(BaseModel):
    """Strict base model shared by every persisted public record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1, le=SCHEMA_VERSION)

    @model_validator(mode="before")
    @classmethod
    def reject_secret_shaped_payloads(cls, value: Any) -> Any:
        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if _SECRET_KEY.search(str(key)):
                        raise ValueError(f"secret-like field is prohibited: {key}")
                    walk(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    walk(child)
            elif isinstance(item, str) and _SECRET_VALUE.search(item):
                raise ValueError("secret-like value is prohibited")

        walk(value)
        return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_github_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "api.github.com"}:
        raise ValueError("evidence links must use https on github.com or api.github.com")
    if parsed.username or parsed.password:
        raise ValueError("credentials are prohibited in evidence URLs")
    return value


def validate_repository_name(value: str) -> str:
    owner, repository = value.split("/", 1)
    if owner in {".", ".."} or repository in {".", ".."}:
        raise ValueError("repository identity contains an unsafe path segment")
    return value


class EvidenceOrigin(StrEnum):
    GITHUB_OBSERVED = "github_observed"
    AGENT_REPORTED = "agent_reported"
    OWNER_ACCEPTED = "owner_accepted"
    RUNTIME_VERIFIED = "runtime_verified"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class TruthStatus(StrEnum):
    OBSERVED = "observed"
    REPORTED = "reported"
    ACCEPTED = "accepted"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    COMMIT = "commit"
    CHECK_RUN = "check_run"
    REVIEW = "review"
    AGENT_REPORT = "agent_report"
    RUNTIME_EVENT = "runtime_event"
    OWNER_DECISION = "owner_decision"


class EvidenceReference(CoordinationModel):
    id: Identifier
    origin: EvidenceOrigin
    truth_status: TruthStatus
    kind: EvidenceKind
    observed_at: datetime
    source_url: str | None = Field(default=None, max_length=500)
    source_ref: str | None = Field(default=None, max_length=240)
    summary: ShortText

    _timestamp = field_validator("observed_at")(_aware)
    _url = field_validator("source_url")(
        lambda value: validate_github_url(value) if value else value
    )


class Project(CoordinationModel):
    id: Identifier
    repository: RepositoryName
    display_name: ShortText
    source_url: str = Field(max_length=500)

    _url = field_validator("source_url")(validate_github_url)
    _repository = field_validator("repository")(validate_repository_name)


class WorkstreamPhase(StrEnum):
    PLANNED = "planned"
    INVESTIGATING = "investigating"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEW = "review"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class Workstream(CoordinationModel):
    id: Identifier
    project_id: Identifier
    title: ShortText
    phase: WorkstreamPhase
    issue_numbers: tuple[int, ...] = Field(min_length=1, max_length=20)
    dependency_ids: tuple[Identifier, ...] = Field(default=(), max_length=50)
    next_action: ShortText
    owning_branch: str | None = Field(default=None, min_length=1, max_length=240)
    pull_request_number: int | None = Field(default=None, ge=1)
    expected_head_sha: Sha | None = None
    updated_at: datetime
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=30)

    _timestamp = field_validator("updated_at")(_aware)

    @model_validator(mode="after")
    def no_self_dependency(self) -> Self:
        if self.id in self.dependency_ids:
            raise ValueError("workstream cannot depend on itself")
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("workstream dependencies must be unique")
        return self


class AgentRunStatus(StrEnum):
    REGISTERED = "registered"
    REPORTED = "reported"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AgentRun(CoordinationModel):
    id: Identifier
    workstream_id: Identifier
    agent_identity: ShortText
    adapter: ShortText
    environment: ShortText
    status: AgentRunStatus = AgentRunStatus.REGISTERED
    started_at: datetime
    last_report_at: datetime | None = None
    completed_at: datetime | None = None
    latest_report_id: Identifier | None = None
    completion_summary: ShortText | None = None
    runtime_verified_at: datetime | None = None

    _timestamps = field_validator(
        "started_at", "last_report_at", "completed_at", "runtime_verified_at"
    )(lambda value: _aware(value) if value else value)

    @model_validator(mode="after")
    def completion_is_consistent(self) -> Self:
        if self.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED}:
            if self.completed_at is None or self.completion_summary is None:
                raise ValueError("completed/failed runs require completion time and summary")
            if self.completed_at < self.started_at:
                raise ValueError("completion cannot precede run start")
            if self.last_report_at and self.completed_at < self.last_report_at:
                raise ValueError("completion cannot precede the latest report")
        elif self.completed_at is not None:
            raise ValueError("non-final runs cannot have completed_at")
        if self.last_report_at and self.last_report_at < self.started_at:
            raise ValueError("latest report cannot precede run start")
        if self.runtime_verified_at and self.runtime_verified_at < self.started_at:
            raise ValueError("runtime verification cannot precede run start")
        return self


class ReportPhase(StrEnum):
    STARTED = "started"
    INVESTIGATING = "investigating"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    WAITING = "waiting"


class AgentReport(CoordinationModel):
    id: Identifier
    agent_run_id: Identifier
    workstream_id: Identifier
    phase: ReportPhase
    summary: LongText
    next_action: ShortText
    reported_at: datetime
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1, max_length=20)

    _timestamp = field_validator("reported_at")(_aware)


class BlockerClassification(StrEnum):
    PRODUCT_DEFECT = "product_defect"
    CI_ERROR = "ci_error"
    ENVIRONMENT = "environment"
    CONTRACT_DECISION = "contract_decision"
    MISSING_EVIDENCE = "missing_evidence"
    SECURITY = "security"
    UNKNOWN = "unknown"


class Blocker(CoordinationModel):
    id: Identifier
    workstream_id: Identifier
    agent_run_id: Identifier | None = None
    classification: BlockerClassification
    summary: ShortText
    detail: LongText
    created_at: datetime
    resolved_at: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=20)

    _timestamps = field_validator("created_at", "resolved_at")(
        lambda value: _aware(value) if value else value
    )


class OwnerActionType(StrEnum):
    PRODUCT_DECISION = "product_decision"
    REVIEW_REQUEST = "review_request"
    MERGE_APPROVAL = "merge_approval"
    MANUAL_ACCEPTANCE = "manual_acceptance"
    ENVIRONMENT_PROBLEM = "environment_problem"
    SECURITY_DECISION = "security_decision"
    BLOCKING_QUESTION = "blocking_question"


class OwnerActionStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OwnerAction(CoordinationModel):
    id: Identifier
    workstream_id: Identifier
    agent_run_id: Identifier | None = None
    action_type: OwnerActionType
    priority: Priority
    status: OwnerActionStatus = OwnerActionStatus.PENDING
    reason: LongText
    requested_action: ShortText
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    owner_decision: ShortText | None = None
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=20)

    _timestamps = field_validator("created_at", "updated_at", "resolved_at")(
        lambda value: _aware(value) if value else value
    )

    @model_validator(mode="after")
    def resolution_is_explicit(self) -> Self:
        if self.status is OwnerActionStatus.RESOLVED:
            if self.resolved_at is None or self.owner_decision is None:
                raise ValueError("resolved owner actions require explicit decision and timestamp")
        elif self.resolved_at is not None or self.owner_decision is not None:
            raise ValueError("pending owner actions cannot contain a resolution")
        return self


RECORD_MODELS: dict[str, type[CoordinationModel]] = {
    "projects": Project,
    "workstreams": Workstream,
    "agent-runs": AgentRun,
    "agent-reports": AgentReport,
    "blockers": Blocker,
    "owner-actions": OwnerAction,
}
