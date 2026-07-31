"""Own the destructive Product-reset stage of one exact candidate deployment."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from tool.lib.development_host import PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT


class DevelopmentProductResetManager:
    """Reset disposable Product state from one exact candidate source."""

    def __init__(
        self,
        *,
        identity: EnvironmentIdentityProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Bind candidate reset to exact host identities and transport.

        Args:
            identity: Exact environment and host paths.
            transport: SSH-over-SSM command boundary.
        """

        self._identity = identity
        self._transport = transport

    def reset(
        self,
        *,
        expected_role_key_list: Sequence[str],
        release_name: str,
        release_root_path: Path,
        ssh_control_path: Path,
        target_platform: str,
        user_email: str,
    ) -> None:
        """Prove preserved state, remove disposable state, and prune old releases.

        Args:
            expected_role_key_list: Exact Product roles that must survive.
            release_name: Exact candidate release identity.
            release_root_path: Retained candidate release root.
            ssh_control_path: Reusable SSH control-socket path.
            target_platform: Exact candidate OCI target platform.
            user_email: Preserved ZITADEL user whose state must survive.
        """

        product_reset_command_list = [
            "sudo",
            "env",
            PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
            "python3.14",
            "-B",
            str(
                release_root_path / "sources" / "workflow-control-center" / "tool" / "development_kubernetes_manage.py"
            ),
            "product-state-reset",
            "--environment-name",
            self._identity.environment_name,
            "--release",
            release_name,
            "--source-root",
            str(release_root_path / "sources"),
            "--target-platform",
            target_platform,
            "--user-email",
            user_email,
        ]
        for expected_role_key in expected_role_key_list:
            product_reset_command_list.extend(["--expected-role-key", expected_role_key])
        self._transport.ssh_run(
            product_reset_command_list,
            ssh_control_path=ssh_control_path,
            should_capture=False,
        )
        self._transport.ssh_run(
            [
                "sudo",
                "env",
                PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                "python3.14",
                "-B",
                str(
                    release_root_path
                    / "sources"
                    / "workflow-infrastructure"
                    / "tool"
                    / "development_environment_manage.py"
                ),
                "host-product-release-reset",
                "--environment-name",
                self._identity.environment_name,
                "--release",
                release_name,
            ],
            ssh_control_path=ssh_control_path,
        )
        print(f"OK: disposable Product state reset before candidate release {release_name}")


class EnvironmentIdentityProtocol(Protocol):
    """Host identities required by candidate Product reset."""

    environment_name: str


class SsmTransportProtocol(Protocol):
    """SSH-over-SSM boundary required by candidate Product reset."""

    def ssh_run(
        self,
        command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> object:
        """Run one command through SSH-over-SSM."""
