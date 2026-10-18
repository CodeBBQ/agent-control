"""Terminal-only first-run setup; background processes never prompt."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from agent_control.config import DEFAULT_PORT, ConfigurationError, Settings
from agent_control.repository_picker import choose_repositories


def _serialize(settings: Settings) -> str:
    def q(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "# Managed by agent-control config. No secrets stored here.",
        f"host = {q(settings.host)}",
        f"port = {settings.port}",
        f"state_directory = {q(str(settings.state_directory))}",
        f"cache_directory = {q(str(settings.cache_directory))}",
        f"github_auth = {q(settings.github_auth or 'gh')}",
        f"coordination_source = {q(settings.coordination_source)}",
        f"refresh_seconds = {settings.refresh_seconds}",
        f"stale_report_seconds = {settings.stale_report_seconds}",
    ]
    if settings.owner_login:
        lines.append(f"owner_login = {q(settings.owner_login)}")
    if settings.github_auth == "env" and settings.github_token_env:
        lines.append(f"github_token_env = {q(settings.github_token_env)}")
    for item in settings.projects:
        lines += [
            "",
            "[[projects]]",
            f"id = {q(item.id)}",
            f"repository = {q(item.repository)}",
            f"display_name = {q(item.display_name)}",
        ]
    return "\n".join(lines) + "\n"


def save_config(path: Path, settings: Settings) -> None:
    if not path.is_absolute():
        raise ConfigurationError("configuration path must be absolute")
    parent = path.parent
    if parent.resolve(strict=False) != parent or parent.is_symlink():
        raise ConfigurationError("configuration directory must not traverse symlinks")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("refusing to overwrite non-file or symlink configuration")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, prefix=".config-", delete=False
        ) as out:
            temporary = Path(out.name)
            out.write(_serialize(settings))
            out.flush()
            os.fsync(out.fileno())
        temporary.chmod(0o600)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigurationError("configuration target changed during save")
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _login() -> None:
    print("GitHub CLI manages credentials; no token is stored in this config.")
    try:
        result = subprocess.run(["gh", "auth", "login", "--hostname", "github.com"], check=False)
    except OSError as error:
        raise ConfigurationError("GitHub CLI missing; install gh first") from error
    if result.returncode:
        raise ConfigurationError("gh auth login did not complete")


def configure(path: Path) -> Settings | None:
    """Numbered configuration menu; cancel leaves the existing file untouched."""
    previous = Settings.from_file(path) if path.exists() or path.is_symlink() else None
    home = Path.home().resolve()
    state = (
        previous.state_directory if previous else home / ".local/share/agent-control-service/state"
    )
    cache = previous.cache_directory if previous else home / ".cache/agent-control-service"
    projects = previous.projects if previous else ()
    owner = previous.owner_login if previous else None
    port = previous.port if previous else DEFAULT_PORT
    source: Literal["issues", "local"] = previous.coordination_source if previous else "issues"
    auth = previous.github_auth if previous and previous.github_auth else "gh"
    while True:
        print(
            "\nAgent Control configuration\n"
            f"  1) Repositories: {', '.join(p.repository for p in projects) or '(not set)'}\n"
            f"  2) Approval owner: {owner or '(not set)'}\n"
            f"  3) Local port: {port}\n"
            f"  4) GitHub login: {auth}\n"
            f"  5) Coordination source: {source}\n"
            "  6) Save and exit\n"
            "  7) Cancel"
        )
        choice = input("Select 1-7: ").strip()
        try:
            if choice == "1":
                choice_projects = choose_repositories(projects)
                if choice_projects is not None:
                    projects = choice_projects
            elif choice == "2":
                value = input("Your GitHub login (Enter keeps current): ").strip()
                if value:
                    owner = value
            elif choice == "3":
                value = input(f"Loopback port [{port}]: ").strip()
                if value:
                    port = int(value)
            elif choice == "4":
                print("  1) Use existing gh login  2) Run gh auth login  3) Public only")
                value = input("Select 1-3: ").strip()
                if value == "1":
                    auth = "gh"
                elif value == "2":
                    _login()
                    auth = "gh"
                elif value == "3":
                    auth = "public"
            elif choice == "5":
                value = input("GitHub issues or local demo? [issues/local]: ").strip()
                if value == "issues":
                    source = "issues"
                elif value == "local":
                    source = "local"
                elif value:
                    raise ConfigurationError("choose issues or local")
            elif choice == "6":
                settings = Settings(
                    host="127.0.0.1",
                    port=port,
                    state_directory=state,
                    cache_directory=cache,
                    projects=projects,
                    owner_login=owner,
                    coordination_source=source,
                    github_auth=auth,
                    github_token_env=(
                        previous.github_token_env if previous and auth == "env" else None
                    ),
                    refresh_seconds=previous.refresh_seconds if previous else 120,
                    stale_report_seconds=previous.stale_report_seconds if previous else 1800,
                )
                save_config(path, settings)
                print(f"Configuration saved: {path} (no credentials stored)")
                return settings
            elif choice == "7":
                return None
            else:
                print("Unknown option.")
        except ValueError as error:
            print(f"Invalid value: {error}")
