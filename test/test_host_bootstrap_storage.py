"""Verify fail-closed retained-volume host initialization."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.storage import (
    HostStorageBootstrap,
)


class _Runner:
    """Model filesystem probes, creation, and mount commands."""

    def __init__(self, filesystem_type: str) -> None:
        """Initialize the runner dependencies.

        Args:
            filesystem_type: Filesystem type.
        """

        self.command_list_list: list[list[str]] = []
        self.filesystem_type = filesystem_type

    def run(
        self,
        command_argument_list: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Record one host-storage command and return a successful result.

        Args:
            command_argument_list: Ordered command argument values.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """

        self.command_list_list.append(command_argument_list)
        if command_argument_list[:5] == ["blkid", "-s", "TYPE", "-o", "value"]:
            returncode = 0 if self.filesystem_type else 2
            if check and returncode:
                raise subprocess.CalledProcessError(returncode, command_argument_list)
            return subprocess.CompletedProcess(
                command_argument_list,
                returncode,
                f"{self.filesystem_type}\n" if self.filesystem_type else "",
                "",
            )
        if command_argument_list[0] == "mkfs.xfs":
            self.filesystem_type = "xfs"
            return subprocess.CompletedProcess(command_argument_list, 0, "", "")
        if command_argument_list[:5] == ["blkid", "-s", "UUID", "-o", "value"]:
            return subprocess.CompletedProcess(
                command_argument_list,
                0,
                "01234567-89ab-cdef-0123-456789abcdef\n",
                "",
            )
        if command_argument_list[0] == "findmnt":
            return subprocess.CompletedProcess(command_argument_list, 1, "", "")
        if command_argument_list[0] == "mount":
            return subprocess.CompletedProcess(command_argument_list, 0, "", "")
        raise AssertionError(command_argument_list)


def _bootstrap_get(
    tmp_path: Path,
    *,
    filesystem_type: str,
    initialization_allowed: bool,
) -> tuple[HostStorageBootstrap, _Runner, Path]:
    """Build one host-storage bootstrap with deterministic test paths.

    Args:
        tmp_path: Temporary directory path.
        filesystem_type: Filesystem type.
        initialization_allowed: Initialization allowed.

    Returns:
        The bootstrap.
    """

    device_root_path = tmp_path / "device-by-id"
    device_root_path.mkdir()
    device_path = tmp_path / "nvme1n1"
    device_path.touch()
    expected_device_path = device_root_path / ("nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0")
    expected_device_path.symlink_to(device_path)
    fstab_path = tmp_path / "fstab"
    fstab_path.write_text("# test mount table\n", encoding="utf-8")
    runner = _Runner(filesystem_type)
    return (
        HostStorageBootstrap(
            initialization_allowed=initialization_allowed,
            retained_root_path=tmp_path / "retained",
            retained_volume_id="vol-0123456789abcdef0",
            runner=runner,
            device_by_id_root_path=device_root_path,
            fstab_path=fstab_path,
        ),
        runner,
        fstab_path,
    )


def test_pending_volume_creates_xfs_without_zero_content_assumption(
    tmp_path: Path,
) -> None:
    """Explicit control-plane authorization replaces invalid raw-byte heuristics.

    Args:
        tmp_path: Temporary directory path.
    """

    bootstrap, runner, fstab_path = _bootstrap_get(
        tmp_path,
        filesystem_type="",
        initialization_allowed=True,
    )

    bootstrap.mount()

    command_name_list = [command[0] for command in runner.command_list_list]
    assert command_name_list.count("mkfs.xfs") == 1
    assert "blockdev" not in command_name_list
    assert "cmp" not in command_name_list
    assert "mount" in command_name_list
    findmnt_command = next(command for command in runner.command_list_list if command[0] == "findmnt")
    assert "--mountpoint" in findmnt_command
    assert "--target" not in findmnt_command
    assert " xfs defaults,nofail,x-systemd.device-timeout=30 0 2" in (fstab_path.read_text(encoding="utf-8"))


def test_complete_volume_without_filesystem_is_never_reformatted(
    tmp_path: Path,
) -> None:
    """Loss of a completed filesystem fails closed instead of destroying data.

    Args:
        tmp_path: Temporary directory path.
    """

    bootstrap, runner, _ = _bootstrap_get(
        tmp_path,
        filesystem_type="",
        initialization_allowed=False,
    )

    with pytest.raises(DevelopmentEnvironmentError, match="not authorized"):
        bootstrap.mount()

    assert all(command[0] != "mkfs.xfs" for command in runner.command_list_list)


def test_existing_non_xfs_filesystem_is_never_overwritten(tmp_path: Path) -> None:
    """Even pending authorization cannot overwrite a recognized foreign filesystem.

    Args:
        tmp_path: Temporary directory path.
    """

    bootstrap, runner, _ = _bootstrap_get(
        tmp_path,
        filesystem_type="ext4",
        initialization_allowed=True,
    )

    with pytest.raises(DevelopmentEnvironmentError, match="required XFS"):
        bootstrap.mount()

    assert all(command[0] != "mkfs.xfs" for command in runner.command_list_list)
