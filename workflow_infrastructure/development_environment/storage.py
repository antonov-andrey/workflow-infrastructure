"""Development-host storage pressure and maintenance scheduling primitives."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

_GIB = 1024**3
_WARNING_FREE_BYTES = 10 * _GIB
_CRITICAL_FREE_BYTES = 5 * _GIB
_WARNING_USED_PERCENT = 75
_CRITICAL_USED_PERCENT = 90
_PRESSURE_REMINDER_INTERVAL = timedelta(hours=6)
_MAINTENANCE_INTERVAL = timedelta(hours=6)
_IDLE_SHUTDOWN_DELAY = timedelta(minutes=30)
_LEASE_RENEW_INTERVAL = timedelta(minutes=30)
_CONTROLLER_POLL_INTERVAL_SECONDS = 60


class DevelopmentHostLifecyclePort(Protocol):
    """Expose the infrastructure operations owned outside the lifecycle state machine."""

    def host_node_uncordon(self) -> None:
        """Allow workloads to schedule on the development node."""

    def host_product_activity_get(self) -> str:
        """Return ``busy`` or ``idle`` using the Product-owned activity contract."""

    def host_product_maintenance_run(self) -> bool:
        """Run idle-only Product maintenance and return whether it succeeded."""

    def host_session_is_busy(self, instance_id: str) -> bool:
        """Return whether an active Session Manager session exists."""

    def host_shutdown(self) -> None:
        """Stop the development host through its fail-safe shutdown sequence."""

    def stop_lease_renew(self) -> None:
        """Renew the external stop lease."""


class DevelopmentHostLifecycleClock(Protocol):
    """Expose controlled UTC time and waiting to the lifecycle state machine."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a non-negative duration."""


class DevelopmentHostLifecycle:
    """Own the development host's activity, maintenance, and shutdown state machine."""

    def __init__(
        self,
        *,
        clock: DevelopmentHostLifecycleClock,
        host_port: DevelopmentHostLifecyclePort,
        retained_root_path: Path,
        state_root_path: Path,
    ) -> None:
        """Initialize one environment-exclusive host lifecycle."""

        self._clock = clock
        self._host_port = host_port
        self._idle_start_path = state_root_path / "idle-start"
        self._maintenance_schedule = DevelopmentMaintenanceSchedule(state_root_path=state_root_path)
        self._retained_root_path = retained_root_path
        self._state_root_path = state_root_path
        self._storage_monitor = DevelopmentStorageMonitor(state_root_path=state_root_path)

    def run(self, *, instance_id: str) -> None:
        """Run until the host is stopped after one continuously idle interval."""

        self._host_port.host_node_uncordon()
        self._state_root_path.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._idle_start_path.unlink(missing_ok=True)
        t_last_lease_renew = datetime.min.replace(tzinfo=UTC)
        while True:
            t_now = self._clock.now()
            self._storage_pressure_observe(t_now=t_now)
            have_session = self._host_port.host_session_is_busy(instance_id)
            product_activity = self._host_port.host_product_activity_get()
            if have_session or product_activity == "busy":
                self._idle_start_path.unlink(missing_ok=True)
                if t_now - t_last_lease_renew >= _LEASE_RENEW_INTERVAL:
                    self._host_port.stop_lease_renew()
                    t_last_lease_renew = t_now
            else:
                if self._maintenance_schedule.is_due(t_now=t_now):
                    self._maintenance_schedule.attempt_record(t_now=t_now)
                    self._host_port.host_product_maintenance_run()
                if not self._idle_start_path.exists():
                    self._idle_start_path.write_text(t_now.isoformat(), encoding="utf-8")
                    os.chmod(self._idle_start_path, 0o600)
                t_idle_start = self._idle_start_get()
                if t_idle_start is None:
                    self._clock.sleep(_CONTROLLER_POLL_INTERVAL_SECONDS)
                    continue
                if t_now - t_idle_start >= _IDLE_SHUTDOWN_DELAY:
                    have_session = self._host_port.host_session_is_busy(instance_id)
                    product_activity = self._host_port.host_product_activity_get()
                    if not have_session and product_activity == "idle":
                        self._host_port.host_shutdown()
                        return
                    self._idle_start_path.unlink(missing_ok=True)
            self._clock.sleep(_CONTROLLER_POLL_INTERVAL_SECONDS)

    def _idle_start_get(self) -> datetime | None:
        """Return the retained idle-start instant or reset malformed state."""

        try:
            t_idle_start = datetime.fromisoformat(self._idle_start_path.read_text(encoding="utf-8").strip())
        except OSError, ValueError:
            self._idle_start_path.unlink(missing_ok=True)
            return None
        if t_idle_start.tzinfo is None:
            self._idle_start_path.unlink(missing_ok=True)
            return None
        return t_idle_start

    def _storage_pressure_observe(self, *, t_now: datetime) -> None:
        """Report capacity pressure without terminating lifecycle protection."""

        try:
            self._storage_monitor.observe(
                mount_path_by_name_map={
                    "root": Path("/"),
                    "retained": self._retained_root_path,
                },
                t_now=t_now,
            )
        except DevelopmentStorageError as error:
            print(f"WARNING: development volume pressure observation failed: {error}")


