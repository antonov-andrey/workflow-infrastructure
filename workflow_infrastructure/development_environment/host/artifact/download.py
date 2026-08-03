"""Bounded HTTPS download and private content-cache boundary."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from workflow_infrastructure.development_environment.host.artifact.model import (
    HostArtifactIdentity,
    HostArtifactResolutionError,
)

MAX_HOST_ARTIFACT_SIZE_BYTES = 1024 * 1024 * 1024
_URL_PATTERN = re.compile(r"https://[A-Za-z0-9._~:/?#\[\]@!&()*+,;=%-]+")


def _file_identity_get(path: Path) -> tuple[str, int]:
    """Return SHA-256 and byte length without loading a large file.

    Args:
        path: Exact filesystem path.

    Returns:
        SHA-256 digest and byte length without loading the file.
    """

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class HostArtifactDownloader:
    """Own safe artifact download, atomic cache publication, and identity."""

    def __init__(self, *, cache_root_path: Path) -> None:
        """Initialize the host artifact downloader dependencies.

        Args:
            cache_root_path: Exact filesystem path for cache root.
        """

        self._cache_root_path = cache_root_path
        self._cache_root_path.mkdir(mode=0o700, parents=True, exist_ok=True)

    def identity_resolve(
        self,
        *,
        name: str,
        selector: str,
        version: str,
        url: str,
        verification: str,
        verification_identity: str,
        allow_cache: bool = True,
        expected_sha256: str = "",
        resolved_ref: str = "",
        source_commit_sha: str = "",
    ) -> HostArtifactIdentity:
        """Download one safe HTTPS URL and return its immutable identity.

        Args:
            name: Canonical name.
            selector: Selector.
            version: Version.
            url: Url.
            verification: Verification.
            verification_identity: Exact verification identity.
            allow_cache: Allow cache.
            expected_sha256: Expected SHA-256.
            resolved_ref: Resolved ref.
            source_commit_sha: Source commit sha.

        Returns:
            Resulting host artifact identity.
        """

        parsed_url = urllib.parse.urlparse(url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or _URL_PATTERN.fullmatch(url) is None
        ):
            raise HostArtifactResolutionError(f"artifact {name} does not use one shell-safe absolute HTTPS URL")
        if expected_sha256 and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise HostArtifactResolutionError(f"artifact {name} has an invalid expected SHA-256")
        if (
            re.fullmatch(r"[a-z0-9+_-]+", verification) is None
            or not verification_identity
            or len(verification_identity) > 256
        ):
            raise HostArtifactResolutionError(f"artifact {name} has an invalid verification identity")
        sha256, size = self.download(
            allow_cache=allow_cache,
            artifact_path=self.cache_path_get(url),
            expected_sha256=expected_sha256,
            url=url,
        )
        return HostArtifactIdentity(
            name=name,
            selector=selector,
            version=version,
            url=url,
            sha256=sha256,
            size=size,
            verification=verification,
            verification_identity=verification_identity,
            resolved_ref=resolved_ref,
            source_commit_sha=source_commit_sha,
        )

    def cache_path_get(self, url: str) -> Path:
        """Return the private cache path for one URL.

        Args:
            url: Url.

        Returns:
            The private cache path for one URL.
        """

        return self._cache_root_path / hashlib.sha256(url.encode()).hexdigest()

    def download(
        self,
        *,
        artifact_path: Path,
        expected_sha256: str,
        url: str,
        allow_cache: bool = True,
    ) -> tuple[str, int]:
        """Publish exact response bytes atomically after bounded validation.

        Args:
            artifact_path: Exact filesystem path for artifact.
            expected_sha256: Expected SHA-256.
            url: Url.
            allow_cache: Allow cache.

        Returns:
            Values in deterministic immutable order.
        """

        if allow_cache and artifact_path.is_file():
            sha256, size = _file_identity_get(artifact_path)
            if not expected_sha256 or sha256 == expected_sha256:
                return sha256, size
            artifact_path.unlink()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "workflow-infrastructure-host-artifact-resolver/1"},
        )
        temporary_path: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                final_url = urllib.parse.urlparse(response.geturl())
                if final_url.scheme != "https":
                    raise HostArtifactResolutionError("artifact redirect left HTTPS")
                declared_size = _declared_size_get(response.headers.get("Content-Length"))
                with tempfile.NamedTemporaryFile(
                    dir=self._cache_root_path,
                    prefix=".download-",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_HOST_ARTIFACT_SIZE_BYTES:
                            raise HostArtifactResolutionError("artifact response exceeds the download size limit")
                        temporary_file.write(chunk)
                        digest.update(chunk)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
            if declared_size is not None and size != declared_size:
                raise HostArtifactResolutionError("artifact response differs from its declared Content-Length")
            sha256 = digest.hexdigest()
            if expected_sha256 and sha256 != expected_sha256:
                raise HostArtifactResolutionError(f"artifact checksum mismatch for {url}")
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, artifact_path)
            _directory_fsync(artifact_path.parent)
            temporary_path = None
            return sha256, size
        except OSError as error:
            raise HostArtifactResolutionError(f"unable to download artifact {url}: {error}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _declared_size_get(value: str | None) -> int | None:
    """Parse and bound an optional HTTP Content-Length declaration.

    Args:
        value: Candidate value.

    Returns:
        Validated byte count when the server declared one.
    """

    if value is None:
        return None
    try:
        size = int(value)
    except ValueError as error:
        raise HostArtifactResolutionError("artifact response has an invalid Content-Length") from error
    if size < 0 or size > MAX_HOST_ARTIFACT_SIZE_BYTES:
        raise HostArtifactResolutionError("artifact response exceeds the download size limit")
    return size


def _directory_fsync(path: Path) -> None:
    """Make one directory-entry mutation durable.

    Args:
        path: Exact filesystem path.
    """

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
