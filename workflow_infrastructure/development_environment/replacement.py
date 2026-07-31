"""Own development EC2 replacement, retained-volume restore, and cutover guards."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET = frozenset(
    {
        "DevelopmentInstance",
        "RetainedVolume",
        "RetainedVolumeAttachment",
        "RetainedVolumeRestoreA",
        "RetainedVolumeRestoreB",
    }
)
COMPUTE_RETAINED_VOLUME_LOGICAL_ID_SET = frozenset(
    {
        "RetainedVolume",
        "RetainedVolumeRestoreA",
        "RetainedVolumeRestoreB",
    }
)


class AccountVerifierProtocol(Protocol):
    """Local AWS operator boundary required by replacement."""

    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class ClockProtocol(Protocol):
    """Controlled UTC boundary required by replacement guards."""

    def now(self) -> datetime:
        """Return the current UTC instant."""


class ComputeProtocol(Protocol):
    """Compute state needed by guarded replacement."""

    def launch_template_version_validate(self, *, require_latest: bool = True) -> None:
        """Validate the active launch-template version."""


class EnvironmentIdentityProtocol(Protocol):
    """Stable environment identities required by replacement."""

    compute_stack_name: str


class LifecycleProtocol(Protocol):
    """Host lifecycle operations required by cutover."""

    def start(
        self,
        *,
        should_publish_infrastructure_source: bool = False,
    ) -> None:
        """Start and prove the current host."""

    def stop(self, *, should_validate_drift: bool = True) -> None:
        """Stop and prove the current host."""


class ProductRecoveryProtocol(Protocol):
    """Retained Product recovery transitions required by cutover."""

    def begin(self) -> None:
        """Enter retained Product recovery."""

    def finish(self) -> None:
        """Complete retained Product recovery."""

    def is_pending(self) -> bool:
        """Return whether recovery is already pending."""

    def status_get(self) -> str:
        """Return current recovery status."""


class RetainedVolumeProtocol(Protocol):
    """Retained-volume operations required by replacement and restore."""

    def attachment_ensure(self) -> None:
        """Restore the stack-declared attachment after failed replacement."""

    def attachment_validate(self) -> None:
        """Validate the exact current retained-volume attachment."""

    def detach_for_replacement(self) -> None:
        """Detach the retained volume after stop proof."""

    def regular_backup_exclude(self, *, volume_id: str) -> None:
        """Exclude a retired restore source from the primary backup selection."""

    def restore_plan_get(self, *, snapshot_id: str) -> tuple[str, dict[str, str]]:
        """Return source volume and CloudFormation restore parameters."""

    def retired_cleanup(self, *, current_volume_id: str) -> None:
        """Clean only already-retired restore artifacts."""

    def snapshot_restore_validate(
        self,
        *,
        snapshot_id: str,
        source_volume_id: str,
    ) -> None:
        """Validate exact snapshot provenance after restore."""


class SourcePublisherProtocol(Protocol):
    """Exact infrastructure source validation required before cutover."""

    def validate_repository(self, repository_path: Path, repository_name: str) -> None:
        """Validate one clean exact repository source."""


class StackManagerProtocol(Protocol):
    """CloudFormation operations required by replacement."""

    def apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: Mapping[str, str],
        must_preserve_resource: bool,
        protected_identity_logical_id_set: Collection[str],
    ) -> None:
        """Apply one exact stack transition."""

    def drift_validate(self, stack_name: str) -> None:
        """Prove the stack has no drift."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""


class StopLeaseProtocol(Protocol):
    """External fail-safe lease behavior required by cutover."""

    def delete(self) -> None:
        """Delete the current lease if present."""

    def upsert(self, *, lease_duration: timedelta | None = None) -> None:
        """Create or renew the lease."""


