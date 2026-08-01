"""Resolve Docker packages through signed Ubuntu APT metadata."""

from __future__ import annotations

import gzip
import re
from collections.abc import Sequence
from typing import Protocol

from workflow_infrastructure.development_environment.host.artifact.download import (
    HostArtifactDownloader,
)
from workflow_infrastructure.development_environment.host.artifact.model import (
    DOCKER_SIGNING_KEY_FINGERPRINT,
    HostArtifactIdentity,
    HostArtifactResolutionError,
)
from workflow_infrastructure.development_environment.host.artifact.verification import (
    HostArtifactVerifier,
)

DOCKER_APT_ROOT_URL = "https://download.docker.com/linux/ubuntu"
DOCKER_CHANNEL = "stable"
DOCKER_CODENAME = "noble"
DOCKER_PACKAGE_NAME_LIST = [
    "containerd.io",
    "docker-buildx-plugin",
    "docker-ce",
    "docker-ce-cli",
]
DOCKER_SIGNING_KEY_URL = f"{DOCKER_APT_ROOT_URL}/gpg"


class CommandResultProtocol(Protocol):
    returncode: int


class CommandRunnerProtocol(Protocol):
    def run(
        self, command_list: Sequence[str], *, check: bool = True
    ) -> CommandResultProtocol:
        """Run one local command."""


