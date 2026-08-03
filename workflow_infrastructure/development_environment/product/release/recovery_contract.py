"""Validate immutable retained Product releases before activation or recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.release.candidate import (
    RetainedProductCandidateLedgerValidator,
)

MOVING_SOURCE_SELECTOR = "HEAD"
PRODUCT_SOURCE_REPOSITORY_NAME_LIST = [
    "browser-runtime",
    "vpn-runtime",
    "workflow-container-runtime",
    "workflow-control-center",
]
REPOSITORY_URL_BY_NAME_MAP = {
    "browser-runtime": "git@github.com:antonov-andrey/browser-runtime.git",
    "vpn-runtime": "git@github.com:antonov-andrey/vpn-runtime.git",
    "workflow-container-contract": "git@github.com:antonov-andrey/workflow-container-contract.git",
    "workflow-container-runtime": "git@github.com:antonov-andrey/workflow-container-runtime.git",
    "workflow-control-center": "git@github.com:antonov-andrey/workflow-control-center.git",
    "workflow-infrastructure": "git@github.com:antonov-andrey/workflow-infrastructure.git",
}
SOURCE_MANIFEST_VERSION = 4
PRODUCT_RELEASE_MANIFEST_VERSION = 4
SOURCE_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "environment_name",
        "host_artifact_manifest",
        "python_bytecode_write_disabled",
        "release",
        "repository_by_name_map",
        "source_manifest_version",
        "t_deploy",
    }
)
SOURCE_REPOSITORY_FIELD_NAME_SET = frozenset(
    {
        "archive_sha256",
        "commit_sha",
        "file_sha256_by_path_map",
        "repository_url",
        "source_kind",
        "submodule_by_path_map",
    }
)
MOVING_SOURCE_REPOSITORY_FIELD_NAME_SET = frozenset(
    {
        *SOURCE_REPOSITORY_FIELD_NAME_SET,
        "package_version",
        "requested_selector",
        "resolved_ref",
    }
)
PRODUCT_RELEASE_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "environment_name",
        "helm_chart_by_name_map",
        "host_artifact_manifest",
        "image_by_name_map",
        "ingress_manifest",
        "release",
        "release_manifest_version",
        "render_sha256",
        "source_by_name_map",
        "source_manifest_sha256",
        "t_deploy",
        "target_platform",
        "ui_http_security_policy",
    }
)


class RetainedProductReleaseIdentity(Protocol):
    """Identity fields required by retained Product release validation."""

    environment_name: str

    @property
    def host_release_root_path(self) -> Path:
        """Return the retained release collection root.

        Returns:
            The retained release collection root.
        """


class RetainedProductReleaseValidator:
    """Own exact source, manifest, image, and provenance validation."""

    def __init__(self, identity: RetainedProductReleaseIdentity) -> None:
        """Retain one environment identity whose releases may be accepted.

        Args:
            identity: Identity.
        """

        self._identity = identity
        self._candidate_ledger_validator = RetainedProductCandidateLedgerValidator()

    def validate(
        self,
        release_root_path: Path,
    ) -> str:
        """Validate every persisted identity and tracked source byte of one Product release.

        Args:
            release_root_path: Exact filesystem path for release root.

        Returns:
            Resulting text value.
        """

        try:
            resolved_release_root_path = release_root_path.resolve(strict=True)
            resolved_release_parent_path = self._identity.host_release_root_path.resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError("Retained Product release path is unavailable") from error
        release_name = resolved_release_root_path.name
        if (
            resolved_release_root_path.parent != resolved_release_parent_path
            or not release_name.isdigit()
            or len(release_name) != 20
        ):
            raise DevelopmentEnvironmentError("Retained Product release path has an invalid exact identity")
        source_manifest_path = resolved_release_root_path / "source-manifest.json"
        product_manifest_path = resolved_release_root_path / "release-manifest.json"
        render_path = resolved_release_root_path / "render.yaml"
        try:
            source_manifest_bytes = source_manifest_path.read_bytes()
            source_manifest = json.loads(source_manifest_bytes)
            product_manifest = json.loads(product_manifest_path.read_text(encoding="utf-8"))
            render_bytes = render_path.read_bytes()
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product release manifests are unavailable or malformed"
            ) from error
        if (
            not isinstance(source_manifest, Mapping)
            or not isinstance(product_manifest, Mapping)
            or source_manifest.get("release") != release_name
            or product_manifest.get("release") != release_name
        ):
            raise DevelopmentEnvironmentError("Retained Product release manifests have inconsistent identities")
        source_manifest_version = source_manifest.get("source_manifest_version")
        if source_manifest_version != SOURCE_MANIFEST_VERSION:
            raise DevelopmentEnvironmentError("Retained Product source manifest is not the current version")
        if product_manifest.get("release_manifest_version") != (PRODUCT_RELEASE_MANIFEST_VERSION):
            raise DevelopmentEnvironmentError("Retained Product release manifest is not the current version")
        if set(source_manifest) != SOURCE_MANIFEST_FIELD_NAME_SET:
            raise DevelopmentEnvironmentError("Retained Product source manifest does not have the exact current shape")
        if set(product_manifest) != PRODUCT_RELEASE_MANIFEST_FIELD_NAME_SET:
            raise DevelopmentEnvironmentError("Retained Product release manifest does not have the exact current shape")
        if source_manifest.get("python_bytecode_write_disabled") is not True:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest does not prohibit Python bytecode writes"
            )
        if source_manifest.get("environment_name") != self._identity.environment_name:
            raise DevelopmentEnvironmentError("Retained Product source manifest belongs to another environment")
        if product_manifest.get("environment_name") != self._identity.environment_name:
            raise DevelopmentEnvironmentError("Retained Product release manifest belongs to another environment")
        source_host_artifact_manifest = source_manifest.get("host_artifact_manifest")
        if (
            not isinstance(source_host_artifact_manifest, Mapping)
            or product_manifest.get("host_artifact_manifest") != source_host_artifact_manifest
        ):
            raise DevelopmentEnvironmentError("Retained Product manifests describe different host artifacts")
        if (
            product_manifest.get("source_manifest_sha256") != hashlib.sha256(source_manifest_bytes).hexdigest()
            or product_manifest.get("render_sha256") != hashlib.sha256(render_bytes).hexdigest()
        ):
            raise DevelopmentEnvironmentError("Retained Product release manifest digests are inconsistent")
        target_platform = product_manifest.get("target_platform")
        if target_platform not in {"linux/amd64", "linux/arm64"}:
            raise DevelopmentEnvironmentError("Retained Product release target platform is invalid")

        repository_by_name_map = source_manifest.get("repository_by_name_map")
        required_repository_name_set = {
            "workflow-infrastructure",
            *PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
            "workflow-container-contract",
        }
        if not isinstance(repository_by_name_map, Mapping) or set(repository_by_name_map) != (
            required_repository_name_set
        ):
            raise DevelopmentEnvironmentError("Retained Product source graph is incomplete")
        source_identity_by_name_map: dict[str, dict[str, str]] = {}
        source_root_path = resolved_release_root_path / "sources"
        for repository_name, repository_payload in repository_by_name_map.items():
            if not isinstance(repository_name, str) or not isinstance(repository_payload, Mapping):
                raise DevelopmentEnvironmentError("Retained Product source entry is malformed")
            source_identity: dict[str, str] = {}
            for field_name, expected_length in (
                ("archive_sha256", 64),
                ("commit_sha", 40),
                ("repository_url", 0),
            ):
                field_value = repository_payload.get(field_name)
                if not isinstance(field_value, str) or not field_value:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} {field_name} is invalid"
                    )
                if expected_length:
                    if len(field_value) != expected_length or field_value != field_value.lower():
                        raise DevelopmentEnvironmentError(
                            f"Retained Product source {repository_name} {field_name} is invalid"
                        )
                    try:
                        int(field_value, 16)
                    except ValueError as error:
                        raise DevelopmentEnvironmentError(
                            f"Retained Product source {repository_name} {field_name} is invalid"
                        ) from error
                source_identity[field_name] = field_value
            if source_identity["repository_url"] != REPOSITORY_URL_BY_NAME_MAP[repository_name]:
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} repository URL is invalid"
                )
            source_kind = repository_payload.get("source_kind")
            expected_source_kind = (
                "resolved_moving_source" if repository_name == "workflow-container-contract" else "exact_checkout"
            )
            if source_kind != expected_source_kind:
                raise DevelopmentEnvironmentError(f"Retained Product source {repository_name} source kind is invalid")
            expected_field_name_set = (
                MOVING_SOURCE_REPOSITORY_FIELD_NAME_SET
                if expected_source_kind == "resolved_moving_source"
                else SOURCE_REPOSITORY_FIELD_NAME_SET
            )
            actual_field_name_set = set(repository_payload)
            override_field_name_set = {"override_identity", "override_reason"}
            if actual_field_name_set != expected_field_name_set and actual_field_name_set != (
                expected_field_name_set | override_field_name_set
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} does not have the exact current shape"
                )
            if expected_source_kind == "exact_checkout" and actual_field_name_set != expected_field_name_set:
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} does not have the exact current shape"
                )
            source_identity["source_kind"] = expected_source_kind
            moving_field_name_set = {
                "override_identity",
                "override_reason",
                "package_version",
                "requested_selector",
                "resolved_ref",
            }
            submodule_by_path_map = repository_payload.get("submodule_by_path_map")
            if not isinstance(submodule_by_path_map, Mapping):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} submodule graph is invalid"
                )
            for submodule_path_text, submodule_payload in submodule_by_path_map.items():
                if (
                    not isinstance(submodule_path_text, str)
                    or not isinstance(submodule_payload, Mapping)
                    or set(submodule_payload) != {"commit_sha", "repository_url"}
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} submodule entry is invalid"
                    )
                submodule_path = PurePosixPath(submodule_path_text)
                submodule_commit_sha = submodule_payload.get("commit_sha")
                submodule_repository_url = submodule_payload.get("repository_url")
                if (
                    not submodule_path_text
                    or submodule_path.is_absolute()
                    or submodule_path.as_posix() != submodule_path_text
                    or any(part in {"", ".", ".."} for part in submodule_path.parts)
                    or not isinstance(submodule_commit_sha, str)
                    or re.fullmatch(r"[0-9a-f]{40}", submodule_commit_sha) is None
                    or not isinstance(submodule_repository_url, str)
                    or not submodule_repository_url
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} submodule identity is invalid"
                    )
            if expected_source_kind == "resolved_moving_source":
                if submodule_by_path_map != {}:
                    raise DevelopmentEnvironmentError(
                        "Retained workflow-container-contract moving source has submodules"
                    )
                for field_name in (
                    "package_version",
                    "requested_selector",
                    "resolved_ref",
                ):
                    field_value = repository_payload.get(field_name)
                    if not isinstance(field_value, str) or not field_value:
                        raise DevelopmentEnvironmentError(
                            "Retained workflow-container-contract " f"{field_name} is invalid"
                        )
                    source_identity[field_name] = field_value
                if source_identity["requested_selector"] != MOVING_SOURCE_SELECTOR:
                    raise DevelopmentEnvironmentError("Retained workflow-container-contract selector is invalid")
                if not source_identity["resolved_ref"].startswith("refs/heads/"):
                    raise DevelopmentEnvironmentError("Retained workflow-container-contract ref is invalid")
                override_identity = repository_payload.get("override_identity")
                override_reason = repository_payload.get("override_reason")
                if override_identity is None and override_reason is None:
                    pass
                elif (
                    isinstance(override_identity, str)
                    and override_identity == source_identity["commit_sha"]
                    and isinstance(override_reason, str)
                    and bool(override_reason)
                ):
                    source_identity["override_identity"] = override_identity
                    source_identity["override_reason"] = override_reason
                else:
                    raise DevelopmentEnvironmentError(
                        "Retained workflow-container-contract override provenance is invalid"
                    )
            elif any(field_name in repository_payload for field_name in moving_field_name_set):
                raise DevelopmentEnvironmentError(f"Retained exact source {repository_name} has moving provenance")
            file_sha256_by_path_map = repository_payload.get("file_sha256_by_path_map")
            if not isinstance(file_sha256_by_path_map, Mapping):
                raise DevelopmentEnvironmentError(f"Retained Product source {repository_name} file graph is invalid")
            repository_root_path = source_root_path / repository_name
            if not repository_root_path.is_dir() or repository_root_path.is_symlink():
                raise DevelopmentEnvironmentError(f"Retained Product source root is unavailable: {repository_name}")
            expected_file_sha256_by_path_map: dict[str, str] = {}
            for relative_path_text, expected_sha256 in file_sha256_by_path_map.items():
                if (
                    not isinstance(relative_path_text, str)
                    or not relative_path_text
                    or not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                    or expected_sha256 != expected_sha256.lower()
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} file identity is invalid"
                    )
                try:
                    int(expected_sha256, 16)
                except ValueError as error:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} file identity is invalid"
                    ) from error
                relative_path = PurePosixPath(relative_path_text)
                if (
                    relative_path.is_absolute()
                    or not relative_path.parts
                    or relative_path.as_posix() != relative_path_text
                    or any(part in {"", ".", ".."} for part in relative_path.parts)
                ):
                    raise DevelopmentEnvironmentError(f"Retained Product source {repository_name} path is unsafe")
                expected_file_sha256_by_path_map[relative_path_text] = expected_sha256
            actual_file_sha256_by_path_map: dict[str, str] = {}
            for source_path in repository_root_path.rglob("*"):
                try:
                    if source_path.is_symlink():
                        source_payload = os.readlink(source_path).encode()
                    elif source_path.is_file():
                        source_payload = source_path.read_bytes()
                    elif source_path.is_dir():
                        continue
                    else:
                        raise DevelopmentEnvironmentError(
                            "Retained Product source contains an unsupported filesystem "
                            f"entry: {repository_name}/"
                            f"{source_path.relative_to(repository_root_path).as_posix()}"
                        )
                except OSError as error:
                    raise DevelopmentEnvironmentError(
                        "Retained Product source file is unavailable: "
                        f"{repository_name}/"
                        f"{source_path.relative_to(repository_root_path).as_posix()}"
                    ) from error
                actual_file_sha256_by_path_map[source_path.relative_to(repository_root_path).as_posix()] = (
                    hashlib.sha256(source_payload).hexdigest()
                )
            if actual_file_sha256_by_path_map != expected_file_sha256_by_path_map:
                raise DevelopmentEnvironmentError(f"Retained Product source file graph differs: {repository_name}")
            source_identity_by_name_map[repository_name] = source_identity
        if product_manifest.get("source_by_name_map") != source_identity_by_name_map:
            raise DevelopmentEnvironmentError(
                "Retained Product and source manifests describe different source identities"
            )
        self._retained_product_image_graph_validate(
            image_payload_by_name_map=product_manifest.get("image_by_name_map"),
            release_root_path=resolved_release_root_path,
            source_identity_by_name_map=source_identity_by_name_map,
            target_platform=target_platform,
        )
        return release_name

    def _retained_product_image_graph_validate(
        self,
        *,
        image_payload_by_name_map: object,
        release_root_path: Path,
        source_identity_by_name_map: Mapping[str, Mapping[str, str]],
        target_platform: str,
    ) -> None:
        """Validate exact image bases, Buildx provenance, SBOM flags, and metadata.

        Args:
            image_payload_by_name_map: Image payload by name mapping.
            release_root_path: Exact filesystem path for release root.
            source_identity_by_name_map: Source identity by name mapping.
            target_platform: Target platform.
        """

        expected_image_name_set = {
            "apwid-backend",
            "apwid-platform-test-bundle",
            "apwid-ui",
            "apwid-workflow-platform-base",
            "browser-runtime",
            "vpn-runtime",
            "vpn-runtime-stable-proxy",
        }
        contract_consumer_name_set = {
            "apwid-backend",
            "apwid-workflow-platform-base",
            "browser-runtime",
        }
        if not isinstance(image_payload_by_name_map, Mapping) or set(image_payload_by_name_map) != (
            expected_image_name_set
        ):
            raise DevelopmentEnvironmentError("Retained Product release image graph is incomplete")
        contract_source_identity = source_identity_by_name_map.get("workflow-container-contract")
        for logical_name, image_payload in image_payload_by_name_map.items():
            if not isinstance(logical_name, str) or not isinstance(image_payload, Mapping):
                raise DevelopmentEnvironmentError("Retained Product release image entry is malformed")
            expected_field_name_set = {
                "base_image_by_name_map",
                "build_metadata_path",
                "build_metadata_sha256",
                "candidate_ledger_path",
                "candidate_ledger_sha256",
                "digest",
                "has_sbom",
                "provenance_mode",
                "pull_reference",
                "target_platform",
            }
            if logical_name in contract_consumer_name_set:
                expected_field_name_set.add("source_by_name_map")
            if set(image_payload) != expected_field_name_set:
                raise DevelopmentEnvironmentError(
                    f"Retained Product image {logical_name} does not have the exact current shape"
                )
            digest = image_payload.get("digest")
            build_metadata_sha256 = image_payload.get("build_metadata_sha256")
            build_metadata_relative_path = image_payload.get("build_metadata_path")
            candidate_ledger_sha256 = image_payload.get("candidate_ledger_sha256")
            candidate_ledger_relative_path = image_payload.get("candidate_ledger_path")
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or image_payload.get("pull_reference") != f"localhost:30500/{logical_name}@{digest}"
                or image_payload.get("target_platform") != target_platform
                or image_payload.get("has_sbom") is not True
                or image_payload.get("provenance_mode") != "max"
                or not isinstance(build_metadata_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", build_metadata_sha256) is None
                or build_metadata_relative_path != f"image-build-metadata/{logical_name}.json"
                or not isinstance(candidate_ledger_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", candidate_ledger_sha256) is None
                or candidate_ledger_relative_path != f"image-candidate/{logical_name}/candidate-ledger.json"
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product image {logical_name} identity or build provenance is invalid"
                )
            base_image_by_name_map = image_payload.get("base_image_by_name_map")
            if not isinstance(base_image_by_name_map, Mapping) or not base_image_by_name_map:
                raise DevelopmentEnvironmentError(f"Retained Product image {logical_name} base graph is malformed")
            for base_name, base_identity in base_image_by_name_map.items():
                if (
                    not isinstance(base_name, str)
                    or not base_name
                    or not isinstance(base_identity, Mapping)
                    or set(base_identity) != {"digest", "pull_reference", "selector"}
                    or not isinstance(base_identity.get("digest"), str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", base_identity["digest"]) is None
                    or not isinstance(base_identity.get("pull_reference"), str)
                    or not base_identity["pull_reference"].endswith(f"@{base_identity['digest']}")
                    or not isinstance(base_identity.get("selector"), str)
                    or not base_identity["selector"]
                ):
                    raise DevelopmentEnvironmentError(f"Retained Product image {logical_name} base identity is invalid")
            image_source_by_name_map = image_payload.get("source_by_name_map")
            if logical_name in contract_consumer_name_set:
                if image_source_by_name_map != {
                    "workflow-container-contract": contract_source_identity,
                }:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product image {logical_name} has another contract source identity"
                    )
            elif image_source_by_name_map is not None:
                raise DevelopmentEnvironmentError(
                    f"Retained Product image {logical_name} has an undeclared source dependency"
                )
            metadata_path = release_root_path / str(build_metadata_relative_path)
            try:
                metadata_bytes = metadata_path.read_bytes()
                metadata_payload = json.loads(metadata_bytes)
            except (OSError, json.JSONDecodeError) as error:
                raise DevelopmentEnvironmentError(
                    f"Retained Product image {logical_name} build metadata is unavailable or malformed"
                ) from error
            if (
                hashlib.sha256(metadata_bytes).hexdigest() != build_metadata_sha256
                or not isinstance(metadata_payload, Mapping)
                or metadata_payload.get("containerimage.digest") != digest
                or not isinstance(metadata_payload.get("buildx.build.provenance"), Mapping)
            ):
                raise DevelopmentEnvironmentError(f"Retained Product image {logical_name} build metadata differs")
            self._candidate_ledger_validator.validate(
                expected_ledger_sha256=candidate_ledger_sha256,
                expected_root_digest=digest,
                logical_name=logical_name,
                release_root_path=release_root_path,
                target_platform=target_platform,
            )