class DevelopmentReplacementManager:
    """Own guarded instance replacement and retained-volume restore transitions."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        clock: ClockProtocol,
        compute: ComputeProtocol,
        compute_template_path: Path,
        identity: EnvironmentIdentityProtocol,
        lease_duration: timedelta,
        lifecycle: LifecycleProtocol,
        product_recovery: ProductRecoveryProtocol,
        project_root_path: Path,
        retained_volume: RetainedVolumeProtocol,
        source_publisher: SourcePublisherProtocol,
        stack: StackManagerProtocol,
        stop_lease: StopLeaseProtocol,
    ) -> None:
        """Bind replacement behavior to one exact development environment."""

        self._account = account
        self._clock = clock
        self._compute = compute
        self._compute_template_path = compute_template_path
        self._identity = identity
        self._lease_duration = lease_duration
        self._lifecycle = lifecycle
        self._product_recovery = product_recovery
        self._project_root_path = project_root_path
        self._retained_volume = retained_volume
        self._source_publisher = source_publisher
        self._stack = stack
        self._stop_lease = stop_lease

    def steady_state_finish(self) -> None:
        """Start current compute and resume only a proven pending Product recovery."""

        self._lifecycle.start(should_publish_infrastructure_source=True)
        product_recovery_is_pending = self._product_recovery.is_pending()
        if product_recovery_is_pending:
            self._product_recovery.begin()
        self.guard_disable()
        if product_recovery_is_pending:
            self._product_recovery.finish()

    def recovery_finish(self) -> None:
        """Finish one created replacement host from retained Product state."""

        self._lifecycle.start(should_publish_infrastructure_source=True)
        if self._product_recovery.status_get() == "absent":
            self.guard_disable()
            print("OK: replacement host has no retained Product release to recover")
            return
        self._product_recovery.begin()
        self.guard_disable()
        self._product_recovery.finish()

    def pending_launch_template_apply(
        self,
        *,
        parameter_by_name_map: dict[str, str],
    ) -> None:
        """Replace a running host for one already-proven launch-template update."""

        self._lifecycle.stop(should_validate_drift=False)
        self.stack_apply(parameter_by_name_map=parameter_by_name_map)
        self.recovery_finish()

    def restore(self, snapshot_id: str) -> None:
        """Replace the retained volume from one exact snapshot and accept recovery."""

        if not snapshot_id.startswith("snap-"):
            raise DevelopmentEnvironmentError("Snapshot ID must start with snap-")
        self._operator_source_validate()
        replacement_parameter_by_name_map = self.parameter_by_name_map_get()
        source_volume_id, restore_parameter_by_name_map = self._retained_volume.restore_plan_get(
            snapshot_id=snapshot_id
        )
        replacement_parameter_by_name_map.update(restore_parameter_by_name_map)
        self._stack.drift_validate(self._identity.compute_stack_name)
        self._retained_volume.retired_cleanup(current_volume_id=source_volume_id)
        self._lifecycle.stop(should_validate_drift=False)
        self.stack_apply(
            parameter_by_name_map=replacement_parameter_by_name_map,
            allow_retained_volume_transition=True,
        )
        self._retained_volume.snapshot_restore_validate(
            snapshot_id=snapshot_id,
            source_volume_id=source_volume_id,
        )
        self._retained_volume.regular_backup_exclude(volume_id=source_volume_id)
        self._lifecycle.start(should_publish_infrastructure_source=True)
        self._product_recovery.begin()
        self.guard_disable()
        self._product_recovery.finish()
        print(f"OK: retained state restored and accepted from {snapshot_id}")

    def replace(self) -> None:
        """Replace the EC2 instance while preserving the exact retained volume."""

        self._operator_source_validate()
        replacement_parameter_by_name_map = self.parameter_by_name_map_get()
        replacement_slot = replacement_parameter_by_name_map["InstanceSlot"]
        self._stack.drift_validate(self._identity.compute_stack_name)
        self._lifecycle.stop(should_validate_drift=False)
        self.stack_apply(parameter_by_name_map=replacement_parameter_by_name_map)
        self._lifecycle.start(should_publish_infrastructure_source=True)
        self._product_recovery.begin()
        self.guard_disable()
        self._product_recovery.finish()
        print(f"OK: replacement instance in slot {replacement_slot} accepted the retained volume")

    def parameter_by_name_map_get(self) -> dict[str, str]:
        """Return explicit slot, launch-template, and fail-safe guard parameters."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        try:
            current_slot = output_by_name_map["InstanceSlot"]
        except KeyError as error:
            raise DevelopmentEnvironmentError("Compute stack replacement outputs are incomplete") from error
        if current_slot not in {"a", "b"}:
            raise DevelopmentEnvironmentError("Compute stack replacement outputs are malformed")
        parameter_by_name_map = self.guard_parameter_by_name_map_get()
        parameter_by_name_map["InstanceSlot"] = "b" if current_slot == "a" else "a"
        try:
            latest_launch_template_version = output_by_name_map["LatestLaunchTemplateVersion"]
        except KeyError as error:
            raise DevelopmentEnvironmentError("Compute stack launch-template output is missing") from error
        if not isinstance(latest_launch_template_version, str) or not latest_launch_template_version.isdigit():
            raise DevelopmentEnvironmentError("Compute stack launch-template output is malformed")
        parameter_by_name_map["InstanceLaunchTemplateVersion"] = latest_launch_template_version
        return parameter_by_name_map

    def guard_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return an enabled time-bounded CloudFormation replacement guard."""

        t_stop = self._clock.now() + self._lease_duration
        return {
            "ReplacementGuardScheduleExpression": (f"at({t_stop.strftime('%Y-%m-%dT%H:%M:%S')})"),
            "ReplacementGuardScheduleState": "ENABLED",
        }

    def guard_disable(self) -> None:
        """Disable the CloudFormation guard after the renewable lease is proven."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        if "ReplacementGuardScheduleName" not in output_by_name_map:
            raise DevelopmentEnvironmentError("Compute stack replacement guard output is missing")
        self._stack.apply(
            stack_name=self._identity.compute_stack_name,
            template_path=self._compute_template_path,
            parameter_by_name_map={"ReplacementGuardScheduleState": "DISABLED"},
            must_preserve_resource=False,
            protected_identity_logical_id_set=COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET,
        )

    def stack_apply(
        self,
        *,
        parameter_by_name_map: dict[str, str],
        allow_retained_volume_transition: bool = False,
    ) -> None:
        """Apply one explicit replacement after proving retained-volume detach."""

        if (
            parameter_by_name_map.get("ReplacementGuardScheduleState") != "ENABLED"
            or "ReplacementGuardScheduleExpression" not in parameter_by_name_map
        ):
            raise DevelopmentEnvironmentError("Explicit replacement requires an enabled CloudFormation guard")
        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        renewable_lease_created = "StopLeaseTargetArn" in output_by_name_map
        if renewable_lease_created:
            self._stop_lease.upsert()
        try:
            self._retained_volume.detach_for_replacement()
        except Exception:
            if renewable_lease_created:
                self._stop_lease.delete()
            raise
        try:
            self._stack.apply(
                stack_name=self._identity.compute_stack_name,
                template_path=self._compute_template_path,
                parameter_by_name_map=parameter_by_name_map,
                must_preserve_resource=False,
                protected_identity_logical_id_set=(
                    () if allow_retained_volume_transition else COMPUTE_RETAINED_VOLUME_LOGICAL_ID_SET
                ),
            )
        except Exception as error:
            try:
                self._retained_volume.attachment_ensure()
            except Exception as recovery_error:
                raise DevelopmentEnvironmentError(
                    "Compute replacement failed and retained-volume attachment recovery failed: " f"{recovery_error}"
                ) from error
            if renewable_lease_created:
                self._stop_lease.delete()
            raise
        self._retained_volume.attachment_validate()
        self._compute.launch_template_version_validate()

    def _operator_source_validate(self) -> None:
        """Validate the exact operator context and infrastructure source."""

        self._account.local_operator_context_validate()
        self._source_publisher.validate_repository(self._project_root_path, "workflow-infrastructure")
