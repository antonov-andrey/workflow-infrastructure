"""Cryptographic and signed-metadata verification boundaries."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.host.artifact.download import (
    HostArtifactDownloader,
)
from workflow_infrastructure.development_environment.host.artifact.model import (
    HostArtifactResolutionError,
)


class CommandResultProtocol(Protocol):
    stdout: str


class CommandRunnerProtocol(Protocol):
    def run(self, command_list: Sequence[str]) -> CommandResultProtocol:
        """Run one local command."""


class HostArtifactVerifier:
    """Own cryptographic trust and external signed-metadata verification."""

    def __init__(
        self,
        *,
        downloader: HostArtifactDownloader,
        runner: CommandRunnerProtocol,
    ) -> None:
        self._downloader = downloader
        self._runner = runner

    def github_release_asset_sha256_get(
        self, *, asset_name: str, repository: str, version: str
    ) -> str:
        """Return GitHub's exact digest for one release asset."""

        encoded_version = urllib.parse.quote(version, safe="")
        url = (
            f"https://api.github.com/repos/{repository}/releases/tags/{encoded_version}"
        )
        metadata_path = self._downloader.cache_path_get(url)
        self._downloader.download(
            allow_cache=False,
            artifact_path=metadata_path,
            expected_sha256="",
            url=url,
        )
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HostArtifactResolutionError(
                "GitHub release metadata is malformed"
            ) from error
        asset_list = payload.get("assets") if isinstance(payload, dict) else None
        matching_asset_list = (
            [
                item
                for item in asset_list
                if isinstance(item, dict) and item.get("name") == asset_name
            ]
            if isinstance(asset_list, list)
            else []
        )
        if len(matching_asset_list) != 1:
            raise HostArtifactResolutionError(
                f"GitHub release has no unique asset {asset_name}"
            )
        digest = matching_asset_list[0].get("digest")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise HostArtifactResolutionError(
                f"GitHub release asset {asset_name} has no SHA-256 identity"
            )
        return digest.removeprefix("sha256:")

    def github_attestation_validate(
        self,
        *,
        artifact_path: Path,
        repository: str,
        signer_workflow: str,
        source_commit_sha: str,
    ) -> None:
        """Verify an artifact against one authorized GitHub workflow."""

        self._runner.run(
            [
                "gh",
                "attestation",
                "verify",
                artifact_path.as_posix(),
                "--repo",
                repository,
                "--signer-workflow",
                signer_workflow,
                "--source-digest",
                source_commit_sha,
            ]
        )

    def signing_key_validate(
        self, *, key_path: Path, expected_primary_fingerprint: str
    ) -> None:
        """Require a downloaded keyring to contain one trusted primary key."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home_path = Path(temporary_directory)
            os.chmod(home_path, 0o700)
            result = self._runner.run(
                [
                    "gpg",
                    "--batch",
                    "--homedir",
                    str(home_path),
                    "--show-keys",
                    "--with-colons",
                    "--fingerprint",
                    str(key_path),
                ]
            )
        if self.primary_key_fingerprint_list_get(result.stdout) != [
            expected_primary_fingerprint
        ]:
            raise HostArtifactResolutionError(
                "signing keyring does not contain exactly the trusted primary key"
            )

    def pgp_detached_signature_validate(
        self,
        *,
        artifact_path: Path,
        expected_primary_fingerprint: str,
        key_path: Path,
        signature_path: Path,
    ) -> None:
        """Verify a detached signature under one repository trust root."""

        if not key_path.is_file():
            raise HostArtifactResolutionError(
                f"Release signing trust root is unavailable: {key_path}"
            )
        with tempfile.TemporaryDirectory() as temporary_directory:
            home_path = Path(temporary_directory)
            os.chmod(home_path, 0o700)
            key_payload = self._runner.run(
                [
                    "gpg",
                    "--batch",
                    "--homedir",
                    home_path.as_posix(),
                    "--show-keys",
                    "--with-colons",
                    "--fingerprint",
                    key_path.as_posix(),
                ]
            )
            if self.primary_key_fingerprint_list_get(key_payload.stdout) != [
                expected_primary_fingerprint
            ]:
                raise HostArtifactResolutionError(
                    "Release signing trust root has another primary fingerprint"
                )
            keyring_path = home_path / "release.gpg"
            self._runner.run(
                [
                    "gpg",
                    "--batch",
                    "--yes",
                    "--dearmor",
                    "--output",
                    keyring_path.as_posix(),
                    key_path.as_posix(),
                ]
            )
            self._runner.run(
                [
                    "gpgv",
                    "--keyring",
                    keyring_path.as_posix(),
                    signature_path.as_posix(),
                    artifact_path.as_posix(),
                ]
            )

    def inrelease_signature_validate(
        self, *, inrelease_path: Path, signing_key_path: Path
    ) -> None:
        """Verify one clear-signed APT InRelease document."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            keyring_path = Path(temporary_directory) / "release.gpg"
            self._runner.run(
                [
                    "gpg",
                    "--batch",
                    "--yes",
                    "--dearmor",
                    "--output",
                    str(keyring_path),
                    str(signing_key_path),
                ]
            )
            self._runner.run(
                [
                    "gpgv",
                    "--keyring",
                    str(keyring_path),
                    str(inrelease_path),
                ]
            )

    @staticmethod
    def primary_key_fingerprint_list_get(gpg_colon_output: str) -> list[str]:
        """Return fingerprints bound only to primary public-key records."""

        primary_fingerprint_list: list[str] = []
        primary_fingerprint_is_pending = False
        for line in gpg_colon_output.splitlines():
            field_list = line.split(":")
            record_type = field_list[0] if field_list else ""
            if record_type == "pub":
                if primary_fingerprint_is_pending:
                    raise HostArtifactResolutionError(
                        "signing keyring has a primary key without a fingerprint"
                    )
                primary_fingerprint_is_pending = True
            elif record_type == "fpr" and primary_fingerprint_is_pending:
                if (
                    len(field_list) <= 9
                    or re.fullmatch(r"[0-9A-F]{40}", field_list[9]) is None
                ):
                    raise HostArtifactResolutionError(
                        "signing keyring has a malformed primary fingerprint"
                    )
                primary_fingerprint_list.append(field_list[9])
                primary_fingerprint_is_pending = False
        if primary_fingerprint_is_pending:
            raise HostArtifactResolutionError(
                "signing keyring has a primary key without a fingerprint"
            )
        return primary_fingerprint_list

    @staticmethod
    def checksum_file_sha256_get(*, artifact_name: str, checksum_path: Path) -> str:
        """Return one unique SHA-256 entry from vendor checksum metadata."""

        try:
            line_list = checksum_path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise HostArtifactResolutionError(
                "Vendor checksum metadata cannot be read"
            ) from error
        matching_sha256_list: list[str] = []
        for line in line_list:
            match = re.fullmatch(
                r"([0-9a-f]{64})[ \t]+(?:\*|)" + re.escape(artifact_name),
                line,
            )
            if match is not None:
                matching_sha256_list.append(match.group(1))
        if len(matching_sha256_list) != 1:
            raise HostArtifactResolutionError(
                f"Vendor checksum metadata has no unique {artifact_name} entry"
            )
        return matching_sha256_list[0]
