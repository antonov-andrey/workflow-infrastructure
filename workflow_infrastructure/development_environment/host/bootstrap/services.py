"""Write and activate host-owned systemd services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class CommandRunnerProtocol(Protocol):
    """Command boundary required by systemd service activation."""

    def run(self, command_argument_list: list[str], *, check: bool = True):
        """Run one command."""


class HostBootstrapServiceManager:
    """Own atomic systemd unit publication and activation."""

    def __init__(
        self,
        *,
        runner: CommandRunnerProtocol,
        systemd_root_path: Path = Path("/etc/systemd/system"),
    ) -> None:
        """Bind service publication to one systemd root.

        Args:
            runner: Checked process boundary.
            systemd_root_path: Host systemd unit directory.
        """

        self._runner = runner
        self._systemd_root_path = systemd_root_path

    def unit_write(self, *, name: str, text: str) -> None:
        """Atomically replace one root-owned unit file.

        Args:
            name: Exact systemd unit name.
            text: Complete unit content.
        """

        destination_path = self._systemd_root_path / name
        temporary_path = destination_path.with_name(f".{destination_path.name}.new")
        with temporary_path.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, destination_path)
        descriptor = os.open(self._systemd_root_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def activate(self, *name_list: str) -> None:
        """Reload systemd and enable every selected service.

        Args:
            name_list: Exact systemd unit names.
        """

        self._runner.run(["systemctl", "daemon-reload"])
        for name in name_list:
            self._runner.run(["systemctl", "enable", "--now", name])
