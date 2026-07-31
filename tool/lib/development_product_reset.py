"""Own the explicit destructive Product reset for one development environment."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from tool.lib.development_environment_error import DevelopmentEnvironmentError
from tool.lib.development_host import PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT


class DevelopmentProductResetManager:
    """Sequence Product-state destruction and retained release removal."""

    def __init__(
        self,
        *,
        identity: EnvironmentIdentityProtocol,
        lifecycle: LifecycleProtocol,
        product_recovery: ProductRecoveryProtocol,
        product_release: ProductReleaseProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Bind one reset workflow to explicit host and Product owners.

        Args:
            identity: Exact environment and host-control identity.
            lifecycle: Host readiness and infrastructure publication boundary.
            product_recovery: Retained Product state boundary.
            product_release: Current Product tool boundary.
            transport: Session Manager command boundary.
        """

        self._identity = identity
        self._lifecycle = lifecycle
        self._product_recovery = product_recovery
        self._product_release = product_release
        self._transport = transport

    def reset(
        self,
        user_email: str,
        expected_role_key_list: Sequence[str],
    ) -> None:
        """Reset disposable Product state and remove its retained release graph.

        Args:
            user_email: Preserved ZITADEL user whose state must survive.
            expected_role_key_list: Exact Product roles that must survive.
        """

        self._lifecycle.start(should_publish_infrastructure_source=True)
        if self._product_recovery.status_get() != "absent":
            product_reset_command_list = self._product_release.current_product_tool_command_list_get(
                "product-state-reset",
                "--user-email",
                user_email,
            )
            for expected_role_key in expected_role_key_list:
                product_reset_command_list.extend(["--expected-role-key", expected_role_key])
            self._transport.ssm_shell_run(["sudo " + shlex.join(product_reset_command_list)])
        self._transport.ssm_shell_run(
            [
                "sudo "
                + shlex.join(
                    [
                        "env",
                        PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                        "python3.14",
                        "-B",
                        str(
                            self._identity.host_control_current_source_path
                            / "sources"
                            / "workflow-infrastructure"
                            / "tool"
                            / "development_environment_manage.py"
                        ),
                        "host-product-release-reset",
                        "--environment-name",
                        self._identity.environment_name,
                    ]
                )
            ]
        )
        if self._product_recovery.status_get() != "absent":
            raise DevelopmentEnvironmentError("Destructive Product reset did not remove retained release state")
        print("OK: destructive Product reset completed; deploy the exact current release")


class EnvironmentIdentityProtocol(Protocol):
    """Host identities required by destructive Product reset."""

    environment_name: str
    host_control_current_source_path: Path


class LifecycleProtocol(Protocol):
    """Host lifecycle boundary required by destructive Product reset."""

    def start(
        self,
        *,
        should_publish_infrastructure_source: bool = False,
    ) -> None:
        """Start the host and optionally publish exact infrastructure source."""


class ProductRecoveryProtocol(Protocol):
    """Retained Product state boundary required by destructive reset."""

    def status_get(self) -> str:
        """Return the retained Product recovery state."""


class ProductReleaseProtocol(Protocol):
    """Product tool command boundary required by destructive reset."""

    def current_product_tool_command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one exact Product tool command."""


class SsmTransportProtocol(Protocol):
    """Remote command boundary required by destructive Product reset."""

    def ssm_shell_run(self, command_list: Sequence[str]) -> None:
        """Run one remote shell command."""
