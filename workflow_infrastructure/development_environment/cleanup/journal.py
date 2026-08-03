"""Crash-safe cleanup phase journal owned independently from orchestration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Mapping, Protocol

from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    CleanupBindingProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

PHASE_LIST = (
    "compute",
    "data-stack",
    "storage",
    "retained",
    "kms",
    "verify",
    "complete",
)


class InventoryResolverProtocol(Protocol):
    """Declare the inventory resolver interface."""

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Return the exact task inventory.

        Args:
            request: Validated operation request.

        Returns:
            The exact task inventory.
        """


class CleanupJournalStore:
    """Load, bind, advance, and durably persist one cleanup operation."""

    def __init__(
        self,
        *,
        binding: CleanupBindingProtocol,
        inventory_resolver: InventoryResolverProtocol,
    ) -> None:
        """Initialize the cleanup journal store dependencies.

        Args:
            binding: Binding.
            inventory_resolver: Inventory resolver.
        """

        self._binding = binding
        self._inventory_resolver = inventory_resolver

    def load_or_create(self, request: CleanupRequest) -> tuple[Path, dict[str, object]]:
        """Load the same operation or durably create its immutable inventory.

        Args:
            request: Validated operation request.

        Returns:
            The same operation or durably create its immutable inventory.
        """

        path = self._path_get(request)
        if path.exists():
            payload = self._load(path)
            inventory = CleanupInventory.from_payload(payload["inventory"])
            if (
                inventory.common_prefix != request.common_prefix
                or inventory.operation_identity != request.operation_identity
            ):
                raise DevelopmentEnvironmentError("Task cleanup journal belongs to another operation")
            return path, payload
        inventory = self._inventory_resolver.resolve(request)
        payload: dict[str, object] = {
            "schema_version": 1,
            "phase": "compute",
            "inventory": inventory.payload_get(),
        }
        atomic_json_write(path, payload)
        return path, payload

    def advance(self, path: Path, payload: dict[str, object]) -> None:
        """Durably advance exactly one completed phase.

        Args:
            path: Exact filesystem path.
            payload: Structured operation payload.
        """

        phase = payload.get("phase")
        if phase not in PHASE_LIST[:-1]:
            raise DevelopmentEnvironmentError("Task cleanup journal has an unsupported phase")
        payload["phase"] = PHASE_LIST[PHASE_LIST.index(phase) + 1]
        atomic_json_write(path, payload)

    def _path_get(self, request: CleanupRequest) -> Path:
        """Return the private durable cleanup-journal path for one operation.

        Args:
            request: Validated operation request.

        Returns:
            The path.
        """

        return (
            self._binding.common_directory_get()
            / "agent-workflows"
            / "external-cleanup"
            / f"{request.common_prefix}.json"
        )

    @staticmethod
    def _load(path: Path) -> dict[str, object]:
        """Load the requested state.

        Args:
            path: Exact filesystem path.

        Returns:
            The requested state.
        """

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError("Task cleanup journal is unavailable or malformed") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"inventory", "phase", "schema_version"}
            or payload.get("schema_version") != 1
            or payload.get("phase") not in PHASE_LIST
        ):
            raise DevelopmentEnvironmentError("Task cleanup journal has another shape")
        return payload


def atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    """Replace one private journal only after file and directory durability.

    Args:
        path: Exact filesystem path.
        payload: Structured operation payload.
    """

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            os.fchmod(file.fileno(), 0o600)
            json.dump(payload, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        if temporary_path is None:
            raise OSError("temporary cleanup journal was not created")
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise DevelopmentEnvironmentError("Task cleanup journal could not be persisted") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
