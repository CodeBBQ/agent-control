"""Behavioral acceptance: HTTP rendering is independent of remote GitHub latency."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agent_control.config import Settings
from agent_control.github import GitHubProjectSnapshot, SnapshotCache
from agent_control.models import Project
from agent_control.projection import CanvasProjection
from agent_control.storage import JsonRecordStore
from agent_control.web.app import create_app


class BlockingSource:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def fetch_project(self, project: Project) -> GitHubProjectSnapshot:
        self.calls += 1
        self.entered.set()
        if self.calls == 1:
            assert self.release.wait(timeout=8), "test must release simulated GitHub call"
        return GitHubProjectSnapshot(
            project_id=project.id,
            repository=project.repository,
            observed_at=datetime.now(UTC),
        )


def _projection(settings: Settings, root: Path, source: BlockingSource) -> CanvasProjection:
    return CanvasProjection(
        settings,
        JsonRecordStore(root / "records"),
        SnapshotCache(root / "snapshots"),
        source,
    )


def test_slow_github_does_not_block_dashboard_or_refresh(
    settings: Settings, tmp_path: Path
) -> None:
    source = BlockingSource()
    projection = _projection(settings, tmp_path, source)
    with TestClient(create_app(settings, projection=projection)) as client:
        assert source.entered.wait(timeout=3), "background sync did not start"
        began = time.monotonic()
        overview = client.get("/")
        failures = client.get("/failures")
        refresh = client.get("/refresh", follow_redirects=False)
        elapsed = time.monotonic() - began
        assert overview.status_code == 200
        assert failures.status_code == 200
        assert refresh.status_code == 303
        assert elapsed < 1.5, "web requests waited for blocked GitHub fetch"
        assert "stale" in overview.text.lower()
        assert source.calls == 1, "Refresh must not start a concurrent fetch"
        source.release.set()
        deadline = time.monotonic() + 3
        while source.calls < len(settings.projects) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert source.calls >= len(settings.projects)


def test_changed_settings_cannot_publish_old_inflight_snapshot(
    settings: Settings, tmp_path: Path
) -> None:
    source = BlockingSource()
    projection = _projection(settings, tmp_path, source)
    worker = threading.Thread(target=projection.refresh_github)
    worker.start()
    try:
        assert source.entered.wait(timeout=3)
        changed = settings.model_copy(update={"projects": (settings.projects[1],)})
        projection.reset_settings(changed)
        source.release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
        data = projection.data()
        assert len(data.github) == 1
        assert settings.projects[0].id not in data.github
        assert data.github[changed.projects[0].id].stale
    finally:
        source.release.set()
        worker.join(timeout=3)


def test_read_only_data_never_fetches_uninitialized_source(
    settings: Settings, tmp_path: Path
) -> None:
    source = BlockingSource()
    projection = _projection(settings, tmp_path, source)
    data = projection.data(force_refresh=True)
    assert source.calls == 0
    assert len(data.github) == len(settings.projects)
    assert all(item.stale and item.sync_error for item in data.github.values())
