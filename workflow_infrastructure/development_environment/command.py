"""Run non-secret external commands for development infrastructure tooling."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class CommandRunner:
    """Run external commands through one explicit process boundary."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one complete argument vector and return its completed process."""

        try:
            return subprocess.run(
                list(command_list),
                capture_output=should_capture,
                check=check,
                input=input_text,
                text=True,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(f"Unable to execute {command_list[0]}: {error}") from error
        except subprocess.CalledProcessError as error:
            error_text = (error.stderr or error.stdout or f"exit {error.returncode}").strip()
            raise DevelopmentEnvironmentError(f"{command_list[0]} failed: {error_text}") from error
