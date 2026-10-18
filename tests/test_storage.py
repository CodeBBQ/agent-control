from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_control.models import Workstream, WorkstreamPhase
from agent_control.storage import (
    IdempotencyConflict,
    JsonRecordStore,
    MalformedRecord,
    RevisionConflict,
    StorageError,
)


def workstream(next_action: str = "Continue") -> Workstream:
    return Workstream(
        id="work-1",
        project_id="project-1",
        title="Concurrent work",
        phase=WorkstreamPhase.IMPLEMENTING,
        issue_numbers=(1,),
        next_action=next_action,
        updated_at=datetime.now(UTC),
    )


def test_save_is_revision_checked_and_idempotent(tmp_path: Path) -> None:
    store = JsonRecordStore(tmp_path / "state")
    original = workstream()
    record, revision = store.save(
        "workstreams", original, expected_revision=0, idempotency_key="create-1"
    )
    repeated, repeated_revision = store.save(
        "workstreams", original, expected_revision=0, idempotency_key="create-1"
    )
    assert (record.id, revision) == (repeated.id, repeated_revision) == ("work-1", 1)
    with pytest.raises(IdempotencyConflict):
        store.save(
            "workstreams", workstream("Different"), expected_revision=0, idempotency_key="create-1"
        )
    with pytest.raises(RevisionConflict) as caught:
        store.save(
            "workstreams", workstream("Stale"), expected_revision=0, idempotency_key="update-stale"
        )
    assert caught.value.actual == 1
    later, later_revision = store.save(
        "workstreams", workstream("Later"), expected_revision=1, idempotency_key="update-later"
    )
    assert (later.next_action, later_revision) == ("Later", 2)
    replayed, replayed_revision = store.save(
        "workstreams", original, expected_revision=0, idempotency_key="create-1"
    )
    assert (replayed.next_action, replayed_revision) == ("Continue", 1)


def test_two_same_revision_writers_do_not_silently_overwrite(tmp_path: Path) -> None:
    store = JsonRecordStore(tmp_path / "state")
    store.save("workstreams", workstream(), expected_revision=0, idempotency_key="base")

    def update(number: int) -> object:
        try:
            return store.save(
                "workstreams",
                workstream(f"Writer {number}"),
                expected_revision=1,
                idempotency_key=f"writer-{number}",
            )
        except RevisionConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (1, 2)))
    assert sum(isinstance(item, RevisionConflict) for item in results) == 1
    _record, revision = store.get("workstreams", "work-1")
    assert revision == 2


def test_malformed_oversized_and_symlinked_records_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = JsonRecordStore(root)
    store.initialize()
    target = root / "workstreams" / "bad.json"
    target.write_text("{not-json", encoding="utf-8")
    with pytest.raises(MalformedRecord):
        store.get("workstreams", "bad")
    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({}), encoding="utf-8")
    target.symlink_to(outside)
    with pytest.raises(StorageError, match="symlink"):
        store.get("workstreams", "bad")


def test_symlinked_internal_state_directory_and_lock_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / ".idempotency").symlink_to(outside, target_is_directory=True)
    store = JsonRecordStore(root)
    with pytest.raises(StorageError, match="subdirectory"):
        store.initialize()

    (root / ".idempotency").unlink()
    store.initialize()
    (root / ".lock").symlink_to(outside / "lock")
    with pytest.raises(StorageError, match="lock"):
        store.save("workstreams", workstream(), expected_revision=0, idempotency_key="unsafe")


def test_identifier_cannot_escape_state_root(tmp_path: Path) -> None:
    store = JsonRecordStore(tmp_path / "state")
    with pytest.raises(StorageError, match="identifier"):
        store.get("workstreams", "../outside")
