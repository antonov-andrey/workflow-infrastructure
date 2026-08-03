"""Own retained Product recovery state transitions on a development host."""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.manager import (
    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
)

HOST_STATUS_COMMAND_TIMEOUT_SECONDS = 120


class EnvironmentIdentityProtocol(Protocol):
    """Environment identity used to address the current control source."""

    environment_name: str
    host_control_entrypoint_path: Path


class ProductToolProtocol(Protocol):
    """Exact retained Product tool surface."""

    def command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one exact Product tool command.

        Args:
            command: Command.
            *argument_list: Exact command arguments.

        Returns:
            One exact Product tool command.
        """


class SsmTransportProtocol(Protocol):
    """Remote command surface consumed by recovery."""

    def ssm_shell_result_get(
        self,
        command_list: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        """Run remote shell commands and return structured SSM output.

        Args:
            command_list: Ordered command values.
            timeout_seconds: Timeout in seconds.

        Returns:
            Successful SSM command invocation payload.
        """

    def ssm_shell_run(self, command_list: Sequence[str]) -> None:
        """Run remote shell commands.

        Args:
            command_list: Ordered command values.
        """


class DevelopmentProductRecoveryManager:
    """Own durable Product recovery savepoint transitions and acceptance."""

    def __init__(
        self,
        *,
        identity: EnvironmentIdentityProtocol,
        product_tool: ProductToolProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Initialize Product recovery from exact host boundaries.

        Args:
            identity: Identity.
            product_tool: Product tool.
            transport: Transport.
        """

        self._identity = identity
        self._product_tool = product_tool
        self._transport = transport

    def status_get(self) -> str:
        """Return exact retained Product recovery state from the active host.

        Returns:
            The exact retained Product recovery state from the active host.
        """

        result_payload = self._transport.ssm_shell_result_get(
            ["sudo " + shlex.join(self._infrastructure_tool_command_list_get("host-product-recovery-status"))],
            timeout_seconds=HOST_STATUS_COMMAND_TIMEOUT_SECONDS,
        )
        output_text = result_payload.get("StandardOutputContent")
        if not isinstance(output_text, str):
            raise DevelopmentEnvironmentError("Product recovery status output is malformed")
        try:
            payload = json.loads(output_text)
            status = payload["status"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError("Product recovery status payload is malformed") from error
        if status not in {"absent", "pending", "ready"}:
            raise DevelopmentEnvironmentError("Product recovery status is unsupported")
        return status

    def is_pending(self) -> bool:
        """Return whether interrupted retained Product recovery must resume.

        Returns:
            Whether interrupted retained Product recovery must resume.
        """

        return self.status_get() == "pending"

    def begin(self) -> None:
        """Persist retained Product recovery savepoint on the active host."""

        self._infrastructure_command_run("host-product-recovery-begin")

    def complete(self) -> None:
        """Clear retained Product recovery savepoint after acceptance."""

        self._infrastructure_command_run("host-product-recovery-complete")

    def finish(self) -> None:
        """Restore, apply, accept, and complete one pending Product recovery."""

        self.link_restore()
        self.apply_run()
        self.acceptance_run()
        self.complete()

    def link_restore(self) -> None:
        """Restore trusted root-volume access to the retained Product release."""

        self._infrastructure_command_run("host-product-release-restore")

    def apply_run(self) -> None:
        """Reapply exact retained Product release and reinstall its host service."""

        self._transport.ssm_shell_run(["sudo " + shlex.join(self._product_tool.command_list_get("recover"))])
        self._transport.ssm_shell_run(["sudo " + shlex.join(self._product_tool.command_list_get("host-install"))])

    def acceptance_run(self) -> None:
        """Run exact Product recovery acceptance without changing savepoint state."""

        self._transport.ssm_shell_run(
            ["sudo " + shlex.join(self._product_tool.command_list_get("recovery-acceptance"))]
        )

    def _infrastructure_command_run(self, command: str) -> None:
        """Run one environment-bound infrastructure command as root.

        Args:
            command: Command.
        """

        self._transport.ssm_shell_run(["sudo " + shlex.join(self._infrastructure_tool_command_list_get(command))])

    def _infrastructure_tool_command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one environment-bound exact infrastructure-control command.

        Args:
            command: Command.
            *argument_list: Exact command arguments.

        Returns:
            One environment-bound exact infrastructure-control command.
        """

        return [
            "env",
            PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
            "python3.14",
            "-B",
            str(self._identity.host_control_entrypoint_path),
            command,
            "--environment-name",
            self._identity.environment_name,
            *argument_list,
        ]
