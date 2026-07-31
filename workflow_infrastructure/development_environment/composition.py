"""Compose independently owned development-environment capabilities."""

from __future__ import annotations

from pathlib import Path

from workflow_infrastructure.development_environment.access import (
    DevelopmentAccessManager,
)
from workflow_infrastructure.development_environment.account import (
    DevelopmentAccountVerifier,
)
from workflow_infrastructure.development_environment.aws import DevelopmentAwsClient
from workflow_infrastructure.development_environment.cost import DevelopmentCostReviewer
from workflow_infrastructure.development_environment.compute import (
    DevelopmentComputeManager,
)
from workflow_infrastructure.development_environment.diagnostics import (
    DevelopmentDiagnostics,
)
from workflow_infrastructure.development_environment.clock import Clock
from workflow_infrastructure.development_environment.command import CommandRunner
from workflow_infrastructure.development_environment.identity import (
    DATA_PLANE_STACK_NAME,
    DevelopmentEnvironmentIdentity,
)
from workflow_infrastructure.development_environment.host.manager import (
    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
    DevelopmentHostManager,
)
from workflow_infrastructure.development_environment.host.artifact import (
    DevelopmentHostArtifactManager,
)
from workflow_infrastructure.development_environment.host.status import (
    DevelopmentHostStatus,
)
from workflow_infrastructure.development_environment.lifecycle import (
    LEASE_DURATION,
    DevelopmentLifecycleManager,
)
from workflow_infrastructure.development_environment.product.deployment import (
    DevelopmentProductDeploymentManager,
)
from workflow_infrastructure.development_environment.product.recovery import (
    DevelopmentProductRecoveryManager,
)
from workflow_infrastructure.development_environment.product.reset import (
    DevelopmentProductResetManager,
)
from workflow_infrastructure.development_environment.product.public_ecr_auth import (
    DevelopmentPublicEcrAuthManager,
)
from workflow_infrastructure.development_environment.provisioning import (
    DevelopmentProvisioningManager,
)
from workflow_infrastructure.development_environment.replacement import (
    COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET,
    DevelopmentReplacementManager,
)
from workflow_infrastructure.development_environment.retained_volume import (
    DevelopmentRetainedVolumeManager,
)
from workflow_infrastructure.development_environment.source import (
    DevelopmentSourcePublisher,
)
from workflow_infrastructure.development_environment.stack import (
    DevelopmentStackManager,
)
from workflow_infrastructure.development_environment.stop_lease import (
    DevelopmentStopLeaseManager,
)
from workflow_infrastructure.development_environment.transport import (
    DevelopmentSsmTransport,
)
from workflow_infrastructure.development_environment.product.release import (
    DevelopmentRetainedProductReleaseManager,
    RetainedProductReleaseValidator,
)

AWS_ACCOUNT_ID = "463564115167"
AWS_PROFILE = "workflow-control-center-devel"
AWS_REGION = "us-east-1"
SSM_DOCUMENT_PORT_FORWARD = "AWS-StartPortForwardingSession"
STACK_POLL_INTERVAL_SECONDS = 5


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
        self._public_ecr_auth = DevelopmentPublicEcrAuthManager(
            aws_region=AWS_REGION,
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
            public_ecr_auth=self._public_ecr_auth,
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
