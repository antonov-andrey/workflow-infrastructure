"""Durable retained Product recovery marker lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.release.model import (
    RetainedProductReleaseCommandRunner,
    RetainedProductReleaseHostIdentity,
)
from workflow_infrastructure.development_environment.product.release.rollback import (
    RetainedProductReleasePointerStore,
)


class RetainedProductRecoveryStore:
    """Own the exact pending marker and recovery-state interpretation."""

    def __init__(
        self,
        *,
        identity: RetainedProductReleaseHostIdentity,
        pointer: RetainedProductReleasePointerStore,
        runner: RetainedProductReleaseCommandRunner,
    ) -> None:
        self._identity = identity
        self._pointer = pointer
        self._runner = runner

    def status_get(self) -> str:
        """Return absent, pending, or ready for the retained Product state."""

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
                raise DevelopmentEnvironmentError(
                    "Product recovery state exists without a retained current release"
                )
            return "absent"
        release_root_path = self._pointer.current_release_path_get()
        marker_exists = self.marker_validate(
            expected_release_name=release_root_path.name
        )
        try:
            current_link_is_exact = product_current_path.is_symlink() and os.readlink(
                product_current_path
            ) == str(retained_current_path)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery current-source link is unavailable"
            ) from error
        return "pending" if marker_exists or not current_link_is_exact else "ready"

    def begin(self) -> str:
        """Persist the exact pending recovery savepoint and return its release."""

        release_root_path = self._pointer.current_release_path_get()
        if self.marker_validate(expected_release_name=release_root_path.name):
            return release_root_path.name
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
        self._atomic_text_file_replace(
            mode=0o600,
            path=self._identity.host_product_recovery_marker_path,
            text=marker_text,
        )
        return release_root_path.name

    def complete(self) -> str:
        """Remove one exact pending savepoint after current-source restoration."""

        release_root_path = self._pointer.current_release_path_get()
        if not self.marker_validate(expected_release_name=release_root_path.name):
            raise DevelopmentEnvironmentError(
                "Product recovery cannot complete without its pending savepoint"
            )
        try:
            current_link_is_exact = (
                self._identity.host_current_source_path.is_symlink()
                and os.readlink(self._identity.host_current_source_path)
                == str(self._identity.host_retained_current_release_path)
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery current-source link is unavailable"
            ) from error
        if not current_link_is_exact:
            raise DevelopmentEnvironmentError(
                "Product recovery current-source link is not restored"
            )
        try:
            self._identity.host_product_recovery_marker_path.unlink()
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery savepoint could not be completed"
            ) from error
        self._runner.run(
            [
                "sync",
                "-f",
                str(self._identity.host_product_recovery_marker_path.parent),
            ]
        )
        return release_root_path.name

    def marker_validate(self, *, expected_release_name: str) -> bool:
        """Validate an optional retained recovery marker."""

        marker_path = self._identity.host_product_recovery_marker_path
        if not marker_path.parent.is_dir() or marker_path.parent.is_symlink():
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker parent is invalid"
            )
        if marker_path.is_symlink():
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker must not be a symlink"
            )
        if not marker_path.exists():
            return False
        if not marker_path.is_file():
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker is not a regular file"
            )
        try:
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker is unavailable or malformed"
            ) from error
        expected_payload = {
            "environment_name": self._identity.environment_name,
            "release": expected_release_name,
            "state": "pending",
        }
        if marker_payload != expected_payload:
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker has an inconsistent identity"
            )
        return True

    def _atomic_text_file_replace(self, *, mode: int, path: Path, text: str) -> None:
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
            self._runner.run(["sync", "-f", str(path.parent)])
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery savepoint could not be persisted"
            ) from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
