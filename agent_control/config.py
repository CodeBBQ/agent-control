"""Fail-fast application configuration."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agent_control.models import Project, validate_repository_name

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
RESERVED_PORTS = {8000, 8001, 8765}


def default_config_path() -> Path:
    override = os.environ.get("AGENT_CONTROL_CONFIG")
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    path = Path(override) if override else root / "agent-control-service" / "config.toml"
    if not path.is_absolute():
        raise ConfigurationError("configuration path must be absolute")
    return path


class ConfigurationError(ValueError):
    """Configuration is absent, malformed, ambiguous, or unsafe."""


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=80)]
    repository: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    ]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=240)]

    _repository = field_validator("repository")(validate_repository_name)

    def project(self) -> Project:
        return Project(
            id=self.id,
            repository=self.repository,
            display_name=self.display_name,
            source_url=f"https://github.com/{self.repository}",
        )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=1024, le=65535)
    state_directory: Path
    cache_directory: Path
    projects: tuple[ProjectConfig, ...] = Field(min_length=1, max_length=50)
    github_token_env: str | None = Field(default="GITHUB_TOKEN", max_length=120)
    github_auth: Literal["gh", "env", "public"] | None = None
    refresh_seconds: int = Field(default=120, ge=30, le=3600)
    stale_report_seconds: int = Field(default=1800, ge=60, le=604800)
    coordination_source: Literal["issues", "local"] = "issues"
    owner_login: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,39}$")

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value != DEFAULT_HOST:
            raise ValueError("v1 must bind exactly to 127.0.0.1")
        return value

    @field_validator("port")
    @classmethod
    def dedicated_port(cls, value: int) -> int:
        if value in RESERVED_PORTS:
            raise ValueError(f"port {value} is reserved by another local service")
        return value

    @field_validator("state_directory", "cache_directory")
    @classmethod
    def absolute_safe_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("state/cache directories must be absolute")
        try:
            if value.resolve(strict=False) != value:
                raise ValueError(f"directory path must not traverse symlinks: {value}")
        except OSError as error:
            raise ValueError(f"directory path cannot be resolved safely: {value}") from error
        return value

    @model_validator(mode="after")
    def unique_scope(self) -> Settings:
        ids = [project.id for project in self.projects]
        repos = [project.repository.lower() for project in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("project ids must be unique")
        if len(repos) != len(set(repos)):
            raise ValueError("repository allowlist entries must be unique")
        if self.state_directory == self.cache_directory:
            raise ValueError("state and cache directories must be separate")
        return self

    @property
    def github_token(self) -> str | None:
        """Resolve viewer credentials without serializing them into settings."""
        if self.github_auth == "public":
            return None
        if self.github_auth == "gh":
            try:
                result = subprocess.run(
                    ["gh", "auth", "token", "--hostname", "github.com"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ConfigurationError(
                    "GitHub CLI unavailable; install gh and run 'gh auth login'"
                ) from error
            if result.returncode or not result.stdout.strip():
                raise ConfigurationError(
                    "GitHub is not authenticated; run 'gh auth login' "
                    "to access private repositories"
                )
            return result.stdout.strip()
        if not self.github_token_env:
            if self.github_auth == "env":
                raise ConfigurationError("github_auth = 'env' requires github_token_env")
            return None
        token = os.environ.get(self.github_token_env)
        if not token:
            raise ConfigurationError(
                f"configured GitHub token environment variable is missing: {self.github_token_env}"
            )
        return token

    @classmethod
    def from_file(cls, path: str | Path) -> Settings:
        supplied = Path(path)
        if not supplied.is_absolute():
            raise ConfigurationError("configuration path must be absolute")
        try:
            if supplied.is_symlink() or not supplied.is_file():
                raise ConfigurationError(
                    f"configuration must be a regular non-symlink file: {supplied}"
                )
            if supplied.resolve(strict=True) != supplied:
                raise ConfigurationError(
                    f"configuration path must not traverse symlinks: {supplied}"
                )
            payload = tomllib.loads(supplied.read_text(encoding="utf-8"))
            return cls.model_validate(payload)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            if isinstance(error, ConfigurationError):
                raise
            raise ConfigurationError(f"invalid configuration {supplied}: {error}") from error

    @classmethod
    def default(cls) -> Settings:
        return cls.from_file(default_config_path())
