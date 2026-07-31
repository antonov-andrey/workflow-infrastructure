"""Own ephemeral AWS Public ECR authentication for one Product deployment."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_RELEASE_NAME_PATTERN = re.compile(r"[0-9]{20}")
_PUBLIC_ECR_LOGIN_SCRIPT = """\
set -eu
docker_config_path=$1
aws_region=$2
rm -rf -- "${docker_config_path}"
install -d -m 0700 -- "${docker_config_path}"
aws ecr-public get-login-password --region "${aws_region}" |
  docker --config "${docker_config_path}" login --username AWS --password-stdin public.ecr.aws >/dev/null
"""


class EnvironmentIdentityProtocol(Protocol):
    """Environment path boundary consumed by registry authentication."""

    host_state_root_path: Path


class SsmTransportProtocol(Protocol):
    """SSH-over-SSM boundary consumed by registry authentication."""

    def ssh_run(
        self,
        command_list: Sequence[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> object:
        """Run one command through SSH-over-SSM."""


class DevelopmentPublicEcrAuthManager:
    """Create and remove one release-local Docker credential directory."""

    def __init__(
        self,
        *,
        aws_region: str,
        identity: EnvironmentIdentityProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Bind Public ECR authentication to one exact development host."""

        self._aws_region = aws_region
        self._identity = identity
        self._transport = transport

    @contextmanager
    def session(
        self,
        *,
        release_name: str,
        ssh_control_path: Path,
    ) -> Iterator[Path]:
        """Yield one authenticated Docker config and always remove it.

        Args:
            release_name: Exact immutable Product release identifier.
            ssh_control_path: Reusable SSH-over-SSM control socket.

        Yields:
            Absolute host path exported as ``DOCKER_CONFIG`` for Product deploy.
        """

        if _RELEASE_NAME_PATTERN.fullmatch(release_name) is None:
            raise DevelopmentEnvironmentError("Public ECR authentication requires one exact Product release")
        docker_config_path = self._identity.host_state_root_path / "docker-auth" / release_name
        try:
            self._transport.ssh_run(
                [
                    "sudo",
                    "sh",
                    "-ceu",
                    _PUBLIC_ECR_LOGIN_SCRIPT,
                    "workflow-public-ecr-login",
                    str(docker_config_path),
                    self._aws_region,
                ],
                ssh_control_path=ssh_control_path,
            )
            yield docker_config_path
        finally:
            self._transport.ssh_run(
                [
                    "sudo",
                    "rm",
                    "-rf",
                    "--",
                    str(docker_config_path),
                ],
                ssh_control_path=ssh_control_path,
            )
