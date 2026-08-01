"""Explicit retained Product release-state reset owner."""

from __future__ import annotations

import shutil

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.release.model import (
    RetainedProductReleaseCommandRunner,
    RetainedProductReleaseHostIdentity,
)


class RetainedProductReleaseReset:
    """Delete the exact release graph while preserving one candidate directory."""

    def __init__(
        self,
        *,
        identity: RetainedProductReleaseHostIdentity,
        runner: RetainedProductReleaseCommandRunner,
    ) -> None:
        self._identity = identity
        self._runner = runner

    def run(self, preserved_release_name: str) -> None:
        """Remove old retained state while preserving one exact candidate."""

        release_owner_root_path = self._identity.host_retained_release_root_path
        product_tool_root_path = self._identity.host_retained_product_tool_path
        current_source_path = self._identity.host_current_source_path
        if (
            not release_owner_root_path.is_dir()
            or release_owner_root_path.is_symlink()
            or (
                product_tool_root_path.exists()
                and (
                    not product_tool_root_path.is_dir()
                    or product_tool_root_path.is_symlink()
                )
            )
            or (
                (current_source_path.exists() or current_source_path.is_symlink())
                and not current_source_path.is_symlink()
            )
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product reset roots are malformed"
            )
        allowed_entry_name_set = {
            ".operation.lock",
            "current",
            "recovery-pending.json",
            "releases",
            "rollback",
        }
        if not {path.name for path in release_owner_root_path.iterdir()} <= (
            allowed_entry_name_set
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release root contains an unexpected entry"
            )
        for link_path in (
            self._identity.host_retained_current_release_path,
            self._identity.host_retained_rollback_release_path,
        ):
            if (
                link_path.exists() or link_path.is_symlink()
            ) and not link_path.is_symlink():
                raise DevelopmentEnvironmentError(
                    "Retained Product release pointer is malformed"
                )
        for file_path in (
            self._identity.host_product_recovery_marker_path,
            release_owner_root_path / ".operation.lock",
        ):
            if (file_path.exists() or file_path.is_symlink()) and (
                not file_path.is_file() or file_path.is_symlink()
            ):
                raise DevelopmentEnvironmentError(
                    "Retained Product release state file is malformed"
                )
        release_root_path = self._identity.host_release_root_path
        if (release_root_path.exists() or release_root_path.is_symlink()) and (
            not release_root_path.is_dir() or release_root_path.is_symlink()
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release collection is malformed"
            )
        preserved_release_root_path = release_root_path / preserved_release_name
        if (
            not preserved_release_name.isdigit()
            or len(preserved_release_name) != 20
            or not preserved_release_root_path.is_dir()
            or preserved_release_root_path.is_symlink()
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product reset candidate is malformed"
            )
        current_source_path.unlink(missing_ok=True)
        self._identity.host_retained_current_release_path.unlink(missing_ok=True)
        self._identity.host_retained_rollback_release_path.unlink(missing_ok=True)
        self._identity.host_product_recovery_marker_path.unlink(missing_ok=True)
        (release_owner_root_path / ".operation.lock").unlink(missing_ok=True)
        for old_release_root_path in release_root_path.iterdir():
            if old_release_root_path == preserved_release_root_path:
                continue
            if not old_release_root_path.is_dir() or old_release_root_path.is_symlink():
                raise DevelopmentEnvironmentError(
                    "Retained Product release collection contains a malformed entry"
                )
            shutil.rmtree(old_release_root_path)
        if product_tool_root_path.exists():
            shutil.rmtree(product_tool_root_path)
        self._runner.run(["sync", "-f", str(release_owner_root_path)])
        self._runner.run(["sync", "-f", str(current_source_path.parent)])
