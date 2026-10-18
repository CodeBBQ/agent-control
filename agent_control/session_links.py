"""Private local session navigation and strict opt-in public comment references.

Links are supplementary navigation metadata, not canonical GitHub workstream
state or proof of current agent liveness. No transcript or credential is stored.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_LINK_FILE_BYTES = 1024 * 1024
MAX_LOCAL_ENTRIES = 1000
MAX_LINKS_PER_ISSUE = 10
SESSION_LINE = re.compile(r"(?im)^\s*(?:Agent-Session|Session-URL)\s*:\s*(https://[^\s<>]+)\s*$")
ISSUE_KEY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")


class SessionLinkError(RuntimeError):
    """Safe error; private URLs are never included in messages."""


def validate_session_url(value: str) -> str:
    """Allow known ChatGPT/Codex URLs, never arbitrary external redirects."""
    if not isinstance(value, str):
        raise ValueError("Expected a session URL")
    value = value.strip()
    if len(value) > 500 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Session URL must be a simple HTTPS address (max 500 characters)")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("Invalid session URL") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"chatgpt.com", "chat.openai.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(("/c/", "/codex/", "/share/", "/g/"))
        or re.fullmatch(r"/[A-Za-z0-9/_-]{3,400}", parsed.path) is None
    ):
        raise ValueError("Use a ChatGPT/Codex conversation HTTPS URL without query or fragment")
    return value


class SessionReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str = Field(max_length=500)
    source: Literal["local", "github"]
    observed_at: datetime
    comment_url: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return validate_session_url(value)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Session timestamp must have a timezone")
        return value.astimezone(UTC)

    @property
    def snapshot_only(self) -> bool:
        return urlsplit(self.url).path.startswith("/share/")


def session_links_in_comment(
    body: str, *, observed_at: datetime, comment_url: str | None = None
) -> tuple[SessionReference, ...]:
    if not isinstance(body, str) or len(body) > 65536:
        return ()
    found: list[SessionReference] = []
    for match in SESSION_LINE.finditer(body):
        try:
            found.append(
                SessionReference(
                    url=match.group(1),
                    source="github",
                    observed_at=observed_at,
                    comment_url=comment_url,
                )
            )
        except ValueError:
            continue
        if len(found) >= 4:
            break
    return tuple(found)


def recent_session_links(
    local: tuple[SessionReference, ...], github: tuple[SessionReference, ...]
) -> tuple[SessionReference, ...]:
    """An explicit private selection wins; keep all unique provenance in history."""
    deduped: dict[str, SessionReference] = {}
    for item in sorted((*github, *local), key=lambda x: x.observed_at):
        deduped[item.url] = item
    values = sorted(
        deduped.values(),
        key=lambda x: (x.source != "local", -x.observed_at.timestamp()),
    )
    return tuple(values[:20])


class SessionLinkStore:
    """Atomic, chmod-0600 machine-local metadata. Never writes to GitHub."""

    def __init__(self, state_directory: Path) -> None:
        if not state_directory.is_absolute():
            raise SessionLinkError("Session storage root must be absolute")
        self.directory = state_directory / "session-links"
        self.data_path = self.directory / "links.json"
        self.lock_path = self.directory / ".lock"

    def _ensure_directory(self) -> None:
        if self.directory.is_symlink():
            raise SessionLinkError("Session storage directory is unsafe")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise SessionLinkError("Session storage directory is unsafe")
        os.chmod(self.directory, 0o700)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_directory()
        if self.lock_path.is_symlink():
            raise SessionLinkError("Session storage lock is unsafe")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            with os.fdopen(descriptor, "r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise SessionLinkError("Session storage is unavailable") from error

    def _read(self) -> dict[str, list[SessionReference]]:
        if self.data_path.is_symlink():
            raise SessionLinkError("Session metadata file is unsafe")
        if not self.data_path.exists():
            return {}
        try:
            if self.data_path.stat().st_size > MAX_LINK_FILE_BYTES:
                raise SessionLinkError("Session metadata exceeds its size limit")
            raw = json.loads(self.data_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or len(raw) > MAX_LOCAL_ENTRIES:
                raise ValueError("Invalid metadata root")
            records: dict[str, list[SessionReference]] = {}
            for key, entries in raw.items():
                if not isinstance(key, str) or ISSUE_KEY.fullmatch(key) is None:
                    raise ValueError("Invalid metadata key")
                if not isinstance(entries, list) or len(entries) > MAX_LINKS_PER_ISSUE:
                    raise ValueError("Invalid metadata history")
                records[key] = [SessionReference.model_validate(item) for item in entries]
                if any(item.source != "local" for item in records[key]):
                    raise ValueError("GitHub links cannot be stored as local metadata")
            return records
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, SessionLinkError):
                raise
            raise SessionLinkError("Session metadata is unavailable or invalid") from error

    def for_repository(self, repository: str) -> dict[int, tuple[SessionReference, ...]]:
        with self._locked():
            records = self._read()
        prefix = repository + "#"
        return {
            int(key[len(prefix) :]): tuple(reversed(entries))
            for key, entries in records.items()
            if key.casefold().startswith(prefix.casefold())
        }

    def add(self, repository: str, issue: int, url: str) -> SessionReference:
        url = validate_session_url(url)
        key = f"{repository}#{issue}"
        if ISSUE_KEY.fullmatch(key) is None:
            raise ValueError("Invalid repository issue reference")
        with self._locked():
            records = self._read()
            if key not in records and len(records) >= MAX_LOCAL_ENTRIES:
                raise SessionLinkError("Session metadata capacity reached")
            entry = SessionReference(url=url, source="local", observed_at=datetime.now(UTC))
            prior = [item for item in records.get(key, []) if item.url != url]
            records[key] = [*prior, entry][-MAX_LINKS_PER_ISSUE:]
            self._save(records)
        return entry

    def remove(self, repository: str, issue: int, url: str) -> bool:
        url = validate_session_url(url)
        key = f"{repository}#{issue}"
        if ISSUE_KEY.fullmatch(key) is None:
            raise ValueError("Invalid repository issue reference")
        with self._locked():
            records = self._read()
            entries = records.get(key, [])
            remaining = [item for item in entries if item.url != url]
            if len(remaining) == len(entries):
                return False
            if remaining:
                records[key] = remaining
            else:
                del records[key]
            self._save(records)
        return True

    def _save(self, records: dict[str, list[SessionReference]]) -> None:
        body = json.dumps(
            {
                key: [item.model_dump(mode="json") for item in items]
                for key, items in records.items()
            },
            sort_keys=True,
        ).encode("utf-8")
        if len(body) > MAX_LINK_FILE_BYTES:
            raise SessionLinkError("Session metadata capacity reached")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(body + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.data_path.is_symlink():
                raise SessionLinkError("Session metadata file is unsafe")
            temporary.replace(self.data_path)
        except OSError as error:
            raise SessionLinkError("Cannot save session metadata") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
