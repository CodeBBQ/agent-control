"""Conservative process ownership, LaunchAgent, and private Serve operations."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_control.config import Settings

MANAGED_MARKER = "AGENT_CONTROL_SERVICE_MANAGED=1"
LAUNCH_AGENT_LABEL = "com.codebbq.agent-control-service"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
CODE_ROOT = Path(__file__).resolve().parent.parent


class OperationError(RuntimeError):
    pass


def run(
    arguments: list[str], *, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(arguments, check=check, capture_output=True, text=True, env=env)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        detail = (
            error.stderr.strip()
            if isinstance(error, subprocess.CalledProcessError) and error.stderr
            else str(error)
        )
        raise OperationError(f"command failed safely: {arguments[0]}: {detail}") from error


class RuntimeManager:
    def __init__(self, settings: Settings, config_path: Path) -> None:
        self.settings = settings
        self.config_path = config_path.resolve(strict=True)
        self.runtime_dir = settings.cache_directory / "runtime"
        self.pid_path = self.runtime_dir / "process.json"
        self.log_dir = settings.cache_directory / "logs"

    @property
    def target(self) -> str:
        return f"http://{self.settings.host}:{self.settings.port}"

    def listener_rows(self) -> list[tuple[int, str]]:
        result = run(["lsof", "-nP", f"-iTCP:{self.settings.port}", "-sTCP:LISTEN"], check=False)
        if result.returncode not in (0, 1):
            raise OperationError(f"cannot inspect port {self.settings.port}")
        rows: list[tuple[int, str]] = []
        for line in result.stdout.splitlines()[1:]:
            words = line.split()
            if len(words) >= 9 and words[1].isdigit():
                rows.append((int(words[1]), words[-2]))
        return rows

    def inspect_listener(self) -> tuple[int, str] | None:
        rows = self.listener_rows()
        if not rows:
            return None
        expected = f"{self.settings.host}:{self.settings.port}"
        if len(rows) != 1 or rows[0][1] != expected:
            raise OperationError(
                f"port {self.settings.port} has a non-loopback or ambiguous listener"
            )
        return rows[0]

    def check_health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.target}/health", timeout=2) as response:
                payload: object = json.loads(response.read())
                return response.status == 200 and payload == {"status": "ok"}
        except (OSError, ValueError, urllib.error.HTTPError):
            return False

    def expected_process(self, pid: int) -> bool:
        process = run(["ps", "eww", "-p", str(pid), "-o", "command="], check=False)
        if process.returncode or MANAGED_MARKER not in process.stdout:
            return False
        command = process.stdout
        return "-m agent_control serve" in command and str(self.config_path) in command

    def managed_pid(self) -> int | None:
        if not self.pid_path.is_file() or self.pid_path.is_symlink():
            return None
        try:
            state = json.loads(self.pid_path.read_text(encoding="utf-8"))
            pid = int(state["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        expected = {
            "config": str(self.config_path),
            "host": self.settings.host,
            "port": self.settings.port,
        }
        if any(state.get(key) != value for key, value in expected.items()):
            return None
        return pid if pid > 0 and self.expected_process(pid) else None

    def _write_pid(self, pid: int) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "config": str(self.config_path),
            "host": self.settings.host,
            "port": self.settings.port,
            "code_root": str(CODE_ROOT),
        }
        temporary = self.pid_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.pid_path)

    def start(self) -> str:
        listener = self.inspect_listener()
        managed = self.managed_pid()
        if listener:
            if managed == listener[0] and self.check_health():
                return f"Agent Control is already healthy (pid {managed})."
            raise OperationError(
                f"port {self.settings.port} is occupied by an unowned or unhealthy process; "
                "nothing was stopped"
            )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stdout = (self.log_dir / "service.out.log").open("ab")
        stderr = (self.log_dir / "service.err.log").open("ab")
        environment = {**os.environ, "AGENT_CONTROL_SERVICE_MANAGED": "1"}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_control",
                "serve",
                "--config",
                str(self.config_path),
            ],
            cwd=CODE_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        stdout.close()
        stderr.close()
        self._write_pid(process.pid)
        for _ in range(30):
            if self.check_health() and self.expected_process(process.pid):
                return f"Started Agent Control on {self.target} (pid {process.pid})."
            if process.poll() is not None:
                break
            time.sleep(0.1)
        raise OperationError(f"service did not become healthy; inspect {self.log_dir}")

    def status(self) -> dict[str, Any]:
        listener = self.inspect_listener()
        managed = self.managed_pid()
        return {
            "managed": bool(listener and managed == listener[0]),
            "healthy": self.check_health() if listener else False,
            "pid": listener[0] if listener else None,
            "target": self.target,
            "ownership": "managed"
            if listener and managed == listener[0]
            else "unowned"
            if listener
            else "stopped",
        }

    def stop(self) -> str:
        listener = self.inspect_listener()
        managed = self.managed_pid()
        if not listener:
            self.pid_path.unlink(missing_ok=True)
            return "Agent Control is not running."
        if managed != listener[0]:
            raise OperationError(
                "listener is not owned by this managed service; refusing to terminate it"
            )
        os.kill(managed, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(managed, 0)
            except ProcessLookupError:
                self.pid_path.unlink(missing_ok=True)
                return f"Stopped managed Agent Control process {managed}."
            time.sleep(0.1)
        raise OperationError("managed process did not stop after SIGTERM; it was not force-killed")

    def launch_agent_payload(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": LAUNCH_AGENT_LABEL,
                "ProgramArguments": [
                    sys.executable,
                    "-m",
                    "agent_control",
                    "start",
                    "--config",
                    str(self.config_path),
                ],
                "RunAtLoad": True,
                "StartInterval": 60,
                "ThrottleInterval": 10,
                "ProcessType": "Background",
                "StandardOutPath": str(self.log_dir / "launchd.out.log"),
                "StandardErrorPath": str(self.log_dir / "launchd.err.log"),
            },
            sort_keys=True,
        )

    def install_launch_agent(self) -> str:
        if sys.platform != "darwin":
            raise OperationError("LaunchAgent installation is supported only on macOS")
        parent = LAUNCH_AGENT_PATH.parent
        parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or LAUNCH_AGENT_PATH.is_symlink():
            raise OperationError("LaunchAgent path must not be a symlink")
        if LAUNCH_AGENT_PATH.exists():
            try:
                existing = plistlib.loads(LAUNCH_AGENT_PATH.read_bytes())
            except (OSError, plistlib.InvalidFileException) as error:
                raise OperationError(
                    "existing LaunchAgent is unreadable; refusing to overwrite"
                ) from error
            if existing.get("Label") != LAUNCH_AGENT_LABEL:
                raise OperationError("existing LaunchAgent path belongs to another label")
        payload = self.launch_agent_payload()
        with tempfile.NamedTemporaryFile(
            dir=parent, prefix=f".{LAUNCH_AGENT_LABEL}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        try:
            temporary.chmod(0o644)
            temporary.replace(LAUNCH_AGENT_PATH)
        finally:
            temporary.unlink(missing_ok=True)
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"], check=False)
        run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT_PATH)])
        return f"Installed {LAUNCH_AGENT_LABEL} for {self.config_path}."


def tailscale_binary() -> str:
    chosen = os.environ.get("TAILSCALE_BIN") or shutil.which("tailscale")
    app_binary = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if not chosen and app_binary.is_file():
        chosen = str(app_binary)
    if not chosen:
        raise OperationError("Tailscale CLI is not available")
    return chosen


def tailscale(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return run([tailscale_binary(), *arguments], env={**os.environ, "TAILSCALE_BE_CLI": "1"})


def serve_route_state(config: dict[str, Any], *, https_port: int, target: str) -> str:
    if https_port == 443 or not 1024 <= https_port <= 65535:
        return "conflict"
    suffix = f":{https_port}"
    services = config.get("Services") or {}
    tcp = config.get("TCP") or {}
    web = config.get("Web") or {}
    funnel = config.get("AllowFunnel") or {}
    if not all(isinstance(item, dict) for item in (services, tcp, web, funnel)):
        return "conflict"
    for service in services.values():
        if isinstance(service, dict) and str(https_port) in (service.get("TCP") or {}):
            return "conflict"
    if any(str(name).endswith(suffix) and allowed for name, allowed in funnel.items()):
        return "conflict"
    relevant = []
    for name, value in web.items():
        if str(name).endswith(suffix):
            if not isinstance(value, dict) or not isinstance(value.get("Handlers"), dict):
                return "conflict"
            relevant.append(value["Handlers"])
    entry = tcp.get(str(https_port))
    if entry is None and not relevant:
        return "free"
    if (
        isinstance(entry, dict)
        and entry.get("HTTPS") is True
        and relevant == [{"/": {"Proxy": target}}]
    ):
        return "configured"
    return "conflict"


def enable_private_serve(settings: Settings, https_port: int = 8443) -> str:
    if https_port == 443:
        raise OperationError(
            "HTTPS 443 is reserved for existing operator routes; choose a dedicated port"
        )
    raw = tailscale(["serve", "status", "--json"]).stdout.strip()
    try:
        config = json.loads(raw) if raw else {}
    except ValueError as error:
        raise OperationError("cannot inspect Tailscale Serve JSON; no changes made") from error
    if not isinstance(config, dict):
        raise OperationError("unexpected Tailscale Serve status; no changes made")
    target = f"http://{settings.host}:{settings.port}"
    state = serve_route_state(config, https_port=https_port, target=target)
    if state == "conflict":
        raise OperationError(
            f"HTTPS port {https_port} is already owned or public; no Serve changes made"
        )
    if state == "configured":
        return f"Private Tailscale Serve route is already configured on HTTPS {https_port}."
    status = tailscale(["status"]).stdout.lower()
    if "stopped" in status or "logged out" in status:
        raise OperationError("Tailscale is not connected; no Serve changes made")
    tailscale(["serve", "--bg", f"--https={https_port}", target])
    checked = tailscale(["serve", "status", "--json"]).stdout.strip()
    try:
        verified = json.loads(checked)
    except ValueError as error:
        raise OperationError(
            "Serve changed but verification failed; inspect Tailscale manually"
        ) from error
    if (
        not isinstance(verified, dict)
        or serve_route_state(verified, https_port=https_port, target=target) != "configured"
    ):
        raise OperationError("Serve route did not verify as the exclusive expected private mapping")
    return f"Configured private Tailscale HTTPS {https_port} → {target}."
