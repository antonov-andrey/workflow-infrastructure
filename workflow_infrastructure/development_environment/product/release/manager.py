"""Sequence retained Product release activation, recovery, and reset."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.release.manifest import (
    RetainedProductHostManifestValidator,
)
from workflow_infrastructure.development_environment.product.release.model import (
    RetainedProductReleaseHostIdentity,
)
from workflow_infrastructure.development_environment.product.release.recovery import (
    RetainedProductRecoveryStore,
)
from workflow_infrastructure.development_environment.product.release.recovery_contract import (
    RetainedProductReleaseValidator,
)
from workflow_infrastructure.development_environment.product.release.reset import (
    RetainedProductReleaseReset,
)
from workflow_infrastructure.development_environment.product.release.rollback import (
    RetainedProductReleasePointerStore,
)


class DevelopmentRetainedProductReleaseManager:
    """Wire cohesive owners and sequence host-local Product lifecycle commands."""

    def __init__(
        self,
        *,
        host_manifest_validator: RetainedProductHostManifestValidator,
        identity: RetainedProductReleaseHostIdentity,
        is_host_get: Callable[[], bool],
        pointer: RetainedProductReleasePointerStore,
        python_bytecode_environment_assignment: str,
        recovery: RetainedProductRecoveryStore,
        reset: RetainedProductReleaseReset,
        validator: RetainedProductReleaseValidator,
    ) -> None:
        self._host_manifest_validator = host_manifest_validator
        self._identity = identity
        self._is_host_get = is_host_get
        self._pointer = pointer
        self._python_bytecode_environment_assignment = (
            python_bytecode_environment_assignment
        )
        self._recovery = recovery
        self._reset = reset
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
        """Return one environment-bound command for the current Product tool."""

        return [
            "env",
            self._python_bytecode_environment_assignment,
            "python3.14",
            "-B",
            str(self.current_product_tool_path_get()),
            command,
            "--environment-name",
            self._identity.environment_name,
            "--public-http-port",
            str(self._identity.local_http_port),
            *argument_list,
        ]

    def release_validate(self, release_root_path: Path) -> str:
        """Validate one retained release against the exact current contract."""

        return self._validator.validate(release_root_path)

    def recovery_status_get(self) -> str:
        """Return the local retained Product recovery state."""

        return self._recovery.status_get()

    def recovery_status_print(self) -> None:
        """Print whether retained Product recovery must be resumed."""

        self._host_only_validate("host-product-recovery-status")
        print(json.dumps({"status": self._recovery.status_get()}, sort_keys=True))

    def recovery_begin(self) -> None:
        """Persist the exact Product recovery savepoint before releasing the guard."""

        self._host_only_validate("host-product-recovery-begin")
        release_name = self._recovery.begin()
        print(f"OK: Product recovery savepoint for {release_name} is pending")

    def recovery_complete(self) -> None:
        """Clear the durable Product recovery savepoint after full acceptance."""

        self._host_only_validate("host-product-recovery-complete")
        release_name = self._recovery.complete()
        print(f"OK: Product recovery savepoint for {release_name} is complete")

    def activate(self, release_name: str) -> None:
        """Validate and atomically activate one retained Product release."""

        self._host_only_validate("host-product-release-activate")
        release_root_path = self._identity.host_release_root_path / release_name
        accepted_release_name = self._validator.validate(release_root_path)
        if accepted_release_name != release_name:
            raise DevelopmentEnvironmentError(
                "Retained Product release activation changed exact identity"
            )
        self._pointer.activate(release_root_path)
        print(f"OK: retained Product release {release_name} is current")

    def restore(self) -> None:
        """Validate snapshot-owned current release and restore its root link."""

        self._host_only_validate("host-product-release-restore")
        release_root_path = self._pointer.current_release_path_get()
        release_name = self._validator.validate(release_root_path)
        self._host_manifest_validator.validate(release_root_path)
        self._pointer.restore_current_source()
        print(
            f"OK: retained Product release {release_name} root-volume link is restored"
        )

    def reset(self, preserved_release_name: str) -> None:
        """Remove old retained Product state while preserving one exact candidate."""

        self._host_only_validate("host-product-release-reset")
        self._reset.run(preserved_release_name)
        if self._recovery.status_get() != "absent":
            raise DevelopmentEnvironmentError(
                "Retained Product release reset did not reach absent state"
            )
        print("OK: retained Product release and management runtime were reset")

    def release_host_identity_validate(self, *, release_root_path: Path) -> None:
        """Require the active host to match one byte-validated retained release."""

        self._host_manifest_validator.validate(release_root_path)

    def _host_only_validate(self, operation: str) -> None:
        if not self._is_host_get():
            raise DevelopmentEnvironmentError(
                f"{operation} is supported only on the development host"
            )
