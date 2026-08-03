"""Exact AWS resource inventory resolution for one task environment."""

from __future__ import annotations

import re

from workflow_infrastructure.development_environment.cleanup.aws_response import (
    tag_map_get,
)
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    EnvironmentIdentityProtocol,
    StackManagerProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_EC2_INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8,17}")
_EBS_VOLUME_ID_PATTERN = re.compile(r"vol-[0-9a-f]{8,17}")
_BUCKET_OUTPUT_NAME_LIST = (
    "DataBucketName",
    "ObservabilityBucketName",
    "ResultBucketName",
    "SecretBucketName",
)


class CleanupInventoryResolver:
    """Resolve and ownership-check the immutable cleanup inventory."""

    def __init__(
        self,
        *,
        account_id: str,
        identity: EnvironmentIdentityProtocol,
        region: str,
        stack: StackManagerProtocol,
    ) -> None:
        """Initialize the cleanup inventory resolver dependencies.

        Args:
            account_id: Exact account identity.
            identity: Identity.
            region: Region.
            stack: Stack.
        """

        self._account_id = account_id
        self._identity = identity
        self._region = region
        self._stack = stack

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Return exact service identities only after both stacks prove ownership.

        Args:
            request: Validated operation request.

        Returns:
            The exact service identities only after both stacks prove ownership.
        """

        self._stack_identity_validate(self._identity.data_plane_stack_name)
        self._stack_identity_validate(self._identity.compute_stack_name)
        data_output = self._stack.output_by_name_map_get(self._identity.data_plane_stack_name)
        compute_output = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        bucket_value_list = [data_output.get(name) for name in _BUCKET_OUTPUT_NAME_LIST]
        kms_key_arn = data_output.get("StorageKmsKeyArn", "")
        instance_id = compute_output.get("InstanceId", "")
        retained_volume_id = compute_output.get("RetainedVolumeId", "")
        if (
            any(not isinstance(item, str) or not item for item in bucket_value_list)
            or len(set(bucket_value_list)) != len(_BUCKET_OUTPUT_NAME_LIST)
            or not kms_key_arn.startswith(f"arn:aws:kms:{self._region}:{self._account_id}:key/")
            or _EC2_INSTANCE_ID_PATTERN.fullmatch(instance_id) is None
            or _EBS_VOLUME_ID_PATTERN.fullmatch(retained_volume_id) is None
        ):
            raise DevelopmentEnvironmentError("Task stack outputs do not form one exact cleanup inventory")
        return CleanupInventory(
            bucket_name_list=tuple(sorted(bucket_value_list)),
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
        """Require one cleanup stack to carry the exact task worktree identity.

        Args:
            stack_name: Stack name.
        """

        payload = self._stack.payload_get(stack_name, is_required=True)
        parameter_map = self._stack.parameter_by_name_map_get(stack_name)
        tag_map = tag_map_get(payload.get("Tags"))
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
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} has another exact ownership identity")
