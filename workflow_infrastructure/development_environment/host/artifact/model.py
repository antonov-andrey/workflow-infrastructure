"""Immutable host-artifact identities and canonical manifest contract."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

DOCKER_SIGNING_KEY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
PYTHON_SELECTOR = "3.14"
ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP = {
    "amd64": {
        "aws_cli": "x86_64",
        "docker": "amd64",
        "helm": "amd64",
        "k3s": "amd64",
        "python": "x86_64",
        "uv": "x86_64",
    },
    "arm64": {
        "aws_cli": "aarch64",
        "docker": "arm64",
        "helm": "arm64",
        "k3s": "arm64",
        "python": "aarch64",
        "uv": "aarch64",
    },
}
HOST_ARTIFACT_NAME_SET = frozenset(
    {
        "aws-cli",
        "aws-cli-signature",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-ce",
        "docker-ce-cli",
        "docker-inrelease",
        "docker-packages-index",
        "docker-signing-key",
        "helm",
        "helm-signature",
        "k3s-binary",
        "k3s-checksums",
        "python",
        "uv",
        "uv-python-metadata",
    }
)
HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET = frozenset(
    {
        "aws-cli",
        "aws-cli-signature",
        "helm",
        "helm-signature",
        "k3s-binary",
        "k3s-checksums",
        "python",
        "uv",
        "uv-python-metadata",
    }
)
_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "architecture",
        "artifact_by_name_map",
        "docker_signing_key_fingerprint",
        "python_build",
        "python_selector",
    }
)
_IDENTITY_FIELD_NAME_SET = frozenset(
    {
        "name",
        "selector",
        "sha256",
        "size",
        "url",
        "verification",
        "verification_identity",
        "version",
    }
)
_RESOLVED_IDENTITY_FIELD_NAME_SET = frozenset(
    {*_IDENTITY_FIELD_NAME_SET, "resolved_ref", "source_commit_sha"}
)


class HostArtifactResolutionError(RuntimeError):
    """Raised when a bootstrap artifact cannot become an immutable input."""


@dataclass(frozen=True)
class HostArtifactIdentity:
    """Describe one exact artifact and its moving-source provenance."""

    name: str
    selector: str
    version: str
    url: str
    sha256: str
    size: int
    verification: str
    verification_identity: str
    resolved_ref: str = ""
    source_commit_sha: str = ""

    def manifest_payload_get(self) -> dict[str, object]:
        """Return the canonical serializable identity."""

        payload: dict[str, object] = {
            "name": self.name,
            "selector": self.selector,
            "sha256": self.sha256,
            "size": self.size,
            "url": self.url,
            "verification": self.verification,
            "verification_identity": self.verification_identity,
            "version": self.version,
        }
        if self.resolved_ref:
            payload["resolved_ref"] = self.resolved_ref
        if self.source_commit_sha:
            payload["source_commit_sha"] = self.source_commit_sha
        return payload


@dataclass(frozen=True)
class HostArtifactResolution:
    """Hold one architecture-specific immutable bootstrap graph."""

    architecture: str
    artifact_by_name_map: Mapping[str, HostArtifactIdentity]
    docker_signing_key_fingerprint: str
    python_build: str

    def manifest_payload_get(self) -> dict[str, object]:
        """Return canonical launch and release provenance."""

        return host_artifact_manifest_validate(
            {
                "architecture": self.architecture,
                "artifact_by_name_map": {
                    name: artifact.manifest_payload_get()
                    for name, artifact in sorted(self.artifact_by_name_map.items())
                },
                "docker_signing_key_fingerprint": self.docker_signing_key_fingerprint,
                "python_build": self.python_build,
                "python_selector": PYTHON_SELECTOR,
            }
        )

    def manifest_sha256_get(self) -> str:
        """Return the canonical manifest digest."""

        return hashlib.sha256(_canonical_bytes(self.manifest_payload_get())).hexdigest()

    def manifest_gzip_base64_get(self) -> str:
        """Return a deterministic compact stack-parameter copy."""

        return base64.b64encode(
            gzip.compress(_canonical_bytes(self.manifest_payload_get()), mtime=0)
        ).decode()

    def cloudformation_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return the bounded canonical stack provenance inputs."""

        if any(len(item.url) > 384 for item in self.artifact_by_name_map.values()):
            raise HostArtifactResolutionError(
                "host artifact URL exceeds the bounded launch-input contract"
            )
        encoded_manifest = self.manifest_gzip_base64_get()
        if len(encoded_manifest) > 3072:
            raise HostArtifactResolutionError(
                "host artifact manifest exceeds the bounded launch-input contract"
            )
        return {
            "HostArtifactManifestSha256": self.manifest_sha256_get(),
            "HostArtifactManifestGzipBase64": encoded_manifest,
        }


