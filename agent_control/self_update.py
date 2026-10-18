"""Guarded macOS self-update: a queued request is executed by a detached worker."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from agent_control.config import Settings
from agent_control.runtime import CODE_ROOT, RuntimeManager

ORIGINS = frozenset(
    f"{prefix}CodeBBQ/agent-control{suffix}"
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/")
    for suffix in ("", ".git")
)
ACTIVE = {"queued", "fetching", "switching", "restarting"}
BRANCH = re.compile(r"[A-Za-z0-9_./-]{1,160}\Z")
LOCK = threading.Lock()


class UpdateError(RuntimeError):
    """Operator-safe error; raw Git output and credentials never leave the worker."""


def git(*args: str, timeout: int = 35) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=CODE_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError("Git unavailable or timed out") from error
    if result.returncode:
        raise UpdateError(f"Git {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def valid_branch(value: str) -> str:
    if (
        not BRANCH.fullmatch(value)
        or value.startswith("-")
        or any(part in ("", ".", "..") for part in value.split("/"))
        or ".." in value
        or value.endswith(".lock")
    ):
        raise UpdateError("Invalid update branch")
    git("check-ref-format", "--branch", value)
    return value


def checked_checkout() -> None:
    if Path(git("rev-parse", "--show-toplevel")).resolve() != CODE_ROOT.resolve():
        raise UpdateError("Code checkout does not match the running service")
    if git("remote", "get-url", "origin") not in ORIGINS:
        raise UpdateError("Git origin is not CodeBBQ/agent-control")


class SelfUpdater:
    def __init__(self, settings: Settings, config_path: Path) -> None:
        if not config_path.is_absolute():
            raise UpdateError("Expected absolute configuration path")
        self.runtime = RuntimeManager(settings, config_path)
        self.config_path = config_path
        self.state = settings.cache_directory / "runtime" / "update.json"
        self.log = settings.cache_directory / "logs" / "update-worker.log"

    def _safe_path(self) -> None:
        root = self.state.parent
        if root.is_symlink() or root.resolve(strict=False) != root:
            raise UpdateError("Unsafe update state directory")
        if self.state.is_symlink() or (self.state.exists() and not self.state.is_file()):
            raise UpdateError("Unsafe update state file")

    def status(self) -> dict[str, Any]:
        self._safe_path()
        if not self.state.exists():
            return {"status": "idle"}
        if self.state.stat().st_size > 4096:
            raise UpdateError("Invalid update state size")
        try:
            data = json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise UpdateError("Invalid update state") from error
        if not isinstance(data, dict) or data.get("status") not in ACTIVE | {
            "complete",
            "failed",
            "idle",
        }:
            raise UpdateError("Invalid update state")
        return data

    def write(self, status: str, request_id: str, branch: str, message: str = "") -> None:
        self._safe_path()
        self.state.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temp = self.state.parent / f".update-{secrets.token_hex(8)}.tmp"
        try:
            with temp.open("x", encoding="utf-8") as handle:
                json.dump(
                    {"status": status, "id": request_id, "branch": branch, "message": message},
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            temp.chmod(0o600)
            self._safe_path()
            temp.replace(self.state)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def code_commit() -> str:
        try:
            checked_checkout()
            return git("rev-parse", "--verify", "HEAD")[:12]
        except UpdateError:
            return "unavailable"

    def branches(self) -> dict[str, Any]:
        checked_checkout()
        current = git("symbolic-ref", "--quiet", "--short", "HEAD")
        output = git("ls-remote", "--heads", "origin", timeout=60)
        names = sorted(
            {
                line.split("\trefs/heads/", 1)[1]
                for line in output.splitlines()
                if "\trefs/heads/" in line and BRANCH.fullmatch(line.split("\trefs/heads/", 1)[1])
            }
        )
        return {"current": current, "branches": names}

    def owner_pid(self) -> int:
        if sys.platform != "darwin" or os.environ.get("AGENT_CONTROL_SERVICE_MANAGED") != "1":
            raise UpdateError("Update requires the managed macOS service")
        listener = self.runtime.inspect_listener()
        pid = self.runtime.managed_pid()
        if listener is None or pid is None or listener[0] != pid or not self.runtime.check_health():
            raise UpdateError("Unmanaged or unhealthy service; no update was started")
        return pid

    def queue(self, branch: str) -> dict[str, str]:
        valid_branch(branch)
        with LOCK:
            pid = self.owner_pid()
            checked_checkout()
            if git("status", "--porcelain", "--untracked-files=all"):
                raise UpdateError("Local code changes present; refusing to overwrite them")
            if branch not in self.branches()["branches"]:
                raise UpdateError("Selected branch is not advertised by origin")
            if self.status().get("status") in ACTIVE:
                raise UpdateError("Another update is already in progress")
            request_id = secrets.token_hex(16)
            self.write("queued", request_id, branch)
            self.log.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.log.open("ab") as log:
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "agent_control",
                            "update-worker",
                            "--config",
                            str(self.config_path),
                            "--branch",
                            branch,
                            "--request-id",
                            request_id,
                            "--managed-pid",
                            str(pid),
                        ],
                        cwd=CODE_ROOT,
                        env={**os.environ, "AGENT_CONTROL_SERVICE_MANAGED": "1"},
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        close_fds=True,
                    )
            except OSError as error:
                self.write("failed", request_id, branch, "Worker could not start")
                raise UpdateError("Could not launch update worker") from error
            return {"id": request_id, "branch": branch, "status": "queued"}

    def worker(self, branch: str, request_id: str, expected_pid: int) -> None:
        valid_branch(branch)
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise UpdateError("Invalid update request")
        state = self.status()
        if (state.get("status"), state.get("id"), state.get("branch")) != (
            "queued",
            request_id,
            branch,
        ):
            raise UpdateError("Update request no longer active")
        try:
            pid = self.owner_pid()
            if pid != expected_pid:
                raise UpdateError("Managed process changed before update")
            checked_checkout()
            if git("status", "--porcelain", "--untracked-files=all"):
                raise UpdateError("Local changes appeared; refusing to update")
            self.write("fetching", request_id, branch)
            git("fetch", "--prune", "--no-tags", "origin", timeout=90)
            refs = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/")
            if f"origin/{branch}" not in refs.splitlines():
                raise UpdateError("Selected branch disappeared from origin")
            for entry in (
                "agent_control/__main__.py",
                "agent_control/self_update.py",
                "agent_control/runtime.py",
            ):
                git("cat-file", "-e", f"refs/remotes/origin/{branch}:{entry}")
            if git("status", "--porcelain", "--untracked-files=all"):
                raise UpdateError("Checkout changed during fetch")
            self.write("switching", request_id, branch)
            # Switching existing branches is safe; ff-only refuses local divergence.
            local_refs = git("for-each-ref", "--format=%(refname:short)", "refs/heads/")
            if branch in local_refs.splitlines():
                git("switch", branch)
            else:
                git("switch", "--track", "-c", branch, f"origin/{branch}")
            git("merge", "--ff-only", f"refs/remotes/origin/{branch}", timeout=60)
            if git("rev-parse", "--verify", "HEAD") != git(
                "rev-parse", "--verify", f"refs/remotes/origin/{branch}"
            ):
                raise UpdateError("Local branch is ahead of origin; review commits before updating")
            if git("status", "--porcelain", "--untracked-files=all"):
                raise UpdateError("Checkout became dirty after update")
            if self.owner_pid() != pid:
                raise UpdateError("Managed process changed during update")
            self.write("restarting", request_id, branch)
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if self.runtime.inspect_listener() is None:
                    break
                time.sleep(0.2)
            else:
                raise UpdateError("Old service did not stop; no forced termination")
            self.runtime.start()
            self.write("complete", request_id, branch, "Update complete; service restarted")
        except Exception as error:
            safe = str(error) if isinstance(error, UpdateError) else "Update failed"
            self.write("failed", request_id, branch, safe)
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(safe) from error
