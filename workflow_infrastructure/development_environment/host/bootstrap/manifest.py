"""Validate the exact host-bootstrap bundle manifest."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import re

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_ARTIFACT_FIELD_NAME_SET = frozenset({"path", "sha256", "size", "version"})
_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "architecture",
        "artifact_by_name_map",
        "host_artifact_manifest_sha256",
        "python_version",
        "schema_version",
    }
)


class HostBootstrapBundle:
    """Own validated paths and identities from one extracted bootstrap bundle."""

    def __init__(
        self, *, bundle_root_path: Path, expected_manifest_sha256: str
    ) -> None:
        """Load and validate one exact bundle manifest.

        Args:
            bundle_root_path: Extracted content-addressed bundle root.
            expected_manifest_sha256: SHA-256 supplied by the immutable compute input.
        """

        self._bundle_root_path = bundle_root_path.resolve(strict=True)
        manifest_path = self._bundle_root_path / "bootstrap-manifest.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            payload = json.loads(manifest_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Host bootstrap bundle manifest is unavailable"
            ) from error
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
            raise DevelopmentEnvironmentError(
                "Host bootstrap bundle manifest digest differs"
            )
        if not isinstance(payload, Mapping) or set(payload) != _MANIFEST_FIELD_NAME_SET:
            raise DevelopmentEnvironmentError(
                "Host bootstrap bundle manifest has an unsupported shape"
            )
        architecture = payload.get("architecture")
        artifact_by_name_map = payload.get("artifact_by_name_map")
        host_artifact_manifest_sha256 = payload.get("host_artifact_manifest_sha256")
        python_version = payload.get("python_version")
        if (
            payload.get("schema_version") != 1
            or architecture not in {"amd64", "arm64"}
            or not isinstance(artifact_by_name_map, Mapping)
            or not _sha256_is_valid(host_artifact_manifest_sha256)
            or not isinstance(python_version, str)
            or re.fullmatch(r"3\.14\.[0-9]+", python_version) is None
        ):
            raise DevelopmentEnvironmentError(
                "Host bootstrap bundle manifest is invalid"
            )
        validated_artifact_by_name_map: dict[str, dict[str, object]] = {}
        for name, artifact in artifact_by_name_map.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name) is None
                or not isinstance(artifact, Mapping)
                or set(artifact) != _ARTIFACT_FIELD_NAME_SET
            ):
                raise DevelopmentEnvironmentError(
                    "Host bootstrap artifact identity is invalid"
                )
            relative_path_text = artifact.get("path")
            relative_path = (
                PurePosixPath(relative_path_text)
                if isinstance(relative_path_text, str)
                else PurePosixPath(".")
            )
            size = artifact.get("size")
            if (
                not isinstance(relative_path_text, str)
                or relative_path.is_absolute()
                or not relative_path.parts
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or not _sha256_is_valid(artifact.get("sha256"))
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(artifact.get("version"), str)
                or not artifact.get("version")
            ):
                raise DevelopmentEnvironmentError(
                    "Host bootstrap artifact identity is invalid"
                )
            artifact_path = self._bundle_root_path.joinpath(*relative_path.parts)
            if (
                artifact_path.is_symlink()
                or not artifact_path.is_file()
                or artifact_path.stat().st_nlink != 1
            ):
                raise DevelopmentEnvironmentError(
                    f"Host bootstrap artifact {name} is unavailable"
                )
            artifact_bytes = artifact_path.read_bytes()
            if len(artifact_bytes) != size or hashlib.sha256(
                artifact_bytes
            ).hexdigest() != artifact.get("sha256"):
                raise DevelopmentEnvironmentError(
                    f"Host bootstrap artifact {name} differs from its identity"
                )
            validated_artifact_by_name_map[name] = dict(artifact)
        self._architecture = architecture
        self._host_artifact_manifest_sha256 = host_artifact_manifest_sha256
        self._python_version = python_version
        self._artifact_by_name_map = validated_artifact_by_name_map

    def architecture_validate(self, expected_architecture: str) -> None:
        """Require the bundle to target one exact host architecture."""

        if self._architecture != expected_architecture:
            raise DevelopmentEnvironmentError(
                "Bootstrap bundle architecture differs from the instance"
            )

    def python_version_validate(self, actual_version: str) -> None:
        """Require an extracted runtime to match the bundle identity."""

        if actual_version != self._python_version:
            raise DevelopmentEnvironmentError(
                "Installed Python version differs from the bundle"
            )

    def artifact_path_get(self, name: str) -> Path:
        """Return one verified artifact path.

        Args:
            name: Canonical host-artifact name.

        Returns:
            Exact ordinary file inside the bundle.
        """

        artifact = self._artifact_by_name_map.get(name)
        if artifact is None:
            raise DevelopmentEnvironmentError(
                f"Host bootstrap artifact {name} is not declared"
            )
        return self._bundle_root_path / str(artifact["path"])

    def artifact_version_get(self, name: str) -> str:
        """Return one verified artifact version.

        Args:
            name: Canonical host-artifact name.

        Returns:
            Exact version string.
        """

        artifact = self._artifact_by_name_map.get(name)
        if artifact is None:
            raise DevelopmentEnvironmentError(
                f"Host bootstrap artifact {name} is not declared"
            )
        return str(artifact["version"])

    def host_artifact_manifest_path_get(self) -> Path:
        """Return the verified canonical host-artifact manifest path.

        Returns:
            Exact manifest file inside the bundle.
        """

        path = self._bundle_root_path / "host-artifact-manifest.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise DevelopmentEnvironmentError(
                "Host artifact manifest is unavailable in bootstrap bundle"
            )
        if (
            hashlib.sha256(path.read_bytes()).hexdigest()
            != self._host_artifact_manifest_sha256
        ):
            raise DevelopmentEnvironmentError(
                "Host artifact manifest differs from its bundle identity"
            )
        return path

    def host_artifact_manifest_install(self, destination_root_path: Path) -> None:
        """Atomically install the manifest and digest with durability proof."""

        destination_root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest_bytes = self.host_artifact_manifest_path_get().read_bytes()
        _atomic_file_write(
            path=destination_root_path / "host-artifact-manifest.json",
            data=manifest_bytes,
        )
        _atomic_file_write(
            path=destination_root_path / "host-artifact-manifest.sha256",
            data=(self._host_artifact_manifest_sha256 + "\n").encode(),
        )
        _directory_fsync(destination_root_path)


def _sha256_is_valid(value: object) -> bool:
    """Return whether one value is a lowercase SHA-256 digest.

    Args:
        value: Candidate digest value.

    Returns:
        True only for one canonical digest.
    """

    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _atomic_file_write(*, path: Path, data: bytes) -> None:
    temporary_path = path.with_name(f".{path.name}.new")
    with temporary_path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def _directory_fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
