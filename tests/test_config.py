from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_control.config import ConfigurationError, ProjectConfig, Settings


def values(tmp_path: Path) -> dict[str, object]:
    return {
        "state_directory": tmp_path / "state",
        "cache_directory": tmp_path / "cache",
        "github_token_env": None,
        "projects": (
            ProjectConfig(id="example", repository="CodeBBQ/example", display_name="Example"),
        ),
    }


def test_configuration_refuses_unsafe_binding_reserved_ports_and_duplicate_repos(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="127.0.0.1"):
        Settings(**values(tmp_path), host="0.0.0.0")
    with pytest.raises(ValidationError, match="reserved"):
        Settings(**values(tmp_path), port=8001)
    duplicate = ProjectConfig(id="other", repository="codebbq/EXAMPLE", display_name="Other")
    duplicate_values = values(tmp_path)
    projects = duplicate_values.pop("projects")
    with pytest.raises(ValidationError, match="allowlist"):
        Settings(**duplicate_values, projects=(*projects, duplicate))  # type: ignore[misc]
    with pytest.raises(ValidationError, match="unsafe path segment"):
        ProjectConfig(id="unsafe", repository="../repo", display_name="Unsafe")


def test_configuration_file_must_be_absolute_regular_and_valid(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        Settings.from_file("relative.toml")
    config = tmp_path / "config.toml"
    config.write_text("port = 8001\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid"):
        Settings.from_file(config)
    target = tmp_path / "target.toml"
    target.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "linked.toml"
    link.symlink_to(target)
    with pytest.raises(ConfigurationError, match="non-symlink"):
        Settings.from_file(link)


def test_missing_configured_token_is_explicit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    requiring = settings.model_copy(update={"github_token_env": "ABSENT_AGENT_CONTROL_TOKEN"})
    monkeypatch.delenv("ABSENT_AGENT_CONTROL_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="missing"):
        _ = requiring.github_token
