"""Sequence immutable development-host artifact resolution and publication."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.artifact import (
    HostArtifactResolution,
    HostArtifactResolutionError,
    host_artifact_manifest_decode,
)


class EnvironmentIdentityProtocol(Protocol):
    """Environment identity consumed by host-artifact resolution."""

    compute_stack_name: str
    environment_name: str


class StackManagerProtocol(Protocol):
    """CloudFormation state consumed by host-artifact resolution."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack parameters."""


class ArtifactResolverProtocol(Protocol):
    """Immutable host-artifact graph resolution boundary."""

    def resolve(self, architecture: str) -> HostArtifactResolution:
        """Resolve one exact host-artifact graph."""


class BootstrapObjectPublisherProtocol(Protocol):
    """Content-addressed bootstrap publication boundary."""

    def publish(
        self,
        *,
        bucket_name: str,
        resolution: HostArtifactResolution,
    ) -> dict[str, str]:
        """Publish one resolved graph and return compute parameters."""


class DevelopmentHostArtifactManager:
    """Own immutable architecture-specific host-artifact provenance."""

    def __init__(
        self,
        *,
        bootstrap_object_publisher: BootstrapObjectPublisherProtocol,
        identity: EnvironmentIdentityProtocol,
        project_root_path: Path,
        resolver: ArtifactResolverProtocol,
        stack: StackManagerProtocol,
    ) -> None:
        """Initialize host-artifact ownership from explicit boundaries.

        Args:
            bootstrap_object_publisher: Content-addressed publication owner.
            identity: Stable environment identity.
            project_root_path: Exact infrastructure checkout.
            resolver: Fully wired immutable artifact resolver.
            stack: CloudFormation state boundary.
        """

        self._bootstrap_object_publisher = bootstrap_object_publisher
        self._identity = identity
        self._project_root_path = project_root_path
        self._resolver = resolver
        self._stack = stack

    def cloudformation_parameter_by_name_map_get(
        self,
        *,
        bucket_name: str,
        compute_stack_exists: bool,
    ) -> dict[str, str]:
        """Resolve, bundle, publish, and return immutable compute inputs.

        Args:
            bucket_name: Environment-owned private artifact bucket.
            compute_stack_exists: Whether one current compute stack supplies architecture.

        Returns:
            Exact compute-template parameter map.
        """

        resolution = self._resolution_get(compute_stack_exists=compute_stack_exists)
        return self._bootstrap_object_publisher.publish(
            bucket_name=bucket_name,
            resolution=resolution,
        )

    def _resolution_get(
        self,
        *,
        compute_stack_exists: bool,
    ) -> HostArtifactResolution:
        """Resolve and persist one exact host bootstrap graph before compute apply."""

        architecture = "arm64"
        if compute_stack_exists:
            architecture = self._stack.parameter_by_name_map_get(self._identity.compute_stack_name).get(
                "ComputeArchitecture", architecture
            )
        try:
            resolution = self._resolver.resolve(architecture)
        except HostArtifactResolutionError as error:
            raise DevelopmentEnvironmentError(f"Host artifact resolution failed: {error}") from error
        manifest_payload = {
            **resolution.manifest_payload_get(),
            "manifest_sha256": resolution.manifest_sha256_get(),
        }
        manifest_path = (
            self._project_root_path / ".local" / f"host-artifact-resolution-{self._identity.environment_name}.json"
        )
        manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)
        return resolution

    def manifest_payload_get(self) -> dict[str, object]:
        """Load exact host bootstrap provenance retained by the compute stack."""

        parameter_by_name_map = self._stack.parameter_by_name_map_get(self._identity.compute_stack_name)
        encoded_manifest = parameter_by_name_map.get(
            "HostArtifactManifestGzipBase64",
            "",
        )
        expected_sha256 = parameter_by_name_map.get(
            "HostArtifactManifestSha256",
            "",
        )
        try:
            payload = host_artifact_manifest_decode(
                encoded_manifest=encoded_manifest,
                expected_sha256=expected_sha256,
            )
        except HostArtifactResolutionError as error:
            raise DevelopmentEnvironmentError(f"Compute stack host artifact provenance is invalid: {error}") from error
        if payload.get("architecture") != parameter_by_name_map.get("ComputeArchitecture"):
            raise DevelopmentEnvironmentError("Compute stack host artifact architecture is inconsistent")
        return payload
