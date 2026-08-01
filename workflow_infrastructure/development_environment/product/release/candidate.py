"""Validate retained Product OCI candidate ledgers without owning publication."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class RetainedProductCandidateLedgerValidator:
    """Prove one published release-local candidate graph and optional archive."""

    def validate(
        self,
        *,
        expected_ledger_sha256: str,
        expected_root_digest: str,
        logical_name: str,
        release_root_path: Path,
        target_platform: str,
    ) -> None:
        """Validate exact ledger bytes, graph closure, progress, and retained bytes."""

        relative_path = Path("image-candidate") / logical_name / "candidate-ledger.json"
        ledger_path = release_root_path / relative_path
        try:
            if (
                ledger_path.is_symlink()
                or ledger_path.resolve(strict=True).parent
                != (release_root_path / "image-candidate" / logical_name).resolve(
                    strict=True
                )
            ):
                raise OSError("unsafe ledger path")
            ledger_bytes = ledger_path.read_bytes()
            ledger = json.loads(ledger_bytes)
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} candidate ledger is unavailable"
            ) from error
        if hashlib.sha256(ledger_bytes).hexdigest() != expected_ledger_sha256:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} candidate ledger differs"
            )
        if not isinstance(ledger, Mapping) or set(ledger) != {
            "archive",
            "candidate_identity",
            "completed_blob_digest_list",
            "completed_manifest_digest_list",
            "deleted_manifest_digest_list",
            "deletion_manifest_digest_list",
            "graph",
            "intended_owner",
            "phase",
            "repository_name",
            "schema_version",
            "target_platform",
            "temporary_tag",
        }:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} candidate ledger is malformed"
            )
        expected_temporary_tag = (
            f"candidate-{release_root_path.name}-"
            f"{hashlib.sha256(logical_name.encode()).hexdigest()[:20]}"
        )
        if (
            ledger.get("candidate_identity")
            != f"ProductRelease:{release_root_path.name}:{logical_name}"
            or ledger.get("intended_owner") != f"ProductImage:{logical_name}"
            or ledger.get("phase") != "published"
            or ledger.get("repository_name") != logical_name
            or ledger.get("schema_version") != 1
            or ledger.get("target_platform") != target_platform
            or ledger.get("temporary_tag") != expected_temporary_tag
            or ledger.get("deletion_manifest_digest_list") != []
            or ledger.get("deleted_manifest_digest_list") != []
        ):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} candidate identity is invalid"
            )
        graph = ledger.get("graph")
        archive = ledger.get("archive")
        if not isinstance(graph, Mapping) or not isinstance(archive, Mapping):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} candidate graph is missing"
            )
        manifest_digest_set, blob_digest_set = self._graph_validate(
            graph=graph,
            logical_name=logical_name,
            root_digest=expected_root_digest,
            target_platform=target_platform,
        )
        if (
            ledger.get("completed_manifest_digest_list")
            != graph.get("publication_manifest_digest_list")
            or set(ledger.get("completed_blob_digest_list", [])) != blob_digest_set
            or len(ledger.get("completed_blob_digest_list", []))
            != len(blob_digest_set)
            or set(ledger.get("completed_manifest_digest_list", []))
            != manifest_digest_set
        ):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} publication proof is incomplete"
            )
        self._archive_validate(
            archive=archive,
            ledger_path=ledger_path,
            logical_name=logical_name,
            release_root_path=release_root_path,
        )

    @staticmethod
    def _descriptor_validate(value: object, *, expected_digest: str) -> None:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"digest", "media_type", "size"}
            or value.get("digest") != expected_digest
            or _DIGEST_PATTERN.fullmatch(expected_digest) is None
            or not isinstance(value.get("media_type"), str)
            or not value["media_type"]
            or not isinstance(value.get("size"), int)
            or value["size"] < 0
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product OCI graph descriptor is invalid"
            )

    def _graph_validate(
        self,
        *,
        graph: Mapping[object, object],
        logical_name: str,
        root_digest: str,
        target_platform: str,
    ) -> tuple[set[str], set[str]]:
        if set(graph) != {
            "blob_descriptor_by_digest_map",
            "manifest_node_by_digest_map",
            "publication_manifest_digest_list",
            "root_digest",
            "root_media_type",
            "target_platform",
        }:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} graph shape is invalid"
            )
        blob_map = graph.get("blob_descriptor_by_digest_map")
        manifest_map = graph.get("manifest_node_by_digest_map")
        publication_list = graph.get("publication_manifest_digest_list")
        if (
            not isinstance(blob_map, Mapping)
            or not isinstance(manifest_map, Mapping)
            or not isinstance(publication_list, list)
            or graph.get("root_digest") != root_digest
            or graph.get("target_platform") != target_platform
            or not isinstance(graph.get("root_media_type"), str)
        ):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} graph identity is invalid"
            )
        manifest_digest_set = set(manifest_map)
        blob_digest_set = set(blob_map)
        if (
            any(not isinstance(value, str) for value in manifest_digest_set | blob_digest_set)
            or manifest_digest_set & blob_digest_set
            or root_digest not in manifest_digest_set
            or len(publication_list) != len(set(publication_list))
            or set(publication_list) != manifest_digest_set
            or publication_list[-1:] != [root_digest]
        ):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} graph closure is invalid"
            )
        for digest, descriptor in blob_map.items():
            self._descriptor_validate(descriptor, expected_digest=digest)
        child_by_parent: dict[str, list[str]] = {}
        for digest, node in manifest_map.items():
            if not isinstance(node, Mapping) or set(node) != {
                "blob_digest_list",
                "child_manifest_digest_list",
                "descriptor",
            }:
                raise DevelopmentEnvironmentError(
                    f"Retained Product image {logical_name} manifest node is invalid"
                )
            self._descriptor_validate(node.get("descriptor"), expected_digest=digest)
            child_list = node.get("child_manifest_digest_list")
            node_blob_list = node.get("blob_digest_list")
            if (
                not isinstance(child_list, list)
                or not isinstance(node_blob_list, list)
                or len(child_list) != len(set(child_list))
                or len(node_blob_list) != len(set(node_blob_list))
                or not set(child_list) <= manifest_digest_set
                or not set(node_blob_list) <= blob_digest_set
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product image {logical_name} manifest edges are invalid"
                )
            child_by_parent[digest] = child_list
        order_by_digest = {digest: index for index, digest in enumerate(publication_list)}
        if any(
            order_by_digest[child] >= order_by_digest[parent]
            for parent, child_list in child_by_parent.items()
            for child in child_list
        ):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} publication order is invalid"
            )
        observed: set[str] = set()
        pending = [root_digest]
        while pending:
            digest = pending.pop()
            if digest in observed:
                continue
            observed.add(digest)
            pending.extend(child_by_parent[digest])
        if observed != manifest_digest_set:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} graph has unreachable manifests"
            )
        return manifest_digest_set, blob_digest_set

    @staticmethod
    def _archive_validate(
        *,
        archive: Mapping[object, object],
        ledger_path: Path,
        logical_name: str,
        release_root_path: Path,
    ) -> None:
        expected_relative_path = f"image-candidate/{logical_name}/candidate.oci.tar"
        archive_sha256 = archive.get("sha256")
        archive_size = archive.get("size")
        if (
            set(archive) != {"relative_path", "sha256", "size"}
            or archive.get("relative_path") != expected_relative_path
            or not isinstance(archive_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
            or not isinstance(archive_size, int)
            or archive_size < 0
        ):
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} archive identity is invalid"
            )
        archive_path = release_root_path / expected_relative_path
        if not archive_path.exists() and not archive_path.is_symlink():
            return
        try:
            if (
                archive_path.is_symlink()
                or archive_path.resolve(strict=True).parent != ledger_path.parent
                or archive_path.stat().st_size != archive_size
            ):
                raise OSError("unsafe archive path")
            digest = hashlib.sha256()
            with archive_path.open("rb") as file_handle:
                while chunk := file_handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} OCI archive is unsafe"
            ) from error
        if digest.hexdigest() != archive_sha256:
            raise DevelopmentEnvironmentError(
                f"Retained Product image {logical_name} OCI archive differs"
            )
