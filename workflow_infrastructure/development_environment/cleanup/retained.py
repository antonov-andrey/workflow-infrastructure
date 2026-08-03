"""Retained EBS volume and task snapshot cleanup."""

from __future__ import annotations

import re
from typing import Mapping

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
from workflow_infrastructure.development_environment.cleanup.aws_response import (
    json_object_get,
    tag_map_get,
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
            self._aws.run(["ec2", "delete-snapshot", "--snapshot-id", snapshot_id])
        result = self._aws.run(
            ["ec2", "describe-volumes", "--volume-ids", inventory.retained_volume_id],
            check=False,
        )
        if result.returncode == 0:
            payload = json_object_get(result.stdout, label="retained volume")
            volume_list = payload.get("Volumes")
            if (
                not isinstance(volume_list, list)
                or len(volume_list) != 1
                or not isinstance(volume_list[0], Mapping)
                or volume_list[0].get("VolumeId") != inventory.retained_volume_id
                or volume_list[0].get("State") != "available"
                or volume_list[0].get("Attachments", []) != []
            ):
                raise DevelopmentEnvironmentError("Task retained volume is not safely deletable")
            self._aws.run(["ec2", "delete-volume", "--volume-id", inventory.retained_volume_id])
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "volume-deleted",
                    "--volume-ids",
                    inventory.retained_volume_id,
                ]
            )
        elif not aws_cli_error_matches(
            result,
            code_set=frozenset({"InvalidVolume.NotFound"}),
            operation="DescribeVolumes",
        ):
            raise DevelopmentEnvironmentError("Task retained volume absence cannot be proven")

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Require both the retained volume and every task snapshot to be absent.

        Args:
            inventory: Inventory.
        """

        result = self._aws.run(
            ["ec2", "describe-volumes", "--volume-ids", inventory.retained_volume_id],
            check=False,
        )
        if result.returncode == 0 or not aws_cli_error_matches(
            result,
            code_set=frozenset({"InvalidVolume.NotFound"}),
            operation="DescribeVolumes",
        ):
            raise DevelopmentEnvironmentError("Task retained volume absence is not proven")
        if self._owned_snapshot_id_list_get(inventory):
            raise DevelopmentEnvironmentError("Task snapshots still exist")

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
                "self",
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
