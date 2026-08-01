"""Execute explicit host-bootstrap commands."""

from __future__ import annotations

from collections.abc import Sequence
import subprocess


class HostBootstrapCommandRunner:
    """Run one checked host-bootstrap process without shell evaluation."""

    def run(
        self, command_argument_list: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run one command and retain text diagnostics.

        Args:
            command_argument_list: Exact executable and arguments.
            check: Raise when the process exits unsuccessfully.

        Returns:
            Completed process result.
        """

        return subprocess.run(
            command_argument_list,
            capture_output=True,
            check=check,
            text=True,
        )