class DockerArtifactProvider:
    """Own Docker repository metadata and package version selection."""

    def __init__(
        self,
        *,
        downloader: HostArtifactDownloader,
        runner: CommandRunnerProtocol,
        verifier: HostArtifactVerifier,
    ) -> None:
        self._downloader = downloader
        self._runner = runner
        self._verifier = verifier

    def resolve(self, architecture: str) -> dict[str, HostArtifactIdentity]:
        """Return exact signed repository metadata and package identities."""

        signing_key = self._downloader.identity_resolve(
            allow_cache=False,
            name="docker-signing-key",
            selector=DOCKER_SIGNING_KEY_URL,
            version=DOCKER_SIGNING_KEY_FINGERPRINT,
            url=DOCKER_SIGNING_KEY_URL,
            verification="primary-key-fingerprint",
            verification_identity=DOCKER_SIGNING_KEY_FINGERPRINT,
        )
        signing_key_path = self._downloader.cache_path_get(signing_key.url)
        self._verifier.signing_key_validate(
            key_path=signing_key_path,
            expected_primary_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
        )
        inrelease_url = f"{DOCKER_APT_ROOT_URL}/dists/{DOCKER_CODENAME}/InRelease"
        inrelease = self._downloader.identity_resolve(
            allow_cache=False,
            name="docker-inrelease",
            selector=f"{DOCKER_CODENAME}/{DOCKER_CHANNEL}",
            version=DOCKER_CODENAME,
            url=inrelease_url,
            verification="pgp",
            verification_identity=DOCKER_SIGNING_KEY_FINGERPRINT,
        )
        inrelease_path = self._downloader.cache_path_get(inrelease.url)
        self._verifier.inrelease_signature_validate(
            inrelease_path=inrelease_path,
            signing_key_path=signing_key_path,
        )
        packages_relative_path, packages_sha256 = self.packages_index_identity_get(
            architecture=architecture,
            inrelease_text=inrelease_path.read_text(encoding="utf-8"),
        )
        packages_index = self._downloader.identity_resolve(
            expected_sha256=packages_sha256,
            name="docker-packages-index",
            selector=f"{DOCKER_CODENAME}/{DOCKER_CHANNEL}/{architecture}",
            version=DOCKER_CODENAME,
            url=f"{DOCKER_APT_ROOT_URL}/dists/{DOCKER_CODENAME}/{packages_relative_path}",
            verification="signed-metadata-sha256",
            verification_identity=inrelease.sha256,
        )
        packages_bytes = self._downloader.cache_path_get(
            packages_index.url
        ).read_bytes()
        if packages_relative_path.endswith(".gz"):
            try:
                packages_bytes = gzip.decompress(packages_bytes)
            except gzip.BadGzipFile as error:
                raise HostArtifactResolutionError(
                    "Docker Packages index is not valid gzip"
                ) from error
        package_payload_list = self.debian_package_payload_list_get(
            packages_bytes.decode("utf-8")
        )
        result = {
            "docker-signing-key": signing_key,
            "docker-inrelease": inrelease,
            "docker-packages-index": packages_index,
        }
        for package_name in DOCKER_PACKAGE_NAME_LIST:
            package_payload = self.latest_debian_package_get(
                architecture=architecture,
                package_name=package_name,
                package_payload_list=package_payload_list,
            )
            result[package_name] = self._downloader.identity_resolve(
                expected_sha256=package_payload["SHA256"],
                name=package_name,
                selector=f"{DOCKER_CODENAME}/{DOCKER_CHANNEL}",
                version=package_payload["Version"],
                url=f"{DOCKER_APT_ROOT_URL}/{package_payload['Filename']}",
                verification="signed-metadata-sha256",
                verification_identity=packages_index.sha256,
            )
        return result

    @staticmethod
    def packages_index_identity_get(
        *, architecture: str, inrelease_text: str
    ) -> tuple[str, str]:
        """Return signed Packages path and SHA-256 for one architecture."""

        section_match = re.search(
            r"(?:^|\n)SHA256:\n(?P<body>(?: [^\n]+\n)+)", inrelease_text
        )
        if section_match is None:
            raise HostArtifactResolutionError("Docker InRelease has no SHA256 section")
        expected_path_set = {
            f"{DOCKER_CHANNEL}/binary-{architecture}/Packages.gz",
            f"{DOCKER_CHANNEL}/binary-{architecture}/Packages",
        }
        for line in section_match.group("body").splitlines():
            part_list = line.split()
            if len(part_list) == 3:
                sha256, _, relative_path = part_list
                if relative_path in expected_path_set and re.fullmatch(
                    r"[0-9a-f]{64}", sha256
                ):
                    return relative_path, sha256
        raise HostArtifactResolutionError(
            "Docker InRelease does not bind the target Packages index"
        )

    @staticmethod
    def debian_package_payload_list_get(packages_text: str) -> list[dict[str, str]]:
        """Parse Debian control paragraphs without interpreting package scripts."""

        payload_list: list[dict[str, str]] = []
        for paragraph in re.split(r"\n\s*\n", packages_text):
            payload: dict[str, str] = {}
            current_name = ""
            for line in paragraph.splitlines():
                if line.startswith((" ", "\t")) and current_name:
                    payload[current_name] += "\n" + line[1:]
                    continue
                name, separator, value = line.partition(":")
                if separator:
                    current_name = name
                    payload[name] = value.strip()
            if payload:
                payload_list.append(payload)
        return payload_list

    def latest_debian_package_get(
        self,
        *,
        architecture: str,
        package_name: str,
        package_payload_list: list[dict[str, str]],
    ) -> dict[str, str]:
        """Use dpkg ordering to select the latest exact matching package."""

        candidate_list = [
            payload
            for payload in package_payload_list
            if payload.get("Package") == package_name
            and payload.get("Architecture") in {architecture, "all"}
            and re.fullmatch(r"[0-9a-f]{64}", payload.get("SHA256", ""))
            and payload.get("Filename")
            and payload.get("Version")
        ]
        if not candidate_list:
            raise HostArtifactResolutionError(
                f"Docker repository has no package {package_name} for {architecture}"
            )
        latest = candidate_list[0]
        for candidate in candidate_list[1:]:
            result = self._runner.run(
                [
                    "dpkg",
                    "--compare-versions",
                    candidate["Version"],
                    "gt",
                    latest["Version"],
                ],
                check=False,
            )
            if result.returncode == 0:
                latest = candidate
        return latest
