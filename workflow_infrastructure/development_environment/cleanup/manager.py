"""Resumable orchestration of exact task-environment cleanup owners."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.cleanup.journal import (
    CleanupJournalStore,
    PHASE_LIST,
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
    """Declare the inventory resolver interface."""

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Resolve one exact fresh live inventory.

        Args:
            request: Validated operation request.

        Returns:
            One exact fresh live inventory.
        """


class InventoryCleanupProtocol(Protocol):
    """Declare the inventory cleanup interface."""

    def delete(self, inventory: CleanupInventory) -> None:
        """Delete the collaborator's resources.

        Args:
            inventory: Inventory.
        """


class KmsCleanupProtocol(Protocol):
    """Declare the KMS cleanup interface."""

    def retire(self, inventory: CleanupInventory) -> None:
        """Retire the inventory KMS key.

        Args:
            inventory: Inventory.
        """


class StackCleanupProtocol(Protocol):
    """Declare the stack cleanup interface."""

    def delete(self, stack_name: str) -> None:
        """Delete one exact stack.

        Args:
            stack_name: Stack name.
        """


class BucketCleanupProtocol(Protocol):
    """Declare the bucket cleanup interface."""

    def delete(self, bucket_name: str, *, expected_owner: str) -> None:
        """Delete one exact bucket.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
        """


class AbsenceVerifierProtocol(Protocol):
    """Declare the absence verifier interface."""

    def validate(self, inventory: CleanupInventory) -> None:
        """Require all inventory resources absent or retired.

        Args:
            inventory: Inventory.
        """

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Return exact current aggregate absence without mutating resources."""


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
        """Initialize the development environment cleanup manager dependencies.

        Args:
            account: Account.
            compute: Compute.
            identity: Identity.
            inventory_resolver: Inventory resolver.
            journal: Journal.
            kms: Kms.
            retained: Retained.
            stack: Stack.
            storage: Storage.
            verifier: Verifier.
        """

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
        """Resume deletion and return the registered provider absence proof.

        Args:
            request: Validated operation request.

        Returns:
            Closed cleanup result proving task resources absent.
        """

        self._request_validate(request)
        journal_path, journal = self._journal.load_or_create(request)
        for phase in PHASE_LIST[:-2]:
            inventory = self._inventory_resolver.resolve(request)
            self._inventory_identity_validate(inventory)
            self._phase_run(phase, inventory)
            if journal["phase"] == phase:
                self._journal.advance(journal_path, journal)
        inventory = self._inventory_resolver.resolve(request)
        self._inventory_identity_validate(inventory)
        self._verifier.validate(inventory)
        if journal["phase"] == "verify":
            self._journal.advance(journal_path, journal)
        return {**request.payload_get(), "external_resources_absent": True}

    def inventory(self, request: CleanupRequest) -> dict[str, object]:
        """Return a non-mutating exact inventory for acceptance diagnostics.

        Args:
            request: Validated operation request.

        Returns:
            A non-mutating exact inventory for acceptance diagnostics.
        """

        self._request_validate(request)
        inventory = self._inventory_resolver.resolve(request)
        self._inventory_identity_validate(inventory)
        external_resources_absent = self._verifier.absent_get(inventory)
        return {
            **request.payload_get(),
            "environment_name": inventory.environment_name,
            "external_resources_absent": external_resources_absent,
            "resource_identity_list": sorted(
                [
                    inventory.compute_stack_name,
                    inventory.data_stack_name,
                    *inventory.instance_id_list,
                    inventory.kms_alias_name,
                    *inventory.kms_key_arn_list,
                    *inventory.retained_volume_id_list,
                    *inventory.bucket_name_list,
                ]
            ),
        }

    def _phase_run(self, phase: str, inventory: CleanupInventory) -> None:
        """Dispatch one journaled cleanup phase to its sole resource owner.

        Args:
            phase: Phase.
            inventory: Inventory.
        """

        if phase == "compute":
            self._compute.delete(inventory)
        elif phase == "data-stack":
            self._stack.delete(inventory.data_stack_name)
        elif phase == "storage":
            for bucket_name in inventory.bucket_name_list:
                self._storage.delete(bucket_name, expected_owner=inventory.account_id)
        elif phase == "retained":
            self._retained.delete(inventory)
        elif phase == "kms":
            self._kms.retire(inventory)
        else:
            raise DevelopmentEnvironmentError("Task cleanup journal has an unsupported phase")

    def _request_validate(self, request: CleanupRequest) -> None:
        """Require the cleanup request to match the natural environment identity.

        Args:
            request: Validated operation request.
        """

        if self._identity.is_primary or not self._identity.git_worktree:
            raise DevelopmentEnvironmentError("Task cleanup cannot target the primary development environment")
        if request.common_prefix != self._identity.git_worktree:
            raise DevelopmentEnvironmentError("Task cleanup request and environment identity differ")
        self._account.local_operator_context_validate()

    def _inventory_identity_validate(self, inventory: CleanupInventory) -> None:
        """Require a fresh inventory to remain inside the selected task scope.

        Args:
            inventory: Inventory.
        """

        if (
            inventory.common_prefix != self._identity.git_worktree
            or inventory.environment_name != self._identity.environment_name
            or inventory.compute_stack_name != self._identity.compute_stack_name
            or inventory.data_stack_name != self._identity.data_plane_stack_name
            or inventory.kms_alias_name != f"alias/storage-{self._identity.environment_name}"
        ):
            raise DevelopmentEnvironmentError("Task cleanup inventory is outside the selected environment scope")