@dataclass(frozen=True)
class VolumePressure:
    """Describe one filesystem's bounded capacity observation."""

    free_bytes: int
    level: str
    mount_path: str
    total_bytes: int
    used_bytes: int
    used_percent: int


class DevelopmentStorageMonitor:
    """Persist transition-aware root and retained-volume pressure warnings."""

    def __init__(self, *, state_root_path: Path) -> None:
        """Initialize the monitor below one environment-exclusive state root."""

        self._state_path = state_root_path / "volume-pressure.json"

    def observe(
        self,
        *,
        mount_path_by_name_map: Mapping[str, Path],
        t_now: datetime,
    ) -> dict[str, VolumePressure]:
        """Observe all mounts and print only transitions or bounded reminders."""

        if t_now.tzinfo is None:
            raise DevelopmentStorageError("storage observation timestamp must be timezone-aware")
        previous_payload = self._state_payload_get()
        current_by_name_map = {
            name: _volume_pressure_get(path) for name, path in sorted(mount_path_by_name_map.items())
        }
        previous_by_name_map_payload = previous_payload.get("volume_by_name_map", {})
        previous_by_name_map = previous_by_name_map_payload if isinstance(previous_by_name_map_payload, Mapping) else {}
        previous_t_report_text = previous_payload.get("t_report", "")
        try:
            previous_t_report = datetime.fromisoformat(previous_t_report_text)
        except TypeError, ValueError:
            previous_t_report = datetime.min.replace(tzinfo=UTC)
        if previous_t_report.tzinfo is None:
            previous_t_report = datetime.min.replace(tzinfo=UTC)
        should_remind = t_now - previous_t_report >= _PRESSURE_REMINDER_INTERVAL
        did_report = False
        for name, pressure in current_by_name_map.items():
            previous = previous_by_name_map.get(name)
            previous_level = previous.get("level") if isinstance(previous, Mapping) else ""
            if pressure.level == "normal":
                if previous_level in {"warning", "critical"}:
                    print(
                        f"OK: development volume {name} recovered "
                        f"({pressure.used_percent}% used, {pressure.free_bytes // _GIB} GiB free)"
                    )
                    did_report = True
                continue
            if pressure.level != previous_level or should_remind:
                print(
                    f"{pressure.level.upper()}: development volume {name} at {pressure.mount_path} "
                    f"is {pressure.used_percent}% used with {pressure.free_bytes // _GIB} GiB free"
                )
                did_report = True
        payload = {
            "t_observed": t_now.isoformat(),
            "t_report": t_now.isoformat() if did_report else previous_t_report_text,
            "volume_by_name_map": {name: asdict(pressure) for name, pressure in current_by_name_map.items()},
        }
        self._state_payload_write(payload)
        return current_by_name_map

    def _state_payload_get(self) -> dict[str, object]:
        """Return prior monitor state without trusting malformed bytes."""

        if not self._state_path.is_file():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _state_payload_write(self, payload: Mapping[str, object]) -> None:
        """Atomically retain the latest pressure state."""

        self._state_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary_path = self._state_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self._state_path)


class DevelopmentMaintenanceSchedule:
    """Persist one bounded cadence for expensive idle-only Product retention."""

    def __init__(self, *, state_root_path: Path) -> None:
        """Initialize the schedule below one environment-exclusive state root."""

        self._attempt_path = state_root_path / "product-maintenance-attempt"

    def is_due(self, *, t_now: datetime) -> bool:
        """Return whether another idle maintenance attempt is due."""

        if t_now.tzinfo is None:
            raise DevelopmentStorageError("maintenance timestamp must be timezone-aware")
        if not self._attempt_path.is_file():
            return True
        try:
            t_previous = datetime.fromisoformat(self._attempt_path.read_text(encoding="utf-8").strip())
        except OSError, ValueError:
            return True
        if t_previous.tzinfo is None:
            return True
        return t_now - t_previous >= _MAINTENANCE_INTERVAL

    def attempt_record(self, *, t_now: datetime) -> None:
        """Atomically record an attempt before invoking Product maintenance."""

        self._attempt_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary_path = self._attempt_path.with_suffix(".tmp")
        temporary_path.write_text(t_now.isoformat() + "\n", encoding="utf-8")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self._attempt_path)


def _volume_pressure_get(path: Path) -> VolumePressure:
    """Return one classified filesystem-capacity observation."""

    try:
        usage = shutil.disk_usage(path)
    except OSError as error:
        raise DevelopmentStorageError(f"volume usage is unavailable: {path}") from error
    used_percent = 100 if usage.total <= 0 else (usage.used * 100) // usage.total
    if used_percent >= _CRITICAL_USED_PERCENT or usage.free < _CRITICAL_FREE_BYTES:
        level = "critical"
    elif used_percent >= _WARNING_USED_PERCENT or usage.free < _WARNING_FREE_BYTES:
        level = "warning"
    else:
        level = "normal"
    return VolumePressure(
        free_bytes=usage.free,
        level=level,
        mount_path=path.as_posix(),
        total_bytes=usage.total,
        used_bytes=usage.used,
        used_percent=used_percent,
    )


class DevelopmentStorageError(RuntimeError):
    """Raised when development storage pressure cannot be observed safely."""
