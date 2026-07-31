"""Tests for development-host storage and idle lifecycle ownership."""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tool.lib.development_storage import (
    DevelopmentHostLifecycle,
    DevelopmentMaintenanceSchedule,
    DevelopmentStorageMonitor,
)

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


class _Clock:
    """Provide controlled UTC time to the host lifecycle."""

    def __init__(self) -> None:
        """Initialize a fixed instant."""

        self.t_now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        """Return the current controlled instant."""

        return self.t_now

    def sleep(self, delay_seconds: float) -> None:
        """Advance without real waiting."""

        self.t_now += timedelta(seconds=delay_seconds)


class _HostPort:
    """Retain lifecycle operations without external side effects."""

    def __init__(self) -> None:
        """Initialize an idle development host."""

        self.event_list: list[str] = []

    def host_node_uncordon(self) -> None:
        """Record node activation."""

        self.event_list.append("uncordon")

    def host_product_activity_get(self) -> str:
        """Return a proved idle Product."""

        return "idle"

    def host_product_maintenance_run(self) -> bool:
        """Record one due retention cycle."""

        self.event_list.append("maintenance")
        return True

    def host_session_is_busy(self, instance_id: str) -> bool:
        """Return no active Session Manager sessions."""

        assert instance_id == "i-test"
        return False

    def host_shutdown(self) -> None:
        """Record the terminal idle transition."""

        self.event_list.append("shutdown")

    def stop_lease_renew(self) -> None:
        """Reject an unexpected busy-host renewal."""

        raise AssertionError("an idle host must not renew a busy lease")


def test_volume_pressure_reports_transitions_and_bounded_reminders(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Warnings are visible without being emitted on every controller poll."""

    usage_by_free_map = {
        "value": _DiskUsage(
            total=100 * 1024**3,
            used=80 * 1024**3,
            free=20 * 1024**3,
        )
    }
    monkeypatch.setattr(
        "tool.lib.development_storage.shutil.disk_usage",
        lambda path: usage_by_free_map["value"],
    )
    monitor = DevelopmentStorageMonitor(state_root_path=tmp_path / "state")
    t_now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)

    monitor.observe(mount_path_by_name_map={"retained": tmp_path}, t_now=t_now)
    monitor.observe(
        mount_path_by_name_map={"retained": tmp_path},
        t_now=t_now + timedelta(minutes=1),
    )
    usage_by_free_map["value"] = _DiskUsage(
        total=100 * 1024**3,
        used=92 * 1024**3,
        free=8 * 1024**3,
    )
    monitor.observe(
        mount_path_by_name_map={"retained": tmp_path},
        t_now=t_now + timedelta(minutes=2),
    )
    usage_by_free_map["value"] = _DiskUsage(
        total=100 * 1024**3,
        used=50 * 1024**3,
        free=50 * 1024**3,
    )
    monitor.observe(
        mount_path_by_name_map={"retained": tmp_path},
        t_now=t_now + timedelta(minutes=3),
    )

    line_list = capsys.readouterr().out.splitlines()
    assert len(line_list) == 3
    assert line_list[0].startswith("WARNING:")
    assert line_list[1].startswith("CRITICAL:")
    assert line_list[2].startswith("OK:")


def test_host_lifecycle_runs_due_maintenance_before_idle_shutdown(
    tmp_path: Path,
) -> None:
    """Retention runs once at an idle safepoint and cannot reset the idle clock."""

    retained_root_path = tmp_path / "retained"
    retained_root_path.mkdir()
    clock = _Clock()
    host_port = _HostPort()

    DevelopmentHostLifecycle(
        clock=clock,
        host_port=host_port,
        retained_root_path=retained_root_path,
        state_root_path=tmp_path / "state",
    ).run(instance_id="i-test")

    assert host_port.event_list == ["uncordon", "maintenance", "shutdown"]
    assert clock.t_now == datetime(2026, 7, 31, 12, 30, 0, tzinfo=UTC)


def test_storage_monitor_ignores_malformed_or_naive_persisted_state(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Corrupt monitor state cannot terminate the host lifecycle controller."""

    state_root_path = tmp_path / "state"
    state_root_path.mkdir()
    (state_root_path / "volume-pressure.json").write_text(
        json.dumps(
            {
                "t_report": "2026-07-31T12:00:00",
                "volume_by_name_map": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tool.lib.development_storage.shutil.disk_usage",
        lambda path: _DiskUsage(
            total=100 * 1024**3,
            used=80 * 1024**3,
            free=20 * 1024**3,
        ),
    )

    DevelopmentStorageMonitor(state_root_path=state_root_path).observe(
        mount_path_by_name_map={"retained": tmp_path},
        t_now=datetime(2026, 7, 31, 13, 0, 0, tzinfo=UTC),
    )

    assert capsys.readouterr().out.startswith("WARNING:")


def test_maintenance_schedule_treats_naive_state_as_due(tmp_path: Path) -> None:
    """A stale non-UTC marker cannot suppress idle retention indefinitely."""

    state_root_path = tmp_path / "state"
    state_root_path.mkdir()
    (state_root_path / "product-maintenance-attempt").write_text(
        "2026-07-31T12:00:00\n",
        encoding="utf-8",
    )

    assert DevelopmentMaintenanceSchedule(state_root_path=state_root_path).is_due(
        t_now=datetime(2026, 7, 31, 13, 0, 0, tzinfo=UTC)
    )
