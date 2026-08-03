"""Atomic current and rollback pointer ownership for retained Product releases."""

from __future__ import annotations

import os
from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.release.model import (
    RetainedProductReleaseHostIdentity,
)
from workflow_infrastructure.development_environment.product.release.recovery_contract import (
    RetainedProductReleaseValidator,
)


def atomic_symlink_replace(*, link_path: Path, target_path: Path) -> None:
    """Durably replace one host symlink without a missing-current gap.

    Args:
        link_path: Exact filesystem path for link.
        target_path: Exact filesystem path for target.
    """

    link_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary_link_path = link_path.with_name(f".{link_path.name}.tmp-{os.getpid()}")
    temporary_link_path.unlink(missing_ok=True)
    temporary_link_path.symlink_to(target_path)
    try:
        os.replace(temporary_link_path, link_path)
        directory_fd = os.open(link_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_link_path.unlink(missing_ok=True)


class RetainedProductReleasePointerStore:
    """Validate and atomically update exact retained Product release pointers."""

    def __init__(
        self,
        *,
        identity: RetainedProductReleaseHostIdentity,
        validator: RetainedProductReleaseValidator,
    ) -> None:
        """Initialize the retained product release pointer store dependencies.

        Args:
            identity: Identity.
            validator: Validator.
        """

        self._identity = identity
        self._validator = validator

    def current_release_path_get(self) -> Path:
        """Return the exact retained current release or fail closed.

        Returns:
            The exact retained current release or fail closed.
        """

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

    def previous_release_path_get(self) -> Path | None:
        """Return the validated predecessor that may become rollback.

        Returns:
            The validated predecessor that may become rollback.
        """

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

    def activate(self, release_root_path: Path) -> None:
        """Atomically install rollback, retained-current, and root-current pointers.

        Args:
            release_root_path: Exact filesystem path for release root.
        """

        previous_release_root_path = self.previous_release_path_get()
        if previous_release_root_path is not None and previous_release_root_path != release_root_path:
            atomic_symlink_replace(
                link_path=self._identity.host_retained_rollback_release_path,
                target_path=previous_release_root_path,
            )
        elif previous_release_root_path is None:
            self._identity.host_retained_rollback_release_path.unlink(missing_ok=True)
        atomic_symlink_replace(
            link_path=self._identity.host_retained_current_release_path,
            target_path=release_root_path,
        )
        self.restore_current_source()

    def restore_current_source(self) -> None:
        """Point root-volume current source at retained current without a gap."""

        atomic_symlink_replace(
            link_path=self._identity.host_current_source_path,
            target_path=self._identity.host_retained_current_release_path,
        )
