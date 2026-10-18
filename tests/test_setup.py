from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_control.config import ConfigurationError, ProjectConfig, Settings, default_config_path
from agent_control.setup import configure, save_config


def test_default_config_is_per_user_and_override_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_CONTROL_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = default_config_path()
    assert path == tmp_path / "agent-control-service" / "config.toml"
    monkeypatch.setenv("AGENT_CONTROL_CONFIG", str(tmp_path / "custom.toml"))
    assert default_config_path() == tmp_path / "custom.toml"
    monkeypatch.setenv("AGENT_CONTROL_CONFIG", "relative.toml")
    with pytest.raises(ConfigurationError, match="absolute"):
        default_config_path()


def test_first_run_menu_and_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config" / "settings.toml"
    answers = iter(["1", "1,2", "d", "2", "CodeBBQ", "6"])
    monkeypatch.setattr(
        "agent_control.repository_picker.discover_repositories",
        lambda: ("example-org/tool-one", "example-org/control-demo"),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    settings = configure(path)
    assert settings is not None
    assert len(settings.projects) == 2
    assert settings.github_auth == "gh"
    assert settings.coordination_source == "issues"
    reloaded = Settings.from_file(path)
    assert reloaded.projects == settings.projects
    assert reloaded.owner_login == "CodeBBQ"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "TOKEN" not in path.read_text(encoding="utf-8")


def test_menu_cancel_preserves_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.toml"
    settings = Settings(
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        projects=(
            ProjectConfig(id="example", repository="CodeBBQ/example", display_name="Example"),
        ),
        github_auth="gh",
        github_token_env=None,
    )
    save_config(path, settings)
    before = path.read_bytes()
    monkeypatch.setattr("builtins.input", lambda _prompt: "7")
    assert configure(path) is None
    assert path.read_bytes() == before


def test_symlink_config_target_is_not_overwritten(settings: Settings, tmp_path: Path) -> None:
    real = tmp_path / "real.toml"
    real.write_text("untouched", encoding="utf-8")
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    with pytest.raises(ConfigurationError, match="symlink"):
        save_config(link, settings)
    assert real.read_text(encoding="utf-8") == "untouched"


def test_removed_rule_token_setting_is_rejected(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_config(path, settings)
    content = path.read_text(encoding="utf-8")
    path.write_text('rules_write_token_env = "OLD_RULES_TOKEN"\n' + content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid"):
        Settings.from_file(path)


def test_gh_credentials_are_read_at_runtime_only(settings: Settings) -> None:
    active = settings.model_copy(update={"github_auth": "gh"})
    with patch(
        "agent_control.config.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="secret-token\n", stderr=""
        ),
    ) as run:
        assert active.github_token == "secret-token"
    assert run.call_args.args[0] == ["gh", "auth", "token", "--hostname", "github.com"]
    assert run.call_args.kwargs["capture_output"] is True
    with (
        patch(
            "agent_control.config.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["gh"], returncode=1, stdout="", stderr="private error token"
            ),
        ),
        pytest.raises(ConfigurationError, match="gh auth login"),
    ):
        _ = active.github_token
    assert active.model_copy(update={"github_auth": "public"}).github_token is None
