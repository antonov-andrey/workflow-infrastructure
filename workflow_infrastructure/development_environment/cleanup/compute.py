"""EC2 and Session Manager cleanup for one task environment."""

from __future__ import annotations

from typing import Mapping, Protocol

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
from workflow_infrastructure.development_environment.cleanup.aws_response import (
    json_object_get,
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


class StackCleanupProtocol(Protocol):
    def delete(self, stack_name: str) -> None:
        """Delete one exact stack."""


class ComputeCleanup:
    """Stop task compute safely before delegating its stack deletion."""

    def __init__(self, *, aws: AwsClientProtocol, stack_cleanup: StackCleanupProtocol) -> None:
        self._aws = aws
        self._stack_cleanup = stack_cleanup

    def delete(self, inventory: CleanupInventory) -> None:
        """Terminate active sessions, stop compute, and delete its stack."""

        self._session_list_terminate(inventory.instance_id)
        state = self.instance_state_get(inventory.instance_id)
        if state not in {
            "absent",
            "stopped",
            "stopping",
            "terminated",
            "shutting-down",
        }:
            self._aws.run(["ec2", "stop-instances", "--instance-ids", inventory.instance_id])
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "instance-stopped",
                    "--instance-ids",
                    inventory.instance_id,
                ]
            )
        self._stack_cleanup.delete(inventory.compute_stack_name)

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Require the task instance to be deleted and its sessions to be absent.

        EC2 retains a read-only ``terminated`` tombstone for a bounded service
        interval after CloudFormation has deleted the instance.  That state is
        the service proof that the resource can no longer run or be recovered;
        waiting for the tombstone itself to disappear would make goal cleanup
        depend on an unrelated AWS visibility delay.
        """

        if self.instance_state_get(inventory.instance_id) not in {
            "absent",
            "terminated",
        }:
            raise DevelopmentEnvironmentError("Task instance still exists")
        if self.active_session_id_list_get(inventory.instance_id):
            raise DevelopmentEnvironmentError("Task Session Manager sessions remain active")

    def active_session_id_list_get(self, instance_id: str) -> list[str]:
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
                raise DevelopmentEnvironmentError("Task Session Manager inventory is malformed")
            session_id_list.extend(item["SessionId"] for item in session_list)
            next_token_value = payload.get("NextToken", "")
            if not isinstance(next_token_value, str):
                raise DevelopmentEnvironmentError("Task Session Manager pagination is malformed")
            if not next_token_value:
                break
            next_token = next_token_value
        if len(session_id_list) != len(set(session_id_list)):
            raise DevelopmentEnvironmentError("Task Session Manager inventory repeats a session")
        return session_id_list

    def instance_state_get(self, instance_id: str) -> str:
        """Return one exact EC2 state or the synthetic absent state."""

        result = self._aws.run(
            ["ec2", "describe-instances", "--instance-ids", instance_id],
            check=False,
        )
        if result.returncode != 0:
            if aws_cli_error_matches(
                result,
                code_set=frozenset({"InvalidInstanceID.NotFound"}),
                operation="DescribeInstances",
            ):
                return "absent"
            raise DevelopmentEnvironmentError("Task instance state cannot be observed")
        payload = json_object_get(result.stdout, label="task instance")
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

    def _session_list_terminate(self, instance_id: str) -> None:
        for session_id in self.active_session_id_list_get(instance_id):
            self._aws.run(["ssm", "terminate-session", "--session-id", session_id])
