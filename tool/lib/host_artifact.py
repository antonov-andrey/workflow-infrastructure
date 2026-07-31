"""Resolve immutable host bootstrap artifacts before a compute change set."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

AWS_CLI_REPOSITORY_URL = "https://github.com/aws/aws-cli.git"
AWS_CLI_SELECTOR = "2"
AWS_CLI_SIGNING_KEY_FINGERPRINT = "FB5DB77FD5C118B80511ADA8A6310ACC4672475C"
DOCKER_APT_ROOT_URL = "https://download.docker.com/linux/ubuntu"
DOCKER_CHANNEL = "stable"
DOCKER_CODENAME = "noble"
DOCKER_PACKAGE_NAME_LIST = [
    "containerd.io",
    "docker-buildx-plugin",
    "docker-ce",
    "docker-ce-cli",
]
DOCKER_SIGNING_KEY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
DOCKER_SIGNING_KEY_URL = f"{DOCKER_APT_ROOT_URL}/gpg"
HELM_REPOSITORY_URL = "https://github.com/helm/helm.git"
HELM_SELECTOR = "4"
HELM_SIGNING_KEY_FINGERPRINT = "672C657BE06B4B30969C4A57461449C25E36B98E"
K3S_REPOSITORY_URL = "https://github.com/k3s-io/k3s.git"
K3S_SELECTOR = "1.36"
PYTHON_SELECTOR = "3.14"
UV_REPOSITORY_URL = "https://github.com/astral-sh/uv.git"
UV_SELECTOR = "0"
UV_SIGNER_WORKFLOW = "astral-sh/uv/.github/workflows/release.yml"

_MAX_HOST_ARTIFACT_SIZE_BYTES = 1024 * 1024 * 1024
_HOST_ARTIFACT_URL_PATTERN = re.compile(r"https://[A-Za-z0-9._~:/?#\[\]@!&()*+,;=%-]+")
_TRUST_ROOT_PATH = Path(__file__).resolve().parents[2] / "trust"
_K3S_TRUST_PATH = _TRUST_ROOT_PATH / "k3s-release.json"
_K3S_TRUST_ARTIFACT_NAME_SET = {
    "k3s",
    "k3s-arm64",
    "sha256sum-amd64.txt",
    "sha256sum-arm64.txt",
}
_ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP = {
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
_HOST_ARTIFACT_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "architecture",
        "artifact_by_name_map",
        "docker_signing_key_fingerprint",
        "python_build",
        "python_selector",
    }
)
_HOST_ARTIFACT_IDENTITY_FIELD_NAME_SET = frozenset(
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
_HOST_ARTIFACT_RESOLVED_IDENTITY_FIELD_NAME_SET = frozenset(
    {
        *_HOST_ARTIFACT_IDENTITY_FIELD_NAME_SET,
        "resolved_ref",
        "source_commit_sha",
    }
)


@dataclass(frozen=True)
class HostArtifactIdentity:
    """Describe one exact downloaded artifact and its moving-source provenance."""

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
        """Return the canonical serializable artifact identity."""

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
    """Hold one architecture-specific immutable bootstrap input graph."""

    architecture: str
    artifact_by_name_map: Mapping[str, HostArtifactIdentity]
    docker_signing_key_fingerprint: str
    python_build: str

    def manifest_payload_get(self) -> dict[str, object]:
        """Return canonical launch/release provenance."""

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
        """Return the digest of the canonical launch/release provenance."""

        payload_bytes = json.dumps(
            self.manifest_payload_get(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload_bytes).hexdigest()

    def manifest_gzip_base64_get(self) -> str:
        """Return a deterministic compact copy retained in stack parameters."""

        payload_bytes = json.dumps(
            self.manifest_payload_get(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.b64encode(gzip.compress(payload_bytes, mtime=0)).decode()

    def cloudformation_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return the canonical manifest as the single compute-template artifact input."""

        artifact = self.artifact_by_name_map
        if any(len(item.url) > 384 for item in artifact.values()):
            raise HostArtifactResolutionError("host artifact URL exceeds the bounded launch-input contract")
        encoded_manifest = self.manifest_gzip_base64_get()
        if len(encoded_manifest) > 3072:
            raise HostArtifactResolutionError("host artifact manifest exceeds the bounded launch-input contract")
        return {
            "HostArtifactManifestSha256": self.manifest_sha256_get(),
            "HostArtifactManifestGzipBase64": encoded_manifest,
        }


