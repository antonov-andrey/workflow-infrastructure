"""Retained EBS volume, restore, rollback, and development backup policy."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol

from tool.lib.development_environment_error import DevelopmentEnvironmentError


class AwsClientProtocol(Protocol):
    """AWS CLI surface required by retained-volume management."""

    def run(
        self,
        aws_argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command."""

    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response."""


class EnvironmentIdentityProtocol(Protocol):
    """Stable identities required by retained-volume management."""

    compute_stack_name: str
    environment_name: str


class StackManagerProtocol(Protocol):
    """CloudFormation state surface required by retained volumes."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""

    def resource_id_by_logical_name_map_get(
        self,
        stack_name: str,
    ) -> dict[str, str]:
        """Return physical resources by logical identity."""


class DevelopmentRetainedVolumeManager:
    """Own retained-volume identity, restore, rollback, and backup invariants."""

    def __init__(
        self,
        *,
        account_id: str,
        aws: AwsClientProtocol,
        aws_region: str,
        identity: EnvironmentIdentityProtocol,
        instance_state_get: Callable[[str], str],
        stack: StackManagerProtocol,
    ) -> None:
        """Bind retained storage to one exact development environment."""

        self._account_id = account_id
        self._aws = aws
        self._aws_region = aws_region
        self._identity = identity
        self._instance_state_get = instance_state_get
        self._stack = stack

    def attachment_validate(self) -> None:
        """Prove current retained volume attachment identity."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        state, attachment_list = self.state_get(volume_id=volume_id)
        if (
            state != "in-use"
            or len(attachment_list) != 1
            or attachment_list[0].get("DeleteOnTermination") is not False
            or attachment_list[0].get("Device") != "/dev/sdf"
            or attachment_list[0].get("InstanceId") != instance_id
            or attachment_list[0].get("State") != "attached"
            or attachment_list[0].get("VolumeId") != volume_id
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume is not exactly attached to the current " "stack instance"
            )

    def detach_for_replacement(self) -> None:
        """Detach only after the old instance is proven stopped."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        if self._instance_state_get(instance_id) != "stopped":
            raise DevelopmentEnvironmentError("Retained EBS volume can be detached only from a stopped instance")
        state, attachment_list = self.state_get(volume_id=volume_id)
        if not attachment_list and state == "available":
            return
        if (
            state != "in-use"
            or len(attachment_list) != 1
            or attachment_list[0].get("InstanceId") != instance_id
            or attachment_list[0].get("Device") != "/dev/sdf"
            or attachment_list[0].get("State") != "attached"
            or attachment_list[0].get("DeleteOnTermination") is not False
        ):
            raise DevelopmentEnvironmentError("Retained EBS volume has an unexpected attachment boundary")
        self._aws.run(
            [
                "ec2",
                "detach-volume",
                "--device",
                "/dev/sdf",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
            ]
        )
        self._aws.run(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
        state, attachment_list = self.state_get(volume_id=volume_id)
        if state != "available" or attachment_list:
            raise DevelopmentEnvironmentError("Retained EBS volume detachment was not proven")

    def attachment_ensure(self) -> None:
        """Recover the stack-declared attachment after failed replacement."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        state, attachment_list = self.state_get(volume_id=volume_id)
        if attachment_list:
            self.attachment_validate()
            return
        if state != "available":
            raise DevelopmentEnvironmentError("Retained EBS volume cannot be reattached from its current state")
        self._aws.run(
            [
                "ec2",
                "attach-volume",
                "--device",
                "/dev/sdf",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
            ]
        )
        self._aws.run(["ec2", "wait", "volume-in-use", "--volume-ids", volume_id])
        self.attachment_validate()

    def restore_plan_get(
        self,
        *,
        snapshot_id: str,
    ) -> tuple[str, dict[str, str]]:
        """Select the next declarative restored-volume slot."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        source_volume_id = output_by_name_map.get("RetainedVolumeId")
        current_slot = output_by_name_map.get("RetainedVolumeSlot", "base")
        if not isinstance(source_volume_id, str) or not source_volume_id.startswith("vol-"):
            raise DevelopmentEnvironmentError("Compute stack retained-volume output is malformed")
        next_slot_by_current_slot_map = {
            "a": "b",
            "b": "a",
            "base": "a",
        }
        try:
            next_slot = next_slot_by_current_slot_map[current_slot]
        except KeyError as error:
            raise DevelopmentEnvironmentError("Compute stack retained-volume slot is malformed") from error
        self._snapshot_source_validate(
            snapshot_id=snapshot_id,
            source_volume_id=source_volume_id,
        )
        return source_volume_id, {
            "RetainedVolumeSlot": next_slot,
            "RetainedVolumeSnapshotId": snapshot_id,
        }

    def snapshot_restore_validate(
        self,
        *,
        snapshot_id: str,
        source_volume_id: str,
    ) -> None:
        """Prove restore created a distinct exact-snapshot volume."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        restored_volume_id = output_by_name_map.get("RetainedVolumeId")
        if not isinstance(restored_volume_id, str) or restored_volume_id == source_volume_id:
            raise DevelopmentEnvironmentError("Snapshot restore did not create a distinct retained volume")
        payload = self.payload_get(volume_id=restored_volume_id)
        tag_by_name_map = _tag_by_name_map_get(payload)
        if (
            payload.get("SnapshotId") != snapshot_id
            or payload.get("Encrypted") is not True
            or (
                self._identity.environment_name == "primary"
                and tag_by_name_map.get("workflow-control-center-regular-backup") != "primary"
            )
            or (
                self._identity.environment_name != "primary"
                and "workflow-control-center-regular-backup" in tag_by_name_map
            )
        ):
            raise DevelopmentEnvironmentError("Restored retained volume does not match the exact snapshot contract")

    def regular_backup_exclude(self, *, volume_id: str) -> None:
        """Remove one no-longer-current volume from primary regular backup."""

        state, attachment_list = self.state_get(volume_id=volume_id)
        if state != "available" or attachment_list:
            raise DevelopmentEnvironmentError("Previous retained volume cannot leave the backup set while attached")
        self._aws.run(
            [
                "ec2",
                "delete-tags",
                "--resources",
                volume_id,
                "--tags",
                "Key=workflow-control-center-regular-backup",
            ]
        )
        payload = self.payload_get(volume_id=volume_id)
        if "workflow-control-center-regular-backup" in _tag_by_name_map_get(payload):
            raise DevelopmentEnvironmentError("Previous retained volume still belongs to the regular backup set")

    def retired_cleanup(self, *, current_volume_id: str) -> None:
        """Delete stale, detached rollback volumes before the next rollback."""

        _retained_volume_id_validate(current_volume_id)
        current_volume_payload = self.payload_get(volume_id=current_volume_id)
        payload = self._aws.json_get(
            [
                "ec2",
                "describe-volumes",
                "--filters",
                "Name=tag:Name,Values=workflow-control-center-development-retained",
                "Name=tag:Project,Values=workflow-control-center",
                "Name=tag:Environment,Values=development",
                ("Name=tag:EnvironmentName,Values=" f"{self._identity.environment_name}"),
                "Name=tag:ManagedBy,Values=CloudFormation",
            ]
        )
        volume_list = payload.get("Volumes", [])
        if not isinstance(volume_list, list) or any(not isinstance(volume, dict) for volume in volume_list):
            raise DevelopmentEnvironmentError("Retained rollback volume inventory is malformed")
        for volume_payload in volume_list:
            volume_id = volume_payload.get("VolumeId")
            if volume_id == current_volume_id:
                continue
            if not isinstance(volume_id, str):
                raise DevelopmentEnvironmentError("Retained rollback volume identity is malformed")
            _retained_volume_id_validate(volume_id)
            tag_by_name_map = _tag_by_name_map_get(volume_payload)
            required_tag_by_name_map = {
                "Environment": "development",
                "EnvironmentName": self._identity.environment_name,
                "ManagedBy": "CloudFormation",
                "Name": "workflow-control-center-development-retained",
                "Project": "workflow-control-center",
                "aws:cloudformation:stack-name": (self._identity.compute_stack_name),
            }
            if any(
                tag_by_name_map.get(tag_name) != tag_value for tag_name, tag_value in required_tag_by_name_map.items()
            ):
                raise DevelopmentEnvironmentError(f"Retained rollback volume {volume_id} ownership is ambiguous")
            if (
                volume_payload.get("State") != "available"
                or volume_payload.get("Attachments") != []
                or volume_payload.get("Encrypted") is not True
                or volume_payload.get("Size") != current_volume_payload.get("Size")
                or volume_payload.get("KmsKeyId") != current_volume_payload.get("KmsKeyId")
                or "workflow-control-center-regular-backup" in tag_by_name_map
            ):
                raise DevelopmentEnvironmentError(f"Retained rollback volume {volume_id} is not safe to replace")
            self._aws.run(["ec2", "delete-volume", "--volume-id", volume_id])
            self._aws.run(["ec2", "wait", "volume-deleted", "--volume-ids", volume_id])
            print(f"OK: stale retained rollback volume {volume_id} deleted")

    def regular_backup_status_get(self) -> dict[str, str]:
        """Return exact primary-only AWS Backup policy status."""

        resource_id_by_logical_name_map = self._stack.resource_id_by_logical_name_map_get(
            self._identity.compute_stack_name
        )
        backup_logical_name_set = {
            "RetainedBackupPlan",
            "RetainedBackupRole",
            "RetainedBackupSelection",
            "RetainedBackupVault",
        }
        present_backup_logical_name_set = backup_logical_name_set & resource_id_by_logical_name_map.keys()
        if self._identity.environment_name != "primary":
            if present_backup_logical_name_set:
                raise DevelopmentEnvironmentError(
                    "A non-primary development environment owns regular " "backup resources"
                )
            return {
                "mode": "disabled",
                "plan_id": "",
                "selection_id": "",
                "state": "NOT_APPLICABLE",
            }
        if present_backup_logical_name_set != backup_logical_name_set:
            raise DevelopmentEnvironmentError("Primary compute stack has an incomplete AWS Backup policy")
        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        volume_id = output_by_name_map.get("RetainedVolumeId")
        if not isinstance(volume_id, str):
            raise DevelopmentEnvironmentError("Primary compute stack has no retained volume identity")
        plan_id = resource_id_by_logical_name_map["RetainedBackupPlan"]
        vault_name = resource_id_by_logical_name_map["RetainedBackupVault"]
        plan_payload = self._aws.json_get(["backup", "get-backup-plan", "--backup-plan-id", plan_id])
        backup_plan = plan_payload.get("BackupPlan")
        rule_list = backup_plan.get("Rules") if isinstance(backup_plan, dict) else None
        if not isinstance(rule_list, list) or len(rule_list) != 1 or not isinstance(rule_list[0], dict):
            raise DevelopmentEnvironmentError("AWS Backup retained-volume plan response is malformed")
        rule = rule_list[0]
        lifecycle = rule.get("Lifecycle")
        if (
            rule.get("RuleName") != "daily-retained-volume"
            or rule.get("ScheduleExpression") != "cron(0 3 * * ? *)"
            or rule.get("ScheduleExpressionTimezone") != "UTC"
            or rule.get("StartWindowMinutes") != 60
            or rule.get("CompletionWindowMinutes") != 360
            or rule.get("TargetBackupVaultName") != vault_name
            or rule.get("RecoveryPointTags")
            != {
                "Environment": "development",
                "EnvironmentName": "primary",
                "ManagedBy": "AWSBackup",
                "Name": ("workflow-control-center-development-retained-recovery-point"),
                "Project": "workflow-control-center",
            }
            or not isinstance(lifecycle, dict)
            or lifecycle.get("DeleteAfterDays") != 7
        ):
            raise DevelopmentEnvironmentError("AWS Backup retained-volume plan differs from the development policy")
        selection_list_payload = self._aws.json_get(
            [
                "backup",
                "list-backup-selections",
                "--backup-plan-id",
                plan_id,
            ]
        )
        selection_list = selection_list_payload.get("BackupSelectionsList")
        matching_selection_list = (
            [
                item
                for item in selection_list
                if isinstance(item, dict) and item.get("SelectionName") == "primary-retained-volume"
            ]
            if isinstance(selection_list, list)
            else []
        )
        if len(matching_selection_list) != 1:
            raise DevelopmentEnvironmentError("AWS Backup primary retained-volume selection is unavailable")
        selection_id = matching_selection_list[0].get("SelectionId")
        if not isinstance(selection_id, str) or not selection_id:
            raise DevelopmentEnvironmentError("AWS Backup primary retained-volume selection has no identity")
        selection_payload = self._aws.json_get(
            [
                "backup",
                "get-backup-selection",
                "--backup-plan-id",
                plan_id,
                "--selection-id",
                selection_id,
            ]
        )
        backup_selection = selection_payload.get("BackupSelection")
        expected_resource_arn = f"arn:aws:ec2:{self._aws_region}:{self._account_id}:" f"volume/{volume_id}"
        if (
            not isinstance(backup_selection, dict)
            or backup_selection.get("SelectionName") != "primary-retained-volume"
            or backup_selection.get("Resources") != [expected_resource_arn]
            or backup_selection.get("ListOfTags", []) != []
            or not isinstance(backup_selection.get("IamRoleArn"), str)
            or not backup_selection["IamRoleArn"].endswith("/workflow-control-center-development-aws-backup")
        ):
            raise DevelopmentEnvironmentError("AWS Backup primary retained-volume selection is malformed")
        return {
            "mode": "aws_backup",
            "plan_id": plan_id,
            "selection_id": selection_id,
            "state": "ACTIVE",
        }

    def regular_backup_validate(self) -> None:
        """Require AWS Backup only for the primary development server."""

        status = self.regular_backup_status_get()
        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        volume_id = output_by_name_map.get("RetainedVolumeId")
        if not isinstance(volume_id, str):
            raise DevelopmentEnvironmentError("Compute stack has no retained volume identity")
        volume_payload = self.payload_get(volume_id=volume_id)
        backup_tag_value = _tag_by_name_map_get(volume_payload).get("workflow-control-center-regular-backup")
        if self._identity.environment_name == "primary":
            if status["state"] != "ACTIVE" or backup_tag_value != "primary":
                raise DevelopmentEnvironmentError("Primary retained volume is outside the active AWS Backup policy")
        elif backup_tag_value is not None:
            raise DevelopmentEnvironmentError("A non-primary retained volume belongs to the regular backup policy")

    def latest_snapshot_id_get(self, volume_id: str) -> str:
        """Return the newest owned snapshot for one retained volume."""

        payload = self._aws.json_get(
            [
                "ec2",
                "describe-snapshots",
                "--owner-ids",
                "self",
                "--filters",
                f"Name=volume-id,Values={volume_id}",
            ]
        )
        snapshot_list = payload.get("Snapshots", [])
        if not isinstance(snapshot_list, list) or not snapshot_list:
            return ""
        snapshot_payload = max(
            (item for item in snapshot_list if isinstance(item, dict)),
            key=lambda item: str(item.get("StartTime", "")),
            default={},
        )
        snapshot_id = snapshot_payload.get("SnapshotId", "")
        return snapshot_id if isinstance(snapshot_id, str) else ""

    @staticmethod
    def volume_id_validate(volume_id: str) -> None:
        """Reject malformed EBS volume identities before any use."""

        _retained_volume_id_validate(volume_id)

    def state_get(
        self,
        *,
        volume_id: str,
    ) -> tuple[str, list[dict[str, object]]]:
        """Return exact EBS state and validated attachment records."""

        volume = self.payload_get(volume_id=volume_id)
        state = volume.get("State")
        attachment_list = volume.get("Attachments", [])
        if (
            not isinstance(state, str)
            or not isinstance(attachment_list, list)
            or any(not isinstance(attachment, dict) for attachment in attachment_list)
        ):
            raise DevelopmentEnvironmentError("Retained EBS volume state is malformed")
        return state, list(attachment_list)

    def payload_get(self, *, volume_id: str) -> dict[str, object]:
        """Return one exact retained EBS volume payload."""

        payload = self._aws.json_get(["ec2", "describe-volumes", "--volume-ids", volume_id])
        volume_list = payload.get("Volumes", [])
        if not isinstance(volume_list, list) or len(volume_list) != 1 or not isinstance(volume_list[0], dict):
            raise DevelopmentEnvironmentError("Retained EBS volume response is malformed")
        return volume_list[0]

    def _snapshot_source_validate(
        self,
        *,
        snapshot_id: str,
        source_volume_id: str,
    ) -> None:
        """Prove one snapshot is a usable encrypted restore source."""

        source_payload = self.payload_get(volume_id=source_volume_id)
        payload = self._aws.json_get(["ec2", "describe-snapshots", "--snapshot-ids", snapshot_id])
        snapshot_list = payload.get("Snapshots", [])
        if not isinstance(snapshot_list, list) or len(snapshot_list) != 1 or not isinstance(snapshot_list[0], dict):
            raise DevelopmentEnvironmentError("Retained EBS snapshot response is malformed")
        snapshot_payload = snapshot_list[0]
        if (
            snapshot_payload.get("SnapshotId") != snapshot_id
            or snapshot_payload.get("State") != "completed"
            or snapshot_payload.get("Encrypted") is not True
            or snapshot_payload.get("OwnerId") != self._account_id
            or not isinstance(snapshot_payload.get("VolumeSize"), int)
            or not isinstance(source_payload.get("Size"), int)
            or snapshot_payload["VolumeSize"] > source_payload["Size"]
        ):
            raise DevelopmentEnvironmentError("Retained EBS snapshot is not an exact usable encrypted source")


def _tag_by_name_map_get(payload: dict[str, object]) -> dict[str, str]:
    """Return validated text tags from one AWS resource payload."""

    return {
        tag["Key"]: tag["Value"]
        for tag in payload.get("Tags", [])
        if isinstance(tag, dict) and isinstance(tag.get("Key"), str) and isinstance(tag.get("Value"), str)
    }


def _retained_volume_id_validate(volume_id: str) -> None:
    """Reject malformed EBS volume identities before destructive actions."""

    if re.fullmatch(r"vol-[0-9a-f]+", volume_id) is None:
        raise DevelopmentEnvironmentError(f"Retained EBS volume ID is malformed: {volume_id}")
