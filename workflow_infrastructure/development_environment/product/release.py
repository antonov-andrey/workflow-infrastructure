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
PRODUCT_RELEASE_MANIFEST_VERSION = 3
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
        """Return the retained release collection root."""


class RetainedProductReleaseValidator:
    """Own exact source, manifest, image, and provenance validation."""

    def __init__(self, identity: RetainedProductReleaseIdentity) -> None:
        """Retain one environment identity whose releases may be accepted."""

        self._identity = identity

    def validate(
        self,
        release_root_path: Path,
    ) -> str:
        """Validate every persisted identity and tracked source byte of one Product release."""

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

    @staticmethod
    def _retained_product_image_graph_validate(
        *,
        image_payload_by_name_map: object,
        release_root_path: Path,
        source_identity_by_name_map: Mapping[str, Mapping[str, str]],
        target_platform: str,
    ) -> None:
        """Validate exact image bases, Buildx provenance, SBOM flags, and metadata."""

        expected_image_name_set = {
            "apwid-backend",
            "apwid-platform-test-bundle",
            "apwid-ui",
            "apwid-workflow-platform-base",
            "apwid-workflow-run-pause-agent",
            "browser-runtime",
            "vpn-runtime",
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


class RetainedProductReleaseHostIdentity(Protocol):
    """Host paths and identity required by retained Product release lifecycle."""

    environment_name: str
    host_current_source_path: Path
    host_product_recovery_marker_path: Path
    host_release_root_path: Path
    host_retained_current_release_path: Path
    host_retained_product_tool_path: Path
    host_retained_release_root_path: Path
    host_retained_rollback_release_path: Path


class RetainedProductReleaseCommandResult(Protocol):
    """Command result fields required by retained Product release lifecycle."""

    returncode: int


class RetainedProductReleaseCommandRunner(Protocol):
    """External command boundary required by retained Product release lifecycle."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> RetainedProductReleaseCommandResult:
        """Run one host command."""


class DevelopmentRetainedProductReleaseManager:
    """Own host-local Product release activation and recovery savepoints."""

    def __init__(
        self,
        *,
        host_artifact_manifest_get: Callable[[], dict[str, object]],
        identity: RetainedProductReleaseHostIdentity,
        is_host_get: Callable[[], bool],
        python_bytecode_environment_assignment: str,
        runner: RetainedProductReleaseCommandRunner,
        validator: RetainedProductReleaseValidator,
    ) -> None:
        """Bind retained release state to one exact development environment."""

        self._host_artifact_manifest_get = host_artifact_manifest_get
        self._identity = identity
        self._is_host_get = is_host_get
        self._python_bytecode_environment_assignment = python_bytecode_environment_assignment
        self._runner = runner
        self._validator = validator

    def current_product_tool_path_get(self) -> Path:
        """Return the current exact Product management-tool path."""

        return (
            self._identity.host_current_source_path
            / "sources"
            / "workflow-control-center"
            / "tool"
            / "development_kubernetes_manage.py"
        )

    def current_product_tool_command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one environment-bound command for the current Product tool.

        Args:
            command: Product management command.
            argument_list: Exact command-specific arguments.

        Returns:
            Complete environment-bound command.
        """

        return [
            "env",
            self._python_bytecode_environment_assignment,
            "python3.14",
            "-B",
            str(self.current_product_tool_path_get()),
            command,
            "--environment-name",
            self._identity.environment_name,
            *argument_list,
        ]

    def release_validate(self, release_root_path: Path) -> str:
        """Validate one retained release against the exact current contract."""

        return self._validator.validate(release_root_path)

    def recovery_status_get(self) -> str:
        """Return the local retained Product recovery state."""

        retained_current_path = self._identity.host_retained_current_release_path
        marker_path = self._identity.host_product_recovery_marker_path
        product_current_path = self._identity.host_current_source_path
        if not retained_current_path.is_symlink():
            if (
                retained_current_path.exists()
                or marker_path.exists()
                or marker_path.is_symlink()
                or product_current_path.exists()
                or product_current_path.is_symlink()
            ):
                raise DevelopmentEnvironmentError("Product recovery state exists without a retained current release")
            return "absent"

        release_root_path = self._current_release_path_get()
        marker_exists = self._recovery_marker_payload_validate(expected_release_name=release_root_path.name)
        try:
            current_link_is_exact = product_current_path.is_symlink() and os.readlink(product_current_path) == str(
                retained_current_path
            )
        except OSError as error:
            raise DevelopmentEnvironmentError("Product recovery current-source link is unavailable") from error
        return "pending" if marker_exists or not current_link_is_exact else "ready"

    def recovery_status_print(self) -> None:
        """Print whether retained Product recovery must be resumed."""

        self._host_only_validate("host-product-recovery-status")
        print(json.dumps({"status": self.recovery_status_get()}, sort_keys=True))

    def recovery_begin(self) -> None:
        """Persist the exact Product recovery savepoint before releasing the guard."""

        self._host_only_validate("host-product-recovery-begin")
        release_root_path = self._current_release_path_get()
        if self._recovery_marker_payload_validate(expected_release_name=release_root_path.name):
            print(f"OK: Product recovery savepoint for {release_root_path.name} " "already exists")
            return
        marker_path = self._identity.host_product_recovery_marker_path
        marker_text = (
            json.dumps(
                {
                    "environment_name": self._identity.environment_name,
                    "release": release_root_path.name,
                    "state": "pending",
                },
                sort_keys=True,
            )
            + "\n"
        )
        try:
            self._atomic_text_file_replace(
                mode=0o600,
                path=marker_path,
                text=marker_text,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError("Product recovery savepoint could not be persisted") from error
        self._runner.run(["sync", "-f", str(marker_path.parent)])
        print(f"OK: Product recovery savepoint for {release_root_path.name} is pending")

    def recovery_complete(self) -> None:
        """Clear the durable Product recovery savepoint after full acceptance."""

        self._host_only_validate("host-product-recovery-complete")
        release_root_path = self._current_release_path_get()
        if not self._recovery_marker_payload_validate(expected_release_name=release_root_path.name):
            raise DevelopmentEnvironmentError("Product recovery cannot complete without its pending savepoint")
        product_current_path = self._identity.host_current_source_path
        try:
            product_current_link_is_exact = product_current_path.is_symlink() and os.readlink(
                product_current_path
            ) == str(self._identity.host_retained_current_release_path)
        except OSError as error:
            raise DevelopmentEnvironmentError("Product recovery current-source link is unavailable") from error
        if not product_current_link_is_exact:
            raise DevelopmentEnvironmentError("Product recovery current-source link is not restored")
        try:
            self._identity.host_product_recovery_marker_path.unlink()
        except OSError as error:
            raise DevelopmentEnvironmentError("Product recovery savepoint could not be completed") from error
        self._runner.run(
            [
                "sync",
                "-f",
                str(self._identity.host_product_recovery_marker_path.parent),
            ]
        )
        print(f"OK: Product recovery savepoint for {release_root_path.name} is complete")

    def activate(self, release_name: str) -> None:
        """Validate and atomically activate one accepted retained Product release."""

        self._host_only_validate("host-product-release-activate")
        release_root_path = self._identity.host_release_root_path / release_name
        accepted_release_name = self._validator.validate(release_root_path)
        if accepted_release_name != release_name:
            raise DevelopmentEnvironmentError("Retained Product release activation changed exact identity")
        previous_release_root_path = self._previous_release_path_get()
        if previous_release_root_path is not None and previous_release_root_path != release_root_path:
            self._atomic_symlink_replace(
                link_path=self._identity.host_retained_rollback_release_path,
                target_path=previous_release_root_path,
            )
        elif previous_release_root_path is None:
            self._identity.host_retained_rollback_release_path.unlink(missing_ok=True)
        self._atomic_symlink_replace(
            link_path=self._identity.host_retained_current_release_path,
            target_path=release_root_path,
        )
        self._atomic_symlink_replace(
            link_path=self._identity.host_current_source_path,
            target_path=self._identity.host_retained_current_release_path,
        )
        print(f"OK: retained Product release {release_name} is current")

    def restore(self) -> None:
        """Validate snapshot-owned current release and restore its root-volume link."""

        self._host_only_validate("host-product-release-restore")
        release_root_path = self._current_release_path_get()
        release_name = self._validator.validate(release_root_path)
        self.release_host_identity_validate(release_root_path=release_root_path)
        self._atomic_symlink_replace(
            link_path=self._identity.host_current_source_path,
            target_path=self._identity.host_retained_current_release_path,
        )
        print(f"OK: retained Product release {release_name} root-volume link is " "restored")

    def reset(self, preserved_release_name: str) -> None:
        """Remove old retained Product state while preserving one exact candidate.

        Args:
            preserved_release_name: Exact candidate release that continues deployment.
        """

        self._host_only_validate("host-product-release-reset")
        release_owner_root_path = self._identity.host_retained_release_root_path
        product_tool_root_path = self._identity.host_retained_product_tool_path
        current_source_path = self._identity.host_current_source_path
        if (
            not release_owner_root_path.is_dir()
            or release_owner_root_path.is_symlink()
            or (
                product_tool_root_path.exists()
                and (not product_tool_root_path.is_dir() or product_tool_root_path.is_symlink())
            )
            or (
                (current_source_path.exists() or current_source_path.is_symlink())
                and not current_source_path.is_symlink()
            )
        ):
            raise DevelopmentEnvironmentError("Retained Product reset roots are malformed")

        allowed_entry_name_set = {
            ".operation.lock",
            "current",
            "recovery-pending.json",
            "releases",
            "rollback",
        }
        actual_entry_name_set = {path.name for path in release_owner_root_path.iterdir()}
        if not actual_entry_name_set <= allowed_entry_name_set:
            raise DevelopmentEnvironmentError("Retained Product release root contains an unexpected entry")
        for link_path in (
            self._identity.host_retained_current_release_path,
            self._identity.host_retained_rollback_release_path,
        ):
            if (link_path.exists() or link_path.is_symlink()) and not link_path.is_symlink():
                raise DevelopmentEnvironmentError("Retained Product release pointer is malformed")
        for file_path in (
            self._identity.host_product_recovery_marker_path,
            release_owner_root_path / ".operation.lock",
        ):
            if (file_path.exists() or file_path.is_symlink()) and (not file_path.is_file() or file_path.is_symlink()):
                raise DevelopmentEnvironmentError("Retained Product release state file is malformed")
        release_root_path = self._identity.host_release_root_path
        if (release_root_path.exists() or release_root_path.is_symlink()) and (
            not release_root_path.is_dir() or release_root_path.is_symlink()
        ):
            raise DevelopmentEnvironmentError("Retained Product release collection is malformed")
        preserved_release_root_path = release_root_path / preserved_release_name
        if (
            not preserved_release_name.isdigit()
            or len(preserved_release_name) != 20
            or not preserved_release_root_path.is_dir()
            or preserved_release_root_path.is_symlink()
        ):
            raise DevelopmentEnvironmentError("Retained Product reset candidate is malformed")

        current_source_path.unlink(missing_ok=True)
        self._identity.host_retained_current_release_path.unlink(missing_ok=True)
        self._identity.host_retained_rollback_release_path.unlink(missing_ok=True)
        self._identity.host_product_recovery_marker_path.unlink(missing_ok=True)
        (release_owner_root_path / ".operation.lock").unlink(missing_ok=True)
        for old_release_root_path in release_root_path.iterdir():
            if old_release_root_path == preserved_release_root_path:
                continue
            if not old_release_root_path.is_dir() or old_release_root_path.is_symlink():
                raise DevelopmentEnvironmentError("Retained Product release collection contains a malformed entry")
            shutil.rmtree(old_release_root_path)
        if product_tool_root_path.exists():
            shutil.rmtree(product_tool_root_path)
        self._runner.run(["sync", "-f", str(release_owner_root_path)])
        self._runner.run(["sync", "-f", str(current_source_path.parent)])
        if self.recovery_status_get() != "absent":
            raise DevelopmentEnvironmentError("Retained Product release reset did not reach absent state")
        print("OK: retained Product release and management runtime were reset")

    def release_host_identity_validate(self, *, release_root_path: Path) -> None:
        """Require the active host to match one byte-validated retained release."""

        try:
            source_manifest = json.loads((release_root_path / "source-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest is unavailable for host validation"
            ) from error
        retained_host_artifact_manifest = (
            source_manifest.get("host_artifact_manifest") if isinstance(source_manifest, Mapping) else None
        )
        if not isinstance(retained_host_artifact_manifest, Mapping):
            raise DevelopmentEnvironmentError("Retained Product host artifact manifest is malformed")
        if dict(retained_host_artifact_manifest) != (self._host_artifact_manifest_get()):
            raise DevelopmentEnvironmentError("Retained Product release has another exact host artifact identity")

    def _current_release_path_get(self) -> Path:
        """Return the exact retained current release or fail closed."""

        current_release_path = self._identity.host_retained_current_release_path
        if not current_release_path.is_symlink():
            raise DevelopmentEnvironmentError("Retained Product current-release link is unavailable")
        try:
            release_root_path = current_release_path.resolve(strict=True)
            current_release_target = os.readlink(current_release_path)
            release_collection_path = self._identity.host_release_root_path.resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError("Retained Product current-release link is broken") from error
        if current_release_target != str(release_root_path):
            raise DevelopmentEnvironmentError("Retained Product current-release link is not an exact absolute target")
        if (
            release_root_path.parent != release_collection_path
            or not release_root_path.name.isdigit()
            or len(release_root_path.name) != 20
        ):
            raise DevelopmentEnvironmentError("Retained Product current-release link has an invalid exact identity")
        return release_root_path

    def _previous_release_path_get(self) -> Path | None:
        """Return the exact current-contract predecessor for rollback."""

        current_link_path = self._identity.host_retained_current_release_path
        if not current_link_path.exists() and not current_link_path.is_symlink():
            return None
        if not current_link_path.is_symlink():
            raise DevelopmentEnvironmentError("Retained Product current release pointer is not a symlink")
        try:
            release_root_path = current_link_path.resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Retained Product current release cannot become a rollback root"
            ) from error
        self._validator.validate(release_root_path)
        return release_root_path

    def _recovery_marker_payload_validate(
        self,
        *,
        expected_release_name: str,
    ) -> bool:
        """Validate an optional retained recovery marker."""

        marker_path = self._identity.host_product_recovery_marker_path
        if not marker_path.parent.is_dir() or marker_path.parent.is_symlink():
            raise DevelopmentEnvironmentError("Retained Product recovery marker parent is invalid")
        if marker_path.is_symlink():
            raise DevelopmentEnvironmentError("Retained Product recovery marker must not be a symlink")
        if not marker_path.exists():
            return False
        if not marker_path.is_file():
            raise DevelopmentEnvironmentError("Retained Product recovery marker is not a regular file")
        try:
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError("Retained Product recovery marker is unavailable or malformed") from error
        expected_payload = {
            "environment_name": self._identity.environment_name,
            "release": expected_release_name,
            "state": "pending",
        }
        if marker_payload != expected_payload:
            raise DevelopmentEnvironmentError("Retained Product recovery marker has an inconsistent identity")
        return True

    def _host_only_validate(self, operation: str) -> None:
        """Reject host-local lifecycle operations from an operator checkout."""

        if not self._is_host_get():
            raise DevelopmentEnvironmentError(f"{operation} is supported only on the development host")

    @staticmethod
    def _atomic_symlink_replace(*, link_path: Path, target_path: Path) -> None:
        """Atomically replace one host symlink without a missing-current gap."""

        link_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary_link_path = link_path.with_name(f".{link_path.name}.tmp-{os.getpid()}")
        temporary_link_path.unlink(missing_ok=True)
        temporary_link_path.symlink_to(target_path)
        try:
            os.replace(temporary_link_path, link_path)
        finally:
            temporary_link_path.unlink(missing_ok=True)

    @staticmethod
    def _atomic_text_file_replace(*, mode: int, path: Path, text: str) -> None:
        """Durably replace one small host-owned text file."""

        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=path.parent,
                encoding="utf-8",
                prefix=f".{path.name}.tmp-",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                os.fchmod(file.fileno(), mode)
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            if temporary_path is None:
                raise OSError("temporary file was not created")
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