def host_artifact_manifest_validate(payload: object) -> dict[str, object]:
    """Validate the one exact current manifest shape."""

    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELD_NAME_SET:
        raise HostArtifactResolutionError(
            "host artifact manifest does not have the exact current shape"
        )
    architecture = payload.get("architecture")
    artifact_by_name_map = payload.get("artifact_by_name_map")
    if (
        architecture not in ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP
        or not isinstance(artifact_by_name_map, dict)
        or set(artifact_by_name_map) != HOST_ARTIFACT_NAME_SET
    ):
        raise HostArtifactResolutionError("host artifact manifest payload is malformed")
    for artifact_name, artifact in artifact_by_name_map.items():
        expected_field_name_set = (
            _RESOLVED_IDENTITY_FIELD_NAME_SET
            if artifact_name in HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET
            else _IDENTITY_FIELD_NAME_SET
        )
        if not isinstance(artifact, dict) or set(artifact) != expected_field_name_set:
            raise HostArtifactResolutionError(
                f"host artifact manifest {artifact_name} does not have the exact current shape"
            )
        if (
            artifact.get("name") != artifact_name
            or not _nonempty_text(artifact.get("selector"))
            or not _nonempty_text(artifact.get("version"))
            or not _nonempty_text(artifact.get("url"))
            or not str(artifact["url"]).startswith("https://")
            or not isinstance(artifact.get("verification"), str)
            or re.fullmatch(r"[a-z0-9+_-]+", artifact["verification"]) is None
            or not _nonempty_text(artifact.get("verification_identity"))
            or not isinstance(artifact.get("size"), int)
            or artifact["size"] <= 0
            or not isinstance(artifact.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
        ):
            raise HostArtifactResolutionError(
                f"host artifact manifest {artifact_name} identity is invalid"
            )
        if artifact_name in HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET and (
            not _nonempty_text(artifact.get("resolved_ref"))
            or not isinstance(artifact.get("source_commit_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", artifact["source_commit_sha"]) is None
        ):
            raise HostArtifactResolutionError(
                f"host artifact manifest {artifact_name} resolved identity is invalid"
            )
    if payload.get("python_selector") != PYTHON_SELECTOR:
        raise HostArtifactResolutionError(
            "host artifact manifest has another Python selector"
        )
    if (
        not isinstance(payload.get("python_build"), str)
        or re.fullmatch(r"[0-9]{8}", payload["python_build"]) is None
    ):
        raise HostArtifactResolutionError(
            "host artifact manifest has no exact Python build"
        )
    if payload.get("docker_signing_key_fingerprint") != DOCKER_SIGNING_KEY_FINGERPRINT:
        raise HostArtifactResolutionError(
            "host artifact manifest has another Docker signing trust anchor"
        )
    return payload


def host_artifact_manifest_decode(
    *, encoded_manifest: str, expected_sha256: str
) -> dict[str, object]:
    """Decode and validate a deterministic gzip/base64 manifest."""

    try:
        payload = json.loads(
            gzip.decompress(base64.b64decode(encoded_manifest, validate=True))
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise HostArtifactResolutionError(
            "host artifact manifest parameter is malformed"
        ) from error
    return _manifest_digest_validate(payload, expected_sha256=expected_sha256)


def host_artifact_manifest_json_decode(
    *, manifest_json: str, expected_sha256: str
) -> dict[str, object]:
    """Decode and validate canonical JSON installed on one host."""

    try:
        payload = json.loads(manifest_json)
    except json.JSONDecodeError as error:
        raise HostArtifactResolutionError(
            "host artifact manifest JSON is malformed"
        ) from error
    return _manifest_digest_validate(payload, expected_sha256=expected_sha256)


def _manifest_digest_validate(
    payload: object, *, expected_sha256: str
) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise HostArtifactResolutionError("host artifact manifest digest is invalid")
    if hashlib.sha256(_canonical_bytes(payload)).hexdigest() != expected_sha256:
        raise HostArtifactResolutionError(
            "host artifact manifest differs from its digest"
        )
    return host_artifact_manifest_validate(payload)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)
