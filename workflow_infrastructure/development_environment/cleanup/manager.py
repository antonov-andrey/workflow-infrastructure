"""Resumable exact-resource cleanup for one task development environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Mapping, Protocol, Sequence

from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.s3 import (
    VersionedBucketCleaner,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_EC2_INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8,17}")
_EBS_VOLUME_ID_PATTERN = re.compile(r"vol-[0-9a-f]{8,17}")
_SNAPSHOT_ID_PATTERN = re.compile(r"snap-[0-9a-f]{8,17}")
_PHASE_LIST = (
    "compute",
    "storage",
    "data-stack",
    "retained",
    "kms",
    "verify",
    "complete",
)


class AccountVerifierProtocol(Protocol):
    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class AwsClientProtocol(Protocol):
    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response."""

    def run(
        self, aws_argument_list: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command."""


class CleanupBindingProtocol(Protocol):
    def common_directory_get(self) -> Path:
        """Return the owning repository Git common directory."""


class EnvironmentIdentityProtocol(Protocol):
    compute_stack_name: str
    data_plane_stack_name: str
    environment_name: str
    git_worktree: str
    is_primary: bool


class StackManagerProtocol(Protocol):
    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return one stack's exact outputs."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return one stack's exact parameters."""

    def payload_get(self, stack_name: str, *, is_required: bool) -> dict[str, object]:
        """Return one stack object, or empty when absent."""


class DevelopmentEnvironmentCleanupManager:
    """Delete only the exact AWS resources bound to one task common prefix."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        account_id: str,
        aws: AwsClientProtocol,
        binding: CleanupBindingProtocol,
        identity: EnvironmentIdentityProtocol,
        region: str,
        stack: StackManagerProtocol,
    ) -> None:
        self._account = account
        self._account_id = account_id
        self._aws = aws
        self._binding = binding
        self._identity = identity
        self._region = region
        self._stack = stack
        self._bucket_cleaner = VersionedBucketCleaner(aws)

    def destroy(self, request: CleanupRequest) -> dict[str, object]:
        """Resume deletion and return the closed goal-delete absence proof."""

        if self._identity.is_primary or not self._identity.git_worktree:
            raise DevelopmentEnvironmentError(
                "Task cleanup cannot target the primary development environment"
            )
        if request.common_prefix != self._identity.git_worktree:
            raise DevelopmentEnvironmentError(
                "Task cleanup request and environment identity differ"
            )
        self._account.local_operator_context_validate()
        journal_path = self._journal_path_get(request)
        journal = self._journal_load_or_create(journal_path, request=request)
        inventory = CleanupInventory.from_payload(journal["inventory"])
        while journal["phase"] != "complete":
            phase = journal["phase"]
            if phase == "compute":
                self._compute_delete(inventory)
            elif phase == "storage":
                for bucket_name in inventory.bucket_name_list:
                    self._bucket_cleaner.delete(bucket_name)
            elif phase == "data-stack":
                self._stack_delete(inventory.data_stack_name)
            elif phase == "retained":
                self._retained_delete(inventory)
            elif phase == "kms":
                self._kms_retire(inventory)
            elif phase == "verify":
                self._absence_validate(inventory)
            else:
                raise DevelopmentEnvironmentError(
                    "Task cleanup journal has an unsupported phase"
                )
            journal["phase"] = _PHASE_LIST[_PHASE_LIST.index(phase) + 1]
            _atomic_json_write(journal_path, journal)
        self._absence_validate(inventory)
        return {**request.payload_get(), "external_resources_absent": True}

    def inventory(self, request: CleanupRequest) -> dict[str, object]:
        """Return a non-mutating exact inventory for acceptance diagnostics."""

        if request.common_prefix != self._identity.git_worktree:
            raise DevelopmentEnvironmentError(
                "Task cleanup inventory request and environment identity differ"
            )
        self._account.local_operator_context_validate()
        inventory = self._inventory_get(request)
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

    def _journal_path_get(self, request: CleanupRequest) -> Path:
        return (
            self._binding.common_directory_get()
            / "agent-workflows"
            / "external-cleanup"
            / f"{request.common_prefix}.json"
        )

    def _journal_load_or_create(
        self, journal_path: Path, *, request: CleanupRequest
    ) -> dict[str, object]:
        if journal_path.exists():
            try:
                payload = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DevelopmentEnvironmentError(
                    "Task cleanup journal is unavailable or malformed"
                ) from error
            if (
                not isinstance(payload, dict)
                or set(payload) != {"inventory", "phase", "schema_version"}
                or payload.get("schema_version") != 1
                or payload.get("phase") not in _PHASE_LIST
            ):
                raise DevelopmentEnvironmentError(
                    "Task cleanup journal has another shape"
                )
            inventory = CleanupInventory.from_payload(payload["inventory"])
            if (
                inventory.common_prefix != request.common_prefix
                or inventory.operation_identity != request.operation_identity
            ):
                raise DevelopmentEnvironmentError(
                    "Task cleanup journal belongs to another operation"
                )
            return payload
        inventory = self._inventory_get(request)
        payload = {
            "schema_version": 1,
            "phase": "compute",
            "inventory": inventory.payload_get(),
        }
        _atomic_json_write(journal_path, payload)
        return payload

    def _inventory_get(self, request: CleanupRequest) -> CleanupInventory:
        self._stack_identity_validate(self._identity.data_plane_stack_name)
        self._stack_identity_validate(self._identity.compute_stack_name)
        data_output = self._stack.output_by_name_map_get(
            self._identity.data_plane_stack_name
        )
        compute_output = self._stack.output_by_name_map_get(
            self._identity.compute_stack_name
        )
        bucket_name_list = tuple(
            sorted(
                data_output[name]
                for name in (
                    "DataBucketName",
                    "ObservabilityBucketName",
                    "ResultBucketName",
                    "SecretBucketName",
                )
            )
        )
        kms_key_arn = data_output.get("StorageKmsKeyArn", "")
        instance_id = compute_output.get("InstanceId", "")
        retained_volume_id = compute_output.get("RetainedVolumeId", "")
        if (
            len(bucket_name_list) != 4
            or len(set(bucket_name_list)) != 4
            or any(not item for item in bucket_name_list)
            or not kms_key_arn.startswith(
                f"arn:aws:kms:{self._region}:{self._account_id}:key/"
            )
            or _EC2_INSTANCE_ID_PATTERN.fullmatch(instance_id) is None
            or _EBS_VOLUME_ID_PATTERN.fullmatch(retained_volume_id) is None
        ):
            raise DevelopmentEnvironmentError(
                "Task stack outputs do not form one exact cleanup inventory"
            )
        return CleanupInventory(
            bucket_name_list=bucket_name_list,
            common_prefix=request.common_prefix,
            compute_stack_name=self._identity.compute_stack_name,
            data_stack_name=self._identity.data_plane_stack_name,
            environment_name=self._identity.environment_name,
            instance_id=instance_id,
            kms_alias_name=f"alias/storage-{self._identity.environment_name}",
            kms_key_arn=kms_key_arn,
            operation_identity=request.operation_identity,
            retained_volume_id=retained_volume_id,
        )

    def _stack_identity_validate(self, stack_name: str) -> None:
        payload = self._stack.payload_get(stack_name, is_required=True)
        parameter_map = self._stack.parameter_by_name_map_get(stack_name)
        tag_map = _tag_map_get(payload.get("Tags"))
        if (
            parameter_map.get("EnvironmentName") != self._identity.environment_name
            or parameter_map.get("GitWorktree") != self._identity.git_worktree
            or tag_map
            != {
                "EnvironmentClass": "development",
                "EnvironmentName": self._identity.environment_name,
                "ManagedBy": "CloudFormation",
                "git-worktree": self._identity.git_worktree,
            }
        ):
            raise DevelopmentEnvironmentError(
                f"Task stack {stack_name} has another exact ownership identity"
            )

    def _compute_delete(self, inventory: CleanupInventory) -> None:
        self._session_list_terminate(inventory.instance_id)
        state = self._instance_state_get(inventory.instance_id)
        if state not in {
            "absent",
            "stopped",
            "stopping",
            "terminated",
            "shutting-down",
        }:
            self._aws.run(
                [
                    "ec2",
                    "stop-instances",
                    "--instance-ids",
                    inventory.instance_id,
                ]
            )
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "instance-stopped",
                    "--instance-ids",
                    inventory.instance_id,
                ]
            )
        self._stack_delete(inventory.compute_stack_name)

    def _session_list_terminate(self, instance_id: str) -> None:
        for session_id in self._active_session_id_list_get(instance_id):
            self._aws.run(["ssm", "terminate-session", "--session-id", session_id])

    def _active_session_id_list_get(self, instance_id: str) -> list[str]:
        """Return a complete duplicate-free active Session Manager inventory."""

        next_token = ""
        session_id_list: list[str] = []
        while True:
            argument_list = [
                "ssm",
                "describe-sessions",
                "--state",
                "Active",
                "--filters",
                f"key=Target,value={instance_id}",
            ]
            if next_token:
                argument_list.extend(["--next-token", next_token])
            payload = self._aws.json_get(argument_list)
            session_list = payload.get("Sessions", [])
            if not isinstance(session_list, list) or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("SessionId"), str)
                or item.get("Target") != instance_id
                for item in session_list
            ):
                raise DevelopmentEnvironmentError(
                    "Task Session Manager inventory is malformed"
                )
            session_id_list.extend(item["SessionId"] for item in session_list)
            next_token_value = payload.get("NextToken", "")
            if not isinstance(next_token_value, str):
                raise DevelopmentEnvironmentError(
                    "Task Session Manager pagination is malformed"
                )
            if not next_token_value:
                break
            next_token = next_token_value
        if len(session_id_list) != len(set(session_id_list)):
            raise DevelopmentEnvironmentError(
                "Task Session Manager inventory repeats a session"
            )
        return session_id_list

    def _stack_delete(self, stack_name: str) -> None:
        if not self._stack.payload_get(stack_name, is_required=False):
            return
        self._aws.run(["cloudformation", "delete-stack", "--stack-name", stack_name])
        self._aws.run(
            [
                "cloudformation",
                "wait",
                "stack-delete-complete",
                "--stack-name",
                stack_name,
            ]
        )
        if self._stack.payload_get(stack_name, is_required=False):
            raise DevelopmentEnvironmentError(
                f"Task stack {stack_name} still exists after deletion"
            )

    def _retained_delete(self, inventory: CleanupInventory) -> None:
        self._snapshot_list_delete(inventory)
        result = self._aws.run(
            [
                "ec2",
                "describe-volumes",
                "--volume-ids",
                inventory.retained_volume_id,
            ],
            check=False,
        )
        if result.returncode == 0:
            payload = _json_object_get(result.stdout, label="retained volume")
            volume_list = payload.get("Volumes")
            if (
                not isinstance(volume_list, list)
                or len(volume_list) != 1
                or not isinstance(volume_list[0], Mapping)
                or volume_list[0].get("VolumeId") != inventory.retained_volume_id
                or volume_list[0].get("State") != "available"
                or volume_list[0].get("Attachments", []) != []
            ):
                raise DevelopmentEnvironmentError(
                    "Task retained volume is not safely deletable"
                )
            self._aws.run(
                [
                    "ec2",
                    "delete-volume",
                    "--volume-id",
                    inventory.retained_volume_id,
                ]
            )
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "volume-deleted",
                    "--volume-ids",
                    inventory.retained_volume_id,
                ]
            )
        elif not _is_not_found(result):
            raise DevelopmentEnvironmentError(
                "Task retained volume absence cannot be proven"
            )

    def _snapshot_list_delete(self, inventory: CleanupInventory) -> None:
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
        if not isinstance(snapshot_list, list) or any(
            not isinstance(item, Mapping) for item in snapshot_list
        ):
            raise DevelopmentEnvironmentError("Task snapshot inventory is malformed")
        for item in snapshot_list:
            snapshot_id = item.get("SnapshotId")
            tag_map = _tag_map_get(item.get("Tags"))
            if (
                not isinstance(snapshot_id, str)
                or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
                or tag_map.get("EnvironmentClass") != "development"
                or tag_map.get("EnvironmentName") != inventory.environment_name
                or tag_map.get("git-worktree") != inventory.common_prefix
            ):
                raise DevelopmentEnvironmentError(
                    "Task snapshot ownership is ambiguous"
                )
            self._aws.run(["ec2", "delete-snapshot", "--snapshot-id", snapshot_id])

    def _kms_retire(self, inventory: CleanupInventory) -> None:
        alias_payload = self._aws.json_get(
            ["kms", "list-aliases", "--key-id", inventory.kms_key_arn]
        )
        alias_list = alias_payload.get("Aliases", [])
        if not isinstance(alias_list, list) or any(
            not isinstance(item, Mapping) for item in alias_list
        ):
            raise DevelopmentEnvironmentError("Task KMS alias inventory is malformed")
        custom_alias_name_list = sorted(
            item["AliasName"]
            for item in alias_list
            if isinstance(item.get("AliasName"), str)
            and not item["AliasName"].startswith("alias/aws/")
        )
        if custom_alias_name_list not in ([], [inventory.kms_alias_name]):
            raise DevelopmentEnvironmentError("Task KMS alias ownership is ambiguous")
        if custom_alias_name_list:
            self._aws.run(
                ["kms", "delete-alias", "--alias-name", inventory.kms_alias_name]
            )
        key_payload = self._aws.json_get(
            ["kms", "describe-key", "--key-id", inventory.kms_key_arn]
        )
        metadata = key_payload.get("KeyMetadata")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("Arn") != inventory.kms_key_arn
        ):
            raise DevelopmentEnvironmentError("Task KMS key identity is malformed")
        key_state = metadata.get("KeyState")
        if key_state == "Enabled":
            self._aws.run(["kms", "disable-key", "--key-id", inventory.kms_key_arn])
            key_state = "Disabled"
        if key_state == "Disabled":
            self._aws.run(
                [
                    "kms",
                    "schedule-key-deletion",
                    "--key-id",
                    inventory.kms_key_arn,
                    "--pending-window-in-days",
                    "7",
                ]
            )
            key_payload = self._aws.json_get(
                ["kms", "describe-key", "--key-id", inventory.kms_key_arn]
            )
            metadata = key_payload.get("KeyMetadata")
            key_state = (
                metadata.get("KeyState") if isinstance(metadata, Mapping) else None
            )
        if key_state != "PendingDeletion":
            raise DevelopmentEnvironmentError(
                "Task KMS key did not enter PendingDeletion"
            )

    def _absence_validate(self, inventory: CleanupInventory) -> None:
        for stack_name in (inventory.compute_stack_name, inventory.data_stack_name):
            if self._stack.payload_get(stack_name, is_required=False):
                raise DevelopmentEnvironmentError(
                    f"Task stack {stack_name} still exists"
                )
        if self._instance_state_get(inventory.instance_id) != "absent":
            raise DevelopmentEnvironmentError("Task instance still exists")
        if self._active_session_id_list_get(inventory.instance_id):
            raise DevelopmentEnvironmentError(
                "Task Session Manager sessions remain active"
            )
        for bucket_name in inventory.bucket_name_list:
            result = self._aws.run(
                ["s3api", "head-bucket", "--bucket", bucket_name], check=False
            )
            if result.returncode == 0 or not _is_not_found(result):
                raise DevelopmentEnvironmentError(
                    f"Task bucket {bucket_name} absence is not proven"
                )
        volume_result = self._aws.run(
            [
                "ec2",
                "describe-volumes",
                "--volume-ids",
                inventory.retained_volume_id,
            ],
            check=False,
        )
        if volume_result.returncode == 0 or not _is_not_found(volume_result):
            raise DevelopmentEnvironmentError(
                "Task retained volume absence is not proven"
            )
        snapshot_payload = self._aws.json_get(
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
        if snapshot_payload.get("Snapshots") != []:
            raise DevelopmentEnvironmentError("Task snapshots still exist")
        key_payload = self._aws.json_get(
            ["kms", "describe-key", "--key-id", inventory.kms_key_arn]
        )
        metadata = key_payload.get("KeyMetadata")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("Arn") != inventory.kms_key_arn
            or metadata.get("KeyState") != "PendingDeletion"
        ):
            raise DevelopmentEnvironmentError(
                "Task KMS key PendingDeletion proof is unavailable"
            )
        alias_payload = self._aws.json_get(
            ["kms", "list-aliases", "--key-id", inventory.kms_key_arn]
        )
        if any(
            isinstance(item, Mapping)
            and item.get("AliasName") == inventory.kms_alias_name
            for item in alias_payload.get("Aliases", [])
        ):
            raise DevelopmentEnvironmentError("Task KMS alias still exists")
        tag_payload = self._aws.json_get(
            [
                "resourcegroupstaggingapi",
                "get-resources",
                "--tag-filters",
                f"Key=git-worktree,Values={inventory.common_prefix}",
            ]
        )
        mapping_list = tag_payload.get("ResourceTagMappingList", [])
        if not isinstance(mapping_list, list):
            raise DevelopmentEnvironmentError(
                "Task tagged-resource leak inventory is malformed"
            )
        remaining_arn_set = {
            item.get("ResourceARN")
            for item in mapping_list
            if isinstance(item, Mapping)
        }
        if remaining_arn_set - {inventory.kms_key_arn}:
            raise DevelopmentEnvironmentError(
                "Unexpected git-worktree tagged resources remain after cleanup"
            )

    def _instance_state_get(self, instance_id: str) -> str:
        result = self._aws.run(
            ["ec2", "describe-instances", "--instance-ids", instance_id],
            check=False,
        )
        if result.returncode != 0:
            if _is_not_found(result):
                return "absent"
            raise DevelopmentEnvironmentError("Task instance state cannot be observed")
        payload = _json_object_get(result.stdout, label="task instance")
        reservation_list = payload.get("Reservations", [])
        instance_list = [
            instance
            for reservation in reservation_list
            if isinstance(reservation, Mapping)
            for instance in reservation.get("Instances", [])
            if isinstance(instance, Mapping)
        ]
        if not instance_list:
            return "absent"
        if len(instance_list) != 1 or instance_list[0].get("InstanceId") != instance_id:
            raise DevelopmentEnvironmentError("Task instance inventory is malformed")
        state = instance_list[0].get("State")
        state_name = state.get("Name") if isinstance(state, Mapping) else None
        if not isinstance(state_name, str):
            raise DevelopmentEnvironmentError("Task instance state is malformed")
        return state_name


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.tmp-",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            json.dump(payload, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        if temporary_path is None:
            raise OSError("temporary cleanup journal was not created")
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise DevelopmentEnvironmentError(
            "Task cleanup journal could not be persisted"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _json_object_get(text: str, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise DevelopmentEnvironmentError(f"AWS {label} response is invalid") from error
    if not isinstance(payload, dict):
        raise DevelopmentEnvironmentError(f"AWS {label} response is malformed")
    return payload


def _is_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    diagnostic = f"{result.stdout}\n{result.stderr}"
    return any(
        marker in diagnostic
        for marker in (
            "InvalidInstanceID.NotFound",
            "InvalidVolume.NotFound",
            "NoSuchBucket",
            "Not Found",
            "404",
            "does not exist",
        )
    )


def _tag_map_get(payload: object) -> dict[str, str]:
    if not isinstance(payload, list):
        raise DevelopmentEnvironmentError("Task resource tags are unavailable")
    tag_map: dict[str, str] = {}
    for item in payload:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("Key"), str)
            or not isinstance(item.get("Value"), str)
            or item["Key"] in tag_map
        ):
            raise DevelopmentEnvironmentError("Task resource tags are malformed")
        tag_map[item["Key"]] = item["Value"]
    return tag_map
