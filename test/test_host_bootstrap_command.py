"""Verify safe host-bootstrap process diagnostics."""

from __future__ import annotations

import subprocess

import pytest

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.command import (
    HostBootstrapCommandRunner,
)


def test_checked_command_reports_bounded_output_without_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bootstrap failure is actionable without echoing its complete argv.

    Args:
        monkeypatch: Pytest mutation fixture.
    """

    diagnostic = "apt failure\x00" + "x" * 5000

    def run_fake(*args: object, **kwargs: object) -> object:
        """Record one bootstrap command and return its scripted result.

        Args:
            *args: Additional positional arguments.
            **kwargs: Provider keyword arguments.

        Returns:
            Scripted bootstrap command result.
        """

        del args, kwargs
        raise subprocess.CalledProcessError(
            100,
            ["apt-get", "secret-shaped-argument"],
            stderr=diagnostic,
        )

    monkeypatch.setattr(subprocess, "run", run_fake)

    with pytest.raises(DevelopmentEnvironmentError) as error_info:
        HostBootstrapCommandRunner().run(["apt-get", "secret-shaped-argument"])

    message = str(error_info.value)
    assert message.startswith("Host-bootstrap command apt-get exited 100: apt failure?")
    assert message.endswith("... [truncated]")
    assert "secret-shaped-argument" not in message
    assert len(message) < 4100
