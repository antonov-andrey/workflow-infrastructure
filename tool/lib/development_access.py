"""Own interactive Session Manager and SSH access to development compute."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol


class AccountVerifierProtocol(Protocol):
    """Operator-account boundary consumed by interactive access."""

    def local_operator_context_validate(self) -> None:
        """Validate exact local AWS operator context."""


class CommandResultProtocol(Protocol):
    """Command result surface consumed by interactive access."""

    returncode: int


class CommandRunnerProtocol(Protocol):
    """Foreground process boundary consumed by interactive access."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> CommandResultProtocol:
        """Run one local command."""


class ComputeManagerProtocol(Protocol):
    """Compute identity and readiness consumed by interactive access."""

    def instance_id_get(self) -> str:
        """Return the exact current instance ID."""

    def online_wait(self) -> None:
        """Wait for EC2 and Session Manager readiness."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment identity consumed by SSH access."""

    instance_name: str


class SsmTransportProtocol(Protocol):
    """SSH-over-SSM control-session boundary."""

    def ssh_control_session(self) -> AbstractContextManager[Path]:
        """Return a context manager yielding one SSH control-socket path."""


class DevelopmentAccessManager:
    """Own interactive HTTP tunnel, console, and SSH process lifecycles."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        aws_profile: str,
        aws_region: str,
        compute: ComputeManagerProtocol,
        identity: EnvironmentIdentityProtocol,
        port_forward_document_name: str,
        runner: CommandRunnerProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Initialize access from explicit identity and transport boundaries."""

        self._account = account
        self._aws_profile = aws_profile
        self._aws_region = aws_region
        self._compute = compute
        self._identity = identity
        self._port_forward_document_name = port_forward_document_name
        self._runner = runner
        self._transport = transport

    def connect(self) -> int:
        """Open the Product HTTP tunnel through Session Manager."""

        self._account.local_operator_context_validate()
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "start-session",
                "--profile",
                self._aws_profile,
                "--region",
                self._aws_region,
                "--target",
                self._compute.instance_id_get(),
                "--document-name",
                self._port_forward_document_name,
                "--parameters",
                json.dumps(
                    {"localPortNumber": ["8080"], "portNumber": ["8080"]},
                    separators=(",", ":"),
                ),
            ],
            check=False,
            should_capture=False,
        )
        return result.returncode

    def console(self) -> int:
        """Open an ordinary Session Manager console."""

        self._account.local_operator_context_validate()
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "start-session",
                "--profile",
                self._aws_profile,
                "--region",
                self._aws_region,
                "--target",
                self._compute.instance_id_get(),
            ],
            check=False,
            should_capture=False,
        )
        return result.returncode

    def ssh(self, ssh_argument_list: list[str]) -> int:
        """Run one SSH client command through ephemeral SSH-over-SSM."""

        self._account.local_operator_context_validate()
        self._compute.online_wait()
        with self._transport.ssh_control_session() as ssh_control_path:
            command_list = [
                "ssh",
                "-S",
                str(ssh_control_path),
                self._identity.instance_name,
                *ssh_argument_list,
            ]
            result = self._runner.run(
                command_list,
                check=False,
                should_capture=False,
            )
            return result.returncode
