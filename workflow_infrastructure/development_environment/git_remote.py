"""Bound retry and timeout policy for non-secret Git remote operations."""

from __future__ import annotations

from collections.abc import Sequence
import subprocess
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentCommandTimeoutError,
)

# A live GitHub SSH metadata lookup measured about two seconds in the accepted
# development path. Each attempt therefore receives a 15x margin, while three
# fresh processes cap an unresponsive remote boundary at 90 seconds.
GIT_REMOTE_COMMAND_ATTEMPT_COUNT = 3
GIT_REMOTE_COMMAND_TIMEOUT_SECONDS = 30


def git_remote_command_run(
    runner: GitRemoteCommandRunnerProtocol,
    command_list: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one Git remote command through fresh bounded timeout attempts.

    Args:
        runner: Deadline-aware process boundary.
        command_list: Ordered Git command values.
        check: Whether a nonzero command exit raises an error.

    Returns:
        The first completed remote command result.
    """

    for attempt_number in range(1, GIT_REMOTE_COMMAND_ATTEMPT_COUNT + 1):
        try:
            return runner.run(
                command_list,
                check=check,
                timeout_seconds=GIT_REMOTE_COMMAND_TIMEOUT_SECONDS,
            )
        except DevelopmentCommandTimeoutError:
            if attempt_number == GIT_REMOTE_COMMAND_ATTEMPT_COUNT:
                raise
    raise AssertionError("Git remote attempt loop must return or raise")


class GitRemoteCommandRunnerProtocol(Protocol):
    """Declare the deadline-aware process boundary required by Git remotes."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one complete Git argument vector.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.
            timeout_seconds: Optional complete-process deadline in seconds.

        Returns:
            Completed text-mode subprocess result.
        """
