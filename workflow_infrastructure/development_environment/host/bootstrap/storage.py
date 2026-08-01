"""Initialize and mount one retained development volume."""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class CommandRunnerProtocol(Protocol):
    """Command boundary required by retained storage."""

    def run(self, command_argument_list: list[str], *, check: bool = True):
        """Run one command."""


class ClockProtocol(Protocol):
    """Monotonic wait boundary required for device discovery."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for one bounded duration."""


class StorageClock:
    """Provide the real host monotonic clock."""

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(delay_seconds: float) -> None:
        time.sleep(delay_seconds)


class HostStorageBootstrap:
    """Own retained EBS identity, filesystem initialization, and mount."""

    def __init__(
        self,
        *,
        initialization_allowed: bool,
        retained_root_path: Path,
        retained_volume_id: str,
        runner: CommandRunnerProtocol,
        clock: ClockProtocol | None = None,
        device_by_id_root_path: Path = Path("/dev/disk/by-id"),
        device_wait_timeout_seconds: int = 360,
        fstab_path: Path = Path("/etc/fstab"),
    ) -> None:
        """Bind storage setup to one exact EBS volume.

        Args:
            retained_root_path: Environment-exclusive mount root.
            retained_volume_id: Exact attached EBS volume identity.
            initialization_allowed: Explicit one-time control-plane permission
                to create XFS on a new base volume.
            runner: Checked process boundary.
            device_by_id_root_path: Linux persistent block-device identity root.
            fstab_path: Persistent mount table path.
        """

        if re.fullmatch(r"vol-[0-9a-f]+", retained_volume_id) is None:
            raise DevelopmentEnvironmentError("Retained EBS volume identity is invalid")
        if not isinstance(initialization_allowed, bool):
            raise DevelopmentEnvironmentError(
                "Retained EBS initialization authorization is invalid"
            )
        if not retained_root_path.is_absolute():
            raise DevelopmentEnvironmentError("Retained root must be absolute")
        if not device_by_id_root_path.is_absolute() or not fstab_path.is_absolute():
            raise DevelopmentEnvironmentError(
                "Retained storage system paths must be absolute"
            )
        if device_wait_timeout_seconds <= 0:
            raise DevelopmentEnvironmentError("Device wait timeout must be positive")
        self._retained_root_path = retained_root_path
        self._retained_volume_id = retained_volume_id
        self._initialization_allowed = initialization_allowed
        self._runner = runner
        self._clock = StorageClock() if clock is None else clock
        self._device_by_id_root_path = device_by_id_root_path
        self._device_wait_timeout_seconds = device_wait_timeout_seconds
        self._fstab_path = fstab_path

    def mount(self) -> None:
        """Create or validate XFS and mount the exact retained volume."""

        device_path = Path(
            self._device_by_id_root_path
            / (
                "nvme-Amazon_Elastic_Block_Store_"
                + self._retained_volume_id.replace("-", "")
            )
        )
        t_deadline = self._clock.monotonic() + self._device_wait_timeout_seconds
        while not device_path.exists() and self._clock.monotonic() < t_deadline:
            self._clock.sleep(2)
        if not device_path.exists():
            raise DevelopmentEnvironmentError(
                "Retained EBS device did not appear within the bounded wait"
            )
        filesystem_type_result = self._runner.run(
            ["blkid", "-s", "TYPE", "-o", "value", str(device_path)],
            check=False,
        )
        if filesystem_type_result.returncode != 0:
            if not self._initialization_allowed:
                raise DevelopmentEnvironmentError(
                    "Retained EBS device has no recognized filesystem and "
                    "initialization is not authorized"
                )
            self._runner.run(["mkfs.xfs", str(device_path)])
            filesystem_type_result = self._runner.run(
                ["blkid", "-s", "TYPE", "-o", "value", str(device_path)]
            )
        if filesystem_type_result.stdout.strip() != "xfs":
            raise DevelopmentEnvironmentError(
                "Retained EBS device does not contain the required XFS filesystem"
            )
        uuid_result = self._runner.run(
            ["blkid", "-s", "UUID", "-o", "value", str(device_path)]
        )
        volume_uuid = uuid_result.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F-]+", volume_uuid) is None:
            raise DevelopmentEnvironmentError("Retained filesystem UUID is invalid")
        self._retained_root_path.mkdir(mode=0o755, parents=True, exist_ok=True)
        fstab_path = self._fstab_path
        fstab_line = f"UUID={volume_uuid} {self._retained_root_path} xfs defaults,nofail,x-systemd.device-timeout=30 0 2"
        fstab_line_list = fstab_path.read_text(encoding="utf-8").splitlines()
        target_entry_list = [
            line
            for line in fstab_line_list
            if not line.lstrip().startswith("#")
            and len(line.split()) >= 2
            and line.split()[1] == str(self._retained_root_path)
        ]
        if target_entry_list not in ([], [fstab_line]):
            raise DevelopmentEnvironmentError(
                "Retained root has another persistent mount declaration"
            )
        if not target_entry_list:
            _fstab_write(
                path=fstab_path,
                line_list=[*fstab_line_list, fstab_line],
            )
        mounted = self._runner.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "UUID",
                "--mountpoint",
                str(self._retained_root_path),
            ],
            check=False,
        )
        if mounted.returncode == 0:
            if mounted.stdout.strip() != volume_uuid:
                raise DevelopmentEnvironmentError(
                    "Retained root is mounted from another filesystem"
                )
        else:
            self._runner.run(["mount", str(self._retained_root_path)])
        for name in (
            "glitchtip",
            "observability",
            "postgres",
            "product-tool",
            "release",
            "secrets",
            "workflow-registry",
            "workflow-run",
        ):
            (self._retained_root_path / name).mkdir(mode=0o750, exist_ok=True)


def _fstab_write(*, path: Path, line_list: list[str]) -> None:
    temporary_path = path.with_name(f".{path.name}.new")
    with temporary_path.open("w", encoding="utf-8") as file:
        file.write("\n".join(line_list).rstrip("\n") + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temporary_path, 0o644)
    os.replace(temporary_path, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
