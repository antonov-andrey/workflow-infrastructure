"""Resolve and verify Helm release artifacts."""

from __future__ import annotations

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
)
from workflow_infrastructure.development_environment.host.artifact.verification import (
    HostArtifactVerifier,
)

HELM_REPOSITORY_URL = "https://github.com/helm/helm.git"
HELM_SELECTOR = "4"
HELM_SIGNING_KEY_FINGERPRINT = "672C657BE06B4B30969C4A57461449C25E36B98E"


class HelmArtifactProvider:
    """Own Helm moving-version selection and PGP verification."""

    def __init__(
        self,
        *,
        downloader: HostArtifactDownloader,
        git_ref: GitRefResolver,
        trust_root_path: Path,
        verifier: HostArtifactVerifier,
    ) -> None:
        """Initialize the helm artifact provider dependencies.

        Args:
            downloader: Downloader.
            git_ref: Git ref.
            trust_root_path: Exact filesystem path for trust root.
            verifier: Verifier.
        """

        self._downloader = downloader
        self._git_ref = git_ref
        self._trust_root_path = trust_root_path
        self._verifier = verifier

    def resolve(self, architecture: str) -> tuple[HostArtifactIdentity, HostArtifactIdentity]:
        """Return exact Helm archive and signature identities.

        Args:
            architecture: Architecture.

        Returns:
            The exact Helm archive and signature identities.
        """

        version, resolved_ref, commit_sha = self._git_ref.latest_tag_resolve(
            repository_url=HELM_REPOSITORY_URL,
            selector=HELM_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(v4\.(\d+)\.(\d+))$"),
        )
        self._git_ref.commit_validate(
            repository_url=HELM_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        archive_name = f"helm-{version}-linux-{architecture}.tar.gz"
        signature = self._downloader.identity_resolve(
            name="helm-signature",
            selector=HELM_SELECTOR,
            version=version,
            url=f"https://github.com/helm/helm/releases/download/{version}/{archive_name}.asc",
            verification="detached-signature",
            verification_identity=HELM_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        artifact = self._downloader.identity_resolve(
            name="helm",
            selector=HELM_SELECTOR,
            version=version,
            url=f"https://get.helm.sh/{archive_name}",
            verification="pgp",
            verification_identity=HELM_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._verifier.pgp_detached_signature_validate(
            artifact_path=self._downloader.cache_path_get(artifact.url),
            expected_primary_fingerprint=HELM_SIGNING_KEY_FINGERPRINT,
            key_path=self._trust_root_path / "helm-release.asc",
            signature_path=self._downloader.cache_path_get(signature.url),
        )
        self._git_ref.unchanged_validate(
            repository_url=HELM_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return artifact, signature
