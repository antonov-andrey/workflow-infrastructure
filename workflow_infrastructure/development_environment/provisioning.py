"""Own declarative data-plane and compute provisioning for development."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class AccountVerifierProtocol(Protocol):
    """Development-account verification required by provisioning."""

    def account_foundation_validate(self) -> None:
        """Validate the one account-global foundation owner and state."""

    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class ComputeProtocol(Protocol):
    """Compute observations and validations required by provisioning."""

    def failed_bootstrap_replacement_is_proven(self) -> bool:
        """Return whether the failed current host is safe to replace.

        Returns:
            Whether the failed current host is safe to replace.
        """

    def launch_template_update_is_pending(self) -> bool:
        """Return whether immutable host inputs require replacement.

        Returns:
            Whether immutable host inputs require replacement.
        """

    def launch_template_version_get(self) -> str:
        """Return the exact current launch-template version.

        Returns:
            The exact current launch-template version.
        """

    def launch_template_version_validate(self, *, require_latest: bool = True) -> None:
        """Validate the active launch-template version.

        Args:
            require_latest: Require latest.
        """


class CostReviewerProtocol(Protocol):
    """Approved-architecture cost review boundary."""

    def record(self) -> None:
        """Record and validate the proposed cost delta."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment names consumed by provisioning."""

    compute_stack_name: str
    data_plane_stack_name: str
    environment_name: str
    git_worktree: str
    is_primary: bool
    local_http_port: int


class HostArtifactProtocol(Protocol):
    """Immutable host artifact resolution boundary."""

    def cloudformation_parameter_by_name_map_get(
        self,
        *,
        bucket_name: str,
        compute_stack_exists: bool,
    ) -> dict[str, str]:
        """Publish exact bootstrap objects and return compute parameters.

        Args:
            bucket_name: Bucket name.
            compute_stack_exists: Compute stack exists.

        Returns:
            Compute-stack parameters for the published bootstrap objects.
        """


class FoundationProtocol(Protocol):
    """Single account-global foundation owner."""

    def ensure(
        self,
        *,
        primary_platform_role_arn: str | None = None,
        primary_retained_volume_arn: str | None = None,
    ) -> None:
        """Apply the primary owner or validate it for a task environment.

        Args:
            primary_platform_role_arn: Primary platform role arn.
            primary_retained_volume_arn: Primary retained volume arn.
        """


