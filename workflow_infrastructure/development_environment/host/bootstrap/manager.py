"""Sequence one idempotent development-host bootstrap."""

from __future__ import annotations

from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.artifacts import (
    HostArtifactBootstrap,
)
from workflow_infrastructure.development_environment.host.bootstrap.command import (
    HostBootstrapCommandRunner,
)
from workflow_infrastructure.development_environment.host.bootstrap.k3s import (
    HostK3sBootstrap,
)
from workflow_infrastructure.development_environment.host.bootstrap.manifest import (
    HostBootstrapBundle,
)
from workflow_infrastructure.development_environment.host.bootstrap.network import (
    HostNetworkBootstrap,
)
from workflow_infrastructure.development_environment.host.bootstrap.services import (
    HostBootstrapServiceManager,
)
from workflow_infrastructure.development_environment.host.bootstrap.storage import (
    HostStorageBootstrap,
)


class DevelopmentHostBootstrapManager:
    """Own the ordered artifact, storage, k3s, and network bootstrap lifecycle."""

    def __init__(
        self,
        *,
        architecture: str,
        bundle_root_path: Path,
        bundle_manifest_sha256: str,
        environment_name: str,
        python_runtime_path: Path,
        retained_root_path: Path,
        retained_volume_id: str,
        runner: HostBootstrapCommandRunner,
    ) -> None:
        """Validate and wire all bootstrap collaborators.

        Args:
            architecture: Exact compute architecture.
            bundle_root_path: Extracted bootstrap bundle root.
            bundle_manifest_sha256: Exact bootstrap manifest identity.
            environment_name: Stable environment machine identity.
            python_runtime_path: Extracted Python runtime root.
            retained_root_path: Environment-exclusive retained mount root.
            retained_volume_id: Exact retained EBS volume identity.
            runner: Checked process boundary.
        """

        if environment_name != "primary" and not environment_name.startswith("w"):
            raise DevelopmentEnvironmentError(
                "Bootstrap environment identity is invalid"
            )
        self._architecture = architecture
        self._bundle = HostBootstrapBundle(
            bundle_root_path=bundle_root_path,
            expected_manifest_sha256=bundle_manifest_sha256,
        )
        self._bundle.architecture_validate(architecture)
        service = HostBootstrapServiceManager(runner=runner)
        self._artifact = HostArtifactBootstrap(
            bundle=self._bundle,
            python_runtime_path=python_runtime_path,
            runner=runner,
        )
        self._storage = HostStorageBootstrap(
            retained_root_path=retained_root_path,
            retained_volume_id=retained_volume_id,
            runner=runner,
        )
        self._k3s = HostK3sBootstrap(
            bundle=self._bundle, runner=runner, service=service
        )
        self._network = HostNetworkBootstrap(service=service)

    def run(self) -> None:
        """Execute every bootstrap stage and publish exact host provenance."""

        self._artifact.install()
        self._storage.mount()
        self._k3s.install()
        self._network.install()
        self._bundle.host_artifact_manifest_install(
            Path("/etc/workflow-infrastructure")
        )
        print(f"OK: development host bootstrap completed for {self._architecture}")
