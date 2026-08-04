"""Aggregate exact absence proof for a completed task cleanup."""

from __future__ import annotations

from typing import Protocol

from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
)


class InventoryAbsenceProtocol(Protocol):
    """Declare the inventory absence interface."""

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Require the collaborator's inventory resources to be absent.

        Args:
            inventory: Inventory.
        """


class StackAbsenceProtocol(Protocol):
    """Declare the stack absence interface."""

    def absence_validate(self, stack_name: str) -> None:
        """Require one stack to be absent.

        Args:
            stack_name: Stack name.
        """


class BucketAbsenceProtocol(Protocol):
    """Declare the bucket absence interface."""

    def absence_validate(self, bucket_name: str) -> None:
        """Require one bucket to be absent.

        Args:
            bucket_name: Bucket name.
        """


class CleanupAbsenceVerifier:
    """Combine service-native absence proofs from each resource owner."""

    def __init__(
        self,
        *,
        compute: InventoryAbsenceProtocol,
        kms: InventoryAbsenceProtocol,
        retained: InventoryAbsenceProtocol,
        stack: StackAbsenceProtocol,
        storage: BucketAbsenceProtocol,
    ) -> None:
        """Initialize the cleanup absence verifier dependencies.

        Args:
            compute: Compute.
            kms: Kms.
            retained: Retained.
            stack: Stack.
            storage: Storage.
        """

        self._compute = compute
        self._kms = kms
        self._retained = retained
        self._stack = stack
        self._storage = storage

    def validate(self, inventory: CleanupInventory) -> None:
        """Require every resource owner to prove its known resources absent.

        Args:
            inventory: Inventory.
        """

        self._stack.absence_validate(inventory.compute_stack_name)
        self._stack.absence_validate(inventory.data_stack_name)
        self._compute.absence_validate(inventory)
        for bucket_name in inventory.bucket_name_list:
            self._storage.absence_validate(bucket_name)
        self._retained.absence_validate(inventory)
        self._kms.absence_validate(inventory)
