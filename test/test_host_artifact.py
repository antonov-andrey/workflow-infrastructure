"""Verify immutable host bootstrap artifact resolution."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tool.lib.host_artifact import (
    DOCKER_SIGNING_KEY_FINGERPRINT,
    HOST_ARTIFACT_NAME_SET,
    HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET,
    HostArtifactIdentity,
    HostArtifactResolution,
    HostArtifactResolutionError,
    HostArtifactResolver,
    host_artifact_manifest_decode,
)


class RunnerFake:
    """Provide exact Git refs and Debian version comparison."""

    def __init__(self) -> None:
        """Initialize recorded commands."""

        self.command_list_list: list[list[str]] = []

    def run(
        self,
        command_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Return deterministic command output."""

        del input_text, should_capture
        self.command_list_list.append(command_list)
        if command_list[:3] == ["git", "ls-remote", "--tags"]:
            output = "\n".join(
                [
                    f"{'a' * 40}\trefs/tags/2.9.9",
                    f"{'b' * 40}\trefs/tags/2.10.0-rc1",
                    f"{'c' * 40}\trefs/tags/2.10.0",
                    f"{'e' * 40}\trefs/tags/2.10.0^{{}}",
                    f"{'d' * 40}\trefs/tags/3.0.0",
                ]
            )
            return subprocess.CompletedProcess(command_list, 0, output + "\n", "")
        if command_list[:2] == ["dpkg", "--compare-versions"]:
            left = tuple(int(value) for value in re.findall(r"\d+", command_list[2]))
            right = tuple(int(value) for value in re.findall(r"\d+", command_list[4]))
            returncode = 0 if left > right else 1
            if check and returncode:
                raise AssertionError("test runner received an unexpected checked comparison")
            return subprocess.CompletedProcess(command_list, returncode, "", "")
        raise AssertionError(f"unexpected command: {command_list}")


def _artifact_get(name: str) -> HostArtifactIdentity:
    """Return one canonical dummy artifact."""

    resolved_source_kwargs = (
        {
            "resolved_ref": "refs/tags/v1.2.3",
            "source_commit_sha": "a" * 40,
        }
        if name in HOST_ARTIFACT_RESOLVED_SOURCE_NAME_SET
        else {}
    )
    return HostArtifactIdentity(
        name=name,
        selector="stable",
        version="1.2.3",
        url=f"https://example.invalid/{name}",
        sha256=hashlib.sha256(name.encode()).hexdigest(),
        size=len(name),
        verification="test-proof",
        verification_identity=f"test:{name}",
        **resolved_source_kwargs,
    )


def test_latest_tag_resolution_uses_numeric_stable_version(
    tmp_path: Path,
) -> None:
    """Numeric stable selection must retain the commit peeled from an annotated tag."""

    runner = RunnerFake()
    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=runner,
    )

    assert resolver._latest_tag_resolve(
        repository_url="https://example.invalid/repository.git",
        selector="2",
        tag_pattern=re.compile(r"refs/tags/(2\.(\d+)\.(\d+))$"),
    ) == (
        "2.10.0",
        "refs/tags/2.10.0",
        "e" * 40,
    )
    resolver._git_ref_unchanged_validate(
        repository_url="https://example.invalid/repository.git",
        resolved_ref="refs/tags/2.10.0",
        expected_commit_sha="e" * 40,
    )
    assert runner.command_list_list[-1][-2:] == [
        "refs/tags/2.10.0",
        "refs/tags/2.10.0^{}",
    ]


