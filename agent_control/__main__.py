"""Local showcase and managed-service CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn

from agent_control.config import ConfigurationError, Settings, default_config_path
from agent_control.demo import initialize_projects, seed_demo
from agent_control.runtime import OperationError, RuntimeManager, enable_private_serve
from agent_control.self_update import SelfUpdater, UpdateError
from agent_control.setup import configure
from agent_control.storage import JsonRecordStore, StorageError
from agent_control.web.app import create_app


def absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def output(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Agent Control Service")
    commands = root.add_subparsers(dest="command", required=True)

    def configured(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=absolute_file, help="optional absolute config path")
        return command

    configured("config", "interactive setup and settings menu")
    configured("serve", "run the foreground local dashboard")
    worker = configured("update-worker", "internal managed service update worker")
    worker.add_argument("--branch", required=True)
    worker.add_argument("--request-id", required=True)
    worker.add_argument("--managed-pid", required=True, type=int)
    init = configured("init", "initialize local demonstration state")
    init.add_argument("--demo", action="store_true", help="add synthetic demonstration data")
    for name in ("start", "status", "stop", "install-launch-agent"):
        configured(name, f"managed service: {name}")
    tailscale = configured("enable-private-serve", "optional private local-service exposure")
    tailscale.add_argument("--https-port", type=int, default=8443)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        path = args.config or default_config_path()
        if args.command == "config":
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise ConfigurationError("configuration menu requires a terminal")
            configure(path)
            return
        if not path.is_file() and not path.is_symlink():
            if sys.stdin.isatty() and sys.stdout.isatty():
                print(f"No configuration at {path}; launching setup menu.")
                if configure(path) is None:
                    raise ConfigurationError("configuration setup was cancelled")
            else:
                raise ConfigurationError(
                    "configuration missing; run 'agent-control config' in a terminal"
                )
        settings = Settings.from_file(path)
        store = JsonRecordStore(settings.state_directory)
        if args.command == "serve":
            if settings.coordination_source == "local":
                store.initialize()
            uvicorn.run(
                create_app(settings, config_path=path), host=settings.host, port=settings.port
            )
        elif args.command == "update-worker":
            SelfUpdater(settings, path).worker(args.branch, args.request_id, args.managed_pid)
        elif args.command == "init":
            if settings.coordination_source != "local":
                raise ConfigurationError(
                    "demo initialization requires coordination_source = 'local'"
                )
            initialize_projects(settings, store)
            if args.demo:
                seed_demo(settings, store)
            output({"status": "initialized", "demo": args.demo})
        elif args.command in {"start", "status", "stop", "install-launch-agent"}:
            manager = RuntimeManager(settings, path)
            result = getattr(manager, args.command.replace("-", "_"))()
            output(result) if isinstance(result, dict) else print(result)
        elif args.command == "enable-private-serve":
            print(enable_private_serve(settings, args.https_port))
    except (ConfigurationError, StorageError, OperationError, UpdateError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
