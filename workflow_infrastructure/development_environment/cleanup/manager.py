"""Resumable orchestration of exact task-environment cleanup owners."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.cleanup.journal import (
    CleanupJournalStore,
)
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    AccountVerifierProtocol,
    EnvironmentIdentityProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class InventoryResolverProtocol(Protocol):
    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Resolve one exact immutable inventory."""


class InventoryCleanupProtocol(Protocol):
    def delete(self, inventory: CleanupInventory) -> None:
        """Delete the collaborator's resources."""


class KmsCleanupProtocol(Protocol):
    def retire(self, inventory: CleanupInventory) -> None:
        """Retire the inventory KMS key."""


class StackCleanupProtocol(Protocol):
    def delete(self, stack_name: str) -> None:
        """Delete one exact stack."""


class BucketCleanupProtocol(Protocol):
    def delete(self, bucket_name: str) -> None:
        """Delete one exact bucket."""


class AbsenceVerifierProtocol(Protocol):
    def validate(self, inventory: CleanupInventory) -> None:
        """Require all inventory resources absent or retired."""


class DevelopmentEnvironmentCleanupManager:
    """Sequence independently owned cleanup phases for one task environment."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        compute: InventoryCleanupProtocol,
        identity: EnvironmentIdentityProtocol,
        inventory_resolver: InventoryResolverProtocol,
        journal: CleanupJournalStore,
        kms: KmsCleanupProtocol,
        retained: InventoryCleanupProtocol,
        stack: StackCleanupProtocol,
        storage: BucketCleanupProtocol,
        verifier: AbsenceVerifierProtocol,
    ) -> None:
        self._account = account
        self._compute = compute
        self._identity = identity
        self._inventory_resolver = inventory_resolver
        self._journal = journal
        self._kms = kms
        self._retained = retained
        self._stack = stack
        self._storage = storage
        self._verifier = verifier

    def destroy(self, request: CleanupRequest) -> dict[str, object]:
        """Resume deletion and return the closed goal-delete absence proof."""

        self._request_validate(request, allow_primary=False)
        journal_path, journal = self._journal.load_or_create(request)
        inventory = CleanupInventory.from_payload(journal["inventory"])
        while journal["phase"] != "complete":
            self._phase_run(journal["phase"], inventory)
            self._journal.advance(journal_path, journal)
        self._verifier.validate(inventory)
        return {**request.payload_get(), "external_resources_absent": True}

    def inventory(self, request: CleanupRequest) -> dict[str, object]:
        """Return a non-mutating exact inventory for acceptance diagnostics."""

        self._request_validate(request, allow_primary=True)
        inventory = self._inventory_resolver.resolve(request)
        return {
            **request.payload_get(),
            "environment_name": inventory.environment_name,
            "resource_identity_list": sorted(
                [
                    inventory.compute_stack_name,
                    inventory.data_stack_name,
                    inventory.instance_id,
                    inventory.kms_alias_name,
                    inventory.kms_key_arn,
                    inventory.retained_volume_id,
                    *inventory.bucket_name_list,
                ]
            ),
        }

    def _phase_run(self, phase: str, inventory: CleanupInventory) -> None:
        if phase == "compute":
            self._compute.delete(inventory)
        elif phase == "data-stack":
            self._stack.delete(inventory.data_stack_name)
        elif phase == "storage":
            for bucket_name in inventory.bucket_name_list:
                self._storage.delete(bucket_name)
        elif phase == "retained":
            self._retained.delete(inventory)
        elif phase == "kms":
            self._kms.retire(inventory)
        elif phase == "verify":
            self._verifier.validate(inventory)
        else:
            raise DevelopmentEnvironmentError("Task cleanup journal has an unsupported phase")

    def _request_validate(self, request: CleanupRequest, *, allow_primary: bool) -> None:
        if not allow_primary and (self._identity.is_primary or not self._identity.git_worktree):
            raise DevelopmentEnvironmentError("Task cleanup cannot target the primary development environment")
        if request.common_prefix != self._identity.git_worktree:
            raise DevelopmentEnvironmentError("Task cleanup request and environment identity differ")
        self._account.local_operator_context_validate()