@pytest.mark.parametrize(
    ("returncode", "actual_sha"),
    [
        (1, ""),
        (0, "f" * 40),
    ],
)
def test_tag_source_identity_requires_exact_commit_object(
    returncode: int,
    actual_sha: str,
    tmp_path: Path,
) -> None:
    """A tree/blob tag or a different fetched commit cannot enter provenance."""

    class GitObjectRunnerFake:
        """Model init, exact fetch, and Git commit peeling."""

        def __init__(self) -> None:
            """Initialize command recording."""

            self.command_list_list: list[list[str]] = []

        def run(
            self,
            command_list: list[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            should_capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            """Return the selected rev-parse result."""

            del input_text, should_capture
            self.command_list_list.append(command_list)
            if command_list[:3] == ["git", "init", "--quiet"]:
                return subprocess.CompletedProcess(command_list, 0, "", "")
            if len(command_list) > 3 and command_list[3] == "fetch":
                return subprocess.CompletedProcess(command_list, 0, "", "")
            if len(command_list) > 3 and command_list[3] == "rev-parse":
                if check and returncode:
                    raise AssertionError("rev-parse must be an unchecked proof")
                return subprocess.CompletedProcess(
                    command_list,
                    returncode,
                    f"{actual_sha}\n" if actual_sha else "",
                    "",
                )
            raise AssertionError(f"unexpected command: {command_list}")

    runner = GitObjectRunnerFake()
    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=runner,
    )

    with pytest.raises(
        HostArtifactResolutionError,
        match="does not resolve to expected commit",
    ):
        resolver._git_ref_commit_validate(
            repository_url="https://example.invalid/repository.git",
            resolved_ref="refs/tags/2.10.0",
            expected_commit_sha="e" * 40,
        )

    assert runner.command_list_list[1][-2:] == [
        "https://example.invalid/repository.git",
        "refs/tags/2.10.0",
    ]
    assert runner.command_list_list[2][-1] == "FETCH_HEAD^{commit}"


def test_tag_source_identity_accepts_exact_commit_object(tmp_path: Path) -> None:
    """An exact fetched commit is accepted without retaining a repository clone."""

    expected_sha = "e" * 40

    class GitObjectRunnerFake:
        """Return the exact commit for the selected tag."""

        def run(
            self,
            command_list: list[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            should_capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            """Model the bounded Git proof."""

            del check, input_text, should_capture
            if len(command_list) > 3 and command_list[3] == "rev-parse":
                return subprocess.CompletedProcess(
                    command_list,
                    0,
                    f"{expected_sha}\n",
                    "",
                )
            return subprocess.CompletedProcess(command_list, 0, "", "")

    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=GitObjectRunnerFake(),
    )

    resolver._git_ref_commit_validate(
        repository_url="https://example.invalid/repository.git",
        resolved_ref="refs/tags/2.10.0",
        expected_commit_sha=expected_sha,
    )


def test_manifest_round_trip_is_canonical_and_tamper_evident() -> None:
    """Stack-retained compact provenance must decode only under its exact digest."""

    resolution = HostArtifactResolution(
        architecture="arm64",
        artifact_by_name_map={name: _artifact_get(name) for name in HOST_ARTIFACT_NAME_SET},
        docker_signing_key_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
        python_build="20260718",
    )

    assert (
        host_artifact_manifest_decode(
            encoded_manifest=resolution.manifest_gzip_base64_get(),
            expected_sha256=resolution.manifest_sha256_get(),
        )
        == resolution.manifest_payload_get()
    )
    with pytest.raises(
        HostArtifactResolutionError,
        match="differs from its digest",
    ):
        host_artifact_manifest_decode(
            encoded_manifest=resolution.manifest_gzip_base64_get(),
            expected_sha256="f" * 64,
        )


def test_manifest_decode_rejects_any_noncurrent_shape() -> None:
    """The retained host contract never accepts unknown compatibility fields."""

    resolution = HostArtifactResolution(
        architecture="arm64",
        artifact_by_name_map={name: _artifact_get(name) for name in HOST_ARTIFACT_NAME_SET},
        docker_signing_key_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
        python_build="20260718",
    )
    payload = resolution.manifest_payload_get()
    payload["compatibility_selector"] = "3.14"
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded_manifest = base64.b64encode(gzip.compress(payload_bytes, mtime=0)).decode()

    with pytest.raises(HostArtifactResolutionError, match="exact current shape"):
        host_artifact_manifest_decode(
            encoded_manifest=encoded_manifest,
            expected_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        )


@pytest.mark.parametrize(
    ("artifact_name", "mutate"),
    [
        (
            "python",
            lambda artifact: (
                artifact.pop("resolved_ref"),
                artifact.pop("source_commit_sha"),
            ),
        ),
        (
            "docker-ce",
            lambda artifact: artifact.update(
                {
                    "resolved_ref": "refs/tags/v1.2.3",
                    "source_commit_sha": "a" * 40,
                }
            ),
        ),
    ],
)
def test_manifest_decode_requires_exact_provenance_shape_by_artifact_owner(
    artifact_name: str,
    mutate: Callable[[dict[str, object]], object],
) -> None:
    """Resolved-source provenance is mandatory only for its exact artifact owners."""

    resolution = HostArtifactResolution(
        architecture="arm64",
        artifact_by_name_map={name: _artifact_get(name) for name in HOST_ARTIFACT_NAME_SET},
        docker_signing_key_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
        python_build="20260718",
    )
    payload = resolution.manifest_payload_get()
    artifact = payload["artifact_by_name_map"][artifact_name]
    assert isinstance(artifact, dict)
    mutate(artifact)
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded_manifest = base64.b64encode(gzip.compress(payload_bytes, mtime=0)).decode()

    with pytest.raises(HostArtifactResolutionError, match="exact current shape"):
        host_artifact_manifest_decode(
            encoded_manifest=encoded_manifest,
            expected_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        )


def test_python_selector_chooses_latest_stable_patch_for_target_architecture() -> None:
    """The source keeps 3.14 while exact patch/build comes from exact uv metadata."""

    metadata = {
        "old": {
            "name": "cpython",
            "arch": {"family": "aarch64", "variant": None},
            "os": "linux",
            "libc": "gnu",
            "major": 3,
            "minor": 14,
            "patch": 5,
            "prerelease": "",
            "url": "https://example.invalid/python-3.14.5.tar.gz",
            "sha256": "a" * 64,
            "variant": None,
            "build": "20260701",
        },
        "current": {
            "name": "cpython",
            "arch": {"family": "aarch64", "variant": None},
            "os": "linux",
            "libc": "gnu",
            "major": 3,
            "minor": 14,
            "patch": 6,
            "prerelease": "",
            "url": "https://example.invalid/python-3.14.6.tar.gz",
            "sha256": "b" * 64,
            "variant": None,
            "build": "20260718",
        },
        "wrong_architecture": {
            "name": "cpython",
            "arch": {"family": "x86_64", "variant": None},
            "os": "linux",
            "libc": "gnu",
            "major": 3,
            "minor": 14,
            "patch": 7,
            "prerelease": "",
            "url": "https://example.invalid/python-3.14.7.tar.gz",
            "sha256": "c" * 64,
            "variant": None,
            "build": "20260720",
        },
    }

    payload = HostArtifactResolver._python_download_payload_get(
        architecture="aarch64",
        metadata=metadata,
    )

    assert payload["patch"] == 6
    assert payload["build"] == "20260718"


def test_k3s_release_requires_exact_repository_owned_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mutable release metadata cannot authorize one root-executed K3s binary."""

    trust_path = tmp_path / "k3s-release.json"
    trust_path.write_text(
        json.dumps(
            {
                "artifact_sha256_by_name_map": {
                    "k3s": "d" * 64,
                    "k3s-arm64": "a" * 64,
                    "sha256sum-amd64.txt": "e" * 64,
                    "sha256sum-arm64.txt": "b" * 64,
                },
                "resolved_ref": "refs/tags/v1.36.1+k3s1",
                "source_commit_sha": "c" * 40,
                "version": "v1.36.1+k3s1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("tool.lib.host_artifact._K3S_TRUST_PATH", trust_path)

    artifact_sha256_by_name_map, trust_sha256 = HostArtifactResolver._k3s_trust_identity_get(
        binary_name="k3s-arm64",
        checksum_name="sha256sum-arm64.txt",
        commit_sha="c" * 40,
        resolved_ref="refs/tags/v1.36.1+k3s1",
        version="v1.36.1+k3s1",
    )

    assert artifact_sha256_by_name_map == {
        "k3s": "d" * 64,
        "k3s-arm64": "a" * 64,
        "sha256sum-amd64.txt": "e" * 64,
        "sha256sum-arm64.txt": "b" * 64,
    }
    assert trust_sha256 == hashlib.sha256(trust_path.read_bytes()).hexdigest()

    with pytest.raises(
        HostArtifactResolutionError,
        match="not accepted",
    ):
        HostArtifactResolver._k3s_trust_identity_get(
            binary_name="k3s-arm64",
            checksum_name="sha256sum-arm64.txt",
            commit_sha="d" * 40,
            resolved_ref="refs/tags/v1.36.2+k3s1",
            version="v1.36.1+k3s1",
        )


def test_docker_signed_index_selects_exact_latest_packages(
    tmp_path: Path,
) -> None:
    """Signed metadata owns exact Docker package URL, version, and SHA selection."""

    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=RunnerFake(),
    )
    index_text = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA512\n\n"
        "SHA256:\n"
        f" {'a' * 64} 100 stable/binary-arm64/Packages.gz\n"
        f" {'b' * 64} 200 stable/binary-amd64/Packages.gz\n"
    )
    relative_path, sha256 = resolver._docker_packages_index_identity_get(
        architecture="arm64",
        inrelease_text=index_text,
    )
    assert relative_path == "stable/binary-arm64/Packages.gz"
    assert sha256 == "a" * 64

    package_payload_list = resolver._debian_package_payload_list_get(
        "\n\n".join(
            [
                (
                    "Package: docker-ce\n"
                    "Architecture: arm64\n"
                    "Version: 5:29.1.1-1~ubuntu.24.04~noble\n"
                    "Filename: dists/noble/pool/stable/arm64/docker-ce-old.deb\n"
                    f"SHA256: {'c' * 64}\n"
                ),
                (
                    "Package: docker-ce\n"
                    "Architecture: arm64\n"
                    "Version: 5:29.2.0-1~ubuntu.24.04~noble\n"
                    "Filename: dists/noble/pool/stable/arm64/docker-ce.deb\n"
                    f"SHA256: {'d' * 64}\n"
                ),
            ]
        )
    )
    selected = resolver._latest_debian_package_get(
        architecture="arm64",
        package_name="docker-ce",
        package_payload_list=package_payload_list,
    )
    assert selected["Version"] == "5:29.2.0-1~ubuntu.24.04~noble"
    assert selected["SHA256"] == "d" * 64


def test_docker_trust_anchor_excludes_additional_primary_keys() -> None:
    """Subkeys are valid, but a second primary key must not become trusted."""

    trusted_primary = DOCKER_SIGNING_KEY_FINGERPRINT
    additional_primary = "A" * 40
    one_primary_with_subkey = "\n".join(
        [
            "pub:-:4096:1:KEY::::::",
            f"fpr:::::::::{trusted_primary}:",
            "uid:-:::::::Docker Release:",
            "sub:-:4096:1:SUBKEY::::::",
            f"fpr:::::::::{'B' * 40}:",
        ]
    )
    two_primary_keys = "\n".join(
        [
            one_primary_with_subkey,
            "pub:-:4096:1:OTHER::::::",
            f"fpr:::::::::{additional_primary}:",
        ]
    )

    assert HostArtifactResolver._primary_key_fingerprint_list_get(one_primary_with_subkey) == [trusted_primary]
    assert HostArtifactResolver._primary_key_fingerprint_list_get(two_primary_keys) == [
        trusted_primary,
        additional_primary,
    ]


def test_artifact_url_rejects_shell_control_characters(tmp_path: Path) -> None:
    """Release metadata cannot inject commands into CloudFormation UserData."""

    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=RunnerFake(),
    )

    with pytest.raises(
        HostArtifactResolutionError,
        match="shell-safe absolute HTTPS URL",
    ):
        resolver._artifact_resolve(
            name="malicious",
            selector="stable",
            version="1.0.0",
            url="https://example.invalid/file'$(touch /tmp/injected)'",
            verification="test-proof",
            verification_identity="test",
        )


def test_artifact_download_verifies_stream_and_reuses_exact_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Downloaded bytes become cache-visible only after complete digest proof."""

    payload = b"verified artifact bytes"
    open_count = 0

    class ResponseFake(io.BytesIO):
        """Expose the urllib response context contract."""

        headers: dict[str, str] = {"Content-Length": str(len(payload))}

        def geturl(self) -> str:
            """Return an HTTPS final URL."""

            return "https://cdn.example.invalid/artifact"

    def urlopen(request: object, timeout: int) -> ResponseFake:
        """Return one deterministic byte stream."""

        nonlocal open_count
        del request
        assert timeout == 120
        open_count += 1
        return ResponseFake(payload)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=RunnerFake(),
    )
    artifact_path = tmp_path / "artifact"
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    first = resolver._artifact_download(
        artifact_path=artifact_path,
        expected_sha256=expected_sha256,
        url="https://example.invalid/artifact",
    )
    second = resolver._artifact_download(
        artifact_path=artifact_path,
        expected_sha256=expected_sha256,
        url="https://example.invalid/artifact",
    )

    assert first == second == (expected_sha256, len(payload))
    assert artifact_path.read_bytes() == payload
    assert open_count == 1


def test_artifact_download_refreshes_one_moving_metadata_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Moving signed metadata must not become permanently stale in local cache."""

    payload_list = [b"signed metadata 1", b"signed metadata 2"]

    class ResponseFake(io.BytesIO):
        """Expose the urllib response context contract."""

        headers: dict[str, str] = {}

        def geturl(self) -> str:
            """Return an HTTPS final URL."""

            return "https://cdn.example.invalid/InRelease"

    def urlopen(request: object, timeout: int) -> ResponseFake:
        """Return the next deterministic metadata snapshot."""

        del request
        assert timeout == 120
        return ResponseFake(payload_list.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=RunnerFake(),
    )
    artifact_path = tmp_path / "moving-metadata"

    first = resolver._artifact_download(
        allow_cache=False,
        artifact_path=artifact_path,
        expected_sha256="",
        url="https://example.invalid/InRelease",
    )
    second = resolver._artifact_download(
        allow_cache=False,
        artifact_path=artifact_path,
        expected_sha256="",
        url="https://example.invalid/InRelease",
    )

    assert first[0] != second[0]
    assert artifact_path.read_bytes() == b"signed metadata 2"
    assert payload_list == []


def test_artifact_download_rejects_oversized_response_before_cache_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A compromised artifact endpoint cannot fill operator storage without a bound."""

    payload = b"oversized"

    class ResponseFake(io.BytesIO):
        """Expose one response whose declared size exceeds the test bound."""

        headers: dict[str, str] = {"Content-Length": str(len(payload))}

        def geturl(self) -> str:
            """Return an HTTPS final URL."""

            return "https://cdn.example.invalid/oversized"

    monkeypatch.setattr("tool.lib.host_artifact._MAX_HOST_ARTIFACT_SIZE_BYTES", 8)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: ResponseFake(payload),
    )
    resolver = HostArtifactResolver(
        cache_root_path=tmp_path,
        runner=RunnerFake(),
    )
    artifact_path = tmp_path / "oversized"

    with pytest.raises(HostArtifactResolutionError, match="download size limit"):
        resolver._artifact_download(
            artifact_path=artifact_path,
            expected_sha256="",
            url="https://example.invalid/oversized",
        )

    assert not artifact_path.exists()


def test_cloudformation_parameters_bind_every_bootstrap_artifact() -> None:
    """Launch input must carry exact URLs, versions, digests, and full provenance."""

    resolution = HostArtifactResolution(
        architecture="arm64",
        artifact_by_name_map={name: _artifact_get(name) for name in HOST_ARTIFACT_NAME_SET},
        docker_signing_key_fingerprint=DOCKER_SIGNING_KEY_FINGERPRINT,
        python_build="20260718",
    )

    parameter_by_name_map = resolution.cloudformation_parameter_by_name_map_get()

    assert parameter_by_name_map["HostArtifactManifestSha256"] == (resolution.manifest_sha256_get())
    assert parameter_by_name_map["HostArtifactManifestGzipBase64"] == (resolution.manifest_gzip_base64_get())
    assert parameter_by_name_map["PythonBuild"] == "20260718"
    assert parameter_by_name_map["DockerSigningKeyFingerprint"] == (DOCKER_SIGNING_KEY_FINGERPRINT)
    assert parameter_by_name_map["AwsCliUrl"].endswith("/aws-cli")
    assert parameter_by_name_map["K3sBinarySha256"] == (resolution.artifact_by_name_map["k3s-binary"].sha256)
