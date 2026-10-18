from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_control.config import Settings
from agent_control.runtime import OperationError, RuntimeManager, serve_route_state


def manager(settings: Settings, tmp_path: Path) -> RuntimeManager:
    config = tmp_path / "config.toml"
    config.write_text("# validated elsewhere\n", encoding="utf-8")
    return RuntimeManager(settings, config)


def test_serve_route_uses_dedicated_port_and_preserves_443() -> None:
    target = "http://127.0.0.1:8420"
    assert serve_route_state({}, https_port=8443, target=target) == "free"
    exact = {
        "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
        "Web": {
            "host.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8001"}}},
            "host.ts.net:8443": {"Handlers": {"/": {"Proxy": target}}},
        },
    }
    assert serve_route_state(exact, https_port=8443, target=target) == "configured"
    assert serve_route_state(exact, https_port=443, target=target) == "conflict"
    public = {**exact, "AllowFunnel": {"host.ts.net:8443": True}}
    assert serve_route_state(public, https_port=8443, target=target) == "conflict"


def test_stop_never_kills_unowned_listener(settings: Settings, tmp_path: Path) -> None:
    runtime = manager(settings, tmp_path)
    with (
        patch.object(runtime, "inspect_listener", return_value=(1234, "127.0.0.1:8420")),
        patch.object(runtime, "managed_pid", return_value=None),
        patch("agent_control.runtime.os.kill") as kill,
        pytest.raises(OperationError, match="not owned"),
    ):
        runtime.stop()
    kill.assert_not_called()


def test_start_refuses_unowned_listener(settings: Settings, tmp_path: Path) -> None:
    runtime = manager(settings, tmp_path)
    with (
        patch.object(runtime, "inspect_listener", return_value=(1234, "127.0.0.1:8420")),
        patch.object(runtime, "managed_pid", return_value=None),
        patch("agent_control.runtime.subprocess.Popen") as popen,
        pytest.raises(OperationError, match="nothing was stopped"),
    ):
        runtime.start()
    popen.assert_not_called()


def test_managed_pid_requires_exact_config_host_port_and_marker(
    settings: Settings, tmp_path: Path
) -> None:
    runtime = manager(settings, tmp_path)
    runtime.runtime_dir.mkdir(parents=True)
    runtime.pid_path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "config": str(runtime.config_path),
                "host": settings.host,
                "port": settings.port,
            }
        ),
        encoding="utf-8",
    )
    with patch.object(runtime, "expected_process", return_value=True):
        assert runtime.managed_pid() == 1234
    with patch.object(runtime, "expected_process", return_value=False):
        assert runtime.managed_pid() is None


def test_listener_must_be_exclusively_loopback(settings: Settings, tmp_path: Path) -> None:
    runtime = manager(settings, tmp_path)
    with (
        patch.object(runtime, "listener_rows", return_value=[(10, "*:8420")]),
        pytest.raises(OperationError, match="non-loopback"),
    ):
        runtime.inspect_listener()
