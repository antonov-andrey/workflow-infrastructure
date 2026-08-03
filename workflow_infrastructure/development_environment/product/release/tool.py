"""Resolve the exact current retained Product management command."""

from __future__ import annotations

from pathlib import Path

from workflow_infrastructure.development_environment.product.release.model import (
    RetainedProductReleaseHostIdentity,
)


class DevelopmentCurrentProductTool:
    """Own current-release Product tool path and environment-bound invocation."""

    def __init__(
        self,
        *,
        identity: RetainedProductReleaseHostIdentity,
        python_bytecode_environment_assignment: str,
    ) -> None:
        """Initialize the development current product tool dependencies.

        Args:
            identity: Identity.
            python_bytecode_environment_assignment: Python bytecode environment assignment.
        """

        self._identity = identity
        self._python_bytecode_environment_assignment = python_bytecode_environment_assignment

    def path_get(self) -> Path:
        """Return the current exact Product management-tool path.

        Returns:
            The current exact Product management-tool path.
        """

        return (
            self._identity.host_current_source_path
            / "sources"
            / "workflow-control-center"
            / "tool"
            / "development_kubernetes_manage.py"
        )

    def command_list_get(self, command: str, *argument_list: str) -> list[str]:
        """Return one environment-bound command for the current Product tool.

        Args:
            command: Command.
            *argument_list: Exact command arguments.

        Returns:
            One environment-bound command for the current Product tool.
        """

        return [
            "env",
            self._python_bytecode_environment_assignment,
            "python3.14",
            "-B",
            str(self.path_get()),
            command,
            "--environment-name",
            self._identity.environment_name,
            "--public-http-port",
            str(self._identity.local_http_port),
            *argument_list,
        ]
