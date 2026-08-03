"""Aggregate exact absence proof for a completed task cleanup."""

from __future__ import annotations

from typing import Mapping, Protocol

from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    AwsClientProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
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
    """Combine independent resource-owner proofs and reject tagged leaks."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        compute: InventoryAbsenceProtocol,
        kms: InventoryAbsenceProtocol,
        retained: InventoryAbsenceProtocol,
        stack: StackAbsenceProtocol,
        storage: BucketAbsenceProtocol,
    ) -> None:
        """Initialize the cleanup absence verifier dependencies.

        Args:
            aws: Aws.
            compute: Compute.
            kms: Kms.
            retained: Retained.
            stack: Stack.
            storage: Storage.
        """

        self._aws = aws
        self._compute = compute
        self._kms = kms
        self._retained = retained
        self._stack = stack
        self._storage = storage

    def validate(self, inventory: CleanupInventory) -> None:
        """Require all known resources absent and no unexplained tagged resource.

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
        payload = self._aws.json_get(
            [
                "resourcegroupstaggingapi",
                "get-resources",
                "--tag-filters",
                f"Key=git-worktree,Values={inventory.common_prefix}",
            ]
        )
        mapping_list = payload.get("ResourceTagMappingList", [])
        if not isinstance(mapping_list, list) or any(not isinstance(item, Mapping) for item in mapping_list):
            raise DevelopmentEnvironmentError("Task tagged-resource leak inventory is malformed")
        remaining_arn_set = {item.get("ResourceARN") for item in mapping_list}
        if remaining_arn_set - {inventory.kms_key_arn}:
            raise DevelopmentEnvironmentError("Unexpected git-worktree tagged resources remain after cleanup")
