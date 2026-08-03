"""Wire cohesive providers into one immutable host-artifact graph."""

from __future__ import annotations

from typing import Protocol

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


class HostArtifactResolver:
    """Sequence provider owners without implementing provider behavior."""

    def __init__(
        self,
        *,
        aws_cli: AwsCliArtifactProvider,
        docker: DockerArtifactProvider,
        helm: HelmArtifactProvider,
        k3s: K3sArtifactProvider,
        python: PythonArtifactProvider,
    ) -> None:
        """Bind already-wired provider owners to one sequencing facade."""

        self._aws_cli = aws_cli
        self._docker = docker
        self._helm = helm
        self._k3s = k3s
        self._python = python

    def resolve(self, architecture: str) -> HostArtifactResolution:
        """Resolve and verify all artifacts for one compute architecture."""

        owner_architecture = ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP.get(architecture)
        if owner_architecture is None:
            raise HostArtifactResolutionError(f"unsupported host artifact architecture: {architecture}")
        artifact_by_name: dict[str, HostArtifactIdentity] = {}
        aws_cli, aws_cli_signature = self._aws_cli.resolve(owner_architecture["aws_cli"])
        artifact_by_name.update({"aws-cli": aws_cli, "aws-cli-signature": aws_cli_signature})
        artifact_by_name.update(self._docker.resolve(owner_architecture["docker"]))
        uv, uv_metadata, python, python_build = self._python.resolve(
            python_architecture=owner_architecture["python"],
            uv_architecture=owner_architecture["uv"],
        )
        artifact_by_name.update({"uv": uv, "uv-python-metadata": uv_metadata, "python": python})
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
