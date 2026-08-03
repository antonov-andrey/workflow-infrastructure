"""Tests for bounded development Git remote process execution."""

from __future__ import annotations

from collections.abc import Sequence
import subprocess
import sys

import pytest

from workflow_infrastructure.development_environment.command import CommandRunner
from workflow_infrastructure.development_environment.error import (
    DevelopmentCommandTimeoutError,
)
from workflow_infrastructure.development_environment.git_remote import (
    GIT_REMOTE_COMMAND_ATTEMPT_COUNT,
    GIT_REMOTE_COMMAND_TIMEOUT_SECONDS,
    git_remote_command_run,
)


def test_command_runner_enforces_complete_process_deadline() -> None:
    """A stuck external process must fail through the typed timeout boundary."""

    with pytest.raises(DevelopmentCommandTimeoutError, match="0.01-second deadline"):
        CommandRunner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=0.01,
        )


def test_git_remote_timeout_exhaustion_remains_typed_and_bounded() -> None:
    """The retry owner must surface the final timeout after its fixed attempt count."""

    runner = _TimeoutThenSuccessRunner(timeout_count=GIT_REMOTE_COMMAND_ATTEMPT_COUNT)

    with pytest.raises(DevelopmentCommandTimeoutError, match="synthetic Git timeout"):
        git_remote_command_run(runner, ["git", "fetch", "origin"])

    assert (
        runner.timeout_seconds_list
        == [
            GIT_REMOTE_COMMAND_TIMEOUT_SECONDS,
        ]
        * GIT_REMOTE_COMMAND_ATTEMPT_COUNT
    )


def test_git_remote_timeout_retries_in_fresh_bounded_attempts() -> None:
    """Transient Git timeouts retry without weakening the per-attempt deadline."""

    runner = _TimeoutThenSuccessRunner(timeout_count=2)

    result = git_remote_command_run(runner, ["git", "ls-remote", "origin"])

    assert result.stdout == "resolved\n"
    assert runner.timeout_seconds_list == [GIT_REMOTE_COMMAND_TIMEOUT_SECONDS] * 3


class _TimeoutThenSuccessRunner:
    """Expose deterministic timeout attempts followed by one successful result."""

    def __init__(self, *, timeout_count: int) -> None:
        """Initialize the scripted timeout count.

        Args:
            timeout_count: Number of initial timeout failures.
        """

        self.timeout_count = timeout_count
        self.timeout_seconds_list: list[float | None] = []

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Return success after the configured number of timeout failures.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.
            timeout_seconds: Optional complete-process deadline in seconds.

        Returns:
            Successful synthetic Git result after the timeout attempts.
        """

        del check
        self.timeout_seconds_list.append(timeout_seconds)
        if len(self.timeout_seconds_list) <= self.timeout_count:
            raise DevelopmentCommandTimeoutError("synthetic Git timeout")
        return subprocess.CompletedProcess(command_list, 0, "resolved\n", "")
