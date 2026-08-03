"""Host-artifact identity validation for retained Product manifests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class RetainedProductHostManifestValidator:
    """Require a retained release to match the installed host artifact graph."""

    def __init__(self, host_artifact_manifest_get: Callable[[], dict[str, object]]) -> None:
        """Initialize the retained product host manifest validator dependencies.

        Args:
            host_artifact_manifest_get: Host artifact manifest get.
        """

        self._host_artifact_manifest_get = host_artifact_manifest_get

    def validate(self, release_root_path: Path) -> None:
        """Validate the retained product host manifest validator contract.

        Args:
            release_root_path: Exact filesystem path for release root.
        """

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
        if dict(retained_host_artifact_manifest) != self._host_artifact_manifest_get():
            raise DevelopmentEnvironmentError("Retained Product release has another exact host artifact identity")
