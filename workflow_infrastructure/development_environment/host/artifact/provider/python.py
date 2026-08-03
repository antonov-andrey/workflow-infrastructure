"""Resolve uv and its pinned standalone Python 3.14 artifact."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from workflow_infrastructure.development_environment.host.artifact.download import (
    HostArtifactDownloader,
)
from workflow_infrastructure.development_environment.host.artifact.git_ref import (
    GitRefResolver,
)
from workflow_infrastructure.development_environment.host.artifact.model import (
    PYTHON_SELECTOR,
    HostArtifactIdentity,
    HostArtifactResolutionError,
)
from workflow_infrastructure.development_environment.host.artifact.verification import (
    HostArtifactVerifier,
)

UV_REPOSITORY_URL = "https://github.com/astral-sh/uv.git"
UV_SELECTOR = "0"
UV_SIGNER_WORKFLOW = "astral-sh/uv/.github/workflows/release.yml"


def python_download_payload_get(*, architecture: str, metadata: object) -> dict[str, object]:
    """Select the newest stable exact Python 3.14 GNU/Linux build."""

    if not isinstance(metadata, Mapping):
        raise HostArtifactResolutionError("uv Python metadata root is malformed")
    candidate_list: list[dict[str, object]] = []
    for payload in metadata.values():
        if not isinstance(payload, dict):
            continue
        architecture_payload = payload.get("arch")
        if (
            payload.get("name") != "cpython"
            or payload.get("major") != 3
            or payload.get("minor") != 14
            or payload.get("prerelease") != ""
            or payload.get("variant") is not None
            or payload.get("os") != "linux"
            or payload.get("libc") != "gnu"
            or not isinstance(architecture_payload, dict)
            or architecture_payload.get("family") != architecture
            or architecture_payload.get("variant") is not None
            or not isinstance(payload.get("patch"), int)
            or not isinstance(payload.get("build"), str)
            or not isinstance(payload.get("url"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256"))) is None
        ):
            continue
        candidate_list.append(payload)
    if not candidate_list:
        raise HostArtifactResolutionError(f"uv release has no stable {PYTHON_SELECTOR} build for {architecture}")
    return max(
        candidate_list,
        key=lambda payload: (int(payload["patch"]), str(payload["build"])),
    )


class PythonArtifactProvider:
    """Own uv release trust and standalone Python metadata selection."""

    def __init__(
        self,
        *,
        downloader: HostArtifactDownloader,
        git_ref: GitRefResolver,
        verifier: HostArtifactVerifier,
    ) -> None:
        self._downloader = downloader
        self._git_ref = git_ref
        self._verifier = verifier

    def resolve(
        self, *, python_architecture: str, uv_architecture: str
    ) -> tuple[HostArtifactIdentity, HostArtifactIdentity, HostArtifactIdentity, str]:
        """Return uv, uv metadata, Python, and exact Python build."""

        uv_version, resolved_ref, commit_sha = self._git_ref.latest_tag_resolve(
            repository_url=UV_REPOSITORY_URL,
            selector=UV_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(0\.(\d+)\.(\d+))$"),
        )
        self._git_ref.commit_validate(
            repository_url=UV_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        uv_url = (
            "https://github.com/astral-sh/uv/releases/download/"
            f"{uv_version}/uv-{uv_architecture}-unknown-linux-gnu.tar.gz"
        )
        uv_artifact = self._downloader.identity_resolve(
            name="uv",
            selector=UV_SELECTOR,
            version=uv_version,
            url=uv_url,
            verification="github-attestation",
            verification_identity=f"{UV_SIGNER_WORKFLOW}@{commit_sha}",
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._verifier.github_attestation_validate(
            artifact_path=self._downloader.cache_path_get(uv_artifact.url),
            repository="astral-sh/uv",
            signer_workflow=UV_SIGNER_WORKFLOW,
            source_commit_sha=commit_sha,
        )
        metadata_url = (
            "https://raw.githubusercontent.com/astral-sh/uv/" f"{commit_sha}/crates/uv-python/download-metadata.json"
        )
        metadata_artifact = self._downloader.identity_resolve(
            name="uv-python-metadata",
            selector=f"uv@{UV_SELECTOR}",
            version=uv_version,
            url=metadata_url,
            verification="github-attested-source",
            verification_identity=f"{UV_SIGNER_WORKFLOW}@{commit_sha}",
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        try:
            metadata = json.loads(self._downloader.cache_path_get(metadata_url).read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise HostArtifactResolutionError("uv Python metadata is invalid JSON") from error
        python_payload = python_download_payload_get(
            architecture=python_architecture,
            metadata=metadata,
        )
        python_version = f"{python_payload['major']}.{python_payload['minor']}.{python_payload['patch']}"
        python_artifact = self._downloader.identity_resolve(
            expected_sha256=str(python_payload["sha256"]),
            name="python",
            selector=PYTHON_SELECTOR,
            version=python_version,
            url=str(python_payload["url"]),
            verification="attested-metadata-sha256",
            verification_identity=metadata_artifact.sha256,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._git_ref.unchanged_validate(
            repository_url=UV_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return (
            uv_artifact,
            metadata_artifact,
            python_artifact,
            str(python_payload["build"]),
        )
