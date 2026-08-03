"""Run non-secret external commands for development infrastructure tooling."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from workflow_infrastructure.development_environment.error import (
    DevelopmentCommandTimeoutError,
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
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one complete argument vector within an optional hard deadline.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.
            input_text: Text supplied to standard input.
            should_capture: Whether stdout and stderr should be captured.
            timeout_seconds: Optional complete-process deadline in seconds.

        Returns:
            Completed text-mode subprocess result.
        """

        try:
            return subprocess.run(
                list(command_list),
                capture_output=should_capture,
                check=check,
                input=input_text,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise DevelopmentCommandTimeoutError(
                f"{command_list[0]} exceeded its {error.timeout:g}-second deadline"
            ) from error
        except OSError as error:
            raise DevelopmentEnvironmentError(f"Unable to execute {command_list[0]}: {error}") from error
        except subprocess.CalledProcessError as error:
            error_text = (error.stderr or error.stdout or f"exit {error.returncode}").strip()
            raise DevelopmentEnvironmentError(f"{command_list[0]} failed: {error_text}") from error
