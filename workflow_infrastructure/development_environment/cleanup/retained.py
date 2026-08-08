"""Retained EBS volume and task snapshot cleanup."""

from __future__ import annotations

import re
import subprocess
from typing import Mapping

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
from workflow_infrastructure.development_environment.cleanup.aws_response import (
    json_object_get,
    tag_map_get,
    task_ownership_tag_validate,
)
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    AwsClientProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_SNAPSHOT_ID_PATTERN = re.compile(r"snap-[0-9a-f]{8,17}")


class RetainedStorageCleanup:
    """Delete only an ownership-proven retained volume and its task snapshots."""

    def __init__(self, aws: AwsClientProtocol) -> None:
        """Initialize the retained storage cleanup dependencies.

        Args:
            aws: Aws.
        """

        self._aws = aws

    def delete(self, inventory: CleanupInventory) -> None:
        """Delete task snapshots before the detached retained volume.

        Args:
            inventory: Inventory.
        """

        for snapshot_id in self._owned_snapshot_id_list_get(inventory):
            if self._owned_snapshot_exists(inventory, snapshot_id):
                self._aws.run(["ec2", "delete-snapshot", "--snapshot-id", snapshot_id])
        for retained_volume_id in inventory.retained_volume_id_list:
            state = self._owned_retained_volume_state_get(inventory, retained_volume_id)
            if state == "absent":
                continue
            if state == "in-use":
                wait_result = self._aws.run(
                    ["ec2", "wait", "volume-available", "--volume-ids", retained_volume_id],
                    check=False,
                )
                if wait_result.returncode != 0:
                    if self._volume_not_found_get(wait_result, operation="DescribeVolumes"):
                        self._account_validate(inventory)
                        continue
                    raise DevelopmentEnvironmentError("Task retained volume did not become available")
                state = self._owned_retained_volume_state_get(inventory, retained_volume_id)
            if state == "absent":
                continue
            if state == "deleting":
                self._volume_deleted_wait(inventory, retained_volume_id)
                continue
            if state != "available":
                raise DevelopmentEnvironmentError("Task retained volume is not safely deletable")
            delete_result = self._aws.run(
                ["ec2", "delete-volume", "--volume-id", retained_volume_id],
                check=False,
            )
            if delete_result.returncode != 0:
                if self._volume_not_found_get(delete_result, operation="DeleteVolume"):
                    self._account_validate(inventory)
                    continue
                raise DevelopmentEnvironmentError("Task retained volume deletion failed")
            self._volume_deleted_wait(inventory, retained_volume_id)

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Require both the retained volume and every task snapshot to be absent.

        Args:
            inventory: Inventory.
        """

        if not self.absent_get(inventory):
            raise DevelopmentEnvironmentError("Task retained volume or snapshot absence is not proven")

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Return exact current retained-volume and snapshot absence."""

        absent = True
        for retained_volume_id in inventory.retained_volume_id_list:
            if self._owned_retained_volume_state_get(inventory, retained_volume_id) != "absent":
                absent = False
        if self._owned_snapshot_id_list_get(inventory):
            absent = False
        return absent

    def _owned_snapshot_id_list_get(self, inventory: CleanupInventory) -> list[str]:
        """Return owned snapshot identity list.

        Args:
            inventory: Inventory.

        Returns:
            The owned snapshot identity list.
        """

        payload = self._aws.json_get(
            [
                "ec2",
                "describe-snapshots",
                "--owner-ids",
                inventory.account_id,
                "--filters",
                f"Name=tag:EnvironmentName,Values={inventory.environment_name}",
                f"Name=tag:git-worktree,Values={inventory.common_prefix}",
            ]
        )
        snapshot_list = payload.get("Snapshots", [])
        if not isinstance(snapshot_list, list) or any(not isinstance(item, Mapping) for item in snapshot_list):
            raise DevelopmentEnvironmentError("Task snapshot inventory is malformed")
        snapshot_id_list: list[str] = []
        for item in snapshot_list:
            snapshot_id = item.get("SnapshotId")
            tag_map = tag_map_get(item.get("Tags"))
            if (
                not isinstance(snapshot_id, str)
                or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
                or tag_map.get("EnvironmentClass") != "development"
                or tag_map.get("EnvironmentName") != inventory.environment_name
                or tag_map.get("git-worktree") != inventory.common_prefix
            ):
                raise DevelopmentEnvironmentError("Task snapshot ownership is ambiguous")
            snapshot_id_list.append(snapshot_id)
        if len(snapshot_id_list) != len(set(snapshot_id_list)):
            raise DevelopmentEnvironmentError("Task snapshot inventory repeats an identity")
        return sorted(snapshot_id_list)

    def _owned_snapshot_exists(self, inventory: CleanupInventory, snapshot_id: str) -> bool:
        """Freshly re-attest one exact task snapshot immediately before deletion."""

        result = self._aws.run(
            [
                "ec2",
                "describe-snapshots",
                "--snapshot-ids",
                snapshot_id,
                "--owner-ids",
                inventory.account_id,
            ],
            check=False,
        )
        if result.returncode != 0:
            if aws_cli_error_matches(
                result,
                code_set=frozenset({"InvalidSnapshot.NotFound"}),
                operation="DescribeSnapshots",
            ):
                return False
            raise DevelopmentEnvironmentError("Task snapshot ownership cannot be observed")
        payload = json_object_get(result.stdout, label="task snapshot ownership")
        snapshot_list = payload.get("Snapshots")
        if not isinstance(snapshot_list, list) or len(snapshot_list) != 1 or not isinstance(snapshot_list[0], Mapping):
            raise DevelopmentEnvironmentError("Task snapshot ownership is malformed")
        snapshot = snapshot_list[0]
        task_ownership_tag_validate(
            tag_map_get(snapshot.get("Tags")),
            common_prefix=inventory.common_prefix,
            environment_name=inventory.environment_name,
            label="snapshot",
        )
        if (
            snapshot.get("SnapshotId") != snapshot_id
            or snapshot.get("OwnerId") != inventory.account_id
            or snapshot.get("State") != "completed"
        ):
            raise DevelopmentEnvironmentError("Task snapshot ownership is malformed")
        self._account_validate(inventory)
        return True

    def _owned_retained_volume_state_get(self, inventory: CleanupInventory, volume_id: str) -> str:
        """Return one freshly re-attested task volume state immediately before mutation."""

        result = self._aws.run(["ec2", "describe-volumes", "--volume-ids", volume_id], check=False)
        if result.returncode != 0:
            if self._volume_not_found_get(result, operation="DescribeVolumes"):
                self._account_validate(inventory)
                return "absent"
            raise DevelopmentEnvironmentError("Task retained volume ownership cannot be observed")
        payload = json_object_get(result.stdout, label="task retained volume ownership")
        volume_list = payload.get("Volumes")
        if not isinstance(volume_list, list) or len(volume_list) != 1 or not isinstance(volume_list[0], Mapping):
            raise DevelopmentEnvironmentError("Task retained volume ownership is malformed")
        volume = volume_list[0]
        availability_zone = volume.get("AvailabilityZone")
        state = volume.get("State")
        attachments = volume.get("Attachments")
        task_ownership_tag_validate(
            tag_map_get(volume.get("Tags")),
            common_prefix=inventory.common_prefix,
            environment_name=inventory.environment_name,
            label="retained volume",
        )
        if (
            volume.get("VolumeId") != volume_id
            or volume.get("VolumeType") != "gp3"
            or not isinstance(availability_zone, str)
            or re.fullmatch(rf"{re.escape(inventory.region)}[a-z]", availability_zone) is None
            or not isinstance(state, str)
            or state not in {"creating", "available", "in-use", "deleting", "deleted", "error"}
            or not isinstance(attachments, list)
            or tag_map_get(volume.get("Tags")).get("Name") != f"retained-{inventory.environment_name}"
        ):
            raise DevelopmentEnvironmentError("Task retained volume ownership is malformed")
        if state == "in-use":
            if not attachments or any(
                not isinstance(attachment, Mapping)
                or attachment.get("VolumeId") != volume_id
                or attachment.get("State") not in {"attaching", "attached", "busy", "detaching"}
                for attachment in attachments
            ):
                raise DevelopmentEnvironmentError("Task retained volume ownership is malformed")
        elif attachments:
            raise DevelopmentEnvironmentError("Task retained volume ownership is malformed")
        self._account_validate(inventory)
        return "absent" if state == "deleted" else state

    def _volume_deleted_wait(self, inventory: CleanupInventory, volume_id: str) -> None:
        """Wait only for deletion and require fresh physical absence."""

        result = self._aws.run(
            ["ec2", "wait", "volume-deleted", "--volume-ids", volume_id],
            check=False,
        )
        if result.returncode != 0 and not self._volume_not_found_get(result, operation="DescribeVolumes"):
            raise DevelopmentEnvironmentError("Task retained volume did not reach deleted state")
        if result.returncode != 0:
            self._account_validate(inventory)
            return
        if self._owned_retained_volume_state_get(inventory, volume_id) != "absent":
            raise DevelopmentEnvironmentError("Task retained volume deletion is not proven")

    @staticmethod
    def _volume_not_found_get(result: subprocess.CompletedProcess[str], *, operation: str) -> bool:
        """Return whether one AWS result is the exact volume-absence error."""

        return aws_cli_error_matches(
            result,
            code_set=frozenset({"InvalidVolume.NotFound"}),
            operation=operation,
        )

    def _account_validate(self, inventory: CleanupInventory) -> None:
        """Require the current AWS caller to remain the inventory account."""

        if self._aws.json_get(["sts", "get-caller-identity"]).get("Account") != inventory.account_id:
            raise DevelopmentEnvironmentError("Task retained storage belongs to another AWS account")
