"""Resolve and verify AWS CLI release artifacts."""

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

AWS_CLI_REPOSITORY_URL = "https://github.com/aws/aws-cli.git"
AWS_CLI_SELECTOR = "2"
AWS_CLI_SIGNING_KEY_FINGERPRINT = "FB5DB77FD5C118B80511ADA8A6310ACC4672475C"


class AwsCliArtifactProvider:
    """Own AWS CLI moving-version selection and PGP verification."""

    def __init__(
        self,
        *,
        downloader: HostArtifactDownloader,
        git_ref: GitRefResolver,
        trust_root_path: Path,
        verifier: HostArtifactVerifier,
    ) -> None:
        """Initialize the AWS CLI artifact provider dependencies.

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
        """Return exact AWS CLI archive and detached signature identities.

        Args:
            architecture: Architecture.

        Returns:
            The exact AWS CLI archive and detached signature identities.
        """

        version, resolved_ref, commit_sha = self._git_ref.latest_tag_resolve(
            repository_url=AWS_CLI_REPOSITORY_URL,
            selector=AWS_CLI_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(2\.(\d+)\.(\d+))$"),
        )
        self._git_ref.commit_validate(
            repository_url=AWS_CLI_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        url = f"https://awscli.amazonaws.com/awscli-exe-linux-{architecture}-{version}.zip"
        signature = self._downloader.identity_resolve(
            name="aws-cli-signature",
            selector=AWS_CLI_SELECTOR,
            version=version,
            url=f"{url}.sig",
            verification="detached-signature",
            verification_identity=AWS_CLI_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        artifact = self._downloader.identity_resolve(
            name="aws-cli",
            selector=AWS_CLI_SELECTOR,
            version=version,
            url=url,
            verification="pgp",
            verification_identity=AWS_CLI_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._verifier.pgp_detached_signature_validate(
            artifact_path=self._downloader.cache_path_get(artifact.url),
            expected_primary_fingerprint=AWS_CLI_SIGNING_KEY_FINGERPRINT,
            key_path=self._trust_root_path / "aws-cli-release.asc",
            signature_path=self._downloader.cache_path_get(signature.url),
        )
        self._git_ref.unchanged_validate(
            repository_url=AWS_CLI_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return artifact, signature
