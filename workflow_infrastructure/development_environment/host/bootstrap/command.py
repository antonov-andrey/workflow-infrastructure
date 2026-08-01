"""Execute explicit host-bootstrap commands."""

from __future__ import annotations

from collections.abc import Sequence
import subprocess

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_MAX_DIAGNOSTIC_CHARACTER_COUNT = 4000


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

        try:
            return subprocess.run(
                command_argument_list,
                capture_output=True,
                check=check,
                text=True,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                f"Unable to execute host-bootstrap command {command_argument_list[0]}: {error}"
            ) from error
        except subprocess.CalledProcessError as error:
            diagnostic = _diagnostic_get(error.stderr or error.stdout)
            suffix = f": {diagnostic}" if diagnostic else ""
            raise DevelopmentEnvironmentError(
                f"Host-bootstrap command {command_argument_list[0]} exited "
                f"{error.returncode}{suffix}"
            ) from error


def _diagnostic_get(value: str | None) -> str:
    """Return bounded printable command output without command arguments or env."""

    if not value:
        return ""
    diagnostic = "".join(
        character if character in {"\n", "\r", "\t"} or character.isprintable() else "?"
        for character in value.strip()
    )
    if len(diagnostic) <= _MAX_DIAGNOSTIC_CHARACTER_COUNT:
        return diagnostic
    return diagnostic[:_MAX_DIAGNOSTIC_CHARACTER_COUNT] + "... [truncated]"