class HostArtifactResolver:
    """Resolve, download, and verify one exact architecture-specific artifact graph."""

    def __init__(self, *, cache_root_path: Path, runner: object) -> None:
        """Initialize the trusted operator-side resolver.

        Args:
            cache_root_path: Private local cache for exact downloaded bytes.
            runner: Command runner exposing ``run`` with the project contract.
        """

        self._cache_root_path = cache_root_path
        self._runner = runner

    def resolve(self, architecture: str) -> HostArtifactResolution:
        """Resolve all moving selectors once and verify every selected byte stream.

        Args:
            architecture: Compute architecture accepted by the template.

        Returns:
            Immutable artifact graph and CloudFormation inputs.

        Raises:
            HostArtifactResolutionError: If an identity cannot be resolved or verified.
        """

        architecture_by_owner_map = _ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP.get(architecture)
        if architecture_by_owner_map is None:
            raise HostArtifactResolutionError(f"unsupported host artifact architecture: {architecture}")
        self._cache_root_path.mkdir(mode=0o700, parents=True, exist_ok=True)

        artifact_by_name_map: dict[str, HostArtifactIdentity] = {}
        aws_cli_artifact, aws_cli_signature_artifact = self._aws_cli_resolve(architecture_by_owner_map["aws_cli"])
        artifact_by_name_map["aws-cli"] = aws_cli_artifact
        artifact_by_name_map["aws-cli-signature"] = aws_cli_signature_artifact
        artifact_by_name_map.update(self._docker_resolve(architecture_by_owner_map["docker"]))
        uv_artifact, uv_metadata_artifact, python_artifact, python_build = self._uv_python_resolve(
            python_architecture=architecture_by_owner_map["python"],
            uv_architecture=architecture_by_owner_map["uv"],
        )
        artifact_by_name_map["uv"] = uv_artifact
        artifact_by_name_map["uv-python-metadata"] = uv_metadata_artifact
        artifact_by_name_map["python"] = python_artifact
        artifact_by_name_map.update(self._k3s_resolve(architecture_by_owner_map["k3s"]))
        helm_artifact, helm_signature_artifact = self._helm_resolve(architecture_by_owner_map["helm"])
        artifact_by_name_map["helm"] = helm_artifact
        artifact_by_name_map["helm-signature"] = helm_signature_artifact
        resolution = HostArtifactResolution(
            architecture=architecture,
            artifact_by_name_map=artifact_by_name_map,
            docker_signing_key_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
            python_build=python_build,
        )
        resolution.cloudformation_parameter_by_name_map_get()
        return resolution

    def _aws_cli_resolve(
        self,
        architecture: str,
    ) -> tuple[HostArtifactIdentity, HostArtifactIdentity]:
        version, resolved_ref, commit_sha = self._latest_tag_resolve(
            repository_url=AWS_CLI_REPOSITORY_URL,
            selector=AWS_CLI_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(2\.(\d+)\.(\d+))$"),
        )
        self._git_ref_commit_validate(
            repository_url=AWS_CLI_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        url = "https://awscli.amazonaws.com/" f"awscli-exe-linux-{architecture}-{version}.zip"
        signature = self._artifact_resolve(
            name="aws-cli-signature",
            selector=AWS_CLI_SELECTOR,
            version=version,
            url=f"{url}.sig",
            verification="detached-signature",
            verification_identity=AWS_CLI_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        artifact = self._artifact_resolve(
            name="aws-cli",
            selector=AWS_CLI_SELECTOR,
            version=version,
            url=url,
            verification="pgp",
            verification_identity=AWS_CLI_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._pgp_detached_signature_validate(
            artifact_path=self._artifact_cache_path_get(artifact.url),
            expected_primary_fingerprint=AWS_CLI_SIGNING_KEY_FINGERPRINT,
            key_path=_TRUST_ROOT_PATH / "aws-cli-release.asc",
            signature_path=self._artifact_cache_path_get(signature.url),
        )
        self._git_ref_unchanged_validate(
            repository_url=AWS_CLI_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return artifact, signature

    def _docker_resolve(
        self,
        architecture: str,
    ) -> dict[str, HostArtifactIdentity]:
        signing_key = self._artifact_resolve(
            allow_cache=False,
            name="docker-signing-key",
            selector=DOCKER_SIGNING_KEY_URL,
            version=DOCKER_SIGNING_KEY_FINGERPRINT,
            url=DOCKER_SIGNING_KEY_URL,
            verification="primary-key-fingerprint",
            verification_identity=DOCKER_SIGNING_KEY_FINGERPRINT,
        )
        signing_key_path = self._artifact_cache_path_get(signing_key.url)
        self._docker_signing_key_validate(signing_key_path)

        inrelease_url = f"{DOCKER_APT_ROOT_URL}/dists/{DOCKER_CODENAME}/InRelease"
        inrelease = self._artifact_resolve(
            allow_cache=False,
            name="docker-inrelease",
            selector=f"{DOCKER_CODENAME}/{DOCKER_CHANNEL}",
            version=DOCKER_CODENAME,
            url=inrelease_url,
            verification="pgp",
            verification_identity=DOCKER_SIGNING_KEY_FINGERPRINT,
        )
        inrelease_path = self._artifact_cache_path_get(inrelease.url)
        self._docker_inrelease_signature_validate(
            inrelease_path=inrelease_path,
            signing_key_path=signing_key_path,
        )
        packages_relative_path, packages_sha256 = self._docker_packages_index_identity_get(
            architecture=architecture,
            inrelease_text=inrelease_path.read_text(encoding="utf-8"),
        )
        packages_index = self._artifact_resolve(
            expected_sha256=packages_sha256,
            name="docker-packages-index",
            selector=f"{DOCKER_CODENAME}/{DOCKER_CHANNEL}/{architecture}",
            version=DOCKER_CODENAME,
            url=f"{DOCKER_APT_ROOT_URL}/dists/{DOCKER_CODENAME}/{packages_relative_path}",
            verification="signed-metadata-sha256",
            verification_identity=inrelease.sha256,
        )
        packages_path = self._artifact_cache_path_get(packages_index.url)
        packages_bytes = packages_path.read_bytes()
        if packages_relative_path.endswith(".gz"):
            try:
                packages_bytes = gzip.decompress(packages_bytes)
            except gzip.BadGzipFile as error:
                raise HostArtifactResolutionError("Docker Packages index is not valid gzip") from error
        package_payload_list = self._debian_package_payload_list_get(packages_bytes.decode("utf-8"))

        result = {
            "docker-signing-key": signing_key,
            "docker-inrelease": inrelease,
            "docker-packages-index": packages_index,
        }
        for package_name in DOCKER_PACKAGE_NAME_LIST:
            package_payload = self._latest_debian_package_get(
                architecture=architecture,
                package_name=package_name,
                package_payload_list=package_payload_list,
            )
            filename = package_payload["Filename"]
            result[package_name] = self._artifact_resolve(
                expected_sha256=package_payload["SHA256"],
                name=package_name,
                selector=f"{DOCKER_CODENAME}/{DOCKER_CHANNEL}",
                version=package_payload["Version"],
                url=f"{DOCKER_APT_ROOT_URL}/{filename}",
                verification="signed-metadata-sha256",
                verification_identity=packages_index.sha256,
            )
        return result

    def _uv_python_resolve(
        self,
        *,
        python_architecture: str,
        uv_architecture: str,
    ) -> tuple[
        HostArtifactIdentity,
        HostArtifactIdentity,
        HostArtifactIdentity,
        str,
    ]:
        uv_version, resolved_ref, commit_sha = self._latest_tag_resolve(
            repository_url=UV_REPOSITORY_URL,
            selector=UV_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(0\.(\d+)\.(\d+))$"),
        )
        self._git_ref_commit_validate(
            repository_url=UV_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        uv_url = (
            "https://github.com/astral-sh/uv/releases/download/"
            f"{uv_version}/uv-{uv_architecture}-unknown-linux-gnu.tar.gz"
        )
        uv_artifact = self._artifact_resolve(
            name="uv",
            selector=UV_SELECTOR,
            version=uv_version,
            url=uv_url,
            verification="github-attestation",
            verification_identity=f"{UV_SIGNER_WORKFLOW}@{commit_sha}",
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._github_attestation_validate(
            artifact_path=self._artifact_cache_path_get(uv_artifact.url),
            repository="astral-sh/uv",
            signer_workflow=UV_SIGNER_WORKFLOW,
            source_commit_sha=commit_sha,
        )
        metadata_url = (
            "https://raw.githubusercontent.com/astral-sh/uv/" f"{commit_sha}/crates/uv-python/download-metadata.json"
        )
        metadata_artifact = self._artifact_resolve(
            name="uv-python-metadata",
            selector=f"uv@{UV_SELECTOR}",
            version=uv_version,
            url=metadata_url,
            verification="github-attested-source",
            verification_identity=f"{UV_SIGNER_WORKFLOW}@{commit_sha}",
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        metadata_path = self._artifact_cache_path_get(metadata_url)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise HostArtifactResolutionError("uv Python metadata is invalid JSON") from error
        python_payload = self._python_download_payload_get(
            architecture=python_architecture,
            metadata=metadata,
        )
        python_version = f"{python_payload['major']}." f"{python_payload['minor']}." f"{python_payload['patch']}"
        python_artifact = self._artifact_resolve(
            expected_sha256=python_payload["sha256"],
            name="python",
            selector=PYTHON_SELECTOR,
            version=python_version,
            url=python_payload["url"],
            verification="attested-metadata-sha256",
            verification_identity=metadata_artifact.sha256,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._git_ref_unchanged_validate(
            repository_url=UV_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return (
            uv_artifact,
            metadata_artifact,
            python_artifact,
            python_payload["build"],
        )

    def _k3s_resolve(
        self,
        architecture: str,
    ) -> dict[str, HostArtifactIdentity]:
        version, resolved_ref, commit_sha = self._latest_tag_resolve(
            repository_url=K3S_REPOSITORY_URL,
            selector=K3S_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(v1\.36\.(\d+)\+k3s(\d+))$"),
        )
        self._git_ref_commit_validate(
            repository_url=K3S_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        binary_name = "k3s" if architecture == "amd64" else "k3s-arm64"
        checksum_name = f"sha256sum-{architecture}.txt"
        trusted_sha256_by_name_map, trust_sha256 = self._k3s_trust_identity_get(
            binary_name=binary_name,
            checksum_name=checksum_name,
            commit_sha=commit_sha,
            resolved_ref=resolved_ref,
            version=version,
        )
        checksum_sha256 = self._github_release_asset_sha256_get(
            asset_name=checksum_name,
            repository="k3s-io/k3s",
            version=version,
        )
        if checksum_sha256 != trusted_sha256_by_name_map[checksum_name]:
            raise HostArtifactResolutionError(
                "k3s release checksum asset differs from the repository-owned trust record"
            )
        checksum_artifact = self._artifact_resolve(
            expected_sha256=trusted_sha256_by_name_map[checksum_name],
            name="k3s-checksums",
            selector=K3S_SELECTOR,
            version=version,
            url=("https://github.com/k3s-io/k3s/releases/download/" f"{version}/{checksum_name}"),
            verification="repository-trust+github-release-digest",
            verification_identity=trust_sha256,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        expected_binary_sha256 = self._checksum_file_sha256_get(
            artifact_name=binary_name,
            checksum_path=self._artifact_cache_path_get(checksum_artifact.url),
        )
        if expected_binary_sha256 != trusted_sha256_by_name_map[binary_name]:
            raise HostArtifactResolutionError("k3s vendor checksum differs from the repository-owned trust record")
        api_binary_sha256 = self._github_release_asset_sha256_get(
            asset_name=binary_name,
            repository="k3s-io/k3s",
            version=version,
        )
        if api_binary_sha256 != expected_binary_sha256:
            raise HostArtifactResolutionError("k3s vendor checksum and GitHub release digest differ")
        binary = self._artifact_resolve(
            expected_sha256=trusted_sha256_by_name_map[binary_name],
            name="k3s-binary",
            selector=K3S_SELECTOR,
            version=version,
            url=("https://github.com/k3s-io/k3s/releases/download/" f"{version}/{binary_name}"),
            verification="repository-trust+vendor-checksum+github-release-digest",
            verification_identity=trust_sha256,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._git_ref_unchanged_validate(
            repository_url=K3S_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return {
            "k3s-binary": binary,
            "k3s-checksums": checksum_artifact,
        }

    @staticmethod
    def _k3s_trust_identity_get(
        *,
        binary_name: str,
        checksum_name: str,
        commit_sha: str,
        resolved_ref: str,
        version: str,
    ) -> tuple[dict[str, str], str]:
        """Return one reviewed K3s release identity from repository-owned trust."""

        try:
            trust_bytes = _K3S_TRUST_PATH.read_bytes()
            payload = json.loads(trust_bytes)
        except (OSError, json.JSONDecodeError) as error:
            raise HostArtifactResolutionError("repository-owned K3s release trust record is unavailable") from error
        expected_field_name_set = {
            "artifact_sha256_by_name_map",
            "resolved_ref",
            "source_commit_sha",
            "version",
        }
        artifact_sha256_by_name_map = payload.get("artifact_sha256_by_name_map") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_field_name_set
            or payload.get("resolved_ref") != resolved_ref
            or payload.get("source_commit_sha") != commit_sha
            or payload.get("version") != version
            or not isinstance(artifact_sha256_by_name_map, dict)
            or set(artifact_sha256_by_name_map) != _K3S_TRUST_ARTIFACT_NAME_SET
            or binary_name not in artifact_sha256_by_name_map
            or checksum_name not in artifact_sha256_by_name_map
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in artifact_sha256_by_name_map.values()
            )
        ):
            raise HostArtifactResolutionError("latest K3s release is not accepted by the repository-owned trust record")
        return (
            dict(artifact_sha256_by_name_map),
            hashlib.sha256(trust_bytes).hexdigest(),
        )

    def _helm_resolve(
        self,
        architecture: str,
    ) -> tuple[HostArtifactIdentity, HostArtifactIdentity]:
        version, resolved_ref, commit_sha = self._latest_tag_resolve(
            repository_url=HELM_REPOSITORY_URL,
            selector=HELM_SELECTOR,
            tag_pattern=re.compile(r"refs/tags/(v4\.(\d+)\.(\d+))$"),
        )
        self._git_ref_commit_validate(
            repository_url=HELM_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        archive_name = f"helm-{version}-linux-{architecture}.tar.gz"
        signature = self._artifact_resolve(
            name="helm-signature",
            selector=HELM_SELECTOR,
            version=version,
            url=("https://github.com/helm/helm/releases/download/" f"{version}/{archive_name}.asc"),
            verification="detached-signature",
            verification_identity=HELM_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        artifact = self._artifact_resolve(
            name="helm",
            selector=HELM_SELECTOR,
            version=version,
            url=f"https://get.helm.sh/{archive_name}",
            verification="pgp",
            verification_identity=HELM_SIGNING_KEY_FINGERPRINT,
            resolved_ref=resolved_ref,
            source_commit_sha=commit_sha,
        )
        self._pgp_detached_signature_validate(
            artifact_path=self._artifact_cache_path_get(artifact.url),
            expected_primary_fingerprint=HELM_SIGNING_KEY_FINGERPRINT,
            key_path=_TRUST_ROOT_PATH / "helm-release.asc",
            signature_path=self._artifact_cache_path_get(signature.url),
        )
        self._git_ref_unchanged_validate(
            repository_url=HELM_REPOSITORY_URL,
            resolved_ref=resolved_ref,
            expected_commit_sha=commit_sha,
        )
        return artifact, signature

    def _latest_tag_resolve(
        self,
        *,
        repository_url: str,
        selector: str,
        tag_pattern: re.Pattern[str],
    ) -> tuple[str, str, str]:
        result = self._runner.run(["git", "ls-remote", "--tags", repository_url])
        commit_sha_by_ref_map = self._git_tag_commit_sha_by_ref_map_get(result.stdout)
        candidate_list: list[tuple[tuple[int, ...], str, str, str]] = []
        for resolved_ref, commit_sha in commit_sha_by_ref_map.items():
            match = tag_pattern.fullmatch(resolved_ref)
            if match is None:
                continue
            numeric_key = tuple(int(group) for group in match.groups()[1:])
            candidate_list.append((numeric_key, match.group(1), resolved_ref, commit_sha))
        if not candidate_list:
            raise HostArtifactResolutionError(f"no stable tag satisfies selector {selector} in {repository_url}")
        _, version, resolved_ref, commit_sha = max(candidate_list)
        return version, resolved_ref, commit_sha

    def _git_ref_commit_validate(
        self,
        *,
        repository_url: str,
        resolved_ref: str,
        expected_commit_sha: str,
    ) -> None:
        """Prove that one selected tag peels to the recorded commit object.

        ``ls-remote`` alone cannot distinguish a lightweight tag pointing to a
        commit from one pointing to a tree or blob. Release provenance is allowed
        to name ``source_commit_sha`` only after Git itself accepts ``^{commit}``
        for an exact shallow fetch of the selected ref.

        Args:
            repository_url: Exact upstream repository URL.
            resolved_ref: Exact selected tag ref.
            expected_commit_sha: Peeled SHA reported by ``ls-remote``.

        Raises:
            HostArtifactResolutionError: If the ref is not the expected commit.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            self._runner.run(["git", "init", "--quiet", temporary_directory])
            self._runner.run(
                [
                    "git",
                    "-C",
                    temporary_directory,
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--no-tags",
                    repository_url,
                    resolved_ref,
                ]
            )
            result = self._runner.run(
                [
                    "git",
                    "-C",
                    temporary_directory,
                    "rev-parse",
                    "--verify",
                    "FETCH_HEAD^{commit}",
                ],
                check=False,
            )
        if result.returncode != 0 or result.stdout.strip().lower() != expected_commit_sha:
            raise HostArtifactResolutionError(f"artifact ref does not resolve to expected commit: {resolved_ref}")

    def _git_ref_unchanged_validate(
        self,
        *,
        repository_url: str,
        resolved_ref: str,
        expected_commit_sha: str,
    ) -> None:
        result = self._runner.run(
            [
                "git",
                "ls-remote",
                "--tags",
                repository_url,
                resolved_ref,
                f"{resolved_ref}^{{}}",
            ]
        )
        commit_sha_by_ref_map = self._git_tag_commit_sha_by_ref_map_get(result.stdout)
        if commit_sha_by_ref_map.get(resolved_ref) != expected_commit_sha:
            raise HostArtifactResolutionError(f"moving artifact ref changed during resolution: {resolved_ref}")

    @staticmethod
    def _git_tag_commit_sha_by_ref_map_get(output: str) -> dict[str, str]:
        """Return each tag ref bound to its peeled commit identity.

        ``git ls-remote`` reports an annotated tag twice: the tag-object ref and
        a ``^{}`` ref peeled to the tagged object. A lightweight tag has only the
        ordinary ref. Release provenance must retain the peeled identity when it
        exists instead of mistaking an annotated tag-object SHA for a commit SHA.

        Args:
            output: Complete ``git ls-remote --tags`` output.

        Returns:
            Exact tag ref to peeled-or-lightweight object SHA mapping.
        """

        sha_by_ref_map: dict[str, str] = {}
        for line in output.splitlines():
            part_list = line.split()
            if len(part_list) != 2:
                continue
            sha, ref = part_list
            if re.fullmatch(r"[0-9a-f]{40}", sha) is None or not ref.startswith("refs/tags/"):
                continue
            previous_sha = sha_by_ref_map.setdefault(ref, sha)
            if previous_sha != sha:
                raise HostArtifactResolutionError(f"Git returned conflicting identities for {ref}")
        commit_sha_by_ref_map: dict[str, str] = {}
        for ref, sha in sha_by_ref_map.items():
            if ref.endswith("^{}"):
                continue
            commit_sha_by_ref_map[ref] = sha_by_ref_map.get(f"{ref}^{{}}", sha)
        return commit_sha_by_ref_map

    def _github_release_asset_sha256_get(
        self,
        *,
        asset_name: str,
        repository: str,
        version: str,
    ) -> str:
        """Return GitHub's immutable digest for one exact release asset."""

        encoded_version = urllib.parse.quote(version, safe="")
        url = f"https://api.github.com/repos/{repository}/releases/tags/" f"{encoded_version}"
        metadata_path = self._artifact_cache_path_get(url)
        self._artifact_download(
            allow_cache=False,
            artifact_path=metadata_path,
            expected_sha256="",
            url=url,
        )
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HostArtifactResolutionError("GitHub release metadata is malformed") from error
        asset_list = payload.get("assets") if isinstance(payload, dict) else None
        matching_asset_list = (
            [item for item in asset_list if isinstance(item, dict) and item.get("name") == asset_name]
            if isinstance(asset_list, list)
            else []
        )
        if len(matching_asset_list) != 1:
            raise HostArtifactResolutionError(f"GitHub release has no unique asset {asset_name}")
        digest = matching_asset_list[0].get("digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise HostArtifactResolutionError(f"GitHub release asset {asset_name} has no SHA-256 identity")
        return digest.removeprefix("sha256:")

    @staticmethod
    def _checksum_file_sha256_get(
        *,
        artifact_name: str,
        checksum_path: Path,
    ) -> str:
        """Return one unique SHA-256 entry from vendor checksum metadata."""

        try:
            line_list = checksum_path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise HostArtifactResolutionError("Vendor checksum metadata cannot be read") from error
        matching_sha256_list: list[str] = []
        for line in line_list:
            match = re.fullmatch(
                r"([0-9a-f]{64})[ \t]+(?:\*|)" + re.escape(artifact_name),
                line,
            )
            if match is not None:
                matching_sha256_list.append(match.group(1))
        if len(matching_sha256_list) != 1:
            raise HostArtifactResolutionError(f"Vendor checksum metadata has no unique {artifact_name} entry")
        return matching_sha256_list[0]

    def _github_attestation_validate(
        self,
        *,
        artifact_path: Path,
        repository: str,
        signer_workflow: str,
        source_commit_sha: str,
    ) -> None:
        """Verify one GitHub release artifact against its authorized workflow."""

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

    def _artifact_resolve(
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
        parsed_url = urllib.parse.urlparse(url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or _HOST_ARTIFACT_URL_PATTERN.fullmatch(url) is None
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
        artifact_path = self._artifact_cache_path_get(url)
        sha256, size = self._artifact_download(
            allow_cache=allow_cache,
            artifact_path=artifact_path,
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

    def _artifact_cache_path_get(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self._cache_root_path / key

    def _artifact_download(
        self,
        *,
        allow_cache: bool = True,
        artifact_path: Path,
        expected_sha256: str,
        url: str,
    ) -> tuple[str, int]:
        if allow_cache and artifact_path.is_file():
            sha256, size = self._file_identity_get(artifact_path)
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
                content_length_text = response.headers.get("Content-Length")
                declared_size: int | None = None
                if content_length_text is not None:
                    try:
                        declared_size = int(content_length_text)
                    except ValueError as error:
                        raise HostArtifactResolutionError("artifact response has an invalid Content-Length") from error
                    if declared_size < 0 or declared_size > _MAX_HOST_ARTIFACT_SIZE_BYTES:
                        raise HostArtifactResolutionError("artifact response exceeds the download size limit")
                with tempfile.NamedTemporaryFile(
                    dir=self._cache_root_path,
                    prefix=".download-",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _MAX_HOST_ARTIFACT_SIZE_BYTES:
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
            temporary_path = None
            return sha256, size
        except OSError as error:
            raise HostArtifactResolutionError(f"unable to download artifact {url}: {error}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _file_identity_get(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _docker_signing_key_validate(self, key_path: Path) -> None:
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
        primary_fingerprint_list = self._primary_key_fingerprint_list_get(result.stdout)
        if primary_fingerprint_list != [DOCKER_SIGNING_KEY_FINGERPRINT]:
            raise HostArtifactResolutionError("Docker signing keyring does not contain exactly the trusted primary key")

    @staticmethod
    def _primary_key_fingerprint_list_get(gpg_colon_output: str) -> list[str]:
        """Return fingerprints bound specifically to primary public-key records.

        A downloaded keyring may legitimately contain subkeys, but a second
        primary key would become another signature trust anchor for ``gpgv``.
        The parser therefore associates only the first fingerprint following
        each ``pub`` record and rejects malformed primary records.

        Args:
            gpg_colon_output: Machine-readable ``gpg --with-colons`` output.

        Returns:
            Ordered primary public-key fingerprints.

        Raises:
            HostArtifactResolutionError: If one primary key has no fingerprint.
        """

        primary_fingerprint_list: list[str] = []
        primary_fingerprint_is_pending = False
        for line in gpg_colon_output.splitlines():
            field_list = line.split(":")
            record_type = field_list[0] if field_list else ""
            if record_type == "pub":
                if primary_fingerprint_is_pending:
                    raise HostArtifactResolutionError("Docker signing keyring has a primary key without a fingerprint")
                primary_fingerprint_is_pending = True
                continue
            if record_type == "fpr" and primary_fingerprint_is_pending:
                if len(field_list) <= 9 or re.fullmatch(r"[0-9A-F]{40}", field_list[9]) is None:
                    raise HostArtifactResolutionError("Docker signing keyring has a malformed primary fingerprint")
                primary_fingerprint_list.append(field_list[9])
                primary_fingerprint_is_pending = False
        if primary_fingerprint_is_pending:
            raise HostArtifactResolutionError("Docker signing keyring has a primary key without a fingerprint")
        return primary_fingerprint_list

    def _pgp_detached_signature_validate(
        self,
        *,
        artifact_path: Path,
        expected_primary_fingerprint: str,
        key_path: Path,
        signature_path: Path,
    ) -> None:
        """Verify a detached signature under one repository-owned trust root."""

        if not key_path.is_file():
            raise HostArtifactResolutionError(f"Release signing trust root is unavailable: {key_path}")
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
            if self._primary_key_fingerprint_list_get(key_payload.stdout) != [expected_primary_fingerprint]:
                raise HostArtifactResolutionError("Release signing trust root has another primary fingerprint")
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

    def _docker_inrelease_signature_validate(
        self,
        *,
        inrelease_path: Path,
        signing_key_path: Path,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            keyring_path = Path(temporary_directory) / "docker.gpg"
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
    def _docker_packages_index_identity_get(
        *,
        architecture: str,
        inrelease_text: str,
    ) -> tuple[str, str]:
        section_match = re.search(
            r"(?:^|\n)SHA256:\n(?P<body>(?: [^\n]+\n)+)",
            inrelease_text,
        )
        if section_match is None:
            raise HostArtifactResolutionError("Docker InRelease has no SHA256 section")
        expected_path_set = {
            f"{DOCKER_CHANNEL}/binary-{architecture}/Packages.gz",
            f"{DOCKER_CHANNEL}/binary-{architecture}/Packages",
        }
        for line in section_match.group("body").splitlines():
            part_list = line.split()
            if len(part_list) != 3:
                continue
            sha256, _, relative_path = part_list
            if relative_path in expected_path_set and re.fullmatch(r"[0-9a-f]{64}", sha256):
                return relative_path, sha256
        raise HostArtifactResolutionError("Docker InRelease does not bind the target Packages index")

    @staticmethod
    def _debian_package_payload_list_get(
        packages_text: str,
    ) -> list[dict[str, str]]:
        payload_list: list[dict[str, str]] = []
        for paragraph in re.split(r"\n\s*\n", packages_text):
            payload: dict[str, str] = {}
            current_name = ""
            for line in paragraph.splitlines():
                if line.startswith((" ", "\t")) and current_name:
                    payload[current_name] += "\n" + line[1:]
                    continue
                name, separator, value = line.partition(":")
                if not separator:
                    continue
                current_name = name
                payload[name] = value.strip()
            if payload:
                payload_list.append(payload)
        return payload_list

    def _latest_debian_package_get(
        self,
        *,
        architecture: str,
        package_name: str,
        package_payload_list: list[dict[str, str]],
    ) -> dict[str, str]:
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
            raise HostArtifactResolutionError(f"Docker repository has no package {package_name} for {architecture}")
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

    @staticmethod
    def _python_download_payload_get(
        *,
        architecture: str,
        metadata: object,
    ) -> dict[str, object]:
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
            key=lambda payload: (
                int(payload["patch"]),
                str(payload["build"]),
            ),
        )


class HostArtifactResolutionError(RuntimeError):
    """Raised when a bootstrap artifact cannot become an immutable input."""


def _host_artifact_manifest_digest_validate(
    payload: object,
    *,
    expected_sha256: str,
) -> dict[str, object]:
    """Validate one decoded manifest against its canonical digest and shape."""

    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise HostArtifactResolutionError("host artifact manifest digest is invalid")
    canonical_bytes = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if hashlib.sha256(canonical_bytes).hexdigest() != expected_sha256:
        raise HostArtifactResolutionError("host artifact manifest differs from its digest")
    return host_artifact_manifest_validate(payload)


def host_artifact_manifest_validate(payload: object) -> dict[str, object]:
    """Validate the one exact current host-artifact manifest shape."""

    if not isinstance(payload, dict) or set(payload) != _HOST_ARTIFACT_MANIFEST_FIELD_NAME_SET:
        raise HostArtifactResolutionError("host artifact manifest does not have the exact current shape")
    architecture = payload.get("architecture")
    artifact_by_name_map = payload.get("artifact_by_name_map")
    if (
        architecture not in _ARCHITECTURE_BY_COMPUTE_ARCHITECTURE_MAP
        or not isinstance(artifact_by_name_map, dict)
        or set(artifact_by_name_map) != HOST_ARTIFACT_NAME_SET
    ):
        raise HostArtifactResolutionError("host artifact manifest payload is malformed")
    for artifact_name, artifact in artifact_by_name_map.items():
        artifact_field_name_set = set(artifact) if isinstance(artifact, dict) else set()
        expected_field_name_set = (
            _HOST_ARTIFACT_RESOLVED_IDENTITY_FIELD_NAME_SET
            if artifact_name in HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET
            else _HOST_ARTIFACT_IDENTITY_FIELD_NAME_SET
        )
        if not isinstance(artifact, dict) or artifact_field_name_set != expected_field_name_set:
            raise HostArtifactResolutionError(
                f"host artifact manifest {artifact_name} does not have the exact current shape"
            )
        if (
            artifact.get("name") != artifact_name
            or not isinstance(artifact.get("selector"), str)
            or not artifact["selector"]
            or not isinstance(artifact.get("version"), str)
            or not artifact["version"]
            or not isinstance(artifact.get("url"), str)
            or not artifact["url"].startswith("https://")
            or not isinstance(artifact.get("verification"), str)
            or re.fullmatch(
                r"[a-z0-9+_-]+",
                artifact["verification"],
            )
            is None
            or not isinstance(
                artifact.get("verification_identity"),
                str,
            )
            or not artifact["verification_identity"]
            or not isinstance(artifact.get("size"), int)
            or artifact["size"] <= 0
            or not isinstance(artifact.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
        ):
            raise HostArtifactResolutionError(f"host artifact manifest {artifact_name} identity is invalid")
        if artifact_name in HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET and (
            not isinstance(artifact.get("resolved_ref"), str)
            or not artifact["resolved_ref"]
            or not isinstance(artifact.get("source_commit_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", artifact["source_commit_sha"]) is None
        ):
            raise HostArtifactResolutionError(f"host artifact manifest {artifact_name} resolved identity is invalid")
    if payload.get("python_selector") != PYTHON_SELECTOR:
        raise HostArtifactResolutionError("host artifact manifest has another Python selector")
    if not isinstance(payload.get("python_build"), str) or re.fullmatch(r"[0-9]{8}", payload["python_build"]) is None:
        raise HostArtifactResolutionError("host artifact manifest has no exact Python build")
    if payload.get("docker_signing_key_fingerprint") != DOCKER_SIGNING_KEY_FINGERPRINT:
        raise HostArtifactResolutionError("host artifact manifest has another Docker signing trust anchor")
    return payload


def host_artifact_manifest_decode(
    *,
    encoded_manifest: str,
    expected_sha256: str,
) -> dict[str, object]:
    """Decode and validate one immutable manifest retained by CloudFormation.

    Args:
        encoded_manifest: Deterministic gzip/base64 manifest parameter.
        expected_sha256: Canonical uncompressed JSON digest.

    Returns:
        Decoded manifest payload.
    """

    try:
        payload_bytes = gzip.decompress(base64.b64decode(encoded_manifest, validate=True))
        payload = json.loads(payload_bytes)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise HostArtifactResolutionError("host artifact manifest parameter is malformed") from error
    return _host_artifact_manifest_digest_validate(
        payload,
        expected_sha256=expected_sha256,
    )


def host_artifact_manifest_json_decode(
    *,
    manifest_json: str,
    expected_sha256: str,
) -> dict[str, object]:
    """Decode and validate the canonical JSON installed on one host."""

    try:
        payload = json.loads(manifest_json)
    except json.JSONDecodeError as error:
        raise HostArtifactResolutionError("host artifact manifest JSON is malformed") from error
    return _host_artifact_manifest_digest_validate(
        payload,
        expected_sha256=expected_sha256,
    )
