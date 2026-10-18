"""Repository discovery and explicit allowlist selection, without live GitHub access."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_control.config import ConfigurationError, ProjectConfig
from agent_control.repository_picker import choose_repositories, discover_repositories


def _project(name: str) -> ProjectConfig:
    return ProjectConfig(
        id=name.lower().replace("/", "-"),
        repository=name,
        display_name=name.split("/")[1],
    )


def test_discover_uses_authenticated_gh_and_filters_untrusted_names() -> None:
    response = subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout='[{"nameWithOwner":"example-org/tool-one"},'
        '{"nameWithOwner":"example-org/tool-three"},'
        '{"nameWithOwner":"example-org/tool-one"},'
        '{"nameWithOwner":"../bad"},{"nameWithOwner":"bad\\nname"}]',
        stderr="",
    )
    with patch("agent_control.repository_picker.subprocess.run", return_value=response) as run:
        assert discover_repositories() == ("example-org/tool-one", "example-org/tool-three")
    assert run.call_args.args[0] == [
        "gh",
        "repo",
        "list",
        "--limit",
        "500",
        "--json",
        "nameWithOwner",
    ]
    assert run.call_args.kwargs["capture_output"]
    assert run.call_args.kwargs["timeout"] == 25


def test_discovery_failure_does_not_expose_gh_stderr() -> None:
    response = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout="", stderr="secret-token"
    )
    with (
        patch("agent_control.repository_picker.subprocess.run", return_value=response),
        pytest.raises(ConfigurationError, match="gh auth login") as caught,
    ):
        discover_repositories()
    assert "secret-token" not in str(caught.value)


def test_toggle_filter_and_preserve_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_control.repository_picker.discover_repositories",
        lambda: ("example-org/tool-one", "example-org/project-two", "example-org/tool-three"),
    )
    answers = iter(["/", "tool-three", "1", "/", "", "1", "d"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    chosen = choose_repositories((_project("example-org/tool-one"),))
    assert chosen is not None
    assert [item.repository for item in chosen] == ["example-org/tool-three"]


def test_manual_fallback_and_shortname_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_control.repository_picker.discover_repositories",
        lambda: ("example-org/tool-one",),
    )
    answers = iter(["a", "tool-one, example-org/control-demo", "d"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    chosen = choose_repositories(())
    assert chosen is not None
    assert {item.repository for item in chosen} == {
        "example-org/tool-one",
        "example-org/control-demo",
    }


def test_cancel_does_not_mutate_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_control.repository_picker.discover_repositories",
        lambda: (),
    )
    existing = (_project("example-org/tool-one"),)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    assert choose_repositories(existing) is None
    assert existing[0].repository == "example-org/tool-one"


def test_discovery_unavailable_allows_manual_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline() -> tuple[str, ...]:
        raise ConfigurationError("gh is offline")

    monkeypatch.setattr("agent_control.repository_picker.discover_repositories", offline)
    answers = iter(["a", "example-org/tool-one", "d"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    chosen = choose_repositories(())
    assert chosen is not None
    assert chosen[0].repository == "example-org/tool-one"


def test_no_implicit_catalog_query_if_config_menu_not_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The repository list is never fetched by merely saving unchanged settings."""
    from agent_control.setup import configure

    monkeypatch.setattr(
        "agent_control.repository_picker.discover_repositories",
        lambda: pytest.fail("repository discovery must be explicit"),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "7")
    assert configure(tmp_path / "settings.toml") is None
