"""Wire cohesive providers into one immutable host-artifact graph."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.host.artifact.download import (
    HostArtifactDownloader,
)
from workflow_infrastructure.development_environment.host.artifact.git_ref import (
    GitRefResolver,
)
from workflow_infrastructure.development_environment.host.artifact.model import (
    ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP,
    DOCKER_SIGNING_KEY_FINGERPRINT,
    HostArtifactIdentity,
    HostArtifactResolution,
    HostArtifactResolutionError,
)
from workflow_infrastructure.development_environment.host.artifact.provider.aws_cli import (
    AwsCliArtifactProvider,
)
from workflow_infrastructure.development_environment.host.artifact.provider.docker import (
    DockerArtifactProvider,
)
from workflow_infrastructure.development_environment.host.artifact.provider.helm import (
    HelmArtifactProvider,
)
from workflow_infrastructure.development_environment.host.artifact.provider.k3s import (
    K3sArtifactProvider,
)
from workflow_infrastructure.development_environment.host.artifact.provider.python import (
    PythonArtifactProvider,
)
from workflow_infrastructure.development_environment.host.artifact.verification import (
    HostArtifactVerifier,
)


class CommandResultProtocol(Protocol):
    returncode: int
    stdout: str


class CommandRunnerProtocol(Protocol):
    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
    ) -> CommandResultProtocol:
        """Run one local command."""


class HostArtifactResolver:
    """Sequence provider owners without implementing provider behavior."""

    def __init__(
        self,
        *,
        cache_root_path: Path,
        runner: CommandRunnerProtocol,
        trust_root_path: Path,
    ) -> None:
        """Wire shared byte, Git, and trust boundaries once."""

        downloader = HostArtifactDownloader(cache_root_path=cache_root_path)
        git_ref = GitRefResolver(runner=runner)
        verifier = HostArtifactVerifier(downloader=downloader, runner=runner)
        self._aws_cli = AwsCliArtifactProvider(
            downloader=downloader,
            git_ref=git_ref,
            trust_root_path=trust_root_path,
            verifier=verifier,
        )
        self._docker = DockerArtifactProvider(
            downloader=downloader,
            runner=runner,
            verifier=verifier,
        )
        self._helm = HelmArtifactProvider(
            downloader=downloader,
            git_ref=git_ref,
            trust_root_path=trust_root_path,
            verifier=verifier,
        )
        self._k3s = K3sArtifactProvider(
            downloader=downloader,
            git_ref=git_ref,
            trust_root_path=trust_root_path,
            verifier=verifier,
        )
        self._python = PythonArtifactProvider(
            downloader=downloader,
            git_ref=git_ref,
            verifier=verifier,
        )

    def resolve(self, architecture: str) -> HostArtifactResolution:
        """Resolve and verify all artifacts for one compute architecture."""

        owner_architecture = ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP.get(architecture)
        if owner_architecture is None:
            raise HostArtifactResolutionError(
                f"unsupported host artifact architecture: {architecture}"
            )
        artifact_by_name: dict[str, HostArtifactIdentity] = {}
        aws_cli, aws_cli_signature = self._aws_cli.resolve(
            owner_architecture["aws_cli"]
        )
        artifact_by_name.update(
            {"aws-cli": aws_cli, "aws-cli-signature": aws_cli_signature}
        )
        artifact_by_name.update(self._docker.resolve(owner_architecture["docker"]))
        uv, uv_metadata, python, python_build = self._python.resolve(
            python_architecture=owner_architecture["python"],
            uv_architecture=owner_architecture["uv"],
        )
        artifact_by_name.update(
            {"uv": uv, "uv-python-metadata": uv_metadata, "python": python}
        )
        artifact_by_name.update(self._k3s.resolve(owner_architecture["k3s"]))
        helm, helm_signature = self._helm.resolve(owner_architecture["helm"])
        artifact_by_name.update({"helm": helm, "helm-signature": helm_signature})
        resolution = HostArtifactResolution(
            architecture=architecture,
            artifact_by_name_map=artifact_by_name,
            docker_signing_key_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
            python_build=python_build,
        )
        resolution.cloudformation_parameter_by_name_map_get()
        return resolution
