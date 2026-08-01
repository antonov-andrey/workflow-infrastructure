"""Authorize one-time retained-volume filesystem initialization."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_COMPLETE_STATE = "complete"
_PENDING_STATE = "pending"


class AwsClientProtocol(Protocol):
    """AWS read surface required for exact volume validation."""

    def json_get(self, argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI operation and decode its object response."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment identity required by retained-volume authorization."""

    compute_stack_name: str
    environment_name: str
    git_worktree: str


class StackManagerProtocol(Protocol):
    """CloudFormation surface required by the monotonic state transition."""

    def apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
        protected_identity_logical_id_set: Collection[str] = (),
    ) -> None:
        """Apply one exact stack transition."""

    def drift_validate(self, stack_name: str) -> None:
        """Prove one stack is in sync."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack parameters."""


class DevelopmentHostStorageInitialization:
    """Own the fail-closed pending-to-complete XFS authorization."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        compute_stable_identity_logical_id_set: Collection[str],
        compute_template_path: Path,
        identity: EnvironmentIdentityProtocol,
        stack: StackManagerProtocol,
    ) -> None:
        """Bind authorization to one exact CloudFormation-owned volume."""

        self._aws = aws
        self._compute_stable_identity_logical_id_set = frozenset(
            compute_stable_identity_logical_id_set
        )
        self._compute_template_path = compute_template_path
        self._identity = identity
        self._stack = stack

    def initialization_allowed_get(self) -> bool:
        """Return whether the exact new base volume remains pending."""

        parameter_by_name = self._stack.parameter_by_name_map_get(
            self._identity.compute_stack_name
        )
        output_by_name = self._stack.output_by_name_map_get(
            self._identity.compute_stack_name
        )
        state = parameter_by_name.get("RetainedVolumeFilesystemState")
        volume_id = output_by_name.get("RetainedVolumeId")
        instance_id = output_by_name.get("InstanceId")
        slot = output_by_name.get("RetainedVolumeSlot")
        source_snapshot_id = output_by_name.get("RetainedVolumeSourceSnapshotId")
        if (
            state not in {_COMPLETE_STATE, _PENDING_STATE}
            or output_by_name.get("RetainedVolumeFilesystemState") != state
            or not isinstance(volume_id, str)
            or not volume_id.startswith("vol-")
            or not isinstance(instance_id, str)
            or not instance_id.startswith("i-")
            or slot not in {"a", "b", "base"}
            or not isinstance(source_snapshot_id, str)
        ):
            raise DevelopmentEnvironmentError(
                "Retained-volume filesystem authorization state is malformed"
            )
        volume_payload = self._volume_payload_get(volume_id)
        tag_by_name = _tag_by_name_map_get(volume_payload)
        expected_tag_by_name = {
            "EnvironmentClass": "development",
            "EnvironmentName": self._identity.environment_name,
            "FilesystemState": state,
            "ManagedBy": "CloudFormation",
            "Name": f"retained-{self._identity.environment_name}",
            "aws:cloudformation:stack-name": self._identity.compute_stack_name,
        }
        if self._identity.git_worktree:
            expected_tag_by_name["git-worktree"] = self._identity.git_worktree
        attachment_list = volume_payload.get("Attachments")
        attachment = (
            attachment_list[0]
            if isinstance(attachment_list, list)
            and len(attachment_list) == 1
            and isinstance(attachment_list[0], Mapping)
            else {}
        )
        if (
            volume_payload.get("Encrypted") is not True
            or volume_payload.get("State") != "in-use"
            or any(
                tag_by_name.get(name) != value
                for name, value in expected_tag_by_name.items()
            )
            or (not self._identity.git_worktree and "git-worktree" in tag_by_name)
            or not isinstance(attachment_list, list)
            or len(attachment_list) != 1
            or attachment.get("DeleteOnTermination") is not False
            or attachment.get("Device") != "/dev/sdf"
            or attachment.get("InstanceId") != instance_id
            or attachment.get("State") != "attached"
            or attachment.get("VolumeId") != volume_id
        ):
            raise DevelopmentEnvironmentError(
                "Retained volume does not match its filesystem authorization owner"
            )
        actual_snapshot_id = volume_payload.get("SnapshotId")
        if actual_snapshot_id != source_snapshot_id:
            raise DevelopmentEnvironmentError(
                "Retained volume differs from its declared snapshot source"
            )
        if state == _PENDING_STATE and (
            slot != "base" or source_snapshot_id or actual_snapshot_id
        ):
            raise DevelopmentEnvironmentError(
                "Only a new base retained volume may remain pending initialization"
            )
        return state == _PENDING_STATE

    def complete(self) -> None:
        """Advance a successfully bootstrapped volume to complete exactly once."""

        if not self.initialization_allowed_get():
            return
        volume_id = self._stack.output_by_name_map_get(
            self._identity.compute_stack_name
        )["RetainedVolumeId"]
        self._stack.apply(
            stack_name=self._identity.compute_stack_name,
            template_path=self._compute_template_path,
            parameter_by_name_map={
                "RetainedVolumeFilesystemState": _COMPLETE_STATE,
            },
            must_preserve_resource=False,
            protected_identity_logical_id_set=(
                self._compute_stable_identity_logical_id_set
            ),
        )
        self._stack.drift_validate(self._identity.compute_stack_name)
        current_volume_id = self._stack.output_by_name_map_get(
            self._identity.compute_stack_name
        ).get("RetainedVolumeId")
        if current_volume_id != volume_id or self.initialization_allowed_get():
            raise DevelopmentEnvironmentError(
                "Retained-volume filesystem completion was not proven"
            )
        print(f"OK: retained volume {volume_id} filesystem state is complete")

    def _volume_payload_get(self, volume_id: str) -> Mapping[str, object]:
        payload = self._aws.json_get(
            ["ec2", "describe-volumes", "--volume-ids", volume_id]
        )
        volume_list = payload.get("Volumes")
        if (
            not isinstance(volume_list, list)
            or len(volume_list) != 1
            or not isinstance(volume_list[0], Mapping)
            or volume_list[0].get("VolumeId") != volume_id
        ):
            raise DevelopmentEnvironmentError(
                "Retained-volume filesystem authorization target is unavailable"
            )
        return volume_list[0]


def _tag_by_name_map_get(payload: Mapping[str, object]) -> dict[str, str]:
    tag_list = payload.get("Tags")
    if not isinstance(tag_list, list):
        raise DevelopmentEnvironmentError("Retained volume tags are malformed")
    tag_by_name: dict[str, str] = {}
    for tag in tag_list:
        if (
            not isinstance(tag, Mapping)
            or not isinstance(tag.get("Key"), str)
            or not isinstance(tag.get("Value"), str)
            or tag["Key"] in tag_by_name
        ):
            raise DevelopmentEnvironmentError("Retained volume tags are malformed")
        tag_by_name[tag["Key"]] = tag["Value"]
    return tag_by_name
