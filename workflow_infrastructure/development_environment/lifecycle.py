"""Own start, graceful stop, and stop-lease acceptance for one development host."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.compute import (
    HOST_READY_TIMEOUT_SECONDS,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.manager import (
    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
)

LEASE_DURATION = timedelta(hours=2)
LIFECYCLE_ACCEPTANCE_INITIAL_LEASE_DURATION = timedelta(minutes=2)
LIFECYCLE_ACCEPTANCE_RENEW_DELAY_SECONDS = 45
LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION = timedelta(minutes=4)
LIFECYCLE_ACCEPTANCE_RENEWAL_PROOF_DELAY = timedelta(minutes=3, seconds=15)
LIFECYCLE_ACCEPTANCE_STOP_GRACE = timedelta(minutes=5)


class AccountVerifierProtocol(Protocol):
    """Local AWS operator boundary required by host lifecycle."""

    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class AwsClientProtocol(Protocol):
    """AWS operations required by host lifecycle."""

    def run(self, argument_list: Sequence[str], *, check: bool = True) -> object:
        """Run one AWS CLI operation."""


class ClockProtocol(Protocol):
    """Controlled UTC and wait boundary required by acceptance."""

    def now(self) -> datetime:
        """Return the current UTC instant."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a bounded duration."""


class ComputeProtocol(Protocol):
    """EC2 and host-readiness surface required by lifecycle."""

    def instance_id_get(self) -> str:
        """Return the exact current instance identity."""

    def state_get(self, instance_id: str) -> str:
        """Return the current EC2 state."""

    def online_wait(self) -> None:
        """Wait for the instance and SSM to become online."""

    def readiness_wait(self) -> None:
        """Wait for complete host and Product readiness."""


class EnvironmentIdentityProtocol(Protocol):
    """Stable environment identity and host paths required by lifecycle."""

    compute_stack_name: str
    environment_name: str
    host_control_entrypoint_path: Path


class HostStatusProtocol(Protocol):
    """Safe Product activity observation required by acceptance."""

    def payload_get(self, *, retained_volume_id: str) -> dict[str, object]:
        """Return current safe host status."""


class ProductRecoveryProtocol(Protocol):
    """Retained Product recovery behavior required after a lifecycle test."""

    def acceptance_run(self) -> None:
        """Run Product recovery acceptance."""


class SourcePublisherProtocol(Protocol):
    """Exact infrastructure source validation and publication."""

    def validate_repository(self, repository_path: Path, repository_name: str) -> None:
        """Validate one clean exact repository source."""

    def infrastructure_publish(self) -> None:
        """Publish and activate exact infrastructure control source."""


