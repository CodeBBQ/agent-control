"""Hermetic PR ready/merge tests. Never invoke a real GitHub mutation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent_control.config import Settings
from agent_control.pr_actions import PRActionError, PRActions, checked_pr
from agent_control.projection import CanvasProjection
from agent_control.web.app import create_app

SHA = "a" * 40
OTHER = "b" * 40
BRANCH = "feature/issue-4-test"
NUMBER = 27


def metadata(*, draft: bool = True, sha: str = SHA, checks: bool = True) -> str:
    return json.dumps(
        {
            "number": NUMBER,
            "title": "Implement canvas",
            "url": "https://github.com/CodeBBQ/agent-control/pull/27",
            "state": "OPEN",
            "isDraft": draft,
            "headRefName": BRANCH,
            "headRefOid": sha,
            "headRepositoryOwner": {"login": "CodeBBQ"},
            "baseRefName": "main",
            "statusCheckRollup": (
                [{"status": "COMPLETED", "conclusion": "SUCCESS"}] if checks else []
            ),
        }
    )


class FakeUpdater:
    def owner_pid(self) -> int:
        return 123

    def status(self) -> dict[str, str]:
        return {"status": "idle"}

    def branches(self) -> dict[str, Any]:
        return {"current": "main", "branches": ["main", BRANCH]}


def prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_control.pr_actions.checked_checkout", lambda: None)
    monkeypatch.setattr("agent_control.pr_actions.valid_branch", lambda b: b)


def test_checked_pr_reads_exact_branch_from_fixed_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare(monkeypatch)
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> str:
        calls.append(args)
        return metadata()

    monkeypatch.setattr("agent_control.pr_actions.gh", fake_gh)
    value = checked_pr(BRANCH)
    assert value["branch"] == BRANCH
    assert value["number"] == NUMBER
    assert value["head"] == SHA
    assert value["draft"]
    assert value["checks_green"]
    assert calls[0][:2] == ("view", BRANCH)
    assert "headRefOid" in calls[0][-1]


@pytest.mark.parametrize(
    "value",
    [
        metadata(sha=OTHER).replace(f'"headRefName": "{BRANCH}"', '"headRefName": "other"'),
        metadata().replace('"state": "OPEN"', '"state": "CLOSED"'),
        metadata().replace(
            '"url": "https://github.com/CodeBBQ/agent-control/pull/27"',
            '"url": "https://github.com/Other/repo/pull/27"',
        ),
        metadata().replace('"login": "CodeBBQ"', '"login": "Other"'),
        "garbage",
    ],
)
def test_checked_pr_rejects_foreign_closed_or_invalid_metadata(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    prepare(monkeypatch)
    monkeypatch.setattr("agent_control.pr_actions.gh", lambda *args: value)
    with pytest.raises(PRActionError):
        checked_pr(BRANCH)


def test_ready_only_never_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(monkeypatch)
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> str:
        calls.append(args)
        return metadata()

    monkeypatch.setattr("agent_control.pr_actions.gh", fake_gh)
    result = PRActions(FakeUpdater()).perform(
        BRANCH, NUMBER, SHA, False, f"ready:{NUMBER}:{SHA[:12]}"
    )
    assert result["status"] == "ready"
    assert [call[0] for call in calls] == ["view", "ready"]
    assert calls[1] == ("ready", str(NUMBER))


def test_checked_merge_exact_command_order_and_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(monkeypatch)
    calls: list[tuple[str, ...]] = []
    state = {"ready": False}

    def fake_gh(*args: str) -> str:
        calls.append(args)
        if args[0] == "view":
            return metadata(draft=not state["ready"])
        if args[0] == "ready":
            state["ready"] = True
        return ""

    monkeypatch.setattr("agent_control.pr_actions.gh", fake_gh)
    result = PRActions(FakeUpdater()).perform(
        BRANCH, NUMBER, SHA, True, f"merge:{NUMBER}:{SHA[:12]}"
    )
    assert result["status"] == "merged"
    assert [call[0] for call in calls] == ["view", "ready", "view", "merge"]
    assert calls[-1] == ("merge", str(NUMBER), "--merge", "--delete-branch")


def test_no_mutation_on_stale_head_or_missing_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(monkeypatch)
    calls: list[str] = []

    def fake_gh(*args: str) -> str:
        calls.append(args[0])
        return metadata(sha=OTHER)

    monkeypatch.setattr("agent_control.pr_actions.gh", fake_gh)
    manager = PRActions(FakeUpdater())
    with pytest.raises(PRActionError, match="confirmation"):
        manager.perform(BRANCH, NUMBER, SHA, True, "")
    assert not calls
    with pytest.raises(PRActionError, match="changed"):
        manager.perform(BRANCH, NUMBER, SHA, True, f"merge:{NUMBER}:{SHA[:12]}")
    assert calls == ["view"]


def test_green_checks_required_before_marking_ready_for_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare(monkeypatch)
    calls: list[str] = []

    def fake_gh(*args: str) -> str:
        calls.append(args[0])
        return metadata(checks=False)

    monkeypatch.setattr("agent_control.pr_actions.gh", fake_gh)
    with pytest.raises(PRActionError, match="checks"):
        PRActions(FakeUpdater()).perform(BRANCH, NUMBER, SHA, True, f"merge:{NUMBER}:{SHA[:12]}")
    assert calls == ["view"]


def test_second_head_changed_after_ready_refuses_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(monkeypatch)
    calls: list[str] = []
    state = {"ready": False}

    def fake_gh(*args: str) -> str:
        calls.append(args[0])
        if args[0] == "ready":
            state["ready"] = True
        return metadata(sha=OTHER if state["ready"] else SHA, draft=not state["ready"])

    monkeypatch.setattr("agent_control.pr_actions.gh", fake_gh)
    with pytest.raises(PRActionError, match="after ready"):
        PRActions(FakeUpdater()).perform(BRANCH, NUMBER, SHA, True, f"merge:{NUMBER}:{SHA[:12]}")
    assert calls == ["view", "ready", "view"]


def test_ready_operation_refuses_inflight_update(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(monkeypatch)

    class Busy(FakeUpdater):
        def status(self) -> dict[str, str]:
            return {"status": "switching"}

    command = Mock()
    monkeypatch.setattr("agent_control.pr_actions.gh", command)
    with pytest.raises(PRActionError, match="update in progress"):
        PRActions(Busy()).perform(BRANCH, NUMBER, SHA, False, f"ready:{NUMBER}:{SHA[:12]}")
    command.assert_not_called()


def test_http_pr_action_is_operator_only_and_unconfigured_disabled(
    settings: Settings,
    projection: CanvasProjection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings, projection=projection)
    assert TestClient(app).post("/maintenance/pr", json={}).status_code == 403

    class FakeActions:
        def preview(self, branch: str) -> dict[str, Any]:
            return {"branch": branch, "number": NUMBER, "head": SHA, "checks_green": True}

        def perform(
            self, branch: str, number: int, expected_head: str, merge: bool, confirmation: str
        ) -> dict[str, Any]:
            assert branch == BRANCH and number == NUMBER and expected_head == SHA
            assert merge is False
            assert confirmation == f"ready:{NUMBER}:{SHA[:12]}"
            return {"status": "ready"}

    monkeypatch.setattr("agent_control.web.app.PRActions", lambda updater: FakeActions())
    config = tmp_path / "config.toml"
    config.write_text("test", encoding="utf-8")
    client = TestClient(
        create_app(settings, projection=projection, config_path=config),
        base_url="http://127.0.0.1:8420",
    )
    csrf_match = re.search(r'name="agent-control-csrf" content="([^"]+)"', client.get("/").text)
    assert csrf_match is not None
    token = csrf_match.group(1)
    headers = {"Origin": "http://127.0.0.1:8420", "X-Agent-Control-CSRF": token}
    payload = {
        "branch": BRANCH,
        "number": NUMBER,
        "head": SHA,
        "merge": False,
        "confirmation": f"ready:{NUMBER}:{SHA[:12]}",
    }
    assert client.get("/maintenance/pr?branch=" + BRANCH).status_code == 200
    assert client.post("/maintenance/pr", json=payload).status_code == 403
    assert (
        client.post(
            "/maintenance/pr",
            json=payload,
            headers={
                **headers,
                "X-Forwarded-For": "198.51.100.1",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/maintenance/pr",
            json={
                **payload,
                "merge": "true",
            },
            headers=headers,
        ).status_code
        == 422
    )
    assert client.post("/maintenance/pr", json=payload, headers=headers).json() == {
        "status": "ready"
    }
    assert "data-pr-form" in client.get("/").text
    assert 'name="merge"' in client.get("/").text
