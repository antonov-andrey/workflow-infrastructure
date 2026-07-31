"""Verify release-local AWS Public ECR authentication."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.public_ecr_auth import (
    DevelopmentPublicEcrAuthManager,
)


class IdentityFake:
    """Provide one deterministic disposable host-state root."""

    host_state_root_path = Path("/var/lib/workflow-infrastructure")


class TransportFake:
    """Record exact remote commands without executing them."""

    def __init__(self) -> None:
        """Initialize the command ledger."""

        self.command_list_list: list[list[str]] = []

    def ssh_run(
        self,
        command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Record one remote command."""

        del ssh_control_path, should_capture
        self.command_list_list.append(command_list)
        return subprocess.CompletedProcess(command_list, 0, "", "")


def test_public_ecr_auth_is_release_local_and_removed_after_failure() -> None:
    """A Product failure cannot retain the short-lived Docker credential."""

    transport = TransportFake()
    manager = DevelopmentPublicEcrAuthManager(
        aws_region="us-east-1",
        identity=IdentityFake(),
        transport=transport,
    )
    release_name = "20260731190000123456"
    expected_docker_config_path = IdentityFake.host_state_root_path / "docker-auth" / release_name

    with pytest.raises(RuntimeError, match="Product deploy failed"):
        with manager.session(
            release_name=release_name,
            ssh_control_path=Path("/tmp/ssh-control"),
        ) as docker_config_path:
            assert docker_config_path == expected_docker_config_path
            raise RuntimeError("Product deploy failed")

    login_command_list, cleanup_command_list = transport.command_list_list
    assert login_command_list[:3] == ["sudo", "sh", "-ceu"]
    assert "aws ecr-public get-login-password" in login_command_list[3]
    assert "--password-stdin" in login_command_list[3]
    assert login_command_list[-2:] == [
        str(expected_docker_config_path),
        "us-east-1",
    ]
    assert cleanup_command_list == [
        "sudo",
        "rm",
        "-rf",
        "--",
        str(expected_docker_config_path),
    ]


def test_public_ecr_auth_rejects_non_release_path_before_remote_mutation() -> None:
    """Untrusted text cannot become a root-owned cleanup path."""

    transport = TransportFake()
    manager = DevelopmentPublicEcrAuthManager(
        aws_region="us-east-1",
        identity=IdentityFake(),
        transport=transport,
    )

    with pytest.raises(
        DevelopmentEnvironmentError,
        match="requires one exact Product release",
    ):
        with manager.session(
            release_name="../../root",
            ssh_control_path=Path("/tmp/ssh-control"),
        ):
            pytest.fail("Invalid release must not open an authentication session")

    assert transport.command_list_list == []