class ReplacementProtocol(Protocol):
    """Guarded replacement transitions used by provisioning."""

    def guard_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return initial fail-safe replacement guard parameters.

        Returns:
            The initial fail-safe replacement guard parameters.
        """

    def recovery_finish(self) -> None:
        """Finish one already-created replacement."""

    def parameter_by_name_map_get(self) -> dict[str, str]:
        """Return exact parameters for the next guarded replacement.

        Returns:
            The exact parameters for the next guarded replacement.
        """

    def pending_launch_template_apply(
        self,
        *,
        parameter_by_name_map: dict[str, str],
    ) -> None:
        """Apply and accept one required launch-template replacement.

        Args:
            parameter_by_name_map: Parameter by name mapping.
        """

    def stack_apply(self, *, parameter_by_name_map: dict[str, str]) -> None:
        """Apply one explicit guarded instance replacement.

        Args:
            parameter_by_name_map: Parameter by name mapping.
        """

    def steady_state_finish(self) -> None:
        """Start and accept steady state."""


class RetainedVolumeProtocol(Protocol):
    """Retained state validation required by provisioning."""

    def attachment_validate(self) -> None:
        """Validate the exact current retained-volume attachment."""

    def regular_backup_validate(self) -> None:
        """Validate the primary-only development backup selection."""


class SourcePublisherProtocol(Protocol):
    """Exact infrastructure source validation boundary."""

    def validate_repository(self, repository_path: Path, repository_name: str) -> None:
        """Validate one clean exact repository source.

        Args:
            repository_path: Exact filesystem path for repository.
            repository_name: Repository name.
        """


class StackManagerProtocol(Protocol):
    """CloudFormation operations required by provisioning."""

    def apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
        protected_identity_logical_id_set: Collection[str] = (),
    ) -> None:
        """Apply one exact stack transition.

        Args:
            stack_name: Stack name.
            template_path: Exact filesystem path for template.
            parameter_by_name_map: Parameter by name mapping.
            must_preserve_resource: Must preserve resource.
            protected_identity_logical_id_set: Unique protected identity logical identity values.
        """

    def drift_validate(self, stack_name: str) -> None:
        """Prove the stack has no drift.

        Args:
            stack_name: Stack name.
        """

    def existing_resource_identity_validate(
        self,
        *,
        current_resource_id_by_logical_name_map: Mapping[str, str],
        previous_resource_id_by_logical_name_map: Mapping[str, str],
    ) -> None:
        """Prove a preservation-required stack retained physical identities.

        Args:
            current_resource_id_by_logical_name_map: Current resource identity by logical name mapping.
            previous_resource_id_by_logical_name_map: Previous resource identity by logical name mapping.
        """

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs.

        Args:
            stack_name: Stack name.

        Returns:
            The exact stack outputs.
        """

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack parameters.

        Args:
            stack_name: Stack name.

        Returns:
            The exact stack parameters.
        """

    def payload_get(
        self,
        stack_name: str,
        *,
        is_required: bool,
    ) -> dict[str, object]:
        """Return current stack payload or an empty mapping.

        Args:
            stack_name: Stack name.
            is_required: Whether required.

        Returns:
            The current stack payload or an empty mapping.
        """

    def resource_id_by_logical_name_map_get(
        self,
        stack_name: str,
    ) -> dict[str, str]:
        """Return physical identities by logical resource.

        Args:
            stack_name: Stack name.

        Returns:
            The physical identities by logical resource.
        """

    def template_validate(self, template_path: Path) -> None:
        """Validate one CloudFormation template.

        Args:
            template_path: Exact filesystem path for template.
        """


class DevelopmentProvisioningManager:
    """Own data-plane and compute stack planning, application, and acceptance."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        aws_account_id: str,
        aws_region: str,
        compute: ComputeProtocol,
        compute_stable_identity_logical_id_set: Collection[str],
        compute_template_path: Path,
        cost_reviewer: CostReviewerProtocol,
        data_plane_template_path: Path,
        foundation: FoundationProtocol,
        host_artifact: HostArtifactProtocol,
        identity: EnvironmentIdentityProtocol,
        project_root_path: Path,
        replacement: ReplacementProtocol,
        retained_volume: RetainedVolumeProtocol,
        source_publisher: SourcePublisherProtocol,
        stack: StackManagerProtocol,
    ) -> None:
        """Bind provisioning to one exact environment and two stack templates.

        Args:
            account: Account.
            aws_account_id: Exact AWS account identity.
            aws_region: Aws region.
            compute: Compute.
            compute_stable_identity_logical_id_set: Unique compute stable identity logical identity values.
            compute_template_path: Exact filesystem path for compute template.
            cost_reviewer: Cost reviewer.
            data_plane_template_path: Exact filesystem path for data plane template.
            foundation: Foundation.
            host_artifact: Host artifact.
            identity: Identity.
            project_root_path: Exact filesystem path for project root.
            replacement: Replacement.
            retained_volume: Retained volume.
            source_publisher: Source publisher.
            stack: Stack.
        """

        self._account = account
        self._aws_account_id = aws_account_id
        self._aws_region = aws_region
        self._compute = compute
        self._compute_stable_identity_logical_id_set = frozenset(compute_stable_identity_logical_id_set)
        self._compute_template_path = compute_template_path
        self._cost_reviewer = cost_reviewer
        self._data_plane_template_path = data_plane_template_path
        self._foundation = foundation
        self._host_artifact = host_artifact
        self._identity = identity
        self._project_root_path = project_root_path
        self._replacement = replacement
        self._retained_volume = retained_volume
        self._source_publisher = source_publisher
        self._stack = stack

    def apply(self) -> None:
        """Validate, plan, apply, and verify the data-plane and compute stacks."""

        self._account.local_operator_context_validate()
        self._foundation.ensure()
        self._source_publisher.validate_repository(self._project_root_path, "workflow-infrastructure")
        self._cost_reviewer.record()
        data_stack_exists = bool(
            self._stack.payload_get(
                self._identity.data_plane_stack_name,
                is_required=False,
            )
        )
        if data_stack_exists:
            self._stack.drift_validate(self._identity.data_plane_stack_name)
            self._data_stack_contract_validate(
                self._stack.parameter_by_name_map_get(self._identity.data_plane_stack_name),
                require_port_reservation=False,
            )
        compute_stack_exists = bool(
            self._stack.payload_get(
                self._identity.compute_stack_name,
                is_required=False,
            )
        )
        replacement_recovery_is_pending = False
        failed_bootstrap_replacement_is_pending = False
        if compute_stack_exists:
            self._stack.drift_validate(self._identity.compute_stack_name)
            current_compute_parameter_by_name_map = self._stack.parameter_by_name_map_get(
                self._identity.compute_stack_name
            )
            self.current_compute_stack_contract_validate(current_compute_parameter_by_name_map)
            replacement_recovery_is_pending = (
                current_compute_parameter_by_name_map.get("ReplacementGuardScheduleState") == "ENABLED"
            )
        data_resource_id_by_logical_name_map = (
            self._stack.resource_id_by_logical_name_map_get(self._identity.data_plane_stack_name)
            if data_stack_exists
            else {}
        )
        self._stack.template_validate(self._data_plane_template_path)
        self._stack.template_validate(self._compute_template_path)
        self._stack.apply(
            stack_name=self._identity.data_plane_stack_name,
            template_path=self._data_plane_template_path,
            parameter_by_name_map={
                "EnvironmentName": self._identity.environment_name,
                "GitWorktree": self._identity.git_worktree,
                "LocalHttpPort": str(self._identity.local_http_port),
                "UiOrigin": f"http://localhost:{self._identity.local_http_port}",
            },
            must_preserve_resource=True,
        )
        self._account.account_foundation_validate()
        if data_resource_id_by_logical_name_map:
            current_resource_id_by_logical_name_map = self._stack.resource_id_by_logical_name_map_get(
                self._identity.data_plane_stack_name
            )
            self._stack.existing_resource_identity_validate(
                current_resource_id_by_logical_name_map=(current_resource_id_by_logical_name_map),
                previous_resource_id_by_logical_name_map=(data_resource_id_by_logical_name_map),
            )
        platform_role_arn = self._stack.output_by_name_map_get(self._identity.data_plane_stack_name)["PlatformRoleArn"]
        observability_bucket_name = self._stack.output_by_name_map_get(self._identity.data_plane_stack_name)[
            "ObservabilityBucketName"
        ]
        host_artifact_parameter_by_name_map = self._host_artifact.cloudformation_parameter_by_name_map_get(
            bucket_name=observability_bucket_name,
            compute_stack_exists=compute_stack_exists,
        )
        if self._identity.is_primary:
            self._foundation.ensure(primary_platform_role_arn=platform_role_arn)
        platform_role_name = platform_role_arn.rsplit("/", maxsplit=1)[-1]
        if not platform_role_name:
            raise DevelopmentEnvironmentError("Data-plane platform role output is malformed")
        compute_parameter_by_name_map: dict[str, str] = {
            "EnvironmentName": self._identity.environment_name,
            "GitWorktree": self._identity.git_worktree,
            "PlatformRoleName": platform_role_name,
            **host_artifact_parameter_by_name_map,
        }
        if compute_stack_exists:
            compute_parameter_by_name_map["InstanceLaunchTemplateVersion"] = self._compute.launch_template_version_get()
        else:
            compute_parameter_by_name_map["RetainedVolumeFilesystemState"] = "pending"
            compute_parameter_by_name_map.update(self._replacement.guard_parameter_by_name_map_get())
        self._stack.apply(
            stack_name=self._identity.compute_stack_name,
            template_path=self._compute_template_path,
            parameter_by_name_map=compute_parameter_by_name_map,
            must_preserve_resource=False,
            protected_identity_logical_id_set=(self._compute_stable_identity_logical_id_set),
        )
        self._retained_volume.attachment_validate()
        if self._identity.is_primary:
            retained_volume_id = self._stack.output_by_name_map_get(self._identity.compute_stack_name)[
                "RetainedVolumeId"
            ]
            self._foundation.ensure(
                primary_platform_role_arn=platform_role_arn,
                primary_retained_volume_arn=(
                    f"arn:aws:ec2:{self._aws_region}:{self._aws_account_id}:volume/{retained_volume_id}"
                ),
            )
        self._compute.launch_template_version_validate(require_latest=False)
        replacement_recovery_finished = False
        if replacement_recovery_is_pending:
            failed_bootstrap_replacement_is_pending = self._compute.failed_bootstrap_replacement_is_proven()
            if not failed_bootstrap_replacement_is_pending:
                self._replacement.recovery_finish()
                replacement_recovery_finished = True
        if self._compute.launch_template_update_is_pending():
            self._replacement.pending_launch_template_apply(
                parameter_by_name_map=self._replacement.parameter_by_name_map_get()
            )
        elif failed_bootstrap_replacement_is_pending:
            raise DevelopmentEnvironmentError(
                "Failed bootstrap host has no newer launch-template version " "available for replacement"
            )
        elif not replacement_recovery_finished:
            self._replacement.steady_state_finish()
        self._stack.drift_validate(self._identity.data_plane_stack_name)
        self._data_stack_contract_validate(
            self._stack.parameter_by_name_map_get(self._identity.data_plane_stack_name),
            require_port_reservation=True,
        )
        self._stack.drift_validate(self._identity.compute_stack_name)
        self._retained_volume.regular_backup_validate()
        print("OK: development data-plane and compute stacks are applied")

    def current_compute_stack_contract_validate(
        self,
        parameter_by_name_map: Mapping[str, str],
    ) -> None:
        """Require an existing compute stack to implement the one current contract.

        Args:
            parameter_by_name_map: Parameter by name mapping.
        """

        manifest_sha256 = parameter_by_name_map.get("HostArtifactManifestSha256")
        encoded_manifest = parameter_by_name_map.get("HostArtifactManifestGzipBase64")
        if (
            not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(encoded_manifest, str)
            or not encoded_manifest
            or parameter_by_name_map.get("RetainedVolumeFilesystemState") not in {"complete", "pending"}
        ):
            raise DevelopmentEnvironmentError(
                "Compute stack does not implement the current host-artifact "
                "contract; delete and recreate the pre-production compute stack"
            )
        self._stack_environment_identity_validate(
            parameter_by_name_map,
            owner="Compute",
        )

    def _stack_environment_identity_validate(
        self,
        parameter_by_name_map: Mapping[str, str],
        *,
        owner: str,
    ) -> None:
        """Reject a short-name collision before changing an existing stack.

        Args:
            parameter_by_name_map: Parameter by name mapping.
            owner: Owner.
        """

        if (
            parameter_by_name_map.get("EnvironmentName") != self._identity.environment_name
            or parameter_by_name_map.get("GitWorktree") != self._identity.git_worktree
        ):
            raise DevelopmentEnvironmentError(f"{owner} stack is bound to another full task common prefix")

    def _data_stack_contract_validate(
        self,
        parameter_by_name_map: Mapping[str, str],
        *,
        require_port_reservation: bool,
    ) -> None:
        """Prove the data stack owns this environment and exact tunnel endpoint.

        Args:
            parameter_by_name_map: Parameter by name mapping.
            require_port_reservation: Require port reservation.
        """

        self._stack_environment_identity_validate(
            parameter_by_name_map,
            owner="Data-plane",
        )
        expected_port = str(self._identity.local_http_port)
        expected_origin = f"http://localhost:{expected_port}"
        current_origin = parameter_by_name_map.get("UiOrigin")
        current_port = parameter_by_name_map.get("LocalHttpPort")
        if current_origin != expected_origin or (current_port is not None and current_port != expected_port):
            raise DevelopmentEnvironmentError("Data-plane stack is bound to another local HTTP endpoint")
        if require_port_reservation and current_port != expected_port:
            raise DevelopmentEnvironmentError("Data-plane stack does not persist its local HTTP port reservation")
