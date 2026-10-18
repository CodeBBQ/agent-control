"""Isolated update controls; never execute git against a real repository."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_control.config import Settings
from agent_control.self_update import SelfUpdater, UpdateError, checked_checkout, valid_branch


def build(settings: Settings, tmp_path: Path) -> SelfUpdater:
    config = tmp_path / "config.toml"
    config.write_text("test", encoding="utf-8")
    return SelfUpdater(settings, config)


def test_branch_validation_rejects_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    git_mock = Mock()
    monkeypatch.setattr("agent_control.self_update.git", git_mock)
    for name in ("", "-x", "../secret", "main;evil", "a//b", "a/.lock", "x" * 161):
        with pytest.raises(UpdateError):
            valid_branch(name)
    git_mock.assert_not_called()
    assert valid_branch("feature/update") == "feature/update"
    git_mock.assert_called_once_with("check-ref-format", "--branch", "feature/update")


def test_checked_checkout_rejects_other_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_control.self_update.CODE_ROOT", tmp_path)
    monkeypatch.setattr(
        "agent_control.self_update.git",
        lambda *args: str(tmp_path) if args[0] == "rev-parse" else "https://example.test/x",
    )
    with pytest.raises(UpdateError, match="origin"):
        checked_checkout()


def test_update_state_refuses_symlink(settings: Settings, tmp_path: Path) -> None:
    manager = build(settings, tmp_path)
    assert manager.status()["status"] == "idle"
    manager.write("queued", "a" * 32, "main")
    assert manager.status()["id"] == "a" * 32
    assert manager.state.stat().st_mode & 0o777 == 0o600
    manager.state.unlink()
    manager.state.symlink_to(tmp_path / "config.toml")
    with pytest.raises(UpdateError, match="Unsafe"):
        manager.status()


def test_queue_refuses_dirty_checkout_before_spawn(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build(settings, tmp_path)
    monkeypatch.setattr(manager, "owner_pid", lambda: 13)
    monkeypatch.setattr("agent_control.self_update.checked_checkout", lambda: None)
    monkeypatch.setattr(
        "agent_control.self_update.git",
        lambda *args, **kwargs: "dirty" if args[0] == "status" else "",
    )
    with pytest.raises(UpdateError, match="Local code changes"):
        manager.queue("main")
    assert manager.status()["status"] == "idle"


def test_queue_refuses_active_request(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build(settings, tmp_path)
    manager.write("fetching", "a" * 32, "main")
    monkeypatch.setattr(manager, "owner_pid", lambda: 13)
    monkeypatch.setattr("agent_control.self_update.checked_checkout", lambda: None)
    monkeypatch.setattr("agent_control.self_update.git", lambda *args, **kwargs: "")
    monkeypatch.setattr(manager, "branches", lambda: {"current": "main", "branches": ["main"]})
    with pytest.raises(UpdateError, match="already in progress"):
        manager.queue("main")


def test_worker_rejects_stale_request(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build(settings, tmp_path)
    manager.write("queued", "a" * 32, "main")
    monkeypatch.setattr("agent_control.self_update.git", lambda *args, **kwargs: "")
    with pytest.raises(UpdateError, match="no longer active"):
        manager.worker("main", "b" * 32, 13)


def test_worker_restarts_only_the_verified_owner_after_exact_fast_forward(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build(settings, tmp_path)
    manager.write("queued", "a" * 32, "main")
    monkeypatch.setattr(manager, "owner_pid", lambda: 1234)
    monkeypatch.setattr("agent_control.self_update.checked_checkout", lambda: None)
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, **_kwargs: object) -> str:
        calls.append(args)
        if args[:2] == ("for-each-ref", "--format=%(refname:short)"):
            return "origin/main" if args[2] == "refs/remotes/origin/" else "main"
        if args[0] == "rev-parse":
            return "a" * 40
        return ""

    monkeypatch.setattr("agent_control.self_update.git", fake_git)
    killed: list[int] = []
    monkeypatch.setattr("agent_control.self_update.os.kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(manager.runtime, "inspect_listener", lambda: None)
    start = Mock()
    monkeypatch.setattr(manager.runtime, "start", start)

    manager.worker("main", "a" * 32, 1234)
    assert ("merge", "--ff-only", "refs/remotes/origin/main") in calls
    assert killed == [1234]
    start.assert_called_once_with()
    assert manager.status()["status"] == "complete"


def test_worker_never_terminates_owner_when_local_head_is_ahead(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build(settings, tmp_path)
    manager.write("queued", "a" * 32, "main")
    monkeypatch.setattr(manager, "owner_pid", lambda: 1234)
    monkeypatch.setattr("agent_control.self_update.checked_checkout", lambda: None)

    def fake_git(*args: str, **_kwargs: object) -> str:
        if args[:2] == ("for-each-ref", "--format=%(refname:short)"):
            return "origin/main" if args[2] == "refs/remotes/origin/" else "main"
        if args[:2] == ("rev-parse", "--verify"):
            return "b" * 40 if args[2] == "HEAD" else "a" * 40
        return ""

    monkeypatch.setattr("agent_control.self_update.git", fake_git)
    terminate = Mock()
    monkeypatch.setattr("agent_control.self_update.os.kill", terminate)
    start = Mock()
    monkeypatch.setattr(manager.runtime, "start", start)

    with pytest.raises(UpdateError, match="ahead of origin"):
        manager.worker("main", "a" * 32, 1234)
    terminate.assert_not_called()
    start.assert_not_called()
    assert manager.status()["status"] == "failed"


def test_owner_gate_refuses_unmanaged_environment(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build(settings, tmp_path)
    monkeypatch.delenv("AGENT_CONTROL_SERVICE_MANAGED", raising=False)
    with pytest.raises(UpdateError, match="managed macOS"):
        manager.owner_pid()
