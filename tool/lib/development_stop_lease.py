"""External fail-safe stop lease for one development environment."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from tool.lib.development_aws import DevelopmentAwsClient
from tool.lib.development_environment_error import DevelopmentEnvironmentError


class ClockProtocol(Protocol):
    """Controlled UTC and wait boundary."""

    def now(self) -> datetime:
        """Return current time."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a bounded duration."""


class IdentityProtocol(Protocol):
    """Environment identities used by Scheduler."""

    compute_stack_name: str
    lease_group_name: str
    lease_name: str


class StackReaderProtocol(Protocol):
    """CloudFormation output reader used by lease renewal."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack outputs."""


class DevelopmentStopLeaseManager:
    """Own creation, proof, expiry waits, and deletion of the stop lease."""

    def __init__(
        self,
        *,
        aws: DevelopmentAwsClient,
        clock: ClockProtocol,
        identity: IdentityProtocol,
        instance_state_get: Callable[[str], str],
        lease_duration: timedelta,
        poll_interval_seconds: int,
        stack: StackReaderProtocol,
    ) -> None:
        """Bind one lease manager to one environment."""

        self._aws = aws
        self._clock = clock
        self._identity = identity
        self._instance_state_get = instance_state_get
        self._lease_duration = lease_duration
        self._poll_interval_seconds = poll_interval_seconds
        self._stack = stack

    def delete(self) -> None:
        """Delete the current stop lease if it exists."""

        result = self._aws.run(
            [
                "scheduler",
                "delete-schedule",
                "--group-name",
                self._identity.lease_group_name,
                "--name",
                self._identity.lease_name,
            ],
            check=False,
        )
        if result.returncode != 0 and "ResourceNotFoundException" not in result.stderr:
            raise DevelopmentEnvironmentError(f"Stop lease deletion failed: {result.stderr.strip()}")

    def payload_get(self) -> dict[str, object]:
        """Return the stable content-free lease status."""

        result = self._aws.run(
            [
                "scheduler",
                "get-schedule",
                "--group-name",
                self._identity.lease_group_name,
                "--name",
                self._identity.lease_name,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            if "ResourceNotFoundException" in result.stderr:
                return {"state": "absent"}
            raise DevelopmentEnvironmentError(f"Stop lease lookup failed: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError("Stop lease response is invalid") from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError("Stop lease response is malformed")
        target_payload = payload.get("Target")
        if not isinstance(target_payload, dict):
            raise DevelopmentEnvironmentError("Stop lease target is malformed")
        return {
            "action_after_completion": payload.get("ActionAfterCompletion"),
            "schedule_expression": payload.get("ScheduleExpression"),
            "state": payload.get("State"),
            "target_arn": target_payload.get("Arn"),
        }

    def upsert(self, *, lease_duration: timedelta | None = None) -> None:
        """Create or renew a lease that resolves the current instance at expiry."""

        effective_lease_duration = lease_duration or self._lease_duration
        if effective_lease_duration <= timedelta():
            raise DevelopmentEnvironmentError("Stop lease duration must be positive")
        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        t_stop = self._clock.now() + effective_lease_duration
        schedule_expression = f"at({t_stop.strftime('%Y-%m-%dT%H:%M:%S')})"
        target_arn = output_by_name_map["StopLeaseTargetArn"]
        target_payload = {
            "Arn": target_arn,
            "Input": "{}",
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": 3600,
                "MaximumRetryAttempts": 3,
            },
            "RoleArn": output_by_name_map["SchedulerExecutionRoleArn"],
        }
        common_argument_list = [
            "--action-after-completion",
            "DELETE",
            "--flexible-time-window",
            json.dumps({"Mode": "OFF"}, separators=(",", ":")),
            "--group-name",
            self._identity.lease_group_name,
            "--name",
            self._identity.lease_name,
            "--schedule-expression",
            schedule_expression,
            "--schedule-expression-timezone",
            "UTC",
            "--state",
            "ENABLED",
            "--target",
            json.dumps(target_payload, separators=(",", ":")),
        ]
        result = self._aws.run(
            [
                "scheduler",
                "get-schedule",
                "--group-name",
                self._identity.lease_group_name,
                "--name",
                self._identity.lease_name,
            ],
            check=False,
        )
        operation = "update-schedule" if result.returncode == 0 else "create-schedule"
        self._aws.run(["scheduler", operation, *common_argument_list])
        lease_payload = self.payload_get()
        if (
            lease_payload.get("action_after_completion") != "DELETE"
            or lease_payload.get("schedule_expression") != schedule_expression
            or lease_payload.get("state") != "ENABLED"
            or lease_payload.get("target_arn") != target_arn
        ):
            raise DevelopmentEnvironmentError("Stop lease was not proven enabled")

    def wait_until(self, t_deadline: datetime) -> None:
        """Wait until one UTC deadline without changing operational policy."""

        while self._clock.now() < t_deadline:
            remaining_seconds = (t_deadline - self._clock.now()).total_seconds()
            self._clock.sleep(min(self._poll_interval_seconds, remaining_seconds))

    def instance_stopped_wait(
        self,
        *,
        instance_id: str,
        t_deadline: datetime,
    ) -> None:
        """Wait for the lease target to stop the exact instance."""

        while self._clock.now() < t_deadline:
            state = self._instance_state_get(instance_id)
            if state == "stopped":
                return
            if state not in {"running", "stopping"}:
                raise DevelopmentEnvironmentError("Lifecycle acceptance reached unexpected " f"instance state {state}")
            self._clock.sleep(self._poll_interval_seconds)
        raise DevelopmentEnvironmentError("Lifecycle acceptance lease did not stop the instance " "before its deadline")

    def absence_wait(self, *, t_deadline: datetime) -> None:
        """Wait for Scheduler to auto-delete a completed acceptance lease."""

        while self._clock.now() < t_deadline:
            if self.payload_get().get("state") == "absent":
                return
            self._clock.sleep(self._poll_interval_seconds)
        raise DevelopmentEnvironmentError("Lifecycle acceptance lease was not auto-deleted")
