"""EC2 and Session Manager cleanup for one task environment."""

from __future__ import annotations

import re
from typing import Mapping, Protocol

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


class StackCleanupProtocol(Protocol):
    """Declare the stack cleanup interface."""

    def delete(self, inventory: CleanupInventory, stack_name: str) -> None:
        """Delete one exact stack.

        Args:
            inventory: Fresh task identity and account/region fence.
            stack_name: Stack name.
        """


class ComputeCleanup:
    """Stop task compute safely before delegating its stack deletion."""

    def __init__(self, *, aws: AwsClientProtocol, stack_cleanup: StackCleanupProtocol) -> None:
        """Initialize the compute cleanup dependencies.

        Args:
            aws: Aws.
            stack_cleanup: Stack cleanup.
        """

        self._aws = aws
        self._stack_cleanup = stack_cleanup

    def delete(self, inventory: CleanupInventory) -> None:
        """Terminate active sessions, stop compute, and delete its stack.

        Args:
            inventory: Inventory.
        """

        instance_id_list = list(inventory.instance_id_list)
        for instance_id in instance_id_list:
            self._instance_stop_as_necessary(inventory, instance_id)
            self.session_list_terminate(inventory, [instance_id])
            self.session_absence_validate(inventory, [instance_id])

        self.session_absence_validate(inventory, instance_id_list)
        self._stack_cleanup.delete(inventory, inventory.compute_stack_name)
        for instance_id in instance_id_list:
            state = self._instance_stop_as_necessary(inventory, instance_id)
            self.session_list_terminate(inventory, [instance_id])
            self.session_absence_validate(inventory, [instance_id])
            if state in {"absent", "terminated"}:
                continue
            if state != "shutting-down":
                state = self._instance_state_get(inventory, instance_id)
                if state in {"absent", "terminated"}:
                    continue
                self._aws.run(["ec2", "terminate-instances", "--instance-ids", instance_id])
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "instance-terminated",
                    "--instance-ids",
                    instance_id,
                ]
            )

    def _instance_stop_as_necessary(self, inventory: CleanupInventory, instance_id: str) -> str:
        """Stop one running task instance and return a freshly attested safe state."""

        state = self._instance_state_get(inventory, instance_id)
        if state == "running":
            self._aws.run(["ec2", "stop-instances", "--instance-ids", instance_id])
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "instance-stopped",
                    "--instance-ids",
                    instance_id,
                ]
            )
            state = self._instance_state_get(inventory, instance_id)
        elif state == "stopping":
            self._aws.run(
                [
                    "ec2",
                    "wait",
                    "instance-stopped",
                    "--instance-ids",
                    instance_id,
                ]
            )
            state = self._instance_state_get(inventory, instance_id)
        if state not in {"absent", "pending", "stopped", "terminated", "shutting-down"}:
            raise DevelopmentEnvironmentError("Task instance did not reach a cleanup-safe lifecycle state")
        return state

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Require the task instance to be deleted and its sessions to be absent.

        EC2 retains a read-only ``terminated`` tombstone for a bounded service
        interval after CloudFormation has deleted the instance.  That state is
        the service proof that the resource can no longer run or be recovered;
        waiting for the tombstone itself to disappear would make goal cleanup
        depend on an unrelated AWS visibility delay.

        Args:
            inventory: Inventory.
        """

        if not self.absent_get(inventory):
            raise DevelopmentEnvironmentError("Task instance or Session Manager session still exists")

    def session_absence_validate(self, inventory: CleanupInventory, instance_id_list: list[str]) -> None:
        """Require fresh active-session absence for exact previously owned instances.

        Args:
            inventory: Current task identity used to reject a now-foreign instance.
            instance_id_list: Sorted identities reconstructed from the fresh task inventory.
        """

        self._instance_id_list_validate(instance_id_list)
        for instance_id in instance_id_list:
            self._instance_state_get(inventory, instance_id)
            if self.active_session_id_list_get(instance_id):
                raise DevelopmentEnvironmentError("Task Session Manager session still exists")

    def session_list_terminate(self, inventory: CleanupInventory, instance_id_list: list[str]) -> None:
        """Terminate active and disconnected sessions for exact previously owned instances.

        Args:
            inventory: Current task identity used to reject a now-foreign instance.
            instance_id_list: Sorted identities reconstructed from the fresh task inventory.
        """

        self._instance_id_list_validate(instance_id_list)
        for instance_id in instance_id_list:
            self._instance_state_get(inventory, instance_id)
            for session_id in self.active_session_id_list_get(instance_id):
                self._aws.run(["ssm", "terminate-session", "--session-id", session_id])

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Return exact current compute and Session Manager absence."""

        absent = True
        for instance_id in inventory.instance_id_list:
            state = self._instance_state_get(inventory, instance_id)
            if state not in {
                "absent",
                "pending",
                "running",
                "shutting-down",
                "stopped",
                "stopping",
                "terminated",
            }:
                raise DevelopmentEnvironmentError("Task instance has an unsupported lifecycle state")
            if state not in {"absent", "terminated"}:
                absent = False
            if self.active_session_id_list_get(instance_id):
                absent = False
        return absent

    def active_session_id_list_get(self, instance_id: str) -> list[str]:
        """Return a complete duplicate-free active Session Manager inventory.

        Args:
            instance_id: Exact instance identity.

        Returns:
            A complete duplicate-free active Session Manager inventory.
        """

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
                or item.get("Status") not in {"Connected", "Connecting", "Disconnected", "Terminating"}
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

    def _instance_id_list_validate(self, instance_id_list: list[str]) -> None:
        """Require one sorted duplicate-free subset of current inventory identities.

        Args:
            instance_id_list: Instance identities retained by the cleanup workflow.
        """

        if (
            not isinstance(instance_id_list, list)
            or instance_id_list != sorted(instance_id_list)
            or len(instance_id_list) != len(set(instance_id_list))
            or any(re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id) is None for instance_id in instance_id_list)
        ):
            raise DevelopmentEnvironmentError("Task cleanup instance progress is malformed")

    def _instance_state_get(self, inventory: CleanupInventory, instance_id: str) -> str:
        """Return one exact task-owned EC2 state or the synthetic absent state.

        Args:
            inventory: Fresh live task inventory.
            instance_id: Exact instance identity.

        Returns:
            One exact EC2 state or the synthetic absent state.
        """

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
        if reservation_list == []:
            return "absent"
        if not isinstance(reservation_list, list) or len(reservation_list) != 1:
            raise DevelopmentEnvironmentError("Task instance inventory is malformed")
        reservation = reservation_list[0]
        instance_list = reservation.get("Instances") if isinstance(reservation, Mapping) else None
        if (
            not isinstance(reservation, Mapping)
            or reservation.get("OwnerId") != inventory.account_id
            or not isinstance(instance_list, list)
            or len(instance_list) != 1
            or not isinstance(instance_list[0], Mapping)
            or instance_list[0].get("InstanceId") != instance_id
        ):
            raise DevelopmentEnvironmentError("Task instance inventory is malformed")
        instance = instance_list[0]
        placement = instance.get("Placement")
        availability_zone = placement.get("AvailabilityZone") if isinstance(placement, Mapping) else None
        if not isinstance(availability_zone, str) or not availability_zone.startswith(inventory.region):
            raise DevelopmentEnvironmentError("Task instance belongs to another region")
        task_ownership_tag_validate(
            tag_map_get(instance.get("Tags")),
            common_prefix=inventory.common_prefix,
            environment_name=inventory.environment_name,
            label="EC2 instance",
        )
        state = instance.get("State")
        state_name = state.get("Name") if isinstance(state, Mapping) else None
        if not isinstance(state_name, str):
            raise DevelopmentEnvironmentError("Task instance state is malformed")
        return state_name
