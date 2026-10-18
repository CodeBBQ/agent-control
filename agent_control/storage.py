"""Conflict-safe bounded local JSON record storage.

This adapter is usable for one operator installation and deterministic tests.
It is not the unresolved GitHub-backed cross-agent write transport from #2/#7.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent_control.models import RECORD_MODELS, CoordinationModel, utc_now

MAX_RECORD_BYTES = 64 * 1024


class StorageError(RuntimeError):
    """Base class for safe, operator-visible storage failures."""


class RecordNotFound(StorageError):
    pass


class RevisionConflict(StorageError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"stale record revision: expected {expected}, current revision is {actual}"
        )
        self.expected = expected
        self.actual = actual


class IdempotencyConflict(StorageError):
    pass


class MalformedRecord(StorageError):
    pass


class RecordEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    revision: int = Field(ge=1)
    updated_at: datetime
    record_type: str
    record: dict[str, Any]

    @field_validator("updated_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must have timezone")
        return value


T = TypeVar("T", bound=CoordinationModel)


@dataclass(frozen=True)
class RecordWrite:
    kind: str
    record: CoordinationModel
    expected_revision: int
    idempotency_key: str


class JsonRecordStore:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise StorageError("state root must be absolute")
        if root.exists() and root.is_symlink():
            raise StorageError("state root must not be a symlink")
        self.root = root

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise StorageError("state root must not be a symlink")
        resolved_root = self.root.resolve(strict=True)
        for name in (*RECORD_MODELS, ".idempotency"):
            directory = self.root / name
            directory.mkdir(exist_ok=True)
            if directory.is_symlink() or not directory.resolve(strict=True).is_relative_to(
                resolved_root
            ):
                raise StorageError(f"state subdirectory is unsafe: {name}")

    def _validate_target(self, kind: str, record_id: str) -> Path:
        if kind not in RECORD_MODELS:
            raise StorageError(f"unsupported record type: {kind}")
        if (
            not record_id
            or len(record_id) > 80
            or not all(char.islower() or char.isdigit() or char in "._-" for char in record_id)
            or not record_id[0].isalnum()
        ):
            raise StorageError("invalid record identifier")
        target = self.root / kind / f"{record_id}.json"
        resolved_parent = target.parent.resolve(strict=False)
        if not resolved_parent.is_relative_to(self.root.resolve(strict=False)):
            raise StorageError("record path escapes state root")
        if target.exists() and target.is_symlink():
            raise StorageError("record path must not be a symlink")
        return target

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.initialize()
        lock_path = self.root / ".lock"
        if lock_path.is_symlink():
            raise StorageError("state lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise StorageError("cannot open state lock safely") from error
        try:
            with os.fdopen(descriptor, "r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            raise

    def _read_envelope(self, path: Path) -> RecordEnvelope | None:
        if not path.exists():
            return None
        try:
            size = path.stat().st_size
            if size > MAX_RECORD_BYTES:
                raise MalformedRecord(f"record exceeds {MAX_RECORD_BYTES} bytes: {path.name}")
            return RecordEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            if isinstance(error, MalformedRecord):
                raise
            raise MalformedRecord(f"cannot read validated record {path.name}: {error}") from error

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        if len(payload) > MAX_RECORD_BYTES:
            raise StorageError(f"serialized record exceeds {MAX_RECORD_BYTES} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _operation_hash(kind: str, record: CoordinationModel, expected_revision: int) -> str:
        canonical = record.model_dump_json(exclude_none=False)
        return hashlib.sha256(f"{kind}\0{expected_revision}\0{canonical}".encode()).hexdigest()

    def save(
        self,
        kind: str,
        record: T,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[T, int]:
        record_id = cast(str, record.model_dump()["id"])
        target = self._validate_target(kind, record_id)
        if not idempotency_key or len(idempotency_key) > 160:
            raise StorageError("idempotency key must contain 1-160 characters")
        operation_hash = self._operation_hash(kind, record, expected_revision)
        idem_name = hashlib.sha256(idempotency_key.encode()).hexdigest()
        idem_path = self.root / ".idempotency" / f"{idem_name}.json"
        with self._lock():
            if idem_path.exists():
                if idem_path.is_symlink():
                    raise StorageError("idempotency record must not be a symlink")
                try:
                    prior = json.loads(idem_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    raise MalformedRecord("idempotency record is corrupt") from error
                if prior.get("operation_hash") != operation_hash:
                    raise IdempotencyConflict(
                        "idempotency key was already used for another operation"
                    )
                try:
                    parsed = RECORD_MODELS[kind].model_validate(prior["record"])
                    prior_revision = int(prior["revision"])
                except (KeyError, TypeError, ValueError, ValidationError) as error:
                    raise MalformedRecord("idempotency result is incomplete") from error
                return cast(T, parsed), prior_revision

            existing = self._read_envelope(target)
            actual = existing.revision if existing else 0
            if actual != expected_revision:
                raise RevisionConflict(expected_revision, actual)
            revision = actual + 1
            envelope = RecordEnvelope(
                revision=revision,
                updated_at=utc_now(),
                record_type=kind,
                record=record.model_dump(mode="json"),
            )
            payload = envelope.model_dump_json(indent=2).encode("utf-8") + b"\n"
            idem_payload = (
                json.dumps(
                    {
                        "operation_hash": operation_hash,
                        "record_type": kind,
                        "record_id": record_id,
                        "revision": revision,
                        "record": record.model_dump(mode="json"),
                    },
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > MAX_RECORD_BYTES or len(idem_payload) > MAX_RECORD_BYTES:
                raise StorageError(f"serialized record exceeds {MAX_RECORD_BYTES} bytes")
            self._atomic_write(target, payload)
            self._atomic_write(idem_path, idem_payload)
            return record, revision

    def save_many(self, operations: tuple[RecordWrite, ...]) -> list[tuple[CoordinationModel, int]]:
        """Apply a conflict-checked local batch while holding one writer lock.

        This prevents another local reporter from interleaving same-record
        updates. The unresolved remote GitHub adapter will need its own
        provider-level conditional-write protocol.
        """
        if not operations:
            return []
        prepared: list[tuple[RecordWrite, Path, Path, str, str]] = []
        targets: set[Path] = set()
        idempotency_paths: set[Path] = set()
        for operation in operations:
            record_id = cast(str, operation.record.model_dump()["id"])
            target = self._validate_target(operation.kind, record_id)
            if not operation.idempotency_key or len(operation.idempotency_key) > 160:
                raise StorageError("idempotency key must contain 1-160 characters")
            idem_name = hashlib.sha256(operation.idempotency_key.encode()).hexdigest()
            idem_path = self.root / ".idempotency" / f"{idem_name}.json"
            if target in targets or idem_path in idempotency_paths:
                raise StorageError("batch targets and idempotency keys must be unique")
            targets.add(target)
            idempotency_paths.add(idem_path)
            operation_hash = self._operation_hash(
                operation.kind, operation.record, operation.expected_revision
            )
            prepared.append((operation, target, idem_path, operation_hash, record_id))

        with self._lock():
            repeated = [idem_path.exists() for _, _, idem_path, _, _ in prepared]
            if any(repeated):
                if not all(repeated):
                    raise IdempotencyConflict("batch idempotency state is partial; no changes made")
                results: list[tuple[CoordinationModel, int]] = []
                for operation, _target, idem_path, operation_hash, _record_id in prepared:
                    if idem_path.is_symlink():
                        raise StorageError("idempotency record must not be a symlink")
                    try:
                        prior = json.loads(idem_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as error:
                        raise MalformedRecord("idempotency record is corrupt") from error
                    if prior.get("operation_hash") != operation_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used for another operation"
                        )
                    try:
                        parsed = RECORD_MODELS[operation.kind].model_validate(prior["record"])
                        prior_revision = int(prior["revision"])
                    except (KeyError, TypeError, ValueError, ValidationError) as error:
                        raise MalformedRecord("idempotency result is incomplete") from error
                    results.append((parsed, prior_revision))
                return results

            revisions: list[int] = []
            for operation, target, _idem_path, _operation_hash, _record_id in prepared:
                existing = self._read_envelope(target)
                actual = existing.revision if existing else 0
                if actual != operation.expected_revision:
                    raise RevisionConflict(operation.expected_revision, actual)
                revisions.append(actual + 1)

            serialized: list[tuple[Path, bytes, Path, bytes]] = []
            for (operation, target, idem_path, operation_hash, record_id), revision in zip(
                prepared, revisions, strict=True
            ):
                envelope = RecordEnvelope(
                    revision=revision,
                    updated_at=utc_now(),
                    record_type=operation.kind,
                    record=operation.record.model_dump(mode="json"),
                )
                record_payload = envelope.model_dump_json(indent=2).encode("utf-8") + b"\n"
                idem_payload = (
                    json.dumps(
                        {
                            "operation_hash": operation_hash,
                            "record_type": operation.kind,
                            "record_id": record_id,
                            "revision": revision,
                            "record": operation.record.model_dump(mode="json"),
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
                if len(record_payload) > MAX_RECORD_BYTES or len(idem_payload) > MAX_RECORD_BYTES:
                    raise StorageError(f"serialized record exceeds {MAX_RECORD_BYTES} bytes")
                serialized.append((target, record_payload, idem_path, idem_payload))
            for target, record_payload, idem_path, idem_payload in serialized:
                self._atomic_write(target, record_payload)
                self._atomic_write(idem_path, idem_payload)
            return [
                (operation.record, revision)
                for (operation, *_unused), revision in zip(prepared, revisions, strict=True)
            ]

    def get(self, kind: str, record_id: str) -> tuple[CoordinationModel, int]:
        target = self._validate_target(kind, record_id)
        envelope = self._read_envelope(target)
        if envelope is None:
            raise RecordNotFound(f"{kind}/{record_id} was not found")
        if envelope.record_type != kind:
            raise MalformedRecord("record type does not match containing directory")
        try:
            model = RECORD_MODELS[kind].model_validate(envelope.record)
        except ValidationError as error:
            raise MalformedRecord(f"invalid {kind}/{record_id}: {error}") from error
        if getattr(model, "id", None) != record_id:
            raise MalformedRecord("record id does not match filename")
        return model, envelope.revision

    def list(self, kind: str) -> list[tuple[CoordinationModel, int]]:
        directory = self._validate_target(kind, "placeholder").parent
        if not directory.exists():
            return []
        records: list[tuple[CoordinationModel, int]] = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                raise StorageError(f"symlinked record is prohibited: {path.name}")
            records.append(self.get(kind, path.stem))
        return records
