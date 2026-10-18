from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from agent_control.config import Settings
from agent_control.models import Workstream
from agent_control.projection import CanvasProjection
from agent_control.setup import save_config
from agent_control.web.app import create_app


def test_http_application_renders_all_read_only_screens(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    for path in (
        "/",
        "/projects/agent-control",
        "/workstreams/agent-control-canvas",
        "/agents/run-canvas-01",
        "/owner-actions",
        "/failures",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Read-only web view" in response.text
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/").status_code == 405


def test_external_text_is_escaped_and_exact_head_status_is_visible(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    workstream = client.get("/workstreams/agent-control-canvas")
    assert "&lt;script&gt;alert(&#39;no&#39;)&lt;/script&gt;" in workstream.text
    assert "<script>alert('no')</script>" not in workstream.text
    assert "a" * 40 in workstream.text
    project = client.get("/projects/project-two")
    assert "stale" in project.text.lower()
    assert "c" * 12 in project.text


def test_missing_branch_pr_and_failed_ci_are_distinct(
    settings: Settings, projection: CanvasProjection
) -> None:
    record, revision = projection.store.get("workstreams", "project-two-release")
    workstream_record = cast(Workstream, record).model_copy(update={"pull_request_number": 999})
    projection.store.save(
        "workstreams",
        workstream_record,
        expected_revision=revision,
        idempotency_key="test-missing-pr",
    )
    client = TestClient(create_app(settings, projection=projection))
    workstream = client.get("/workstreams/project-two-release")
    assert "missing in latest GitHub snapshot" in workstream.text
    assert "Pull request evidence missing" in workstream.text
    failures = client.get("/failures")
    assert "Failed exact-head verification" in failures.text
    assert "ci-failed" in failures.text


def test_owner_actions_survive_completed_run(
    settings: Settings, projection: CanvasProjection
) -> None:
    response = TestClient(create_app(settings, projection=projection)).get("/owner-actions")
    assert "physical mobile-browser acceptance" in response.text
    assert "remote coordination-state location" in response.text


def test_local_settings_and_operator_write_guards(
    settings: Settings, projection: CanvasProjection, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    save_config(path, settings)
    client = TestClient(
        create_app(settings, projection=projection, config_path=path),
        base_url="http://127.0.0.1:8420",
    )
    page = client.get("/settings")
    assert page.status_code == 200
    token = re.search(r'name="agent-control-csrf" content="([^"]+)"', page.text)
    revision = re.search(r'data-revision="([^"]+)"', page.text)
    assert token is not None and revision is not None
    payload = {
        "revision": revision.group(1),
        "owner_login": "AnotherOwner",
        "repositories": "example-org/control-demo\nexample-org/tool-one",
    }
    headers = {
        "Origin": "http://127.0.0.1:8420",
        "X-Agent-Control-CSRF": token.group(1),
    }
    assert client.post("/settings", json=payload).status_code == 403
    assert (
        client.post(
            "/settings",
            json=payload,
            headers={**headers, "X-Forwarded-For": "192.0.2.1"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/settings/rules/sync",
            json={"project_id": "absent", "expected_head": "a" * 40},
            headers=headers,
        ).status_code
        == 404
    )
    assert client.post("/settings", json=payload, headers=headers).status_code == 200
    reloaded = Settings.from_file(path)
    assert reloaded.owner_login == "AnotherOwner"
    assert reloaded.projects[0].id == settings.projects[0].id
    assert projection.settings == reloaded
    assert client.post("/settings", json=payload, headers=headers).status_code == 409


def test_local_settings_reject_duplicate_repositories(
    settings: Settings, projection: CanvasProjection, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    save_config(path, settings)
    client = TestClient(
        create_app(settings, projection=projection, config_path=path),
        base_url="http://127.0.0.1:8420",
    )
    page = client.get("/settings")
    token = re.search(r'name="agent-control-csrf" content="([^"]+)"', page.text)
    revision = re.search(r'data-revision="([^"]+)"', page.text)
    assert token is not None and revision is not None
    payload = {
        "revision": revision.group(1),
        "owner_login": "CodeBBQ",
        "repositories": "example-org/tool-one\nEXAMPLE-ORG/TOOL-ONE",
    }
    headers = {
        "Origin": "http://127.0.0.1:8420",
        "X-Agent-Control-CSRF": token.group(1),
    }
    assert client.post("/settings", json=payload, headers=headers).status_code == 422
    assert Settings.from_file(path).projects == settings.projects


def test_removed_rule_routes_are_not_available(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    assert client.get("/settings/rules").status_code == 404
    assert client.post("/settings/rules/preview", json={}).status_code == 404
    assert client.post("/settings/rules/sync", json={}).status_code == 404
    assert "/settings/rules" not in client.get("/settings").text


def test_update_header_is_visible_but_unconfigured_writes_are_rejected(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    response = client.get("/")
    assert "data-dashboard-update" in response.text
    assert "data-update-form" in response.text
    assert "Running code commit" in response.text
    assert client.get("/maintenance/update-status").json() == {"status": "unavailable"}
    assert client.get("/maintenance/branches").status_code == 409
    assert client.post("/maintenance/update").status_code == 403


def test_update_requires_operator_csrf_even_with_config(
    settings: Settings, projection: CanvasProjection, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    save_config(path, settings)
    client = TestClient(
        create_app(settings, projection=projection, config_path=path),
        base_url="http://127.0.0.1:8420",
    )
    response = client.get("/")
    token = re.search(r'name="agent-control-csrf" content="([^"]+)"', response.text)
    assert token is not None
    payload = {"branch": "main"}
    assert client.post("/maintenance/update", json=payload).status_code == 403
    assert (
        client.post(
            "/maintenance/update",
            json=payload,
            headers={
                "Origin": "http://127.0.0.1:8420",
                "X-Agent-Control-CSRF": token.group(1),
                "X-Forwarded-For": "198.51.100.9",
            },
        ).status_code
        == 403
    )


def test_unknown_routes_use_safe_error_page(
    settings: Settings, projection: CanvasProjection
) -> None:
    response = TestClient(create_app(settings, projection=projection)).get("/no-such-page")
    assert response.status_code == 404
    assert "No repository or coordination data was changed" in response.text


def test_issue_roadmap_sidebar_and_workstream_cards_are_rendered(
    settings: Settings, projection: CanvasProjection
) -> None:
    response = TestClient(create_app(settings, projection=projection)).get(
        "/?project=agent-control"
    )
    assert response.status_code == 200
    assert 'aria-label="Project roadmap"' in response.text
    assert 'aria-label="Workstream status"' in response.text
    assert "2 total" in response.text
    assert "2 open" in response.text or "2 in progress" in response.text
    assert "Agent reports:" in response.text
    assert "live unknown" in response.text
    assert "HEAD:" in response.text
    assert "PR #10" in response.text
    assert 'href="/workstreams/' in response.text


def test_roadmap_picker_shows_only_selected_project_and_preserves_view(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    overview = client.get("/?project=agent-control")
    assert overview.status_code == 200
    assert '<details class="roadmap-project-picker" data-project-picker>' in overview.text
    assert 'aria-label="Choose project"' in overview.text
    assert 'href="/?project=project-two"' in overview.text
    assert overview.text.count('class="roadmap-project active"') == 1
    assert "2 total" in overview.text

    switched = client.get("/?project=project-two")
    assert switched.status_code == 200
    assert "<strong>Example Project Two</strong>" in switched.text
    assert 'href="/?project=agent-control"' in switched.text
    assert switched.text.count('class="roadmap-project active"') == 1
    assert "0 total" in switched.text
    assert "2 total" not in switched.text
    assert "No GitHub issues found in this project." in switched.text

    detail = client.get("/projects/agent-control")
    assert detail.status_code == 200
    assert 'href="/?project=project-two"' in detail.text
    assert detail.text.count('aria-current="page"') == 1


def test_persistent_three_column_shell_on_all_screens(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    for path in (
        "/",
        "/projects/agent-control",
        "/workstreams/agent-control-canvas",
        "/agents/run-canvas-01",
        "/owner-actions",
        "/failures",
        "/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text.count('class="global-roadmap"') == 1
        assert response.text.count('class="right-rail"') == 1
        assert 'data-inspector-tab="actions"' in response.text
        assert 'data-inspector-tab="agents"' in response.text
        assert 'data-inspector-section="actions"' in response.text
        assert 'data-inspector-section="agents"' in response.text
        assert "Recent agent runs" in response.text
        assert "No pending owner actions." not in response.text
        assert 'class="header-attention"' in response.text
        assert "What needs attention now" not in response.text


def test_global_roadmap_selection_persists_between_pages(
    settings: Settings, projection: CanvasProjection
) -> None:
    client = TestClient(create_app(settings, projection=projection))
    selected = client.get("/?project=project-two")
    assert selected.status_code == 200
    assert selected.cookies["agent_control_project"] == "project-two"
    assert selected.text.count('class="roadmap-project active"') == 1
    assert "<strong>Example Project Two</strong>" in selected.text
    assert 'aria-label="Workstream status"' in selected.text
    assert "No GitHub issues found in this project." in selected.text

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "<strong>Example Project Two</strong>" in settings_page.text
    detail = client.get("/workstreams/agent-control-canvas")
    assert detail.status_code == 200
    assert "<strong>Agent Control Service</strong>" in detail.text
    assert detail.cookies["agent_control_project"] == "agent-control"


def test_global_shell_has_fixed_sidebars_and_independent_scroll() -> None:
    css = (
        Path(__file__).resolve().parents[1] / "agent_control/web/static/dashboard.css"
    ).read_text(encoding="utf-8")
    assert ".global-roadmap { position: fixed;" in css
    assert ".right-rail { position: fixed;" in css
    assert ".app-center { position: fixed;" in css
    assert ".app-center > main { min-width: 0;" in css
    assert "overflow: auto; overscroll-behavior: contain;" in css
    assert ".global-roadmap .roadmap-sidebar" in css
    assert ".inspector-open .right-inspector-content { display: block;" in css


def test_project_page_renders_same_sidebar_without_extra_github_fetch(
    settings: Settings, projection: CanvasProjection
) -> None:
    response = TestClient(create_app(settings, projection=projection)).get(
        "/projects/agent-control"
    )
    assert response.status_code == 200
    assert "Project roadmap" in response.text
    assert "Recent pull requests" in response.text
    assert (
        "No native blockers observed" in response.text
        or "Native links not yet checked" in response.text
    )


def test_calm_workstream_hierarchy_and_details_are_complete(
    settings: Settings, projection: CanvasProjection
) -> None:
    html = (
        TestClient(create_app(settings, projection=projection)).get("/?project=agent-control").text
    )
    assert "data-workstream-toolbar" in html
    assert 'data-workstream-filter="blocked"' in html
    assert 'data-workstream-filter="review"' in html
    assert "data-workstream-sort" in html
    assert "data-workstream-card" in html
    assert 'data-priority="' in html
    assert 'data-last-activity="' in html
    assert 'class="workstream-details"' in html
    assert "Dependencies" in html
    assert "<dt>Branch</dt>" in html
    assert "<dt>CI observed SHA</dt>" in html
    assert "<dt>Sessions</dt>" in html
    assert "live unknown" in html
    assert "Next →" in html


def test_roadmap_navigation_keeps_closed_issues_discoverable(
    settings: Settings, projection: CanvasProjection
) -> None:
    html = (
        TestClient(create_app(settings, projection=projection)).get("/?project=agent-control").text
    )
    assert "roadmap-milestone" in html
    assert "milestone-title" in html
    assert "roadmap-closed-details" not in html  # Fixture has no closed issues.
    assert "2 total" in html


def test_soft_slate_palette_and_local_filter_controls() -> None:
    root = Path(__file__).resolve().parents[1] / "agent_control/web/static"
    css = (root / "dashboard.css").read_text(encoding="utf-8")
    js = (root / "dashboard.js").read_text(encoding="utf-8")
    assert "--bg: #1b2430;" in css
    assert "--surface: #222e3c;" in css
    assert "roadmap-closed-details" in css
    assert ".workstream-filters button" in css
    assert "card.hidden = !matches(card, currentFilter);" in js
    assert "list.replaceChildren(...ordered)" in js


def test_roadmap_counts_are_exclusive_and_milestones_optional(
    settings: Settings, projection: CanvasProjection
) -> None:
    from dataclasses import replace

    from agent_control.github import IssueObservation
    from agent_control.models import WorkstreamPhase
    from agent_control.roadmap import project_roadmaps, workstream_cards

    data = projection.data()
    snapshot = data.github["agent-control"]
    active = snapshot.issues[0].model_copy(update={"milestone_title": "V1"})
    planned = snapshot.issues[1]
    closed = IssueObservation(
        number=9,
        title="Shipped milestone",
        state="closed",
        source_url="https://github.com/example-org/control-demo/issues/9",
        updated_at=active.updated_at,
        milestone_title="V1",
    )
    changed_snapshot = snapshot.model_copy(update={"issues": (active, planned, closed)})
    streams = tuple(
        stream.model_copy(update={"phase": WorkstreamPhase.PLANNED})
        if 2 in stream.issue_numbers
        else stream
        for stream in data.workstreams
    )
    changed = replace(
        data, github={**data.github, "agent-control": changed_snapshot}, workstreams=streams
    )
    summary = next(item for item in project_roadmaps(changed) if item.project.id == "agent-control")
    assert summary.total == 3
    assert summary.closed + summary.in_progress + summary.open == 3
    assert summary.closed == 1
    assert summary.in_progress == 1
    assert summary.open == 1
    assert {item.title for item in summary.milestones} == {"V1", "Unassigned"}
    cards = workstream_cards(changed, "agent-control")
    assert len(cards) == 3
    for card in cards:
        count = sum(run.workstream_id == card.stream.id for run in changed.agent_runs)
        assert card.historical_agents == count
        assert card.reported_agents <= count