class StackManagerProtocol(Protocol):
    """CloudFormation state required by lifecycle."""

    def drift_validate(self, stack_name: str) -> None:
        """Prove the stack has no drift."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""


class StopLeaseProtocol(Protocol):
    """External fail-safe stop lease behavior."""

    def absence_wait(self, *, t_deadline: datetime) -> None:
        """Wait for schedule deletion."""

    def delete(self) -> None:
        """Delete the current lease if present."""

    def instance_stopped_wait(self, *, instance_id: str, t_deadline: datetime) -> None:
        """Wait for lease-triggered EC2 stop."""

    def payload_get(self) -> dict[str, object]:
        """Return safe lease state."""

    def upsert(self, *, lease_duration: timedelta | None = None) -> None:
        """Create or renew the lease."""

    def wait_until(self, t_deadline: datetime) -> None:
        """Wait until an exact UTC boundary."""


class SsmTransportProtocol(Protocol):
    """Session Manager operations required by lifecycle."""

    def ssm_command_start(self, command_list: Sequence[str]) -> str:
        """Start one remote command and return its identity."""

    def ssm_shell_result_get(
        self,
        command_list: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        """Run bounded remote shell commands."""

    def ssm_shell_run(self, command_list: Sequence[str]) -> None:
        """Run remote shell commands."""


class DevelopmentLifecycleManager:
    """Own the complete EC2 start, stop, and stop-lease acceptance lifecycle."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        aws: AwsClientProtocol,
        clock: ClockProtocol,
        compute: ComputeProtocol,
        host_status: HostStatusProtocol,
        identity: EnvironmentIdentityProtocol,
        product_recovery: ProductRecoveryProtocol,
        project_root_path: Path,
        source_publisher: SourcePublisherProtocol,
        stack: StackManagerProtocol,
        stop_lease: StopLeaseProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Bind lifecycle behavior to one exact environment."""

        self._account = account
        self._aws = aws
        self._clock = clock
        self._compute = compute
        self._host_status = host_status
        self._identity = identity
        self._product_recovery = product_recovery
        self._project_root_path = project_root_path
        self._source_publisher = source_publisher
        self._stack = stack
        self._stop_lease = stop_lease
        self._transport = transport

    def start(
        self,
        *,
        should_publish_infrastructure_source: bool = False,
    ) -> None:
        """Start EC2 under a fail-safe lease and prove complete host readiness."""

        self._start_foundation(
            should_validate_source=should_publish_infrastructure_source,
        )
        if should_publish_infrastructure_source:
            self._source_publisher.infrastructure_publish()
        self._compute.readiness_wait()
        instance_id = self._compute.instance_id_get()
        print(f"OK: development instance {instance_id} is ready")

    def stop(self, *, should_validate_drift: bool = True) -> None:
        """Run graceful remote shutdown, prove EC2 stop, and remove its lease."""

        self._account.local_operator_context_validate()
        if should_validate_drift:
            self._stack.drift_validate(self._identity.compute_stack_name)
        instance_id = self._compute.instance_id_get()
        state = self._compute.state_get(instance_id)
        if state == "stopped":
            self._stop_lease.delete()
            print(f"OK: development instance {instance_id} is already stopped")
            return
        if state != "running":
            raise DevelopmentEnvironmentError(f"Instance cannot stop gracefully from state {state}")
        command_id = self._transport.ssm_command_start(
            [
                (
                    f"if [ -f {self._identity.host_control_entrypoint_path} ]; then "
                    f"sudo env {PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT} python3.14 -B "
                    f"{self._identity.host_control_entrypoint_path} host-shutdown "
                    f"--environment-name {self._identity.environment_name}; "
                    "else sudo systemctl stop k3s || true; sudo systemctl poweroff; fi"
                )
            ]
        )
        print(f"OK: graceful shutdown command {command_id} started")
        self._aws.run(["ec2", "wait", "instance-stopped", "--instance-ids", instance_id])
        self._stop_lease.delete()
        print(f"OK: development instance {instance_id} stopped")

    def acceptance_run(self) -> None:
        """Prove stop-lease renewal, fail-safe stop, and environment restoration."""

        self._account.local_operator_context_validate()
        self._source_publisher.validate_repository(self._project_root_path, "workflow-infrastructure")
        self._stack.drift_validate(self._identity.compute_stack_name)
        instance_id = self._compute.instance_id_get()
        if self._compute.state_get(instance_id) != "running":
            raise DevelopmentEnvironmentError("Lifecycle acceptance requires the development instance to be running")
        host_status_payload = self._host_status.payload_get(
            retained_volume_id=self._stack.output_by_name_map_get(self._identity.compute_stack_name)["RetainedVolumeId"]
        )
        if host_status_payload.get("wcc_activity") != "idle":
            raise DevelopmentEnvironmentError("Lifecycle acceptance requires an idle Product")

        is_environment_restored = False
        try:
            self._transport.ssm_shell_run(
                [
                    "sudo systemctl stop workflow-control-center-host-controller",
                    ('test "$(systemctl is-active ' 'workflow-control-center-host-controller || true)" = inactive'),
                ]
            )
            t_initial_lease = self._clock.now()
            self._stop_lease.upsert(lease_duration=LIFECYCLE_ACCEPTANCE_INITIAL_LEASE_DURATION)
            initial_expression = self._stop_lease.payload_get().get("schedule_expression")
            self._clock.sleep(LIFECYCLE_ACCEPTANCE_RENEW_DELAY_SECONDS)

            t_renewed_lease = self._clock.now()
            self._stop_lease.upsert(lease_duration=LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION)
            renewed_expression = self._stop_lease.payload_get().get("schedule_expression")
            if initial_expression == renewed_expression:
                raise DevelopmentEnvironmentError("Lifecycle acceptance lease renewal did not change its deadline")

            self._stop_lease.wait_until(t_initial_lease + LIFECYCLE_ACCEPTANCE_RENEWAL_PROOF_DELAY)
            if self._compute.state_get(instance_id) != "running":
                raise DevelopmentEnvironmentError("Lifecycle acceptance instance stopped at the superseded deadline")
            self._stop_lease.instance_stopped_wait(
                instance_id=instance_id,
                t_deadline=(
                    t_renewed_lease + LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION + LIFECYCLE_ACCEPTANCE_STOP_GRACE
                ),
            )
            self._stop_lease.absence_wait(t_deadline=self._clock.now() + LIFECYCLE_ACCEPTANCE_STOP_GRACE)
            self.start()
            self._product_recovery.acceptance_run()
            is_environment_restored = True
        finally:
            if not is_environment_restored:
                self._acceptance_environment_restore(instance_id)
        print(
            "OK: real AWS lifecycle acceptance renewed the lease, "
            "failed safe, and restored the development environment"
        )

    def _start_foundation(
        self,
        *,
        should_validate_source: bool,
    ) -> None:
        """Start EC2 and await cloud-init without changing installed control source."""

        self._account.local_operator_context_validate()
        self._stack.drift_validate(self._identity.compute_stack_name)
        if should_validate_source:
            self._source_publisher.validate_repository(self._project_root_path, "workflow-infrastructure")
        instance_id = self._compute.instance_id_get()
        self._stop_lease.upsert()
        state = self._compute.state_get(instance_id)
        if state == "stopped":
            self._aws.run(["ec2", "start-instances", "--instance-ids", instance_id])
        elif state not in {"pending", "running"}:
            raise DevelopmentEnvironmentError(f"Instance cannot start from state {state}")
        self._compute.online_wait()
        self._transport.ssm_shell_result_get(
            ["cloud-init status --wait"],
            timeout_seconds=HOST_READY_TIMEOUT_SECONDS,
        )

    def _acceptance_environment_restore(self, instance_id: str) -> None:
        """Best-effort restore of the ordinary controller and two-hour lease."""

        state = self._compute.state_get(instance_id)
        if state == "pending":
            self._compute.online_wait()
            state = "running"
        if state == "running":
            self._stop_lease.upsert()
            self._transport.ssm_shell_run(["sudo systemctl start workflow-control-center-host-controller"])
            self._compute.readiness_wait()
            return
        if state == "stopping":
            self._aws.run(["ec2", "wait", "instance-stopped", "--instance-ids", instance_id])
            state = "stopped"
        if state == "stopped":
            self.start()
            return
        raise DevelopmentEnvironmentError(
            "Lifecycle acceptance could not restore the environment from " f"instance state {state}"
        )
