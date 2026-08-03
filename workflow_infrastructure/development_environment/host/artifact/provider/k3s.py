"""Resolve K3s through repository trust and independent release digests."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from workflow_infrastructure.development_environment.host.artifact.download import (
    HostArtifactDownloader,
)
from workflow_infrastructure.development_environment.host.artifact.git_ref import (
    GitRefResolver,
)
from workflow_infrastructure.development_environment.host.artifact.model import (
    HostArtifactIdentity,
    HostArtifactResolutionError,
)
from workflow_infrastructure.development_environment.host.artifact.verification import (
    HostArtifactVerifier,
    checksum_file_sha256_get,
)

K3S_REPOSITORY_URL = "https://github.com/k3s-io/k3s.git"
K3S_SELECTOR = "1.36"
_TRUST_ARTIFACT_NAME_SET = {
    "k3s",
    "k3s-arm64",
    "sha256sum-amd64.txt",
    "sha256sum-arm64.txt",
}


class K3sArtifactProvider:
    """Own K3s version selection and multi-source digest agreement."""

    def __init__(
        self,
        *,
        downloader: HostArtifactDownloader,
        git_ref: GitRefResolver,
        trust_root_path: Path,
        verifier: HostArtifactVerifier,
    ) -> None:
        self._downloader = downloader
        self._git_ref = git_ref
        self._trust_root_path = trust_root_path
        self._verifier = verifier

    def resolve(self, architecture: str) -> dict[str, HostArtifactIdentity]:
        """Return exact K3s binary and vendor checksum identities."""

        version, resolved_ref, commit_sha = self._git_ref.latest_tag_resolve(
            repository_url=K3S_REPOSITORY_URL,
            selector=K3S_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(v1\.36\.(\d+)\+k3s(\d+))$"),
        )
        self._git_ref.commit_validate(
            repository_url=K3S_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        binary_name = "k3s" if architecture == "amd64" else "k3s-arm64"
        checksum_name = f"sha256sum-{architecture}.txt"
        trusted_sha256_by_name, trust_sha256 = self.trust_identity_get(
            binary_name=binary_name,
            checksum_name=checksum_name,
            commit_sha=commit_sha,
            resolved_ref=resolved_ref,
            version=version,
        )
        checksum_sha256 = self._verifier.github_release_asset_sha256_get(
            asset_name=checksum_name,
            repository="k3s-io/k3s",
            version=version,
        )
        if checksum_sha256 != trusted_sha256_by_name[checksum_name]:
            raise HostArtifactResolutionError(
                "k3s release checksum asset differs from the repository-owned trust record"
            )
        checksum_artifact = self._downloader.identity_resolve(
            expected_sha256=trusted_sha256_by_name[checksum_name],
            name="k3s-checksums",
            selector=K3S_SELECTOR,
            version=version,
            url=f"https://github.com/k3s-io/k3s/releases/download/{version}/{checksum_name}",
            verification="repository-trust+github-release-digest",
            verification_identity=trust_sha256,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        expected_binary_sha256 = checksum_file_sha256_get(
            artifact_name=binary_name,
            checksum_path=self._downloader.cache_path_get(checksum_artifact.url),
        )
        if expected_binary_sha256 != trusted_sha256_by_name[binary_name]:
            raise HostArtifactResolutionError("k3s vendor checksum differs from the repository-owned trust record")
        if (
            self._verifier.github_release_asset_sha256_get(
                asset_name=binary_name,
                repository="k3s-io/k3s",
                version=version,
            )
            != expected_binary_sha256
        ):
            raise HostArtifactResolutionError("k3s vendor checksum and GitHub release digest differ")
        binary = self._downloader.identity_resolve(
            expected_sha256=trusted_sha256_by_name[binary_name],
            name="k3s-binary",
            selector=K3S_SELECTOR,
            version=version,
            url=f"https://github.com/k3s-io/k3s/releases/download/{version}/{binary_name}",
            verification="repository-trust+vendor-checksum+github-release-digest",
            verification_identity=trust_sha256,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._git_ref.unchanged_validate(
            repository_url=K3S_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return {"k3s-binary": binary, "k3s-checksums": checksum_artifact}

    def trust_identity_get(
        self,
        *,
        binary_name: str,
        checksum_name: str,
        commit_sha: str,
        resolved_ref: str,
        version: str,
    ) -> tuple[dict[str, str], str]:
        """Return one reviewed K3s release identity from repository trust."""

        try:
            trust_bytes = (self._trust_root_path / "k3s-release.json").read_bytes()
            payload = json.loads(trust_bytes)
        except (OSError, json.JSONDecodeError) as error:
            raise HostArtifactResolutionError("repository-owned K3s release trust record is unavailable") from error
        artifact_sha256_by_name = payload.get("artifact_sha256_by_name_map") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "artifact_sha256_by_name_map",
                "resolved_ref",
                "source_commit_sha",
                "version",
            }
            or payload.get("resolved_ref") != resolved_ref
            or payload.get("source_commit_sha") != commit_sha
            or payload.get("version") != version
            or not isinstance(artifact_sha256_by_name, dict)
            or set(artifact_sha256_by_name) != _TRUST_ARTIFACT_NAME_SET
            or binary_name not in artifact_sha256_by_name
            or checksum_name not in artifact_sha256_by_name
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in artifact_sha256_by_name.values()
            )
        ):
            raise HostArtifactResolutionError("latest K3s release is not accepted by the repository-owned trust record")
        return dict(artifact_sha256_by_name), hashlib.sha256(trust_bytes).hexdigest()
