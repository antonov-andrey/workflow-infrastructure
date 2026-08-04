"""Exact idempotent AWS resource inventory for one task environment."""

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
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    AwsClientProtocol,
    EnvironmentIdentityProtocol,
    StackManagerProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_EC2_INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8,17}")
_EBS_VOLUME_ID_PATTERN = re.compile(r"vol-[0-9a-f]{8,17}")
_BUCKET_OUTPUT_BY_SUFFIX = {
    "data": "DataBucketName",
    "observability": "ObservabilityBucketName",
    "result": "ResultBucketName",
    "secret": "SecretBucketName",
}


class CleanupInventoryResolver:
    """Resolve every remaining task-owned cleanup resource."""

    def __init__(
        self,
        *,
        account_id: str,
        aws: AwsClientProtocol,
        identity: EnvironmentIdentityProtocol,
        region: str,
        stack: StackManagerProtocol,
    ) -> None:
        """Initialize the cleanup inventory resolver dependencies.

        Args:
            account_id: Exact account identity.
            aws: AWS client.
            identity: Identity.
            region: Region.
            stack: Stack.
        """

        self._account_id = account_id
        self._aws = aws
        self._identity = identity
        self._region = region
        self._stack = stack

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Return all known and discovered task-owned resource identities.

        Stack outputs are useful identities while a stack exists, but deletion
        never depends on retaining either stack. Deterministic names and exact
        task tags recover retained resources after any partial cleanup.

        Args:
            request: Validated operation request.

        Returns:
            Every remaining task-owned cleanup resource identity.
        """

        data_output = self._owned_stack_output_get(self._identity.data_plane_stack_name)
        compute_output = self._owned_stack_output_get(self._identity.compute_stack_name)
        bucket_name_by_suffix = {
            suffix: f"{self._account_id}-{self._region}-{self._identity.environment_name}-{suffix}"
            for suffix in _BUCKET_OUTPUT_BY_SUFFIX
        }
        if data_output is not None and any(
            output_name in data_output and data_output[output_name] != bucket_name_by_suffix[suffix]
            for suffix, output_name in _BUCKET_OUTPUT_BY_SUFFIX.items()
        ):
            raise DevelopmentEnvironmentError("Task data stack bucket outputs differ from deterministic identities")

        instance_id_set = set(self._tagged_instance_id_list_get())
        retained_volume_id_set = set(self._tagged_retained_volume_id_list_get())
        kms_key_arn_set = set(self._tagged_kms_key_arn_list_get())
        if compute_output is not None:
            if "InstanceId" in compute_output:
                instance_id_set.add(_pattern_value_get(compute_output, "InstanceId", _EC2_INSTANCE_ID_PATTERN))
            if "RetainedVolumeId" in compute_output:
                retained_volume_id_set.add(
                    _pattern_value_get(compute_output, "RetainedVolumeId", _EBS_VOLUME_ID_PATTERN)
                )
        if data_output is not None and "StorageKmsKeyArn" in data_output:
            kms_key_arn_set.add(self._kms_key_arn_validate(data_output.get("StorageKmsKeyArn")))
        alias_key_arn = self._alias_key_arn_get()
        if alias_key_arn:
            kms_key_arn_set.add(alias_key_arn)

        return CleanupInventory(
            bucket_name_list=tuple(sorted(bucket_name_by_suffix.values())),
            common_prefix=request.common_prefix,
            compute_stack_name=self._identity.compute_stack_name,
            data_stack_name=self._identity.data_plane_stack_name,
            environment_name=self._identity.environment_name,
            instance_id_list=tuple(sorted(instance_id_set)),
            kms_alias_name=f"alias/storage-{self._identity.environment_name}",
            kms_key_arn_list=tuple(sorted(kms_key_arn_set)),
            operation_identity=request.operation_identity,
            retained_volume_id_list=tuple(sorted(retained_volume_id_set)),
        )

    def _alias_key_arn_get(self) -> str:
        """Return the exact task-tagged key targeted by the deterministic alias.

        Returns:
            Exact key ARN or an empty value when the alias is already absent.
        """

        alias_name = f"alias/storage-{self._identity.environment_name}"
        result = self._aws.run(["kms", "describe-key", "--key-id", alias_name], check=False)
        if result.returncode != 0:
            if aws_cli_error_matches(
                result,
                code_set=frozenset({"NotFoundException"}),
                operation="DescribeKey",
            ):
                return ""
            raise DevelopmentEnvironmentError("Task KMS alias target cannot be observed")
        payload = json_object_get(result.stdout, label="task KMS alias target")
        metadata = payload.get("KeyMetadata")
        key_arn = metadata.get("Arn") if isinstance(metadata, Mapping) else None
        validated_key_arn = self._kms_key_arn_validate(key_arn)
        tag_payload = self._aws.json_get(["kms", "list-resource-tags", "--key-id", validated_key_arn])
        self._task_tag_validate(
            tag_map_get(
                tag_payload.get("Tags"),
                key_name="TagKey",
                value_name="TagValue",
            ),
            label="KMS alias target",
        )
        return validated_key_arn

    def _kms_key_arn_validate(self, value: object) -> str:
        """Return one exact in-account KMS key ARN.

        Args:
            value: Candidate value.

        Returns:
            Exact KMS key ARN.
        """

        prefix = f"arn:aws:kms:{self._region}:{self._account_id}:key/"
        if not isinstance(value, str) or not value.startswith(prefix) or not value.removeprefix(prefix):
            raise DevelopmentEnvironmentError("Task KMS key identity is malformed")
        return value

    def _owned_stack_output_get(self, stack_name: str) -> dict[str, str] | None:
        """Return outputs after exact stack ownership proof, or none when absent.

        Args:
            stack_name: Exact stack name.

        Returns:
            Output values by logical name, or none when the stack is absent.
        """

        payload = self._stack.payload_get(stack_name, is_required=False)
        if not payload:
            return None
        parameter_map = _name_value_map_get(
            payload.get("Parameters"),
            key_name="ParameterKey",
            label=f"stack {stack_name} parameters",
            value_name="ParameterValue",
        )
        self._task_tag_validate(tag_map_get(payload.get("Tags")), label=f"stack {stack_name}")
        if (
            parameter_map.get("EnvironmentName") != self._identity.environment_name
            or parameter_map.get("GitWorktree") != self._identity.git_worktree
        ):
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} has another ownership identity")
        return _name_value_map_get(
            payload.get("Outputs", []),
            key_name="OutputKey",
            label=f"stack {stack_name} outputs",
            value_name="OutputValue",
        )

    def _tagged_instance_id_list_get(self) -> list[str]:
        """Return every live EC2 instance carrying the exact task identity.

        Returns:
            Sorted task instance identities.
        """

        payload = self._aws.json_get(
            [
                "ec2",
                "describe-instances",
                "--filters",
                f"Name=tag:EnvironmentName,Values={self._identity.environment_name}",
                f"Name=tag:git-worktree,Values={self._identity.git_worktree}",
                "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped",
            ]
        )
        reservation_list = payload.get("Reservations", [])
        if not isinstance(reservation_list, list):
            raise DevelopmentEnvironmentError("Task instance discovery is malformed")
        instance_id_list: list[str] = []
        for reservation in reservation_list:
            instance_list = reservation.get("Instances") if isinstance(reservation, Mapping) else None
            if not isinstance(instance_list, list):
                raise DevelopmentEnvironmentError("Task instance discovery is malformed")
            for instance in instance_list:
                if not isinstance(instance, Mapping):
                    raise DevelopmentEnvironmentError("Task instance discovery is malformed")
                self._task_tag_validate(tag_map_get(instance.get("Tags")), label="EC2 instance")
                instance_id_list.append(_pattern_value_get(instance, "InstanceId", _EC2_INSTANCE_ID_PATTERN))
        return _unique_sorted_get(instance_id_list, label="Task instance discovery")

    def _tagged_kms_key_arn_list_get(self) -> list[str]:
        """Return every KMS key carrying the exact task identity.

        Returns:
            Sorted task KMS key ARNs.
        """

        payload = self._aws.json_get(
            [
                "resourcegroupstaggingapi",
                "get-resources",
                "--resource-type-filters",
                "kms:key",
                "--tag-filters",
                f"Key=EnvironmentName,Values={self._identity.environment_name}",
                f"Key=git-worktree,Values={self._identity.git_worktree}",
            ]
        )
        mapping_list = payload.get("ResourceTagMappingList", [])
        if not isinstance(mapping_list, list):
            raise DevelopmentEnvironmentError("Task KMS key discovery is malformed")
        key_arn_list: list[str] = []
        for item in mapping_list:
            if not isinstance(item, Mapping):
                raise DevelopmentEnvironmentError("Task KMS key discovery is malformed")
            self._task_tag_validate(tag_map_get(item.get("Tags")), label="KMS key")
            key_arn_list.append(self._kms_key_arn_validate(item.get("ResourceARN")))
        return _unique_sorted_get(key_arn_list, label="Task KMS key discovery")

    def _tagged_retained_volume_id_list_get(self) -> list[str]:
        """Return every retained EBS volume carrying the exact task identity.

        Returns:
            Sorted task retained-volume identities.
        """

        payload = self._aws.json_get(
            [
                "ec2",
                "describe-volumes",
                "--filters",
                f"Name=tag:EnvironmentName,Values={self._identity.environment_name}",
                f"Name=tag:git-worktree,Values={self._identity.git_worktree}",
                f"Name=tag:Name,Values=retained-{self._identity.environment_name}",
            ]
        )
        volume_list = payload.get("Volumes", [])
        if not isinstance(volume_list, list):
            raise DevelopmentEnvironmentError("Task retained-volume discovery is malformed")
        volume_id_list: list[str] = []
        for item in volume_list:
            if not isinstance(item, Mapping):
                raise DevelopmentEnvironmentError("Task retained-volume discovery is malformed")
            tag_map = tag_map_get(item.get("Tags"))
            self._task_tag_validate(tag_map, label="retained volume")
            if tag_map.get("Name") != f"retained-{self._identity.environment_name}":
                raise DevelopmentEnvironmentError("Task retained-volume name is ambiguous")
            volume_id_list.append(_pattern_value_get(item, "VolumeId", _EBS_VOLUME_ID_PATTERN))
        return _unique_sorted_get(volume_id_list, label="Task retained-volume discovery")

    def _task_tag_validate(self, tag_map: Mapping[str, str], *, label: str) -> None:
        """Require the minimum exact task ownership tags and allow unrelated metadata.

        Args:
            tag_map: Tag values by name.
            label: Diagnostic owner label.
        """

        if any(
            tag_map.get(name) != value
            for name, value in {
                "EnvironmentClass": "development",
                "EnvironmentName": self._identity.environment_name,
                "ManagedBy": "CloudFormation",
                "git-worktree": self._identity.git_worktree,
            }.items()
        ):
            raise DevelopmentEnvironmentError(f"Task {label} has another ownership identity")


def _name_value_map_get(
    payload: object,
    *,
    key_name: str,
    label: str,
    value_name: str,
) -> dict[str, str]:
    """Decode one duplicate-free AWS name/value list.

    Args:
        payload: Structured AWS list.
        key_name: Name field.
        label: Diagnostic owner label.
        value_name: Value field.

    Returns:
        Values by unique name.
    """

    if not isinstance(payload, list):
        raise DevelopmentEnvironmentError(f"Task {label} are malformed")
    value_by_name_map: dict[str, str] = {}
    for item in payload:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get(key_name), str)
            or not isinstance(item.get(value_name), str)
            or item[key_name] in value_by_name_map
        ):
            raise DevelopmentEnvironmentError(f"Task {label} are malformed")
        value_by_name_map[item[key_name]] = item[value_name]
    return value_by_name_map


def _pattern_value_get(payload: Mapping[str, object], name: str, pattern: re.Pattern[str]) -> str:
    """Return one exact patterned identity from a mapping.

    Args:
        payload: Structured value owner.
        name: Field name.
        pattern: Required full-match pattern.

    Returns:
        Exact patterned identity.
    """

    value = payload.get(name)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DevelopmentEnvironmentError(f"Task {name} identity is malformed")
    return value


def _unique_sorted_get(value_list: list[str], *, label: str) -> list[str]:
    """Return sorted values after rejecting a repeated service identity.

    Args:
        value_list: Candidate values.
        label: Diagnostic owner label.

    Returns:
        Sorted unique values.
    """

    if len(value_list) != len(set(value_list)):
        raise DevelopmentEnvironmentError(f"{label} repeats an identity")
    return sorted(value_list)
