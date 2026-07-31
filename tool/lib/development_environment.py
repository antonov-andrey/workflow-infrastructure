"""Compose independently owned development-environment capabilities."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from tool.lib.development_access import DevelopmentAccessManager
from tool.lib.development_account import DevelopmentAccountVerifier
from tool.lib.development_aws import DevelopmentAwsClient
from tool.lib.development_cost import DevelopmentCostReviewer
from tool.lib.development_compute import (
    DevelopmentComputeManager,
)
from tool.lib.development_diagnostics import DevelopmentDiagnostics
from tool.lib.development_environment_error import DevelopmentEnvironmentError
from tool.lib.development_host import (
    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
    DevelopmentHostManager,
)
from tool.lib.development_host_artifact import DevelopmentHostArtifactManager
from tool.lib.development_host_status import DevelopmentHostStatus
from tool.lib.development_lifecycle import (
    LEASE_DURATION,
    LIFECYCLE_ACCEPTANCE_INITIAL_LEASE_DURATION,
    LIFECYCLE_ACCEPTANCE_RENEW_DELAY_SECONDS,
    LIFECYCLE_ACCEPTANCE_RENEWAL_PROOF_DELAY,
    LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION,
    LIFECYCLE_ACCEPTANCE_STOP_GRACE,
    DevelopmentLifecycleManager,
)
from tool.lib.development_product_deployment import (
    DevelopmentProductDeploymentManager,
)
from tool.lib.development_product_recovery import (
    DevelopmentProductRecoveryManager,
)
from tool.lib.development_product_reset import DevelopmentProductResetManager
from tool.lib.development_provisioning import DevelopmentProvisioningManager
from tool.lib.development_replacement import (
    COMPUTE_RETAINED_VOLUME_LOGICAL_ID_SET,
    COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET,
    DevelopmentReplacementManager,
)
from tool.lib.development_retained_volume import (
    DevelopmentRetainedVolumeManager,
)
from tool.lib.development_source import DevelopmentSourcePublisher
from tool.lib.development_stack import DevelopmentStackManager
from tool.lib.development_stop_lease import DevelopmentStopLeaseManager
from tool.lib.development_transport import DevelopmentSsmTransport
from tool.lib.retained_product_release import (
    DevelopmentRetainedProductReleaseManager,
    RetainedProductReleaseValidator,
)

AWS_ACCOUNT_ID = "463564115167"
AWS_PROFILE = "workflow-control-center-devel"
AWS_REGION = "us-east-1"
CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT = 51_200
CLOUDFORMATION_S3_TEMPLATE_MAX_BYTE_COUNT = 1_048_576
COMPUTE_STACK_NAME = "workflow-control-center-development-compute"
DATA_PLANE_STACK_NAME = "workflow-control-center-development"
HOST_CONTROL_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/control/current")
HOST_CONTROL_RELEASE_ROOT_PATH = Path("/opt/workflow-infrastructure/control/releases")
HOST_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/current")
HOST_EBS_DEVICE_BY_ID_ROOT_PATH = Path("/dev/disk/by-id")
HOST_RETAINED_ROOT_PATH = Path("/srv/workflow-control-center")
HOST_RETAINED_RELEASE_ROOT_PATH = HOST_RETAINED_ROOT_PATH / "release"
HOST_RETAINED_CURRENT_RELEASE_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "current"
HOST_RELEASE_ROOT_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "releases"
HOST_STATE_ROOT_PATH = Path("/var/lib/workflow-infrastructure")
INSTANCE_NAME = "workflow-control-center-development"
LEASE_GROUP_NAME = "workflow-control-center-development"
LEASE_NAME = "workflow-control-center-development-stop"
MOVING_SOURCE_RESOLUTION_ATTEMPT_COUNT = 3
SSM_DOCUMENT_PORT_FORWARD = "AWS-StartPortForwardingSession"
SSM_COMMAND_TIMEOUT_SECONDS = 3600
STACK_POLL_INTERVAL_SECONDS = 5
STACK_TIMEOUT_SECONDS = 3600
ENVIRONMENT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]{0,15}")


class DevelopmentEnvironmentIdentity:
    """Derive every physical development identity from one stable machine name."""

    def __init__(self, environment_name: str = "primary") -> None:
        """Validate and retain one environment name.

        Args:
            environment_name: Stable lowercase environment selector.

        Raises:
            DevelopmentEnvironmentError: If the selector is unsafe across AWS and host paths.
        """

        if ENVIRONMENT_NAME_PATTERN.fullmatch(environment_name) is None:
            raise DevelopmentEnvironmentError("environment_name must match [a-z][a-z0-9]{0,15}")
        self.environment_name = environment_name

    @property
    def is_primary(self) -> bool:
        """Return whether this is the stable primary development environment."""

        return self.environment_name == "primary"

    @property
    def data_plane_stack_name(self) -> str:
        """Return the exact data-plane stack identity."""

        if self.is_primary:
            return DATA_PLANE_STACK_NAME
        return f"workflow-control-center-development-{self.environment_name}"

    @property
    def compute_stack_name(self) -> str:
        """Return the exact compute stack identity."""

        if self.is_primary:
            return COMPUTE_STACK_NAME
        return f"workflow-control-center-development-{self.environment_name}-compute"

    @property
    def instance_name(self) -> str:
        """Return the local SSH alias and EC2 Name identity."""

        if self.is_primary:
            return INSTANCE_NAME
        return f"workflow-control-center-development-{self.environment_name}"

    @property
    def lease_group_name(self) -> str:
        """Return the environment-owned Scheduler group name."""

        if self.is_primary:
            return LEASE_GROUP_NAME
        return f"workflow-control-center-development-{self.environment_name}"

    @property
    def lease_name(self) -> str:
        """Return the renewable stop-lease schedule name."""

        if self.is_primary:
            return LEASE_NAME
        return f"workflow-control-center-development-{self.environment_name}-stop"

    @property
    def host_control_root_path(self) -> Path:
        """Return the disposable infrastructure-control source root."""

        if self.is_primary:
            return HOST_CONTROL_RELEASE_ROOT_PATH.parent
        return Path("/opt/workflow-infrastructure/environments") / self.environment_name / "control"

    @property
    def host_control_release_root_path(self) -> Path:
        """Return the exact infrastructure-control release collection."""

        if self.is_primary:
            return HOST_CONTROL_RELEASE_ROOT_PATH
        return self.host_control_root_path / "releases"

    @property
    def host_control_current_source_path(self) -> Path:
        """Return the current infrastructure-control release pointer."""

        if self.is_primary:
            return HOST_CONTROL_CURRENT_SOURCE_PATH
        return self.host_control_root_path / "current"

    @property
    def host_retained_root_path(self) -> Path:
        """Return the environment-exclusive retained volume mount."""

        if self.is_primary:
            return HOST_RETAINED_ROOT_PATH
        return Path(f"/srv/workflow-control-center-{self.environment_name}")

    @property
    def host_retained_release_root_path(self) -> Path:
        """Return the retained Product release owner root."""

        if self.is_primary:
            return HOST_RETAINED_RELEASE_ROOT_PATH
        return self.host_retained_root_path / "release"

    @property
    def host_retained_product_tool_path(self) -> Path:
        """Return the retained Product management runtime root."""

        return self.host_retained_root_path / "product-tool"

    @property
    def host_release_root_path(self) -> Path:
        """Return the retained immutable Product release collection."""

        if self.is_primary:
            return HOST_RELEASE_ROOT_PATH
        return self.host_retained_release_root_path / "releases"

    @property
    def host_retained_current_release_path(self) -> Path:
        """Return the retained accepted Product release pointer."""

        if self.is_primary:
            return HOST_RETAINED_CURRENT_RELEASE_PATH
        return self.host_retained_release_root_path / "current"

    @property
    def host_retained_rollback_release_path(self) -> Path:
        """Return the retained previous-current Product release pointer."""

        return self.host_retained_release_root_path / "rollback"

    @property
    def host_product_recovery_marker_path(self) -> Path:
        """Return the retained interrupted-Product-recovery marker."""

        return self.host_retained_release_root_path / "recovery-pending.json"

    @property
    def host_current_source_path(self) -> Path:
        """Return the root-volume Product current-source pointer."""

        if self.is_primary:
            return HOST_CURRENT_SOURCE_PATH
        return Path("/opt/workflow-infrastructure/environments") / self.environment_name / "current"

    @property
    def host_state_root_path(self) -> Path:
        """Return the disposable host-controller state root."""

        if self.is_primary:
            return HOST_STATE_ROOT_PATH
        return Path("/var/lib/workflow-infrastructure") / self.environment_name

    @property
    def qualified_registry_identity(self) -> str:
        """Return the collision-proof logical registry identity."""

        return f"{self.compute_stack_name}:" "apwid-workflow/workflow-image-registry"

    @property
    def qualified_product_database_identity(self) -> str:
        """Return the collision-proof logical Product database identity."""

        return f"{self.compute_stack_name}:apwid-db/apwid"

    @property
    def qualified_credential_identity(self) -> str:
        """Return the collision-proof logical renewable credential identity."""

        return f"{self.compute_stack_name}:" "apwid-platform/workflow-control-center-aws-credentials"


class CommandRunner:
    """Run external commands through one explicit process boundary."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command and return its completed process.

        Args:
            command_list: Complete argument vector.
            check: Whether a non-zero exit raises an exception.
            input_text: Optional standard-input text.
            should_capture: Whether to capture standard output and standard error.

        Returns:
            Completed process.
        """

        try:
            return subprocess.run(
                list(command_list),
                capture_output=should_capture,
                check=check,
                input=input_text,
                text=True,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(f"Unable to execute {command_list[0]}: {error}") from error
        except subprocess.CalledProcessError as error:
            error_text = (error.stderr or error.stdout or f"exit {error.returncode}").strip()
            raise DevelopmentEnvironmentError(f"{command_list[0]} failed: {error_text}") from error


class Clock:
    """Expose UTC time, monotonic time, and controlled waiting."""

    def monotonic(self) -> float:
        """Return the current monotonic clock value.

        Returns:
            Monotonic seconds.
        """

        return time.monotonic()

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant.

        Returns:
            Current UTC instant.
        """

        return datetime.now(UTC)

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a non-negative duration.

        Args:
            delay_seconds: Duration in seconds.
        """

        time.sleep(delay_seconds)


class DevelopmentEnvironment:
    """Compose one development environment from cohesive capability owners."""

    def __init__(
        self,
        *,
        clock: Clock,
        environment_name: str = "primary",
        project_root_path: Path,
        runner: CommandRunner,
    ) -> None:
        """Initialize the environment workflow.

        Args:
            clock: UTC and monotonic time boundary.
            environment_name: Stable development environment selector.
            project_root_path: Root of the workflow-infrastructure checkout.
            runner: External process boundary.
        """

        self._clock = clock
        self._identity = DevelopmentEnvironmentIdentity(environment_name)
        self._is_host = project_root_path.is_relative_to(
            self._identity.host_control_release_root_path
        ) or project_root_path.is_relative_to(self._identity.host_release_root_path)
        self._project_root_path = project_root_path
        self._runner = runner
        self._workspace_root_path = project_root_path.parent
        self._aws = DevelopmentAwsClient(
            is_host=self._is_host,
            profile=AWS_PROFILE,
            region=AWS_REGION,
            runner=runner,
        )
        self._stack = DevelopmentStackManager(
            aws=self._aws,
            aws_region=AWS_REGION,
            clock=clock,
            identity=self._identity,
            project_root_path=project_root_path,
            runner=runner,
        )
        self._account = DevelopmentAccountVerifier(
            account_id=AWS_ACCOUNT_ID,
            aws=self._aws,
            data_plane_stack_name=self._identity.data_plane_stack_name,
            primary_data_plane_stack_name=DATA_PLANE_STACK_NAME,
            profile=AWS_PROFILE,
            region=AWS_REGION,
            runner=runner,
            stack=self._stack,
        )
        self._cost_reviewer = DevelopmentCostReviewer(
            aws=self._aws,
            clock=clock,
            project_root_path=project_root_path,
            region=AWS_REGION,
        )
        self._retained_volume = DevelopmentRetainedVolumeManager(
            account_id=AWS_ACCOUNT_ID,
            aws=self._aws,
            aws_region=AWS_REGION,
            identity=self._identity,
            instance_state_get=lambda instance_id: self.compute.state_get(instance_id),
            stack=self._stack,
        )
        self._transport = DevelopmentSsmTransport(
            aws=self._aws,
            aws_profile=AWS_PROFILE,
            aws_region=AWS_REGION,
            clock=clock,
            identity=self._identity,
            instance_id_get=lambda: self.compute.instance_id_get(),
            runner=runner,
        )
        self._source_publisher = DevelopmentSourcePublisher(
            clock=clock,
            identity=self._identity,
            project_root_path=project_root_path,
            runner=runner,
            transport=self._transport,
        )
        self.product_release = DevelopmentRetainedProductReleaseManager(
            host_artifact_manifest_get=lambda: (self.host.host_artifact_manifest_get()),
            identity=self._identity,
            is_host_get=lambda: self._is_host,
            python_bytecode_environment_assignment=(PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT),
            runner=runner,
            validator=RetainedProductReleaseValidator(self._identity),
        )
        self._host_status = DevelopmentHostStatus(
            identity=self._identity,
            is_host=self._is_host,
            product_activity_get=lambda: self.host.host_product_activity_get(),
            retained_volume=self._retained_volume,
            runner=runner,
            transport=self._transport,
        )
        self._stop_lease = DevelopmentStopLeaseManager(
            aws=self._aws,
            clock=clock,
            identity=self._identity,
            instance_state_get=lambda instance_id: self.compute.state_get(instance_id),
            lease_duration=LEASE_DURATION,
            poll_interval_seconds=STACK_POLL_INTERVAL_SECONDS,
            stack=self._stack,
        )
        self.compute = DevelopmentComputeManager(
            aws=self._aws,
            clock=clock,
            host_status=self._host_status,
            identity=self._identity,
            runner=runner,
            stack=self._stack,
            transport=self._transport,
        )
        self.host_artifact = DevelopmentHostArtifactManager(
            identity=self._identity,
            project_root_path=project_root_path,
            runner=runner,
            stack=self._stack,
        )
        self.host = DevelopmentHostManager(
            aws_region=AWS_REGION,
            clock=clock,
            identity=self._identity,
            is_host=self._is_host,
            product_release=self.product_release,
            project_root_path=project_root_path,
            runner=runner,
            stop_lease=self._stop_lease,
        )
        self.product_recovery = DevelopmentProductRecoveryManager(
            identity=self._identity,
            product_release=self.product_release,
            transport=self._transport,
        )
        self.product_reset = DevelopmentProductResetManager(
            identity=self._identity,
            transport=self._transport,
        )
        self.access = DevelopmentAccessManager(
            account=self._account,
            aws_profile=AWS_PROFILE,
            aws_region=AWS_REGION,
            compute=self.compute,
            identity=self._identity,
            port_forward_document_name=SSM_DOCUMENT_PORT_FORWARD,
            runner=runner,
            transport=self._transport,
        )
        self.product_deployment = DevelopmentProductDeploymentManager(
            account=self._account,
            clock=clock,
            compute=self.compute,
            host_artifact=self.host_artifact,
            identity=self._identity,
            product_recovery=self.product_recovery,
            product_reset=self.product_reset,
            project_root_path=project_root_path,
            source_publisher=self._source_publisher,
            stack=self._stack,
            transport=self._transport,
            workspace_root_path=self._workspace_root_path,
        )
        self.lifecycle = DevelopmentLifecycleManager(
            account=self._account,
            aws=self._aws,
            clock=clock,
            compute=self.compute,
            host_status=self._host_status,
            identity=self._identity,
            product_recovery=self.product_recovery,
            project_root_path=project_root_path,
            source_publisher=self._source_publisher,
            stack=self._stack,
            stop_lease=self._stop_lease,
            transport=self._transport,
        )
        self.replacement = DevelopmentReplacementManager(
            account=self._account,
            clock=clock,
            compute=self.compute,
            compute_template_path=(
                project_root_path / "cloudformation/workflow-control-center-development-compute.yaml"
            ),
            identity=self._identity,
            lease_duration=LEASE_DURATION,
            lifecycle=self.lifecycle,
            product_recovery=self.product_recovery,
            project_root_path=project_root_path,
            retained_volume=self._retained_volume,
            source_publisher=self._source_publisher,
            stack=self._stack,
            stop_lease=self._stop_lease,
        )
        self.provisioning = DevelopmentProvisioningManager(
            account=self._account,
            compute=self.compute,
            compute_stable_identity_logical_id_set=(COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET),
            compute_template_path=(
                project_root_path / "cloudformation/workflow-control-center-development-compute.yaml"
            ),
            cost_reviewer=self._cost_reviewer,
            data_plane_template_path=(project_root_path / "cloudformation/workflow-control-center-development.yaml"),
            host_artifact=self.host_artifact,
            identity=self._identity,
            project_root_path=project_root_path,
            replacement=self.replacement,
            retained_volume=self._retained_volume,
            source_publisher=self._source_publisher,
            stack=self._stack,
        )
        self.diagnostics = DevelopmentDiagnostics(
            account=self._account,
            account_id=AWS_ACCOUNT_ID,
            compute=self.compute,
            host_status=self._host_status,
            identity=self._identity,
            product_release=self.product_release,
            region=AWS_REGION,
            retained_volume=self._retained_volume,
            stack=self._stack,
            stop_lease=self._stop_lease,
            transport=self._transport,
        )
        self.host_status = self._host_status
