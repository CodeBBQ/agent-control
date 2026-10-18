"""Private session URL storage, GitHub extraction, and HTTP security regression tests."""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_control.config import Settings
from agent_control.github import GitHubReadAdapter
from agent_control.models import Project
from agent_control.projection import CanvasProjection
from agent_control.session_links import (
    SessionLinkError,
    SessionLinkStore,
    recent_session_links,
    session_links_in_comment,
    validate_session_url,
)
from agent_control.web.app import create_app

SESSION_1 = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
SESSION_2 = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
REPOSITORY = "example-org/control-demo"
WHEN = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "url",
    [
        "http://chatgpt.com/c/a",
        "https://evil.example/c/xyz",
        "https://chatgpt.com.evil.example/c/xyz",
        "https://chatgpt.com@evil.example/c/xyz",
        "https://chatgpt.com/c/xyz?token=secret",
        "https://chatgpt.com/c/xyz#fragment",
        "javascript:alert(1)",
        "https://chatgpt.com/",
        "https://chatgpt.com/c/abc\nX-Spoofed: yes",
        "https://chatgpt.com/c/%3Cscript%3E",
    ],
)
def test_reject_untrusted_session_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_session_url(url)


def test_only_explicit_comment_lines_become_session_links() -> None:
    note = (
        f"Agent-Session: {SESSION_1}\n"
        "See https://chatgpt.com/c/33333333-3333-4333-8333-333333333333\n"
        "Session-URL: https://evil.example/c/xyz\n"
        "Session-Type: chatgpt\n"
        "Next: review"
    )
    links = session_links_in_comment(note, observed_at=WHEN)
    assert len(links) == 1
    assert links[0].url == SESSION_1
    assert links[0].source == "github"
    assert not links[0].snapshot_only
    assert session_links_in_comment("unrelated URL: " + SESSION_1, observed_at=WHEN) == ()


def test_private_store_persists_history_and_permissions(tmp_path: Path) -> None:
    first = SessionLinkStore(tmp_path)
    first.add(REPOSITORY, 4, SESSION_1)
    first.add(REPOSITORY, 4, SESSION_2)
    first.add(REPOSITORY, 4, SESSION_1)  # Explicit selection; no duplicate history.
    second = SessionLinkStore(tmp_path)
    links = second.for_repository(REPOSITORY)[4]
    assert [entry.url for entry in links] == [SESSION_1, SESSION_2]
    assert all(entry.source == "local" for entry in links)
    assert stat.S_IMODE(first.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.data_path.stat().st_mode) == 0o600
    assert recent_session_links(links, ())[:1] == (links[0],)
    assert second.remove(REPOSITORY, 4, SESSION_1)
    assert [entry.url for entry in second.for_repository(REPOSITORY)[4]] == [SESSION_2]
    assert not second.remove(REPOSITORY, 4, SESSION_1)


def test_unsafe_session_storage_symlink_refused(tmp_path: Path) -> None:
    store = SessionLinkStore(tmp_path)
    other = tmp_path / "unrelated.txt"
    other.write_text("secret", encoding="utf-8")
    store.directory.symlink_to(other)
    with pytest.raises(SessionLinkError):
        store.add(REPOSITORY, 4, SESSION_1)
    assert other.read_text(encoding="utf-8") == "secret"


def test_github_session_links_via_existing_repository_comment_feed(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/issues"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 4,
                        "title": "UI",
                        "state": "open",
                        "html_url": f"https://github.com/{REPOSITORY}/issues/4",
                        "updated_at": WHEN.isoformat(),
                        "comments": 2,
                    }
                ],
            )
        if path.endswith("/issues/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 99,
                        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/4",
                        "body": f"Agent-Session: {SESSION_1}",
                        "updated_at": WHEN.isoformat(),
                    },
                    {
                        "id": 100,
                        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/4",
                        "body": "Context: " + SESSION_2,
                        "updated_at": WHEN.isoformat(),
                    },
                ],
            )
        if path.endswith("/pulls") or path.endswith("/branches"):
            return httpx.Response(200, json=[])
        raise AssertionError(path)

    adapter = GitHubReadAdapter(
        token=None,
        cache_directory=tmp_path,
        transport=httpx.MockTransport(handler),
        read_relationships=False,
    )
    snapshot = adapter.fetch_project(
        Project(
            id="control",
            repository=REPOSITORY,
            display_name="Control",
            source_url=f"https://github.com/{REPOSITORY}",
        )
    )
    assert [link.url for link in snapshot.issues[0].session_links] == [SESSION_1]
    assert snapshot.issues[0].session_links[0].comment_url is not None
    assert calls.count("/repos/example-org/control-demo/issues/comments") == 1
    assert not any("/issues/4/comments" in path for path in calls)


def test_operator_only_session_link_creation_and_removal(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(
        create_app(settings, projection=projection),
        base_url="http://127.0.0.1:8420",
    )
    page = client.get("/workstreams/agent-control-canvas")
    assert page.status_code == 200
    assert "No session linked yet" in page.text
    csrf = re.search(r'name="agent-control-csrf" content="([^"]+)"', page.text)
    assert csrf is not None
    headers = {
        "Origin": "http://127.0.0.1:8420",
        "X-Agent-Control-CSRF": csrf.group(1),
    }
    endpoint = "/workstreams/agent-control-canvas/sessions"
    payload = {"url": SESSION_1, "action": "add"}
    assert client.post(endpoint, json=payload).status_code == 403
    assert (
        client.post(
            endpoint,
            json=payload,
            headers={
                **headers,
                "Origin": "https://other.example",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            endpoint,
            json=payload,
            headers={
                **headers,
                "X-Forwarded-For": "192.0.2.2",
            },
        ).status_code
        == 403
    )
    assert (
        client.post("/workstreams/unknown/sessions", json=payload, headers=headers).status_code
        == 404
    )
    invalid = client.post(endpoint, json={"url": "https://evil.example/x"}, headers=headers)
    assert invalid.status_code == 422
    assert client.post(endpoint, json=payload, headers=headers).json() == {"status": "saved"}

    detail = client.get("/workstreams/agent-control-canvas")
    overview = client.get("/?project=agent-control")
    assert SESSION_1 in detail.text
    assert "Last linked session" in overview.text
    assert "Selected locally" in detail.text
    assert "No session linked yet" not in detail.text
    removed = client.post(endpoint, json={"url": SESSION_1, "action": "remove"}, headers=headers)
    assert removed.json() == {"status": "removed"}
    assert SESSION_1 not in client.get("/workstreams/agent-control-canvas").text


def test_session_links_never_migrate_into_a_different_repository(
    settings: Settings, projection: CanvasProjection
) -> None:
    store = SessionLinkStore(settings.state_directory)
    store.add(REPOSITORY, 4, SESSION_1)
    assert store.for_repository("example-org/project-two") == {}
    assert store.for_repository(REPOSITORY)[4][0].url == SESSION_1
