"""CloudFormation stack deletion boundary for task cleanup."""

from __future__ import annotations

import re
from typing import Mapping

from workflow_infrastructure.development_environment.cleanup.aws_response import (
    tag_map_get,
    task_ownership_tag_validate,
)
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    AwsClientProtocol,
    StackManagerProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class StackCleanup:
    """Delete and verify exact task CloudFormation stacks."""

    def __init__(self, *, aws: AwsClientProtocol, stack: StackManagerProtocol) -> None:
        """Initialize the stack cleanup dependencies.

        Args:
            aws: Aws.
            stack: Stack.
        """

        self._aws = aws
        self._stack = stack

    def delete(self, inventory: CleanupInventory, stack_name: str) -> None:
        """Delete every current exact task-owned incarnation of one stack name.

        Args:
            inventory: Fresh task identity and account/region fence.
            stack_name: Stack name.
        """

        self._stack_name_validate(inventory, stack_name)
        while True:
            stack_payload = self._stack.payload_get(stack_name, is_required=False)
            if not stack_payload:
                return
            stack_id = self._owned_stack_id_get(inventory, stack_name, stack_payload)
            if stack_payload.get("StackStatus") != "DELETE_IN_PROGRESS":
                self._aws.run(["cloudformation", "delete-stack", "--stack-name", stack_id])
            self._aws.run(
                [
                    "cloudformation",
                    "wait",
                    "stack-delete-complete",
                    "--stack-name",
                    stack_id,
                ]
            )
            if self._stack.payload_get(stack_id, is_required=False):
                raise DevelopmentEnvironmentError(f"Task stack {stack_id} still exists after deletion")

            replacement_payload = self._stack.payload_get(stack_name, is_required=False)
            if not replacement_payload:
                return
            self._owned_stack_id_get(inventory, stack_name, replacement_payload)

    def absence_validate(self, inventory: CleanupInventory, stack_name: str) -> None:
        """Require one exact stack to be absent.

        Args:
            inventory: Fresh task identity and account/region fence.
            stack_name: Stack name.
        """

        if not self.absent_get(inventory, stack_name):
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} still exists after deletion")

    def absent_get(self, inventory: CleanupInventory, stack_name: str) -> bool:
        """Return exact current task-owned CloudFormation absence for one stack."""

        self._stack_name_validate(inventory, stack_name)
        stack_payload = self._stack.payload_get(stack_name, is_required=False)
        if not stack_payload:
            return True
        self._owned_stack_id_get(inventory, stack_name, stack_payload)
        return False

    @staticmethod
    def _stack_name_validate(inventory: CleanupInventory, stack_name: str) -> None:
        """Require one deterministic stack identity from the fresh inventory."""

        if stack_name not in {inventory.compute_stack_name, inventory.data_stack_name}:
            raise DevelopmentEnvironmentError("Task stack name is outside the cleanup inventory")

    @staticmethod
    def _owned_stack_id_get(
        inventory: CleanupInventory,
        stack_name: str,
        stack_payload: Mapping[str, object],
    ) -> str:
        """Re-attest one current stack and return its unique StackId ARN."""

        stack_id = stack_payload.get("StackId")
        stack_id_pattern = re.compile(
            rf"arn:aws:cloudformation:{re.escape(inventory.region)}:{re.escape(inventory.account_id)}:"
            rf"stack/{re.escape(stack_name)}/[A-Za-z0-9-]+"
        )
        if (
            stack_payload.get("StackName") != stack_name
            or not isinstance(stack_id, str)
            or stack_id_pattern.fullmatch(stack_id) is None
        ):
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} identity is malformed")
        task_ownership_tag_validate(
            tag_map_get(stack_payload.get("Tags")),
            common_prefix=inventory.common_prefix,
            environment_name=inventory.environment_name,
            label=f"stack {stack_name}",
        )
        parameter_list = stack_payload.get("Parameters")
        if not isinstance(parameter_list, list):
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} parameters are malformed")
        parameter_map: dict[str, str] = {}
        for item in parameter_list:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("ParameterKey"), str)
                or not isinstance(item.get("ParameterValue"), str)
                or item["ParameterKey"] in parameter_map
            ):
                raise DevelopmentEnvironmentError(f"Task stack {stack_name} parameters are malformed")
            parameter_map[item["ParameterKey"]] = item["ParameterValue"]
        if (
            parameter_map.get("EnvironmentName") != inventory.environment_name
            or parameter_map.get("GitWorktree") != inventory.common_prefix
        ):
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} has another ownership identity")
        return stack_id
