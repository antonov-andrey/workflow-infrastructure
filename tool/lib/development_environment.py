"""Own provisioning, access, delivery, lifecycle, and recovery of the development environment."""

from __future__ import annotations

import base64
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import tomllib

from tool.lib.host_artifact import (
    HostArtifactResolution,
    HostArtifactResolutionError,
    HostArtifactResolver,
    host_artifact_manifest_decode,
)

AWS_ACCOUNT_ID = "463564115167"
AWS_PROFILE = "workflow-control-center-devel"
AWS_REGION = "us-east-1"
CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT = 51_200
CLOUDFORMATION_S3_TEMPLATE_MAX_BYTE_COUNT = 1_048_576
COMPUTE_STACK_NAME = "workflow-control-center-development-compute"
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
DATA_PLANE_STACK_NAME = "workflow-control-center-development"
HOST_CONTROL_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/control/current")
HOST_CONTROL_RELEASE_ROOT_PATH = Path("/opt/workflow-infrastructure/control/releases")
HOST_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/current")
HOST_PYTHON_PATH = Path("/usr/local/bin/python3.14")
HOST_ARTIFACT_MANIFEST_PATH = Path(
    "/etc/workflow-control-center/host-artifact-manifest.json.gz.b64"
)
HOST_ARTIFACT_MANIFEST_SHA256_PATH = Path(
    "/etc/workflow-control-center/host-artifact-manifest.sha256"
)
HOST_EBS_DEVICE_BY_ID_ROOT_PATH = Path("/dev/disk/by-id")
HOST_RETAINED_ROOT_PATH = Path("/srv/workflow-control-center")
HOST_RETAINED_RELEASE_ROOT_PATH = HOST_RETAINED_ROOT_PATH / "release"
HOST_RETAINED_CURRENT_RELEASE_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "current"
HOST_RELEASE_ROOT_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "releases"
HOST_STATE_ROOT_PATH = Path("/var/lib/workflow-infrastructure")
HELM_BINARY_PATH = Path("/usr/local/bin/helm")
INSTANCE_NAME = "workflow-control-center-development"
LEASE_DURATION = timedelta(hours=2)
LEASE_GROUP_NAME = "workflow-control-center-development"
LEASE_NAME = "workflow-control-center-development-stop"
LEASE_RENEW_INTERVAL = timedelta(minutes=30)
LIFECYCLE_ACCEPTANCE_INITIAL_LEASE_DURATION = timedelta(minutes=2)
LIFECYCLE_ACCEPTANCE_RENEW_DELAY_SECONDS = 45
LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION = timedelta(minutes=4)
LIFECYCLE_ACCEPTANCE_RENEWAL_PROOF_DELAY = timedelta(minutes=3, seconds=15)
LIFECYCLE_ACCEPTANCE_STOP_GRACE = timedelta(minutes=5)
MOVING_SOURCE_RESOLUTION_ATTEMPT_COUNT = 3
MOVING_SOURCE_SELECTOR = "HEAD"
PRODUCT_SOURCE_REPOSITORY_NAME_LIST = [
    "browser-runtime",
    "vpn-runtime",
    "workflow-container-runtime",
    "workflow-control-center",
]
PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT = "PYTHONDONTWRITEBYTECODE=1"
REPOSITORY_URL_BY_NAME_MAP = {
    "browser-runtime": "git@github.com:antonov-andrey/browser-runtime.git",
    "vpn-runtime": "git@github.com:antonov-andrey/vpn-runtime.git",
    "workflow-container-contract": "git@github.com:antonov-andrey/workflow-container-contract.git",
    "workflow-container-runtime": "git@github.com:antonov-andrey/workflow-container-runtime.git",
    "workflow-control-center": "git@github.com:antonov-andrey/workflow-control-center.git",
    "workflow-infrastructure": "git@github.com:antonov-andrey/workflow-infrastructure.git",
}
SSM_DOCUMENT_PORT_FORWARD = "AWS-StartPortForwardingSession"
SSM_COMMAND_TIMEOUT_SECONDS = 3600
SSM_ONLINE_TIMEOUT_SECONDS = 1800
HOST_READY_TIMEOUT_SECONDS = 1800
HOST_STATUS_COMMAND_TIMEOUT_SECONDS = 120
SOURCE_MANIFEST_VERSION = 4
PRODUCT_RELEASE_MANIFEST_VERSION = 2
SOURCE_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "environment_name",
        "host_artifact_manifest",
        "python_bytecode_write_disabled",
        "release",
        "repository_by_name_map",
        "source_manifest_version",
        "t_deploy",
    }
)
SOURCE_REPOSITORY_FIELD_NAME_SET = frozenset(
    {
        "archive_sha256",
        "commit_sha",
        "file_sha256_by_path_map",
        "repository_url",
        "source_kind",
        "submodule_by_path_map",
    }
)
MOVING_SOURCE_REPOSITORY_FIELD_NAME_SET = frozenset(
    {
        *SOURCE_REPOSITORY_FIELD_NAME_SET,
        "package_version",
        "requested_selector",
        "resolved_ref",
    }
)
PRODUCT_RELEASE_MANIFEST_FIELD_NAME_SET = frozenset(
    {
        "environment_name",
        "helm_chart_by_name_map",
        "host_artifact_manifest",
        "image_by_name_map",
        "ingress_manifest",
        "release",
        "release_manifest_version",
        "render_sha256",
        "source_by_name_map",
        "source_manifest_sha256",
        "t_deploy",
        "target_platform",
        "ui_http_security_policy",
    }
)
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
            raise DevelopmentEnvironmentError(
                "environment_name must match [a-z][a-z0-9]{0,15}"
            )
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
        return (
            Path("/opt/workflow-infrastructure/environments")
            / self.environment_name
            / "control"
        )

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
    def host_product_recovery_marker_path(self) -> Path:
        """Return the retained interrupted-Product-recovery marker."""

        return self.host_retained_release_root_path / "recovery-pending.json"

    @property
    def host_current_source_path(self) -> Path:
        """Return the root-volume Product current-source pointer."""

        if self.is_primary:
            return HOST_CURRENT_SOURCE_PATH
        return (
            Path("/opt/workflow-infrastructure/environments")
            / self.environment_name
            / "current"
        )

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

        return (
            f"{self.compute_stack_name}:"
            "apwid-platform/workflow-control-center-aws-credentials"
        )


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
            raise DevelopmentEnvironmentError(
                f"Unable to execute {command_list[0]}: {error}"
            ) from error
        except subprocess.CalledProcessError as error:
            error_text = (
                error.stderr or error.stdout or f"exit {error.returncode}"
            ).strip()
            raise DevelopmentEnvironmentError(
                f"{command_list[0]} failed: {error_text}"
            ) from error


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
    """Own the complete bounded development-environment workflow."""

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

    def apply(self) -> None:
        """Validate, plan, apply, and verify the data-plane and compute stacks."""

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        self._cost_review_record()
        self._stack_drift_validate(self._identity.data_plane_stack_name)
        compute_stack_exists = bool(
            self._stack_payload_get(
                self._identity.compute_stack_name, is_required=False
            )
        )
        replacement_recovery_is_pending = False
        failed_bootstrap_replacement_is_pending = False
        if compute_stack_exists:
            self._stack_drift_validate(self._identity.compute_stack_name)
            current_compute_parameter_by_name_map = (
                self._stack_parameter_by_name_map_get(self._identity.compute_stack_name)
            )
            self._current_compute_stack_contract_validate(
                current_compute_parameter_by_name_map
            )
            replacement_recovery_is_pending = (
                current_compute_parameter_by_name_map.get(
                    "ReplacementGuardScheduleState"
                )
                == "ENABLED"
            )
        host_artifact_resolution = self._host_artifact_resolution_get(
            compute_stack_exists=compute_stack_exists
        )
        data_resource_id_by_logical_name_map = (
            self._stack_resource_id_by_logical_name_map_get(
                self._identity.data_plane_stack_name
            )
        )
        self._template_validate(
            self._project_root_path
            / "cloudformation/workflow-control-center-development.yaml"
        )
        self._template_validate(
            self._project_root_path
            / "cloudformation/workflow-control-center-development-compute.yaml"
        )
        self._stack_apply(
            stack_name=self._identity.data_plane_stack_name,
            template_path=self._project_root_path
            / "cloudformation/workflow-control-center-development.yaml",
            parameter_by_name_map={
                "EnvironmentName": self._identity.environment_name,
                "UiOrigin": "http://localhost:8080",
            },
            must_preserve_resource=True,
        )
        if data_resource_id_by_logical_name_map:
            current_resource_id_by_logical_name_map = (
                self._stack_resource_id_by_logical_name_map_get(
                    self._identity.data_plane_stack_name
                )
            )
            self._existing_stack_resource_identity_validate(
                current_resource_id_by_logical_name_map=current_resource_id_by_logical_name_map,
                previous_resource_id_by_logical_name_map=data_resource_id_by_logical_name_map,
            )
        platform_role_arn = self._stack_output_by_name_map_get(
            self._identity.data_plane_stack_name
        )["PlatformRoleArn"]
        platform_role_name = platform_role_arn.rsplit("/", maxsplit=1)[-1]
        if not platform_role_name:
            raise DevelopmentEnvironmentError(
                "Data-plane platform role output is malformed"
            )
        if replacement_recovery_is_pending:
            failed_bootstrap_replacement_is_pending = (
                self._failed_replacement_host_bootstrap_is_proven()
            )
            if not failed_bootstrap_replacement_is_pending:
                self._replacement_recovery_finish()
        compute_parameter_by_name_map: dict[str, str] = {
            "EnvironmentName": self._identity.environment_name,
            "PlatformRoleName": platform_role_name,
            **host_artifact_resolution.cloudformation_parameter_by_name_map_get(),
        }
        if compute_stack_exists:
            compute_parameter_by_name_map["InstanceLaunchTemplateVersion"] = (
                self._instance_launch_template_version_get()
            )
        else:
            compute_parameter_by_name_map.update(
                self._replacement_guard_parameter_by_name_map_get()
            )
        self._stack_apply(
            stack_name=self._identity.compute_stack_name,
            template_path=self._project_root_path
            / "cloudformation/workflow-control-center-development-compute.yaml",
            parameter_by_name_map=compute_parameter_by_name_map,
            must_preserve_resource=False,
            protected_identity_logical_id_set=(COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET),
        )
        self._retained_volume_attachment_validate()
        self._instance_launch_template_version_validate(require_latest=False)
        if self._instance_launch_template_update_is_pending():
            self.stop(should_validate_drift=False)
            self._replacement_stack_apply(
                parameter_by_name_map=(self._replacement_parameter_by_name_map_get())
            )
            self._replacement_recovery_finish()
        elif failed_bootstrap_replacement_is_pending:
            raise DevelopmentEnvironmentError(
                "Failed bootstrap host has no newer launch-template version "
                "available for replacement"
            )
        else:
            self._steady_state_start_finish()
        self._stack_drift_validate(self._identity.data_plane_stack_name)
        self._stack_drift_validate(self._identity.compute_stack_name)
        self._retained_snapshot_policy_validate()
        print("OK: development data-plane and compute stacks are applied")

    def _current_compute_stack_contract_validate(
        self,
        parameter_by_name_map: Mapping[str, str],
    ) -> None:
        """Require an existing compute stack to implement the one current contract."""

        manifest_sha256 = parameter_by_name_map.get("HostArtifactManifestSha256")
        encoded_manifest = parameter_by_name_map.get("HostArtifactManifestGzipBase64")
        if (
            parameter_by_name_map.get("EnvironmentName")
            != self._identity.environment_name
            or not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(encoded_manifest, str)
            or not encoded_manifest
        ):
            raise DevelopmentEnvironmentError(
                "Compute stack does not implement the current host-artifact "
                "contract; delete and recreate the pre-production compute stack"
            )

    def _steady_state_start_finish(self) -> None:
        """Start current compute and resume only a proven pending Product recovery."""

        self.start(should_publish_infrastructure_source=True)
        product_recovery_is_pending = self._product_recovery_is_pending()
        if product_recovery_is_pending:
            self._product_recovery_begin()
        self._replacement_guard_disable()
        if product_recovery_is_pending:
            self._product_recovery_finish()

    def _replacement_recovery_finish(
        self,
    ) -> None:
        """Finish one created replacement host from retained Product state."""

        self.start(should_publish_infrastructure_source=True)
        if self._product_recovery_status_get() == "absent":
            self._replacement_guard_disable()
            print("OK: replacement host has no retained Product release to recover")
            return
        self._product_recovery_begin()
        self._replacement_guard_disable()
        self._product_recovery_finish()

    def _product_recovery_finish(self) -> None:
        """Restore and accept Product state, then clear its durable savepoint."""

        self._retained_product_release_link_restore()
        self._product_recovery_apply_run()
        self._product_recovery_acceptance_run()
        self._product_recovery_complete()

    def _failed_replacement_host_bootstrap_is_proven(self) -> bool:
        """Return whether an unmounted disposable replacement host failed bootstrap."""

        diagnostic_code = "\n".join(
            [
                "import json",
                "import subprocess",
                "cloud_init = subprocess.run(",
                "    ['cloud-init', 'status', '--long'],",
                "    capture_output=True,",
                "    check=False,",
                "    text=True,",
                ")",
                "retained_mount = subprocess.run(",
                "    [",
                "        'findmnt',",
                "        '--noheadings',",
                "        '--output',",
                "        'TARGET',",
                "        '--target',",
                f"        {str(self._identity.host_retained_root_path)!r},",
                "    ],",
                "    capture_output=True,",
                "    check=False,",
                "    text=True,",
                ")",
                "k3s = subprocess.run(",
                "    ['systemctl', 'is-active', 'k3s'],",
                "    capture_output=True,",
                "    check=False,",
                "    text=True,",
                ")",
                "print(",
                "    json.dumps(",
                "        {",
                "            'cloud_init_returncode': cloud_init.returncode,",
                "            'cloud_init_status': cloud_init.stdout,",
                "            'k3s_status': k3s.stdout.strip(),",
                "            'retained_mount_target': retained_mount.stdout.strip(),",
                "        },",
                "        sort_keys=True,",
                "    )",
                ")",
            ]
        )
        payload = self._ssm_shell_result_get(
            [shlex.join(["python3", "-c", diagnostic_code])],
            timeout_seconds=HOST_STATUS_COMMAND_TIMEOUT_SECONDS,
        )
        output_text = payload.get("StandardOutputContent")
        if not isinstance(output_text, str):
            raise DevelopmentEnvironmentError(
                "Replacement bootstrap diagnostic output is malformed"
            )
        try:
            diagnostic = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Replacement bootstrap diagnostic output is not JSON"
            ) from error
        if not isinstance(diagnostic, dict):
            raise DevelopmentEnvironmentError(
                "Replacement bootstrap diagnostic payload is malformed"
            )
        cloud_init_returncode = diagnostic.get("cloud_init_returncode")
        cloud_init_status = diagnostic.get("cloud_init_status")
        k3s_status = diagnostic.get("k3s_status")
        retained_mount_target = diagnostic.get("retained_mount_target")
        if (
            not isinstance(cloud_init_returncode, int)
            or not isinstance(cloud_init_status, str)
            or not isinstance(k3s_status, str)
            or not isinstance(retained_mount_target, str)
        ):
            raise DevelopmentEnvironmentError(
                "Replacement bootstrap diagnostic fields are malformed"
            )
        if cloud_init_returncode == 0:
            return False
        if (
            cloud_init_returncode != 1
            or "status: error" not in cloud_init_status
            or "extended_status: error - done" not in cloud_init_status
        ):
            raise DevelopmentEnvironmentError(
                "Replacement host cloud-init state is neither success nor a "
                "proven terminal bootstrap failure"
            )
        if (
            retained_mount_target == str(self._identity.host_retained_root_path)
            or k3s_status == "active"
        ):
            raise DevelopmentEnvironmentError(
                "Failed replacement bootstrap reached retained state or active k3s; "
                "automatic host replacement is unsafe"
            )
        print(
            "OK: replacement host bootstrap failure is terminal and retained state "
            "is unmounted"
        )
        return True

    def connect(self) -> int:
        """Open the Product HTTP tunnel through Session Manager.

        Returns:
            Foreground Session Manager process status.
        """

        self._local_operator_context_validate()
        instance_id = self._instance_id_get()
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "start-session",
                "--profile",
                AWS_PROFILE,
                "--region",
                AWS_REGION,
                "--target",
                instance_id,
                "--document-name",
                SSM_DOCUMENT_PORT_FORWARD,
                "--parameters",
                json.dumps(
                    {"localPortNumber": ["8080"], "portNumber": ["8080"]},
                    separators=(",", ":"),
                ),
            ],
            check=False,
            should_capture=False,
        )
        return result.returncode

    def console(self) -> int:
        """Open an ordinary Session Manager console.

        Returns:
            Foreground Session Manager process status.
        """

        self._local_operator_context_validate()
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "start-session",
                "--profile",
                AWS_PROFILE,
                "--region",
                AWS_REGION,
                "--target",
                self._instance_id_get(),
            ],
            check=False,
            should_capture=False,
        )
        return result.returncode

    def deploy(self, *, workflow_container_contract_commit: str = "") -> None:
        """Publish exact Product sources and invoke the Product-owned deployment.

        Args:
            workflow_container_contract_commit: Optional exact one-deploy source override.
        """

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        for repository_name in PRODUCT_SOURCE_REPOSITORY_NAME_LIST:
            self._source_repository_validate(
                self._workspace_root_path / repository_name, repository_name
            )
        self._stack_drift_validate(self._identity.data_plane_stack_name)
        self._stack_drift_validate(self._identity.compute_stack_name)
        self._instance_online_wait()
        self._instance_launch_template_version_validate(require_latest=True)
        if self._product_recovery_status_get() == "pending":
            raise DevelopmentEnvironmentError(
                "Pending retained Product recovery must complete before a new deploy"
            )
        release_name = self._clock.now().strftime("%Y%m%d%H%M%S%f")
        source_manifest_by_repository_name_map: dict[str, dict[str, object]] = {}
        with self._ssh_control_session() as ssh_control_path:
            repository_name_list = [
                "workflow-infrastructure",
                *PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
            ]
            for repository_name in repository_name_list:
                repository_path = (
                    self._project_root_path
                    if repository_name == "workflow-infrastructure"
                    else self._workspace_root_path / repository_name
                )
                source_manifest_by_repository_name_map[repository_name] = (
                    self._source_archive_publish(
                        repository_name=repository_name,
                        repository_path=repository_path,
                        release_name=release_name,
                        remote_release_root_path=self._identity.host_release_root_path,
                        ssh_control_path=ssh_control_path,
                    )
                )
            source_manifest_by_repository_name_map["workflow-container-contract"] = (
                self._moving_source_archive_publish(
                    exact_override_commit=workflow_container_contract_commit,
                    release_name=release_name,
                    remote_release_root_path=self._identity.host_release_root_path,
                    repository_name="workflow-container-contract",
                    ssh_control_path=ssh_control_path,
                )
            )
            release_manifest_text = json.dumps(
                {
                    "environment_name": self._identity.environment_name,
                    "host_artifact_manifest": self._host_artifact_manifest_payload_get(),
                    "python_bytecode_write_disabled": True,
                    "release": release_name,
                    "repository_by_name_map": source_manifest_by_repository_name_map,
                    "source_manifest_version": SOURCE_MANIFEST_VERSION,
                    "t_deploy": self._clock.now().isoformat().replace("+00:00", "Z"),
                },
                indent=2,
                sort_keys=True,
            )
            self._remote_text_write(
                remote_path=self._identity.host_release_root_path
                / release_name
                / "source-manifest.json",
                text=release_manifest_text,
                ssh_control_path=ssh_control_path,
            )
            release_root_path = self._identity.host_release_root_path / release_name
            self._ssh_run(
                [
                    "sudo",
                    "env",
                    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                    "python3.14",
                    "-B",
                    str(
                        release_root_path
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-prepare",
                    "--environment-name",
                    self._identity.environment_name,
                ],
                ssh_control_path=ssh_control_path,
            )
            platform = self._runtime_platform_get(ssh_control_path)
            product_command_list = [
                "sudo",
                "env",
                PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                "python3.14",
                "-B",
                str(
                    release_root_path
                    / "sources"
                    / "workflow-control-center"
                    / "tool"
                    / "development_kubernetes_manage.py"
                ),
                "deploy",
                "--release",
                release_name,
                "--source-root",
                str(release_root_path / "sources"),
                "--target-platform",
                platform,
                "--environment-name",
                self._identity.environment_name,
            ]
            self._ssh_run(
                product_command_list,
                ssh_control_path=ssh_control_path,
                should_capture=False,
            )
            self._ssh_run(
                [
                    "sudo",
                    "env",
                    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                    "python3.14",
                    "-B",
                    str(
                        release_root_path
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-product-release-activate",
                    "--environment-name",
                    self._identity.environment_name,
                    "--release",
                    release_name,
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "install",
                    "-d",
                    "-m",
                    "0755",
                    str(self._identity.host_control_release_root_path),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "ln",
                    "-sfn",
                    str(release_root_path),
                    str(self._identity.host_control_current_source_path),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "env",
                    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                    "python3.14",
                    "-B",
                    str(
                        self._identity.host_current_source_path
                        / "sources"
                        / "workflow-control-center"
                        / "tool"
                        / "development_kubernetes_manage.py"
                    ),
                    "host-install",
                    "--environment-name",
                    self._identity.environment_name,
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "env",
                    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                    "python3.14",
                    "-B",
                    str(
                        self._identity.host_control_current_source_path
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-install",
                    "--environment-name",
                    self._identity.environment_name,
                ],
                ssh_control_path=ssh_control_path,
            )
        print(f"OK: exact Product release {release_name} is deployed for {platform}")

    def _current_product_tool_path_get(self) -> Path:
        """Return the current exact Product management-tool path."""

        return (
            self._identity.host_current_source_path
            / "sources"
            / "workflow-control-center"
            / "tool"
            / "development_kubernetes_manage.py"
        )

    def _current_product_tool_command_list_get(self, command: str) -> list[str]:
        """Return one environment-bound command for the current Product tool.

        Args:
            command: Product management subcommand.

        Returns:
            Exact Python command with the selected environment identity.
        """

        command_list = [
            "env",
            PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
            "python3.14",
            "-B",
            str(self._current_product_tool_path_get()),
            command,
            "--environment-name",
            self._identity.environment_name,
        ]
        return command_list

    def _current_infrastructure_tool_command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one environment-bound exact infrastructure-control command."""

        return [
            "env",
            PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
            "python3.14",
            "-B",
            str(
                self._identity.host_control_current_source_path
                / "sources"
                / "workflow-infrastructure"
                / "tool"
                / "development_environment_manage.py"
            ),
            command,
            "--environment-name",
            self._identity.environment_name,
            *argument_list,
        ]

    def diagnose(self) -> None:
        """Print bounded infrastructure and Product diagnostics without secret values."""

        self._local_operator_context_validate()
        self.status()
        instance_id = self._instance_id_get()
        if self._instance_state_get(instance_id) != "running":
            print("OK: remote diagnostics skipped because the instance is not running")
            return
        self._ssm_shell_run(
            [
                f"df -h / {self._identity.host_retained_root_path}",
                "sudo systemctl --no-pager --full status k3s workflow-control-center-host-controller || true",
                "sudo k3s kubectl get nodes,namespaces -o wide",
                "sudo k3s kubectl get pods --all-namespaces -o wide",
                "sudo k3s kubectl get events --all-namespaces --sort-by=.lastTimestamp | tail -200",
                (
                    "sudo "
                    + shlex.join(
                        self._current_product_tool_command_list_get("diagnose")
                    )
                    + " || true"
                ),
            ]
        )

    def host_controller(self) -> None:
        """Run the fail-safe host lifecycle controller until shutdown."""

        instance_id = self._instance_metadata_get("instance-id")
        self._runner.run(
            ["k3s", "kubectl", "uncordon", self._host_node_name_get()], check=False
        )
        idle_start_path = self._identity.host_state_root_path / "idle-start"
        self._identity.host_state_root_path.mkdir(
            mode=0o750, parents=True, exist_ok=True
        )
        idle_start_path.unlink(missing_ok=True)
        t_last_lease_renew = datetime.min.replace(tzinfo=UTC)
        while True:
            t_now = self._clock.now()
            have_session = self._host_session_state_is_busy(instance_id)
            product_activity = self._host_product_activity_get()
            is_busy = have_session or product_activity == "busy"
            if is_busy:
                idle_start_path.unlink(missing_ok=True)
                if t_now - t_last_lease_renew >= LEASE_RENEW_INTERVAL:
                    self._stop_lease_upsert()
                    t_last_lease_renew = t_now
            else:
                if not idle_start_path.exists():
                    idle_start_path.write_text(t_now.isoformat(), encoding="utf-8")
                    os.chmod(idle_start_path, 0o600)
                idle_start_text = idle_start_path.read_text(encoding="utf-8").strip()
                try:
                    t_idle_start = datetime.fromisoformat(idle_start_text)
                except ValueError:
                    idle_start_path.unlink(missing_ok=True)
                    self._clock.sleep(60)
                    continue
                if t_now - t_idle_start >= timedelta(minutes=30):
                    have_session = self._host_session_state_is_busy(instance_id)
                    product_activity = self._host_product_activity_get()
                    if not have_session and product_activity == "idle":
                        self.host_shutdown()
                        return
                    idle_start_path.unlink(missing_ok=True)
            self._clock.sleep(60)

    def host_prepare(self) -> None:
        """Install exact source-owned host dependencies required before Product deploy."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-prepare is supported only from an exact source release on the development host"
            )
        host_artifact_manifest = self._host_artifact_manifest_get()
        if self._project_root_path.is_relative_to(
            self._identity.host_release_root_path
        ):
            source_manifest_path = (
                self._project_root_path.parent.parent / "source-manifest.json"
            )
            try:
                source_manifest = json.loads(
                    source_manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise DevelopmentEnvironmentError(
                    "Product source manifest is unavailable during host preparation"
                ) from error
            if (
                not isinstance(source_manifest, Mapping)
                or source_manifest.get("environment_name")
                != self._identity.environment_name
                or source_manifest.get("host_artifact_manifest")
                != host_artifact_manifest
            ):
                raise DevelopmentEnvironmentError(
                    "Product release and active host artifact identities differ"
                )
        helm_version = self._helm_validate(host_artifact_manifest)
        print(f"OK: exact Helm {helm_version} is installed")

    @staticmethod
    def _product_tool_runtime_validate(runtime_path: Path) -> None:
        """Require one portable Product-tool virtual environment.

        Args:
            runtime_path: Content-addressed runtime directory to validate.

        Raises:
            DevelopmentEnvironmentError: If runtime ownership or Python links
                can depend on the disposable root volume.
        """

        if (
            not runtime_path.is_dir()
            or runtime_path.is_symlink()
            or not (runtime_path / "pyvenv.cfg").is_file()
        ):
            raise DevelopmentEnvironmentError(
                "Product-tool runtime directory is unavailable or unsafe"
            )
        try:
            pyvenv_line_list = (
                (runtime_path / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product-tool runtime Python metadata is unavailable"
            ) from error
        pyvenv_value_by_name_map: dict[str, str] = {}
        for line in pyvenv_line_list:
            name, separator, value = line.partition("=")
            if separator:
                pyvenv_value_by_name_map[name.strip()] = value.strip()
        if pyvenv_value_by_name_map.get("home") != str(HOST_PYTHON_PATH.parent):
            raise DevelopmentEnvironmentError(
                "Product-tool runtime Python home is not host-portable"
            )
        executable = pyvenv_value_by_name_map.get("executable")
        if executable is not None and executable != str(HOST_PYTHON_PATH):
            raise DevelopmentEnvironmentError(
                "Product-tool runtime Python executable is not host-portable"
            )
        for python_name in ("python", "python3", "python3.14"):
            python_path = runtime_path / "bin" / python_name
            if not python_path.is_symlink() or os.readlink(python_path) != str(
                HOST_PYTHON_PATH
            ):
                raise DevelopmentEnvironmentError(
                    "Product-tool runtime Python link is not host-portable"
                )

    def host_status(self, retained_volume_id: str) -> None:
        """Print safe host state from one exact infrastructure release.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the Product root.
        """

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-status is supported only from an exact source release "
                "on the development host"
            )
        payload = self._host_status_payload_validate(
            self._host_status_local_payload_get(retained_volume_id=retained_volume_id)
        )
        print(json.dumps(payload, sort_keys=True))

    @staticmethod
    def _atomic_symlink_replace(*, link_path: Path, target_path: Path) -> None:
        """Atomically replace one host symlink without exposing a missing-current gap."""

        link_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary_link_path = link_path.with_name(
            f".{link_path.name}.tmp-{os.getpid()}"
        )
        temporary_link_path.unlink(missing_ok=True)
        temporary_link_path.symlink_to(target_path)
        try:
            os.replace(temporary_link_path, link_path)
        finally:
            temporary_link_path.unlink(missing_ok=True)

    @staticmethod
    def _atomic_text_file_replace(
        *,
        mode: int,
        path: Path,
        text: str,
    ) -> None:
        """Durably replace one small host-owned text file on its target filesystem."""

        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=path.parent,
                encoding="utf-8",
                prefix=f".{path.name}.tmp-",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                os.fchmod(file.fileno(), mode)
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            if temporary_path is None:
                raise OSError("temporary file was not created")
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _retained_product_current_release_path_get(self) -> Path:
        """Return the exact retained current release or fail closed."""

        current_release_path = self._identity.host_retained_current_release_path
        if not current_release_path.is_symlink():
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link is unavailable"
            )
        try:
            release_root_path = current_release_path.resolve(strict=True)
            current_release_target = os.readlink(current_release_path)
            release_collection_path = self._identity.host_release_root_path.resolve(
                strict=True
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link is broken"
            ) from error
        if current_release_target != str(release_root_path):
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link is not an exact absolute target"
            )
        if (
            release_root_path.parent != release_collection_path
            or not release_root_path.name.isdigit()
            or len(release_root_path.name) not in {17, 20}
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link has an invalid exact identity"
            )
        return release_root_path

    def _product_recovery_marker_payload_validate(
        self,
        *,
        expected_release_name: str,
    ) -> bool:
        """Validate an optional retained recovery marker.

        Args:
            expected_release_name: Exact retained release expected by the marker.

        Returns:
            Whether the marker exists and is valid.
        """

        marker_path = self._identity.host_product_recovery_marker_path
        if not marker_path.parent.is_dir() or marker_path.parent.is_symlink():
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker parent is invalid"
            )
        if marker_path.is_symlink():
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker must not be a symlink"
            )
        if not marker_path.exists():
            return False
        if not marker_path.is_file():
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker is not a regular file"
            )
        try:
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker is unavailable or malformed"
            ) from error
        expected_payload = {
            "environment_name": self._identity.environment_name,
            "release": expected_release_name,
            "state": "pending",
        }
        if marker_payload != expected_payload:
            raise DevelopmentEnvironmentError(
                "Retained Product recovery marker has an inconsistent identity"
            )
        return True

    def _host_product_recovery_status_local_get(self) -> str:
        """Return the local retained Product recovery state."""

        retained_current_path = self._identity.host_retained_current_release_path
        marker_path = self._identity.host_product_recovery_marker_path
        product_current_path = self._identity.host_current_source_path
        if not retained_current_path.is_symlink():
            if (
                retained_current_path.exists()
                or marker_path.exists()
                or marker_path.is_symlink()
                or product_current_path.exists()
                or product_current_path.is_symlink()
            ):
                raise DevelopmentEnvironmentError(
                    "Product recovery state exists without a retained current release"
                )
            status = "absent"
        else:
            release_root_path = self._retained_product_current_release_path_get()
            marker_exists = self._product_recovery_marker_payload_validate(
                expected_release_name=release_root_path.name
            )
            try:
                current_link_is_exact = (
                    product_current_path.is_symlink()
                    and os.readlink(product_current_path) == str(retained_current_path)
                )
            except OSError as error:
                raise DevelopmentEnvironmentError(
                    "Product recovery current-source link is unavailable"
                ) from error
            status = (
                "pending" if marker_exists or not current_link_is_exact else "ready"
            )
        return status

    def host_product_recovery_status(self) -> None:
        """Print whether retained Product recovery must be resumed."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-recovery-status is supported only on the development host"
            )
        status = self._host_product_recovery_status_local_get()
        print(json.dumps({"status": status}, sort_keys=True))

    def host_product_recovery_begin(self) -> None:
        """Persist the exact Product recovery savepoint before releasing the guard."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-recovery-begin is supported only on the development host"
            )
        release_root_path = self._retained_product_current_release_path_get()
        if self._product_recovery_marker_payload_validate(
            expected_release_name=release_root_path.name
        ):
            print(
                f"OK: Product recovery savepoint for {release_root_path.name} already exists"
            )
            return
        marker_path = self._identity.host_product_recovery_marker_path
        marker_text = (
            json.dumps(
                {
                    "environment_name": self._identity.environment_name,
                    "release": release_root_path.name,
                    "state": "pending",
                },
                sort_keys=True,
            )
            + "\n"
        )
        try:
            self._atomic_text_file_replace(
                mode=0o600,
                path=marker_path,
                text=marker_text,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery savepoint could not be persisted"
            ) from error
        self._runner.run(["sync", "-f", str(marker_path.parent)])
        print(f"OK: Product recovery savepoint for {release_root_path.name} is pending")

    def host_product_recovery_complete(self) -> None:
        """Clear the durable Product recovery savepoint after full acceptance."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-recovery-complete is supported only on the development host"
            )
        release_root_path = self._retained_product_current_release_path_get()
        if not self._product_recovery_marker_payload_validate(
            expected_release_name=release_root_path.name
        ):
            raise DevelopmentEnvironmentError(
                "Product recovery cannot complete without its pending savepoint"
            )
        product_current_path = self._identity.host_current_source_path
        try:
            product_current_link_is_exact = (
                product_current_path.is_symlink()
                and os.readlink(product_current_path)
                == str(self._identity.host_retained_current_release_path)
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery current-source link is unavailable"
            ) from error
        if not product_current_link_is_exact:
            raise DevelopmentEnvironmentError(
                "Product recovery current-source link is not restored"
            )
        try:
            self._identity.host_product_recovery_marker_path.unlink()
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Product recovery savepoint could not be completed"
            ) from error
        self._runner.run(
            [
                "sync",
                "-f",
                str(self._identity.host_product_recovery_marker_path.parent),
            ]
        )
        print(
            f"OK: Product recovery savepoint for {release_root_path.name} is complete"
        )

    def _retained_product_release_validate(
        self,
        release_root_path: Path,
    ) -> str:
        """Validate every persisted identity and tracked source byte of one Product release."""

        try:
            resolved_release_root_path = release_root_path.resolve(strict=True)
            resolved_release_parent_path = (
                self._identity.host_release_root_path.resolve(strict=True)
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Retained Product release path is unavailable"
            ) from error
        release_name = resolved_release_root_path.name
        if (
            resolved_release_root_path.parent != resolved_release_parent_path
            or not release_name.isdigit()
            or len(release_name) != 20
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release path has an invalid exact identity"
            )
        source_manifest_path = resolved_release_root_path / "source-manifest.json"
        product_manifest_path = resolved_release_root_path / "release-manifest.json"
        render_path = resolved_release_root_path / "render.yaml"
        try:
            source_manifest_bytes = source_manifest_path.read_bytes()
            source_manifest = json.loads(source_manifest_bytes)
            product_manifest = json.loads(
                product_manifest_path.read_text(encoding="utf-8")
            )
            render_bytes = render_path.read_bytes()
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product release manifests are unavailable or malformed"
            ) from error
        if (
            not isinstance(source_manifest, Mapping)
            or not isinstance(product_manifest, Mapping)
            or source_manifest.get("release") != release_name
            or product_manifest.get("release") != release_name
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release manifests have inconsistent identities"
            )
        source_manifest_version = source_manifest.get("source_manifest_version")
        if source_manifest_version != SOURCE_MANIFEST_VERSION:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest is not the current version"
            )
        if product_manifest.get("release_manifest_version") != (
            PRODUCT_RELEASE_MANIFEST_VERSION
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release manifest is not the current version"
            )
        if set(source_manifest) != SOURCE_MANIFEST_FIELD_NAME_SET:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest does not have the exact current shape"
            )
        if set(product_manifest) != PRODUCT_RELEASE_MANIFEST_FIELD_NAME_SET:
            raise DevelopmentEnvironmentError(
                "Retained Product release manifest does not have the exact current shape"
            )
        if source_manifest.get("python_bytecode_write_disabled") is not True:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest does not prohibit Python bytecode writes"
            )
        if source_manifest.get("environment_name") != self._identity.environment_name:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest belongs to another environment"
            )
        if product_manifest.get("environment_name") != self._identity.environment_name:
            raise DevelopmentEnvironmentError(
                "Retained Product release manifest belongs to another environment"
            )
        source_host_artifact_manifest = source_manifest.get("host_artifact_manifest")
        if (
            not isinstance(source_host_artifact_manifest, Mapping)
            or product_manifest.get("host_artifact_manifest")
            != source_host_artifact_manifest
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product manifests describe different host artifacts"
            )
        if (
            product_manifest.get("source_manifest_sha256")
            != hashlib.sha256(source_manifest_bytes).hexdigest()
            or product_manifest.get("render_sha256")
            != hashlib.sha256(render_bytes).hexdigest()
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release manifest digests are inconsistent"
            )
        target_platform = product_manifest.get("target_platform")
        if target_platform not in {"linux/amd64", "linux/arm64"}:
            raise DevelopmentEnvironmentError(
                "Retained Product release target platform is invalid"
            )

        repository_by_name_map = source_manifest.get("repository_by_name_map")
        required_repository_name_set = {
            "workflow-infrastructure",
            *PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
            "workflow-container-contract",
        }
        if not isinstance(repository_by_name_map, Mapping) or set(
            repository_by_name_map
        ) != (required_repository_name_set):
            raise DevelopmentEnvironmentError(
                "Retained Product source graph is incomplete"
            )
        source_identity_by_name_map: dict[str, dict[str, str]] = {}
        source_root_path = resolved_release_root_path / "sources"
        for repository_name, repository_payload in repository_by_name_map.items():
            if not isinstance(repository_name, str) or not isinstance(
                repository_payload, Mapping
            ):
                raise DevelopmentEnvironmentError(
                    "Retained Product source entry is malformed"
                )
            source_identity: dict[str, str] = {}
            for field_name, expected_length in (
                ("archive_sha256", 64),
                ("commit_sha", 40),
                ("repository_url", 0),
            ):
                field_value = repository_payload.get(field_name)
                if not isinstance(field_value, str) or not field_value:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} {field_name} is invalid"
                    )
                if expected_length:
                    if (
                        len(field_value) != expected_length
                        or field_value != field_value.lower()
                    ):
                        raise DevelopmentEnvironmentError(
                            f"Retained Product source {repository_name} {field_name} is invalid"
                        )
                    try:
                        int(field_value, 16)
                    except ValueError as error:
                        raise DevelopmentEnvironmentError(
                            f"Retained Product source {repository_name} {field_name} is invalid"
                        ) from error
                source_identity[field_name] = field_value
            if (
                source_identity["repository_url"]
                != REPOSITORY_URL_BY_NAME_MAP[repository_name]
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} repository URL is invalid"
                )
            source_kind = repository_payload.get("source_kind")
            expected_source_kind = (
                "resolved_moving_source"
                if repository_name == "workflow-container-contract"
                else "exact_checkout"
            )
            if source_kind != expected_source_kind:
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} source kind is invalid"
                )
            expected_field_name_set = (
                MOVING_SOURCE_REPOSITORY_FIELD_NAME_SET
                if expected_source_kind == "resolved_moving_source"
                else SOURCE_REPOSITORY_FIELD_NAME_SET
            )
            actual_field_name_set = set(repository_payload)
            override_field_name_set = {"override_identity", "override_reason"}
            if (
                actual_field_name_set != expected_field_name_set
                and actual_field_name_set
                != (expected_field_name_set | override_field_name_set)
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} does not have the exact current shape"
                )
            if (
                expected_source_kind == "exact_checkout"
                and actual_field_name_set != expected_field_name_set
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} does not have the exact current shape"
                )
            source_identity["source_kind"] = expected_source_kind
            moving_field_name_set = {
                "override_identity",
                "override_reason",
                "package_version",
                "requested_selector",
                "resolved_ref",
            }
            submodule_by_path_map = repository_payload.get("submodule_by_path_map")
            if not isinstance(submodule_by_path_map, Mapping):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} submodule graph is invalid"
                )
            for submodule_path_text, submodule_payload in submodule_by_path_map.items():
                if (
                    not isinstance(submodule_path_text, str)
                    or not isinstance(submodule_payload, Mapping)
                    or set(submodule_payload) != {"commit_sha", "repository_url"}
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} submodule entry is invalid"
                    )
                submodule_path = PurePosixPath(submodule_path_text)
                submodule_commit_sha = submodule_payload.get("commit_sha")
                submodule_repository_url = submodule_payload.get("repository_url")
                if (
                    not submodule_path_text
                    or submodule_path.is_absolute()
                    or submodule_path.as_posix() != submodule_path_text
                    or any(part in {"", ".", ".."} for part in submodule_path.parts)
                    or not isinstance(submodule_commit_sha, str)
                    or re.fullmatch(r"[0-9a-f]{40}", submodule_commit_sha) is None
                    or not isinstance(submodule_repository_url, str)
                    or not submodule_repository_url
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} submodule identity is invalid"
                    )
            if expected_source_kind == "resolved_moving_source":
                if submodule_by_path_map != {}:
                    raise DevelopmentEnvironmentError(
                        "Retained workflow-container-contract moving source has submodules"
                    )
                for field_name in (
                    "package_version",
                    "requested_selector",
                    "resolved_ref",
                ):
                    field_value = repository_payload.get(field_name)
                    if not isinstance(field_value, str) or not field_value:
                        raise DevelopmentEnvironmentError(
                            "Retained workflow-container-contract "
                            f"{field_name} is invalid"
                        )
                    source_identity[field_name] = field_value
                if source_identity["requested_selector"] != MOVING_SOURCE_SELECTOR:
                    raise DevelopmentEnvironmentError(
                        "Retained workflow-container-contract selector is invalid"
                    )
                if not source_identity["resolved_ref"].startswith("refs/heads/"):
                    raise DevelopmentEnvironmentError(
                        "Retained workflow-container-contract ref is invalid"
                    )
                override_identity = repository_payload.get("override_identity")
                override_reason = repository_payload.get("override_reason")
                if override_identity is None and override_reason is None:
                    pass
                elif (
                    isinstance(override_identity, str)
                    and override_identity == source_identity["commit_sha"]
                    and isinstance(override_reason, str)
                    and bool(override_reason)
                ):
                    source_identity["override_identity"] = override_identity
                    source_identity["override_reason"] = override_reason
                else:
                    raise DevelopmentEnvironmentError(
                        "Retained workflow-container-contract override provenance is invalid"
                    )
            elif any(
                field_name in repository_payload for field_name in moving_field_name_set
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained exact source {repository_name} has moving provenance"
                )
            file_sha256_by_path_map = repository_payload.get("file_sha256_by_path_map")
            if not isinstance(file_sha256_by_path_map, Mapping):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} file graph is invalid"
                )
            repository_root_path = source_root_path / repository_name
            if not repository_root_path.is_dir() or repository_root_path.is_symlink():
                raise DevelopmentEnvironmentError(
                    f"Retained Product source root is unavailable: {repository_name}"
                )
            expected_file_sha256_by_path_map: dict[str, str] = {}
            for relative_path_text, expected_sha256 in file_sha256_by_path_map.items():
                if (
                    not isinstance(relative_path_text, str)
                    or not relative_path_text
                    or not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                    or expected_sha256 != expected_sha256.lower()
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} file identity is invalid"
                    )
                try:
                    int(expected_sha256, 16)
                except ValueError as error:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} file identity is invalid"
                    ) from error
                relative_path = PurePosixPath(relative_path_text)
                if (
                    relative_path.is_absolute()
                    or not relative_path.parts
                    or relative_path.as_posix() != relative_path_text
                    or any(part in {"", ".", ".."} for part in relative_path.parts)
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} path is unsafe"
                    )
                expected_file_sha256_by_path_map[relative_path_text] = expected_sha256
            actual_file_sha256_by_path_map: dict[str, str] = {}
            for source_path in repository_root_path.rglob("*"):
                try:
                    if source_path.is_symlink():
                        source_payload = os.readlink(source_path).encode()
                    elif source_path.is_file():
                        source_payload = source_path.read_bytes()
                    elif source_path.is_dir():
                        continue
                    else:
                        raise DevelopmentEnvironmentError(
                            "Retained Product source contains an unsupported filesystem "
                            f"entry: {repository_name}/"
                            f"{source_path.relative_to(repository_root_path).as_posix()}"
                        )
                except OSError as error:
                    raise DevelopmentEnvironmentError(
                        "Retained Product source file is unavailable: "
                        f"{repository_name}/"
                        f"{source_path.relative_to(repository_root_path).as_posix()}"
                    ) from error
                actual_file_sha256_by_path_map[
                    source_path.relative_to(repository_root_path).as_posix()
                ] = hashlib.sha256(source_payload).hexdigest()
            if actual_file_sha256_by_path_map != expected_file_sha256_by_path_map:
                raise DevelopmentEnvironmentError(
                    f"Retained Product source file graph differs: {repository_name}"
                )
            source_identity_by_name_map[repository_name] = source_identity
        if product_manifest.get("source_by_name_map") != source_identity_by_name_map:
            raise DevelopmentEnvironmentError(
                "Retained Product and source manifests describe different source identities"
            )
        return release_name

    def host_product_release_activate(self, release_name: str) -> None:
        """Validate and atomically activate one accepted retained Product release."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-release-activate is supported only on the development host"
            )
        release_root_path = self._identity.host_release_root_path / release_name
        accepted_release_name = self._retained_product_release_validate(
            release_root_path
        )
        if accepted_release_name != release_name:
            raise DevelopmentEnvironmentError(
                "Retained Product release activation changed exact identity"
            )
        self._atomic_symlink_replace(
            link_path=self._identity.host_retained_current_release_path,
            target_path=release_root_path,
        )
        self._atomic_symlink_replace(
            link_path=self._identity.host_current_source_path,
            target_path=self._identity.host_retained_current_release_path,
        )
        print(f"OK: retained Product release {release_name} is current")

    def host_product_release_restore(self) -> None:
        """Validate the snapshot-owned current release and restore its root-volume link."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-release-restore is supported only on the development host"
            )
        release_root_path = self._retained_product_current_release_path_get()
        release_name = self._retained_product_release_validate(release_root_path)
        self._retained_product_release_host_identity_validate(
            release_root_path=release_root_path
        )
        self._atomic_symlink_replace(
            link_path=self._identity.host_current_source_path,
            target_path=self._identity.host_retained_current_release_path,
        )
        print(
            f"OK: retained Product release {release_name} root-volume link is restored"
        )

    def _retained_product_release_host_identity_validate(
        self,
        *,
        release_root_path: Path,
    ) -> None:
        """Require the active host to match the retained release host identity.

        Args:
            release_root_path: Already byte-validated retained Product release.
        """

        try:
            source_manifest = json.loads(
                (release_root_path / "source-manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product source manifest is unavailable for host validation"
            ) from error
        retained_host_artifact_manifest = (
            source_manifest.get("host_artifact_manifest")
            if isinstance(source_manifest, Mapping)
            else None
        )
        if not isinstance(retained_host_artifact_manifest, Mapping):
            raise DevelopmentEnvironmentError(
                "Retained Product host artifact manifest is malformed"
            )
        if dict(retained_host_artifact_manifest) != self._host_artifact_manifest_get():
            raise DevelopmentEnvironmentError(
                "Retained Product release has another exact host artifact identity"
            )

    def _host_artifact_manifest_get(self) -> dict[str, object]:
        """Return the immutable launch manifest installed on this exact host."""

        try:
            encoded_manifest = HOST_ARTIFACT_MANIFEST_PATH.read_text(encoding="utf-8")
            expected_sha256 = HOST_ARTIFACT_MANIFEST_SHA256_PATH.read_text(
                encoding="utf-8"
            ).strip()
            manifest = host_artifact_manifest_decode(
                encoded_manifest=encoded_manifest,
                expected_sha256=expected_sha256,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Host artifact manifest is unavailable"
            ) from error
        except HostArtifactResolutionError as error:
            raise DevelopmentEnvironmentError(
                f"Host artifact manifest is invalid: {error}"
            ) from error
        return manifest

    def _helm_validate(self, host_artifact_manifest: Mapping[str, object]) -> str:
        """Validate the preinstalled Helm binary against immutable launch input."""

        artifact_by_name_map = host_artifact_manifest.get("artifact_by_name_map")
        helm_artifact = (
            artifact_by_name_map.get("helm")
            if isinstance(artifact_by_name_map, dict)
            else None
        )
        helm_version = (
            helm_artifact.get("version") if isinstance(helm_artifact, dict) else None
        )
        if (
            not isinstance(helm_version, str)
            or re.fullmatch(r"v4\.[0-9]+\.[0-9]+", helm_version) is None
        ):
            raise DevelopmentEnvironmentError(
                "Host artifact manifest has no exact Helm identity"
            )
        if not HELM_BINARY_PATH.is_file():
            raise DevelopmentEnvironmentError(
                "Exact Helm binary was not installed by host bootstrap"
            )
        installed_result = self._runner.run(
            [
                str(HELM_BINARY_PATH),
                "version",
                "--template",
                "{{.Version}}",
            ],
            check=False,
        )
        if (
            installed_result.returncode != 0
            or installed_result.stdout.strip() != helm_version
        ):
            raise DevelopmentEnvironmentError(
                "Installed Helm version differs from immutable launch input"
            )
        return helm_version

    def host_install(self) -> None:
        """Install the source-owned host controller service from the current exact release."""

        self.host_prepare()
        infrastructure_source_path = (
            self._identity.host_control_current_source_path
            / "sources"
            / "workflow-infrastructure"
        )
        runtime_home_path = self._identity.host_state_root_path / "home"
        for path in (self._identity.host_state_root_path, runtime_home_path):
            if path.is_symlink():
                raise DevelopmentEnvironmentError(
                    f"Host controller state path must not be a symbolic link: {path}"
                )
        self._identity.host_state_root_path.mkdir(
            mode=0o750, parents=True, exist_ok=True
        )
        runtime_home_path.mkdir(mode=0o700, exist_ok=True)
        for path in (self._identity.host_state_root_path, runtime_home_path):
            if path.is_symlink():
                raise DevelopmentEnvironmentError(
                    f"Host controller state path must not be a symbolic link: {path}"
                )
        os.chmod(self._identity.host_state_root_path, 0o750)
        os.chmod(runtime_home_path, 0o700)
        service_path = Path(
            "/etc/systemd/system/workflow-control-center-host-controller.service"
        )
        service_text = f"""[Unit]
Description=Workflow Control Center development host lifecycle controller
After=k3s.service network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment={PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT}
Environment=HOME={runtime_home_path}
WorkingDirectory={self._identity.host_state_root_path}
ExecStart={HOST_PYTHON_PATH} -B {infrastructure_source_path}/tool/development_environment_manage.py host-controller --environment-name {self._identity.environment_name}
Restart=always
RestartSec=30
User=root

[Install]
WantedBy=multi-user.target
"""
        service_path.write_text(service_text, encoding="utf-8")
        os.chmod(service_path, 0o644)
        self._runner.run(["systemctl", "daemon-reload"])
        self._runner.run(
            ["systemctl", "enable", "workflow-control-center-host-controller"]
        )
        self._runner.run(
            ["systemctl", "restart", "workflow-control-center-host-controller"]
        )
        print("OK: host lifecycle controller is installed")

    def host_shutdown(self) -> None:
        """Gracefully stop Product workloads and power off the development instance."""

        product_tool_path = self._current_product_tool_path_get()
        if product_tool_path.is_file():
            result = self._runner.run(
                self._current_product_tool_command_list_get("shutdown"),
                check=False,
                should_capture=False,
            )
            if result.returncode != 0:
                self._runner.run(
                    ["k3s", "kubectl", "uncordon", self._host_node_name_get()],
                    check=False,
                )
                raise DevelopmentEnvironmentError(
                    "Product graceful shutdown failed; node was uncordoned"
                )
        else:
            self._runner.run(["systemctl", "stop", "k3s"], check=False)
        self._runner.run(["systemctl", "poweroff"], should_capture=False)

    def lifecycle_acceptance(self) -> None:
        """Exercise the real renewable stop lease with bounded acceptance timings.

        This disruptive operator-only check leaves the ordinary development controller policy
        unchanged. It temporarily stops that controller, creates and renews the real
        EventBridge Scheduler lease, proves that the original deadline is superseded,
        then proves fail-safe EC2 stop and schedule auto-deletion before restoring the
        ordinary two-hour lease and complete host readiness.
        """

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        self._stack_drift_validate(self._identity.compute_stack_name)
        instance_id = self._instance_id_get()
        if self._instance_state_get(instance_id) != "running":
            raise DevelopmentEnvironmentError(
                "Lifecycle acceptance requires the development instance to be running"
            )
        host_status_payload = self._host_status_payload_get(
            retained_volume_id=self._stack_output_by_name_map_get(
                self._identity.compute_stack_name
            )["RetainedVolumeId"]
        )
        if host_status_payload.get("wcc_activity") != "idle":
            raise DevelopmentEnvironmentError(
                "Lifecycle acceptance requires an idle Product"
            )

        is_environment_restored = False
        try:
            self._ssm_shell_run(
                [
                    "sudo systemctl stop workflow-control-center-host-controller",
                    (
                        'test "$(systemctl is-active '
                        'workflow-control-center-host-controller || true)" = inactive'
                    ),
                ]
            )
            t_initial_lease = self._clock.now()
            self._stop_lease_upsert(
                lease_duration=LIFECYCLE_ACCEPTANCE_INITIAL_LEASE_DURATION
            )
            initial_expression = self._stop_lease_payload_get().get(
                "schedule_expression"
            )
            self._clock.sleep(LIFECYCLE_ACCEPTANCE_RENEW_DELAY_SECONDS)

            t_renewed_lease = self._clock.now()
            self._stop_lease_upsert(
                lease_duration=LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION
            )
            renewed_expression = self._stop_lease_payload_get().get(
                "schedule_expression"
            )
            if initial_expression == renewed_expression:
                raise DevelopmentEnvironmentError(
                    "Lifecycle acceptance lease renewal did not change its deadline"
                )

            self._wait_until(t_initial_lease + LIFECYCLE_ACCEPTANCE_RENEWAL_PROOF_DELAY)
            if self._instance_state_get(instance_id) != "running":
                raise DevelopmentEnvironmentError(
                    "Lifecycle acceptance instance stopped at the superseded deadline"
                )
            self._instance_stopped_wait(
                instance_id=instance_id,
                t_deadline=(
                    t_renewed_lease
                    + LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION
                    + LIFECYCLE_ACCEPTANCE_STOP_GRACE
                ),
            )
            self._stop_lease_absence_wait(
                t_deadline=self._clock.now() + LIFECYCLE_ACCEPTANCE_STOP_GRACE
            )
            self.start()
            self._product_recovery_acceptance_run()
            is_environment_restored = True
        finally:
            if not is_environment_restored:
                self._lifecycle_acceptance_environment_restore(instance_id)
        print(
            "OK: real AWS lifecycle acceptance renewed the lease, "
            "failed safe, and restored the development environment"
        )

    def restore(self, snapshot_id: str) -> None:
        """Replace the retained volume from one exact snapshot and run recovery acceptance.

        Args:
            snapshot_id: Exact EBS snapshot identifier.
        """

        if not snapshot_id.startswith("snap-"):
            raise DevelopmentEnvironmentError("Snapshot ID must start with snap-")
        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        replacement_parameter_by_name_map = (
            self._replacement_parameter_by_name_map_get()
        )
        (
            source_volume_id,
            restore_parameter_by_name_map,
        ) = self._retained_volume_restore_plan_get(snapshot_id=snapshot_id)
        replacement_parameter_by_name_map.update(restore_parameter_by_name_map)
        self._stack_drift_validate(self._identity.compute_stack_name)
        self._retired_retained_volume_cleanup(current_volume_id=source_volume_id)
        self.stop(should_validate_drift=False)
        self._replacement_stack_apply(
            parameter_by_name_map=replacement_parameter_by_name_map,
            allow_retained_volume_transition=True,
        )
        self._retained_volume_snapshot_restore_validate(
            snapshot_id=snapshot_id,
            source_volume_id=source_volume_id,
        )
        self._retained_volume_backup_disable(volume_id=source_volume_id)
        self.start(should_publish_infrastructure_source=True)
        self._product_recovery_begin()
        self._replacement_guard_disable()
        self._product_recovery_finish()
        print(f"OK: retained state restored and accepted from {snapshot_id}")

    def replace(self) -> None:
        """Replace the EC2 instance while preserving the exact retained volume."""

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        replacement_parameter_by_name_map = (
            self._replacement_parameter_by_name_map_get()
        )
        replacement_slot = replacement_parameter_by_name_map["InstanceSlot"]
        self._stack_drift_validate(self._identity.compute_stack_name)
        self.stop(should_validate_drift=False)
        self._replacement_stack_apply(
            parameter_by_name_map=replacement_parameter_by_name_map
        )
        self.start(should_publish_infrastructure_source=True)
        self._product_recovery_begin()
        self._replacement_guard_disable()
        self._product_recovery_finish()
        print(
            f"OK: replacement instance in slot {replacement_slot} accepted the retained volume"
        )

    def _replacement_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return explicit slot and fail-safe schedule parameters for replacement.

        Returns:
            Parameter overrides that deliberately replace the current instance.
        """

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        try:
            current_slot = output_by_name_map["InstanceSlot"]
        except KeyError as error:
            raise DevelopmentEnvironmentError(
                "Compute stack replacement outputs are incomplete"
            ) from error
        if current_slot not in {"a", "b"}:
            raise DevelopmentEnvironmentError(
                "Compute stack replacement outputs are malformed"
            )
        parameter_by_name_map = self._replacement_guard_parameter_by_name_map_get()
        parameter_by_name_map["InstanceSlot"] = "b" if current_slot == "a" else "a"
        try:
            latest_launch_template_version = output_by_name_map[
                "LatestLaunchTemplateVersion"
            ]
        except KeyError as error:
            raise DevelopmentEnvironmentError(
                "Compute stack launch-template output is missing"
            ) from error
        if (
            not isinstance(latest_launch_template_version, str)
            or not latest_launch_template_version.isdigit()
        ):
            raise DevelopmentEnvironmentError(
                "Compute stack launch-template output is malformed"
            )
        parameter_by_name_map["InstanceLaunchTemplateVersion"] = (
            latest_launch_template_version
        )
        return parameter_by_name_map

    def _replacement_guard_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return an enabled two-hour CloudFormation replacement guard."""

        t_stop = self._clock.now() + LEASE_DURATION
        return {
            "ReplacementGuardScheduleExpression": (
                f"at({t_stop.strftime('%Y-%m-%dT%H:%M:%S')})"
            ),
            "ReplacementGuardScheduleState": "ENABLED",
        }

    def _replacement_guard_disable(self) -> None:
        """Disable the CloudFormation guard after the renewable lease is proven."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        if "ReplacementGuardScheduleName" not in output_by_name_map:
            raise DevelopmentEnvironmentError(
                "Compute stack replacement guard output is missing"
            )
        self._stack_apply(
            stack_name=self._identity.compute_stack_name,
            template_path=self._project_root_path
            / "cloudformation/workflow-control-center-development-compute.yaml",
            parameter_by_name_map={"ReplacementGuardScheduleState": "DISABLED"},
            must_preserve_resource=False,
            protected_identity_logical_id_set=COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET,
        )

    def _replacement_stack_apply(
        self,
        *,
        parameter_by_name_map: dict[str, str],
        allow_retained_volume_transition: bool = False,
    ) -> None:
        """Apply one explicit replacement after proving the retained volume detached.

        Args:
            parameter_by_name_map: Exact replacement and optional restore parameters.
            allow_retained_volume_transition: Whether this operation explicitly replaces
                the retained volume from a caller-selected snapshot.
        """

        if (
            parameter_by_name_map.get("ReplacementGuardScheduleState") != "ENABLED"
            or "ReplacementGuardScheduleExpression" not in parameter_by_name_map
        ):
            raise DevelopmentEnvironmentError(
                "Explicit replacement requires an enabled CloudFormation guard"
            )
        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        renewable_lease_created = "StopLeaseTargetArn" in output_by_name_map
        if renewable_lease_created:
            self._stop_lease_upsert()
        try:
            self._retained_volume_detach_for_replacement()
        except Exception:
            if renewable_lease_created:
                self._stop_lease_delete()
            raise
        try:
            self._stack_apply(
                stack_name=self._identity.compute_stack_name,
                template_path=self._project_root_path
                / "cloudformation/workflow-control-center-development-compute.yaml",
                parameter_by_name_map=parameter_by_name_map,
                must_preserve_resource=False,
                protected_identity_logical_id_set=(
                    ()
                    if allow_retained_volume_transition
                    else COMPUTE_RETAINED_VOLUME_LOGICAL_ID_SET
                ),
            )
        except Exception as error:
            try:
                self._retained_volume_attachment_ensure()
            except Exception as recovery_error:
                raise DevelopmentEnvironmentError(
                    "Compute replacement failed and retained-volume attachment recovery failed: "
                    f"{recovery_error}"
                ) from error
            if renewable_lease_created:
                self._stop_lease_delete()
            raise
        self._retained_volume_attachment_validate()
        self._instance_launch_template_version_validate()

    def ssh(self, ssh_argument_list: list[str]) -> int:
        """Run one SSH client command through an ephemeral SSH-over-SSM session.

        Args:
            ssh_argument_list: Additional OpenSSH arguments.

        Returns:
            SSH process status.
        """

        self._local_operator_context_validate()
        self._instance_online_wait()
        with self._ssh_control_session() as ssh_control_path:
            command_list = [
                "ssh",
                "-S",
                str(ssh_control_path),
                self._identity.instance_name,
            ]
            command_list.extend(ssh_argument_list)
            result = self._runner.run(command_list, check=False, should_capture=False)
            return result.returncode

    def start(
        self,
        *,
        should_publish_infrastructure_source: bool = False,
    ) -> None:
        """Create the external stop lease before starting and verify host readiness.

        Args:
            should_publish_infrastructure_source: Whether a newly replaced host needs
                the already validated exact controller source installed before proof.
        """

        self._host_start_foundation(
            should_validate_source=should_publish_infrastructure_source,
        )
        if should_publish_infrastructure_source:
            self._infrastructure_source_publish()
        self._host_readiness_wait()
        instance_id = self._instance_id_get()
        print(f"OK: development instance {instance_id} is ready")

    def _host_start_foundation(
        self,
        *,
        should_validate_source: bool,
    ) -> None:
        """Start the host through SSM without changing its installed controller.

        Args:
            should_validate_source: Whether a following exact-source publication
                requires the local infrastructure checkout to be validated.
        """

        self._local_operator_context_validate()
        self._stack_drift_validate(self._identity.compute_stack_name)
        if should_validate_source:
            self._source_repository_validate(
                self._project_root_path, "workflow-infrastructure"
            )
        instance_id = self._instance_id_get()
        self._stop_lease_upsert()
        state = self._instance_state_get(instance_id)
        if state == "stopped":
            self._aws_run(["ec2", "start-instances", "--instance-ids", instance_id])
        elif state not in {"pending", "running"}:
            raise DevelopmentEnvironmentError(
                f"Instance cannot start from state {state}"
            )
        self._instance_online_wait()
        self._ssm_shell_result_get(
            ["cloud-init status --wait"],
            timeout_seconds=HOST_READY_TIMEOUT_SECONDS,
        )

    def status(self) -> None:
        """Print safe infrastructure, access, lease, storage, and release state."""

        self._local_operator_context_validate()
        data_stack = self._stack_payload_get(
            self._identity.data_plane_stack_name, is_required=False
        )
        compute_stack = self._stack_payload_get(
            self._identity.compute_stack_name, is_required=False
        )
        payload: dict[str, object] = {
            "account_id": AWS_ACCOUNT_ID,
            "compute_stack_status": compute_stack.get("StackStatus", "absent"),
            "data_plane_stack_status": data_stack.get("StackStatus", "absent"),
            "region": AWS_REGION,
        }
        if compute_stack:
            output_by_name_map = self._stack_output_by_name_map_get(
                self._identity.compute_stack_name
            )
            instance_id = output_by_name_map["InstanceId"]
            instance_state = self._instance_state_get(instance_id)
            ssm_ping_status = self._instance_ssm_ping_status_get(instance_id)
            payload.update(
                {
                    "active_ssm_session_count": self._active_session_count_get(
                        instance_id
                    ),
                    "instance_id": instance_id,
                    "instance_state": instance_state,
                    "instance_type": output_by_name_map["InstanceType"],
                    "latest_retained_snapshot_id": (
                        self._latest_snapshot_id_get(
                            output_by_name_map["RetainedVolumeId"]
                        )
                        or output_by_name_map.get("RetainedVolumeSourceSnapshotId", "")
                    ),
                    "retained_volume_id": output_by_name_map["RetainedVolumeId"],
                    "retained_volume_slot": output_by_name_map.get(
                        "RetainedVolumeSlot", "base"
                    ),
                    "retained_volume_source_snapshot_id": output_by_name_map.get(
                        "RetainedVolumeSourceSnapshotId", ""
                    ),
                    "retained_snapshot_policy": (
                        self._retained_snapshot_policy_status_get()
                    ),
                    "ssm_ping_status": ssm_ping_status,
                    "stop_lease": self._stop_lease_payload_get(),
                }
            )
            host_status_payload = self._host_status_unavailable_payload_get()
            if instance_state == "running" and ssm_ping_status == "Online":
                try:
                    host_status_payload = self._host_status_payload_get(
                        retained_volume_id=output_by_name_map["RetainedVolumeId"]
                    )
                except DevelopmentEnvironmentError:
                    pass
            payload.update(host_status_payload)
        print(json.dumps(payload, indent=2, sort_keys=True))

    def stop(self, *, should_validate_drift: bool = True) -> None:
        """Run graceful remote shutdown, prove EC2 stop, and remove the pending lease.

        Args:
            should_validate_drift: Whether this lifecycle boundary must run compute
                drift detection; controlled replacement already does it once.
        """

        self._local_operator_context_validate()
        if should_validate_drift:
            self._stack_drift_validate(self._identity.compute_stack_name)
        instance_id = self._instance_id_get()
        state = self._instance_state_get(instance_id)
        if state == "stopped":
            self._stop_lease_delete()
            print(f"OK: development instance {instance_id} is already stopped")
            return
        if state != "running":
            raise DevelopmentEnvironmentError(
                f"Instance cannot stop gracefully from state {state}"
            )
        command_id = self._ssm_command_start(
            [
                (
                    f"if [ -f {self._identity.host_control_current_source_path}/sources/workflow-infrastructure/"
                    "tool/development_environment_manage.py ]; then "
                    f"sudo env {PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT} python3.14 -B "
                    f"{self._identity.host_control_current_source_path}/sources/workflow-infrastructure/"
                    "tool/development_environment_manage.py host-shutdown "
                    f"--environment-name {self._identity.environment_name}; "
                    "else sudo systemctl stop k3s || true; sudo systemctl poweroff; fi"
                )
            ]
        )
        print(f"OK: graceful shutdown command {command_id} started")
        self._aws_run(
            ["ec2", "wait", "instance-stopped", "--instance-ids", instance_id]
        )
        self._stop_lease_delete()
        print(f"OK: development instance {instance_id} stopped")

    def _active_session_count_get(self, instance_id: str) -> int:
        payload = self._aws_json_get(
            [
                "ssm",
                "describe-sessions",
                "--state",
                "Active",
                "--filters",
                f"key=Target,value={instance_id}",
            ]
        )
        session_list = payload.get("Sessions", [])
        if not isinstance(session_list, list):
            raise DevelopmentEnvironmentError(
                "Session Manager returned malformed Sessions"
            )
        return len(session_list)

    def _aws_json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        result = self._aws_run([*aws_argument_list, "--output", "json"])
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                f"AWS {aws_argument_list[0]} returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(
                f"AWS {aws_argument_list[0]} returned unexpected JSON"
            )
        return payload

    def _aws_run(
        self,
        aws_argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_list = ["aws", *aws_argument_list, "--region", AWS_REGION]
        if not self._is_host:
            command_list.extend(["--profile", AWS_PROFILE])
        return self._runner.run(command_list, check=check)

    def _host_active_session_count_get(self, instance_id: str) -> int:
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "describe-sessions",
                "--region",
                AWS_REGION,
                "--state",
                "Active",
                "--filters",
                f"key=Target,value={instance_id}",
                "--output",
                "json",
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Host Session Manager response is invalid"
            ) from error
        session_list = payload.get("Sessions", []) if isinstance(payload, dict) else []
        if not isinstance(session_list, list):
            raise DevelopmentEnvironmentError(
                "Host Session Manager response is malformed"
            )
        return len(session_list)

    def _host_session_state_is_busy(self, instance_id: str) -> bool:
        """Fail closed when Session Manager cannot prove that no session exists."""

        try:
            return self._host_active_session_count_get(instance_id) > 0
        except DevelopmentEnvironmentError:
            return True

    def _host_node_name_get(self) -> str:
        result = self._runner.run(
            [
                "k3s",
                "kubectl",
                "get",
                "node",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ]
        )
        node_name = result.stdout.strip()
        if not node_name:
            raise DevelopmentEnvironmentError("Kubernetes node name is empty")
        return node_name

    def _host_product_activity_get(self) -> str:
        try:
            if self._host_product_recovery_status_local_get() == "pending":
                return "busy"
        except DevelopmentEnvironmentError:
            return "busy"
        product_tool_path = self._current_product_tool_path_get()
        if not product_tool_path.is_file():
            return "busy"
        result = self._runner.run(
            self._current_product_tool_command_list_get("activity"),
            check=False,
        )
        if result.returncode != 0:
            return "busy"
        try:
            observation = json.loads(result.stdout)
            status = observation["status"]
            reason_key_list = observation["reason_key_list"]
            t_observed = datetime.fromisoformat(observation["t_observed"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return "busy"
        if (
            status not in {"busy", "idle"}
            or not isinstance(reason_key_list, list)
            or any(
                not isinstance(reason_key, str)
                or reason_key
                not in {
                    "controller_unavailable",
                    "persisted_work",
                    "probe_unavailable",
                    "recovery",
                }
                for reason_key in reason_key_list
            )
            or t_observed.utcoffset() != timedelta(0)
            or (status == "idle" and reason_key_list)
            or (status == "busy" and not reason_key_list)
        ):
            return "busy"
        return status

    def _cost_review_record(self) -> None:
        instance_hour_price = self._price_usd_get(
            {
                "capacitystatus": "Used",
                "instanceType": "m7g.xlarge",
                "operatingSystem": "Linux",
                "preInstalledSw": "NA",
                "regionCode": AWS_REGION,
                "tenancy": "Shared",
            },
            unit="Hrs",
            usage_type="",
        )
        gp3_gib_month_price = self._price_usd_get(
            {
                "regionCode": AWS_REGION,
                "volumeApiName": "gp3",
            },
            unit="GB-Mo",
            usage_type="EBS:VolumeUsage.gp3",
        )
        snapshot_gib_month_price = self._price_usd_get(
            {
                "productFamily": "Storage Snapshot",
                "regionCode": AWS_REGION,
            },
            unit="GB-Mo",
            usage_type="EBS:SnapshotUsage",
        )
        active_hour_count_monthly = Decimal(80)
        gp3_gib_count_max = Decimal(260)
        snapshot_retention_count = Decimal(7)
        snapshot_source_volume_gib_count_max = Decimal(80)
        snapshot_stored_gib_count_max = (
            snapshot_retention_count * snapshot_source_volume_gib_count_max
        )
        usage_price_meter_spec_by_service_name_map = {
            "api_gateway": {
                "rest_api_request": (
                    "AmazonApiGateway",
                    {
                        "operation": "ApiGatewayRequest",
                        "regionCode": AWS_REGION,
                        "usagetype": "USE1-ApiGatewayRequest",
                    },
                    "USE1-ApiGatewayRequest",
                ),
            },
            "athena": {
                "data_scanned": (
                    "AmazonAthena",
                    {
                        "regionCode": AWS_REGION,
                        "usagetype": "USE1-DataScannedInTB",
                    },
                    "USE1-DataScannedInTB",
                ),
            },
            "data_transfer": {
                "internet_outbound": (
                    "AWSDataTransfer",
                    {
                        "fromRegionCode": AWS_REGION,
                        "toLocation": "External",
                        "transferType": "AWS Outbound",
                        "usagetype": "DataTransfer-Out-Bytes",
                    },
                    "DataTransfer-Out-Bytes",
                ),
            },
            "glue": {
                "catalog_request": (
                    "AWSGlue",
                    {
                        "regionCode": AWS_REGION,
                        "usagetype": "USE1-Catalog-Request",
                    },
                    "USE1-Catalog-Request",
                ),
                "catalog_storage": (
                    "AWSGlue",
                    {
                        "regionCode": AWS_REGION,
                        "usagetype": "USE1-Catalog-Storage",
                    },
                    "USE1-Catalog-Storage",
                ),
            },
            "kms": {
                "customer_managed_key": (
                    "awskms",
                    {
                        "regionCode": AWS_REGION,
                        "usagetype": "us-east-1-KMS-Keys",
                    },
                    "us-east-1-KMS-Keys",
                ),
                "request": (
                    "awskms",
                    {
                        "regionCode": AWS_REGION,
                        "usagetype": "us-east-1-KMS-Requests",
                    },
                    "us-east-1-KMS-Requests",
                ),
            },
            "s3": {
                "standard_storage": (
                    "AmazonS3",
                    {
                        "regionCode": AWS_REGION,
                        "storageClass": "General Purpose",
                        "usagetype": "TimedStorage-ByteHrs",
                        "volumeType": "Standard",
                    },
                    "TimedStorage-ByteHrs",
                ),
                "tier_1_request": (
                    "AmazonS3",
                    {
                        "group": "S3-API-Tier1",
                        "regionCode": AWS_REGION,
                        "usagetype": "Requests-Tier1",
                    },
                    "Requests-Tier1",
                ),
                "tier_2_request": (
                    "AmazonS3",
                    {
                        "group": "S3-API-Tier2",
                        "regionCode": AWS_REGION,
                        "usagetype": "Requests-Tier2",
                    },
                    "Requests-Tier2",
                ),
            },
        }
        usage_based_service_by_name_map: dict[str, dict[str, object]] = {}
        price_dimension_list_by_service_meter_map: dict[
            tuple[str, str], list[dict[str, str]]
        ] = {}
        for (
            service_name,
            price_meter_spec_by_name_map,
        ) in usage_price_meter_spec_by_service_name_map.items():
            price_meter_by_name_map = {}
            for meter_name, (
                service_code,
                filter_by_field_map,
                usage_type,
            ) in price_meter_spec_by_name_map.items():
                price_dimension_list = self._price_dimension_list_get(
                    service_code=service_code,
                    filter_by_field_map=filter_by_field_map,
                    usage_type=usage_type,
                )
                price_dimension_list_by_service_meter_map[
                    (service_name, meter_name)
                ] = price_dimension_list
                price_meter_by_name_map[meter_name] = {
                    "price_dimension_list": price_dimension_list,
                    "service_code": service_code,
                    "usage_type": usage_type,
                }
            usage_based_service_by_name_map[service_name] = {
                "architecture_delta_monthly_usd": "0.00",
                "assumption": (
                    "Existing approved usage quantity is unchanged; "
                    "architecture delta quantity is zero."
                ),
                "price_meter_by_name_map": price_meter_by_name_map,
            }
        kms_key_price_dimension_list = price_dimension_list_by_service_meter_map[
            ("kms", "customer_managed_key")
        ]
        kms_key_price_set = {
            Decimal(price_dimension["price_per_unit_usd"])
            for price_dimension in kms_key_price_dimension_list
            if price_dimension["unit"] == "Keys"
        }
        if len(kms_key_price_set) != 1:
            raise DevelopmentEnvironmentError(
                "AWS Pricing did not return one KMS key price"
            )
        kms_key_monthly_price = next(iter(kms_key_price_set))
        kms_customer_managed_key_count = Decimal(1)
        estimated_compute_monthly = instance_hour_price * active_hour_count_monthly
        estimated_gp3_monthly_max = gp3_gib_month_price * gp3_gib_count_max
        estimated_snapshot_monthly_max = (
            snapshot_gib_month_price * snapshot_stored_gib_count_max
        )
        estimated_kms_key_monthly = (
            kms_key_monthly_price * kms_customer_managed_key_count
        )
        retained_rollback_monthly_delta_max = gp3_gib_month_price * Decimal(80)
        review_payload = {
            "architecture_delta_monthly_usd": {
                "bounded_retained_rollback_volume_max": str(
                    retained_rollback_monthly_delta_max.quantize(Decimal("0.01"))
                ),
                "total_max": str(
                    retained_rollback_monthly_delta_max.quantize(Decimal("0.01"))
                ),
            },
            "architecture_checkpoint": "approved-2026-07-28",
            "assumption": {
                "active_hour_count_monthly": int(active_hour_count_monthly),
                "gp3_gib_count_max": int(gp3_gib_count_max),
                "kms_customer_managed_key_count": int(kms_customer_managed_key_count),
                "snapshot_retention_count": int(snapshot_retention_count),
                "snapshot_source_volume_gib_count_max": int(
                    snapshot_source_volume_gib_count_max
                ),
                "snapshot_stored_gib_count_max": int(snapshot_stored_gib_count_max),
            },
            "estimated_monthly_usd": {
                "compute": str(estimated_compute_monthly.quantize(Decimal("0.01"))),
                "gp3_max": str(estimated_gp3_monthly_max.quantize(Decimal("0.01"))),
                "kms_customer_managed_key": str(
                    estimated_kms_key_monthly.quantize(Decimal("0.01"))
                ),
                "snapshot_max": str(
                    estimated_snapshot_monthly_max.quantize(Decimal("0.01"))
                ),
                "total_fixed_max": str(
                    (
                        estimated_compute_monthly
                        + estimated_gp3_monthly_max
                        + estimated_kms_key_monthly
                        + estimated_snapshot_monthly_max
                    ).quantize(Decimal("0.01"))
                ),
            },
            "price_usd": {
                "gp3_gib_month": str(gp3_gib_month_price),
                "kms_customer_managed_key_month": str(kms_key_monthly_price),
                "m7g_xlarge_hour": str(instance_hour_price),
                "snapshot_gib_month": str(snapshot_gib_month_price),
            },
            "t_calculate": self._clock.now().isoformat().replace("+00:00", "Z"),
            "usage_based_service_by_name_map": usage_based_service_by_name_map,
        }
        review_path = self._project_root_path / ".local" / "cost-review.json"
        review_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        review_path.write_text(
            json.dumps(review_payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.chmod(review_path, 0o600)
        print(json.dumps(review_payload, indent=2, sort_keys=True))

    def _infrastructure_source_publish(
        self,
        *,
        should_install_host_controller: bool = True,
    ) -> None:
        """Publish one exact control source and optionally install its controller.

        Args:
            should_install_host_controller: Whether the target host already owns
                the immutable host-artifact manifest required by ``host-install``.
        """

        release_name = self._clock.now().strftime("%Y%m%d%H%M%S%f")
        with self._ssh_control_session() as ssh_control_path:
            self._source_archive_publish(
                repository_name="workflow-infrastructure",
                repository_path=self._project_root_path,
                release_name=release_name,
                remote_release_root_path=self._identity.host_control_release_root_path,
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "install",
                    "-d",
                    "-m",
                    "0755",
                    str(
                        self._identity.host_control_release_root_path
                        / release_name
                        / "sources"
                    ),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "ln",
                    "-sfn",
                    str(self._identity.host_control_release_root_path / release_name),
                    str(self._identity.host_control_current_source_path),
                ],
                ssh_control_path=ssh_control_path,
            )
            if should_install_host_controller:
                self._ssh_run(
                    [
                        "sudo",
                        "env",
                        PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                        "python3.14",
                        "-B",
                        str(
                            self._identity.host_control_current_source_path
                            / "sources"
                            / "workflow-infrastructure"
                            / "tool"
                            / "development_environment_manage.py"
                        ),
                        "host-install",
                        "--environment-name",
                        self._identity.environment_name,
                    ],
                    ssh_control_path=ssh_control_path,
                )

    def _host_artifact_resolution_get(
        self,
        *,
        compute_stack_exists: bool,
    ) -> HostArtifactResolution:
        """Resolve and persist one exact host bootstrap graph before compute apply.

        Args:
            compute_stack_exists: Whether the current environment already owns compute.

        Returns:
            Verified architecture-specific artifact identities.
        """

        architecture = "arm64"
        if compute_stack_exists:
            architecture = self._stack_parameter_by_name_map_get(
                self._identity.compute_stack_name
            ).get("ComputeArchitecture", architecture)
        try:
            resolution = HostArtifactResolver(
                cache_root_path=self._project_root_path
                / ".local"
                / "host-artifact-cache",
                runner=self._runner,
            ).resolve(architecture)
        except HostArtifactResolutionError as error:
            raise DevelopmentEnvironmentError(
                f"Host artifact resolution failed: {error}"
            ) from error
        manifest_payload = {
            **resolution.manifest_payload_get(),
            "manifest_sha256": resolution.manifest_sha256_get(),
        }
        manifest_path = (
            self._project_root_path
            / ".local"
            / f"host-artifact-resolution-{self._identity.environment_name}.json"
        )
        manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)
        return resolution

    def _host_artifact_manifest_payload_get(self) -> dict[str, object]:
        """Load exact host bootstrap provenance retained by the compute stack."""

        parameter_by_name_map = self._stack_parameter_by_name_map_get(
            self._identity.compute_stack_name
        )
        encoded_manifest = parameter_by_name_map.get(
            "HostArtifactManifestGzipBase64",
            "",
        )
        expected_sha256 = parameter_by_name_map.get(
            "HostArtifactManifestSha256",
            "",
        )
        try:
            payload = host_artifact_manifest_decode(
                encoded_manifest=encoded_manifest,
                expected_sha256=expected_sha256,
            )
        except HostArtifactResolutionError as error:
            raise DevelopmentEnvironmentError(
                f"Compute stack host artifact provenance is invalid: {error}"
            ) from error
        if payload.get("architecture") != parameter_by_name_map.get(
            "ComputeArchitecture"
        ):
            raise DevelopmentEnvironmentError(
                "Compute stack host artifact architecture is inconsistent"
            )
        return payload

    def _instance_id_get(self) -> str:
        return self._stack_output_by_name_map_get(self._identity.compute_stack_name)[
            "InstanceId"
        ]

    def _instance_metadata_get(self, path: str) -> str:
        token_result = self._runner.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--request",
                "PUT",
                "--header",
                "X-aws-ec2-metadata-token-ttl-seconds: 21600",
                "http://169.254.169.254/latest/api/token",
            ]
        )
        result = self._runner.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--header",
                f"X-aws-ec2-metadata-token: {token_result.stdout.strip()}",
                f"http://169.254.169.254/latest/meta-data/{path}",
            ]
        )
        value = result.stdout.strip()
        if not value:
            raise DevelopmentEnvironmentError(f"Instance metadata {path} is empty")
        return value

    def _instance_online_wait(self) -> None:
        instance_id = self._instance_id_get()
        self._aws_run(
            ["ec2", "wait", "instance-status-ok", "--instance-ids", instance_id]
        )
        t_deadline = self._clock.monotonic() + SSM_ONLINE_TIMEOUT_SECONDS
        while self._clock.monotonic() < t_deadline:
            if self._instance_ssm_ping_status_get(instance_id) == "Online":
                return
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(
            f"Instance {instance_id} did not become SSM Online"
        )

    def _instance_ssm_ping_status_get(self, instance_id: str) -> str:
        """Return the current SSM managed-node ping status.

        Args:
            instance_id: Exact target EC2 instance identifier.

        Returns:
            SSM ping status or ``Unavailable`` when the node is not registered.
        """

        payload = self._aws_json_get(
            [
                "ssm",
                "describe-instance-information",
                "--filters",
                f"Key=InstanceIds,Values={instance_id}",
            ]
        )
        information_list = payload.get("InstanceInformationList", [])
        if not isinstance(information_list, list):
            raise DevelopmentEnvironmentError(
                "SSM instance information response is malformed"
            )
        if not information_list:
            return "Unavailable"
        if len(information_list) != 1 or not isinstance(information_list[0], dict):
            raise DevelopmentEnvironmentError(
                "SSM instance information response is malformed"
            )
        ping_status = information_list[0].get("PingStatus")
        if ping_status not in {"ConnectionLost", "Inactive", "Online"}:
            raise DevelopmentEnvironmentError("SSM instance ping status is malformed")
        return ping_status

    def _host_readiness_wait(self) -> None:
        """Prove retained storage, k3s, node, and lifecycle-controller readiness."""

        retained_volume_id = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )["RetainedVolumeId"]
        t_deadline = self._clock.monotonic() + HOST_READY_TIMEOUT_SECONDS
        host_status_payload = self._host_status_unavailable_payload_get()
        while self._clock.monotonic() < t_deadline:
            try:
                host_status_payload = self._host_status_payload_get(
                    retained_volume_id=retained_volume_id
                )
            except DevelopmentEnvironmentError:
                self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
                continue
            foundation_is_ready = (
                host_status_payload["retained_mount_status"] == "ready"
                and host_status_payload["k3s_service_status"] == "active"
                and host_status_payload["kubernetes_node_status"] == "ready"
            )
            controller_is_ready = (
                host_status_payload["host_controller_unit_status"] == "loaded"
                and host_status_payload["host_controller_service_status"] == "active"
            )
            if foundation_is_ready and controller_is_ready:
                return
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        safe_status_text = json.dumps(host_status_payload, sort_keys=True)
        raise DevelopmentEnvironmentError(
            "Development host did not become ready within "
            f"{HOST_READY_TIMEOUT_SECONDS} seconds; last safe status: "
            f"{safe_status_text}"
        )

    def _host_status_payload_get(self, *, retained_volume_id: str) -> dict[str, str]:
        """Inspect safe host state through one bounded SSM Run Command.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the Product root.

        Returns:
            Safe normalized host status fields.
        """

        self._retained_volume_id_validate(retained_volume_id)
        result_payload = self._ssm_shell_result_get(
            [
                shlex.join(
                    [
                        "env",
                        PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                        "python3.14",
                        "-B",
                        str(
                            self._identity.host_control_current_source_path
                            / "sources"
                            / "workflow-infrastructure"
                            / "tool"
                            / "development_environment_manage.py"
                        ),
                        "host-status",
                        "--environment-name",
                        self._identity.environment_name,
                        "--retained-volume-id",
                        retained_volume_id,
                    ]
                )
            ],
            timeout_seconds=HOST_STATUS_COMMAND_TIMEOUT_SECONDS,
        )
        output_text = result_payload.get("StandardOutputContent")
        if not isinstance(output_text, str):
            raise DevelopmentEnvironmentError(
                "Development host status output is malformed"
            )
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Development host status output is invalid"
            ) from error
        return self._host_status_payload_validate(payload)

    def _host_status_local_payload_get(
        self, *, retained_volume_id: str
    ) -> dict[str, str]:
        """Collect safe state directly on the development host.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the Product root.

        Returns:
            Safe host status fields.
        """

        self._retained_volume_id_validate(retained_volume_id)
        k3s_service_status = self._host_service_status_get("k3s")
        host_controller_service_status = self._host_service_status_get(
            "workflow-control-center-host-controller"
        )
        host_controller_unit_result = self._runner.run(
            [
                "systemctl",
                "show",
                "--property=LoadState",
                "--value",
                "workflow-control-center-host-controller",
            ],
            check=False,
        )
        host_controller_unit_status = host_controller_unit_result.stdout.strip()
        if host_controller_unit_status not in {"loaded", "masked", "not-found"}:
            host_controller_unit_status = "unknown"

        return {
            "current_release": self._host_current_release_get(),
            "host_controller_service_status": host_controller_service_status,
            "host_controller_unit_status": host_controller_unit_status,
            "host_status_probe": "ok",
            "k3s_service_status": k3s_service_status,
            "kubernetes_node_status": self._host_kubernetes_node_status_get(
                k3s_service_status=k3s_service_status
            ),
            "retained_mount_status": self._host_retained_mount_status_get(
                retained_volume_id=retained_volume_id
            ),
            "wcc_activity": self._host_product_activity_get(),
        }

    def _host_current_release_get(self) -> str:
        """Return the safe exact retained release name or its invalid state."""

        if not self._identity.host_retained_current_release_path.is_symlink():
            return ""
        try:
            current_release_path = (
                self._identity.host_retained_current_release_path.resolve(strict=True)
            )
        except OSError:
            return "invalid"
        release_name = current_release_path.name
        if (
            current_release_path.parent == self._identity.host_release_root_path
            and len(release_name) == 20
            and release_name.isdigit()
        ):
            return release_name
        return "invalid"

    def _host_kubernetes_node_status_get(self, *, k3s_service_status: str) -> str:
        """Return normalized readiness across every node in the local k3s cluster.

        Args:
            k3s_service_status: Already normalized k3s systemd state.

        Returns:
            ``ready``, ``not-ready``, or ``unavailable``.
        """

        if k3s_service_status != "active":
            return "unavailable"
        node_result = self._runner.run(
            [
                "k3s",
                "kubectl",
                "get",
                "nodes",
                "--output",
                "json",
                "--request-timeout=10s",
            ],
            check=False,
        )
        if node_result.returncode != 0:
            return "unavailable"
        try:
            node_payload = json.loads(node_result.stdout)
            node_list = node_payload["items"]
            readiness_list = [
                next(
                    condition["status"]
                    for condition in node["status"]["conditions"]
                    if condition["type"] == "Ready"
                )
                for node in node_list
            ]
        except (
            KeyError,
            StopIteration,
            TypeError,
            json.JSONDecodeError,
        ):
            return "unavailable"
        if readiness_list and all(status == "True" for status in readiness_list):
            return "ready"
        return "not-ready"

    def _host_retained_mount_status_get(self, *, retained_volume_id: str) -> str:
        """Prove the exact retained EBS device is the Product XFS mount.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the mount.

        Returns:
            ``ready``, ``unmounted``, or ``wrong-device``.
        """

        retained_device_path = HOST_EBS_DEVICE_BY_ID_ROOT_PATH / (
            "nvme-Amazon_Elastic_Block_Store_" + retained_volume_id.replace("-", "")
        )
        findmnt_result = self._runner.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "SOURCE,FSTYPE,TARGET",
                "--target",
                str(self._identity.host_retained_root_path),
            ],
            check=False,
        )
        if findmnt_result.returncode != 0:
            return "unmounted"
        findmnt_field_list = findmnt_result.stdout.strip().split()
        if len(findmnt_field_list) != 3:
            return "wrong-device"
        source_text, filesystem_type, target_text = findmnt_field_list
        try:
            actual_device_path = Path(source_text).resolve(strict=True)
            expected_device_path = retained_device_path.resolve(strict=True)
        except OSError:
            return "wrong-device"
        if (
            actual_device_path == expected_device_path
            and filesystem_type == "xfs"
            and target_text == str(self._identity.host_retained_root_path)
        ):
            return "ready"
        return "wrong-device"

    def _host_service_status_get(self, unit_name: str) -> str:
        """Return one normalized systemd service state.

        Args:
            unit_name: Exact systemd unit name.

        Returns:
            Safe normalized active state.
        """

        result = self._runner.run(
            ["systemctl", "is-active", unit_name],
            check=False,
        )
        status = result.stdout.strip()
        if status not in {
            "active",
            "activating",
            "deactivating",
            "failed",
            "inactive",
            "maintenance",
            "reloading",
        }:
            return "unknown"
        return status

    @staticmethod
    def _retained_volume_id_validate(retained_volume_id: str) -> None:
        """Validate one exact EBS volume identifier used at a command boundary.

        Args:
            retained_volume_id: Candidate EBS volume identifier.
        """

        volume_suffix = retained_volume_id.removeprefix("vol-")
        if (
            not retained_volume_id.startswith("vol-")
            or not volume_suffix
            or any(character not in "0123456789abcdef" for character in volume_suffix)
        ):
            raise DevelopmentEnvironmentError("Retained volume ID is malformed")

    @staticmethod
    def _host_status_payload_validate(payload: object) -> dict[str, str]:
        """Validate the fixed safe host-status response contract.

        Args:
            payload: Decoded host-status response.

        Returns:
            Normalized safe status fields.
        """

        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(
                "Development host status output is malformed"
            )
        expected_field_set = {
            "current_release",
            "host_controller_service_status",
            "host_controller_unit_status",
            "host_status_probe",
            "k3s_service_status",
            "kubernetes_node_status",
            "retained_mount_status",
            "wcc_activity",
        }
        if set(payload) != expected_field_set or any(
            not isinstance(value, str) for value in payload.values()
        ):
            raise DevelopmentEnvironmentError(
                "Development host status output is malformed"
            )
        current_release = payload["current_release"]
        if current_release not in {"", "invalid"} and (
            len(current_release) != 20 or not current_release.isdigit()
        ):
            raise DevelopmentEnvironmentError(
                "Development host current release is malformed"
            )
        allowed_value_by_field_map = {
            "host_controller_service_status": {
                "active",
                "activating",
                "deactivating",
                "failed",
                "inactive",
                "maintenance",
                "reloading",
                "unknown",
            },
            "host_controller_unit_status": {
                "loaded",
                "masked",
                "not-found",
                "unknown",
            },
            "host_status_probe": {"ok"},
            "k3s_service_status": {
                "active",
                "activating",
                "deactivating",
                "failed",
                "inactive",
                "maintenance",
                "reloading",
                "unknown",
            },
            "kubernetes_node_status": {
                "not-ready",
                "ready",
                "unavailable",
            },
            "retained_mount_status": {
                "ready",
                "unmounted",
                "wrong-device",
            },
            "wcc_activity": {"busy", "idle"},
        }
        for field, allowed_value_set in allowed_value_by_field_map.items():
            if payload[field] not in allowed_value_set:
                raise DevelopmentEnvironmentError(
                    f"Development host status field {field} is malformed"
                )
        return {field: str(value) for field, value in payload.items()}

    @staticmethod
    def _host_status_unavailable_payload_get() -> dict[str, str]:
        """Return stable status fields when the remote host cannot be inspected."""

        return {
            "current_release": "",
            "host_controller_service_status": "unavailable",
            "host_controller_unit_status": "unavailable",
            "host_status_probe": "unavailable",
            "k3s_service_status": "unavailable",
            "kubernetes_node_status": "unavailable",
            "retained_mount_status": "unavailable",
            "wcc_activity": "unavailable",
        }

    def _instance_state_get(self, instance_id: str) -> str:
        payload = self._aws_json_get(
            ["ec2", "describe-instances", "--instance-ids", instance_id]
        )
        try:
            state = payload["Reservations"][0]["Instances"][0]["State"]["Name"]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "EC2 instance state response is malformed"
            ) from error
        if not isinstance(state, str):
            raise DevelopmentEnvironmentError("EC2 instance state is not text")
        return state

    def _instance_launch_template_version_get(self) -> str:
        """Return the exact launch-template version recorded by the EC2 instance."""

        instance_id = self._instance_id_get()
        payload = self._aws_json_get(
            ["ec2", "describe-instances", "--instance-ids", instance_id]
        )
        try:
            tag_list = payload["Reservations"][0]["Instances"][0]["Tags"]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "EC2 instance launch-template response is malformed"
            ) from error
        if not isinstance(tag_list, list) or any(
            not isinstance(tag, dict) for tag in tag_list
        ):
            raise DevelopmentEnvironmentError(
                "EC2 instance launch-template tags are malformed"
            )
        tag_by_name_map = {
            tag.get("Key"): tag.get("Value")
            for tag in tag_list
            if isinstance(tag.get("Key"), str)
        }
        actual_version = tag_by_name_map.get("aws:ec2launchtemplate:version")
        if not isinstance(actual_version, str) or not actual_version.isdigit():
            raise DevelopmentEnvironmentError(
                "EC2 instance has no exact launch-template version"
            )
        return actual_version

    def _instance_launch_template_update_is_pending(self) -> bool:
        """Return whether an exact new launch input requires controlled replacement."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        active_version = output_by_name_map.get("InstanceLaunchTemplateVersion")
        latest_version = output_by_name_map.get("LatestLaunchTemplateVersion")
        if (
            not isinstance(active_version, str)
            or not active_version.isdigit()
            or not isinstance(latest_version, str)
            or not latest_version.isdigit()
        ):
            raise DevelopmentEnvironmentError(
                "Compute stack launch-template outputs are malformed"
            )
        return active_version != latest_version

    def _instance_launch_template_version_validate(
        self,
        *,
        require_latest: bool = True,
    ) -> None:
        """Prove the instance uses the declared active and optionally latest version."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        active_version = output_by_name_map.get("InstanceLaunchTemplateVersion")
        latest_version = output_by_name_map.get("LatestLaunchTemplateVersion")
        actual_version = self._instance_launch_template_version_get()
        if (
            not isinstance(active_version, str)
            or not active_version.isdigit()
            or actual_version != active_version
        ):
            raise DevelopmentEnvironmentError(
                "EC2 instance does not use the declared launch-template version"
            )
        if require_latest and (
            not isinstance(latest_version, str)
            or not latest_version.isdigit()
            or active_version != latest_version
        ):
            raise DevelopmentEnvironmentError(
                "EC2 instance does not use the exact latest launch-template version"
            )

    def _retained_volume_state_get(
        self, *, volume_id: str
    ) -> tuple[str, list[dict[str, object]]]:
        """Return exact EBS state and validated attachment records.

        Args:
            volume_id: Retained EBS volume identity.

        Returns:
            Volume state and attachment payload list.
        """

        volume = self._retained_volume_payload_get(volume_id=volume_id)
        state = volume.get("State")
        attachment_list = volume.get("Attachments", [])
        if (
            not isinstance(state, str)
            or not isinstance(attachment_list, list)
            or any(not isinstance(attachment, dict) for attachment in attachment_list)
        ):
            raise DevelopmentEnvironmentError("Retained EBS volume state is malformed")
        return state, list(attachment_list)

    def _retained_volume_attachment_validate(self) -> None:
        """Prove the current retained volume is attached only to the stack instance."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if (
            state != "in-use"
            or len(attachment_list) != 1
            or attachment_list[0].get("DeleteOnTermination") is not False
            or attachment_list[0].get("Device") != "/dev/sdf"
            or attachment_list[0].get("InstanceId") != instance_id
            or attachment_list[0].get("State") != "attached"
            or attachment_list[0].get("VolumeId") != volume_id
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume is not exactly attached to the current stack instance"
            )

    def _retained_volume_detach_for_replacement(self) -> None:
        """Detach the retained volume only after the old instance is proven stopped."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        if self._instance_state_get(instance_id) != "stopped":
            raise DevelopmentEnvironmentError(
                "Retained EBS volume can be detached only from a stopped instance"
            )
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if not attachment_list and state == "available":
            return
        if (
            state != "in-use"
            or len(attachment_list) != 1
            or attachment_list[0].get("InstanceId") != instance_id
            or attachment_list[0].get("Device") != "/dev/sdf"
            or attachment_list[0].get("State") != "attached"
            or attachment_list[0].get("DeleteOnTermination") is not False
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume has an unexpected attachment boundary"
            )
        self._aws_run(
            [
                "ec2",
                "detach-volume",
                "--device",
                "/dev/sdf",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
            ]
        )
        self._aws_run(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if state != "available" or attachment_list:
            raise DevelopmentEnvironmentError(
                "Retained EBS volume detachment was not proven"
            )

    def _retained_volume_attachment_ensure(self) -> None:
        """Recover the stack-declared attachment after a failed replacement."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if attachment_list:
            self._retained_volume_attachment_validate()
            return
        if state != "available":
            raise DevelopmentEnvironmentError(
                "Retained EBS volume cannot be reattached from its current state"
            )
        self._aws_run(
            [
                "ec2",
                "attach-volume",
                "--device",
                "/dev/sdf",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
            ]
        )
        self._aws_run(["ec2", "wait", "volume-in-use", "--volume-ids", volume_id])
        self._retained_volume_attachment_validate()

    def _retained_volume_restore_plan_get(
        self, *, snapshot_id: str
    ) -> tuple[str, dict[str, str]]:
        """Select the next declarative restored-volume slot.

        Args:
            snapshot_id: Exact completed retained-volume snapshot.

        Returns:
            Source volume identity and exact CloudFormation parameter overrides.
        """

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        source_volume_id = output_by_name_map.get("RetainedVolumeId")
        current_slot = output_by_name_map.get("RetainedVolumeSlot", "base")
        if not isinstance(source_volume_id, str) or not source_volume_id.startswith(
            "vol-"
        ):
            raise DevelopmentEnvironmentError(
                "Compute stack retained-volume output is malformed"
            )
        next_slot_by_current_slot_map = {
            "a": "b",
            "b": "a",
            "base": "a",
        }
        try:
            next_slot = next_slot_by_current_slot_map[current_slot]
        except KeyError as error:
            raise DevelopmentEnvironmentError(
                "Compute stack retained-volume slot is malformed"
            ) from error
        self._retained_volume_snapshot_source_validate(
            snapshot_id=snapshot_id,
            source_volume_id=source_volume_id,
        )
        return source_volume_id, {
            "RetainedVolumeSlot": next_slot,
            "RetainedVolumeSnapshotId": snapshot_id,
        }

    def _retained_volume_snapshot_source_validate(
        self, *, snapshot_id: str, source_volume_id: str
    ) -> None:
        """Prove one snapshot is a usable encrypted source before stopping compute.

        Args:
            snapshot_id: Exact snapshot selected by the operator.
            source_volume_id: Current retained volume used for size validation.
        """

        source_payload = self._retained_volume_payload_get(volume_id=source_volume_id)
        payload = self._aws_json_get(
            ["ec2", "describe-snapshots", "--snapshot-ids", snapshot_id]
        )
        snapshot_list = payload.get("Snapshots", [])
        if (
            not isinstance(snapshot_list, list)
            or len(snapshot_list) != 1
            or not isinstance(snapshot_list[0], dict)
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS snapshot response is malformed"
            )
        snapshot_payload = snapshot_list[0]
        if (
            snapshot_payload.get("SnapshotId") != snapshot_id
            or snapshot_payload.get("State") != "completed"
            or snapshot_payload.get("Encrypted") is not True
            or snapshot_payload.get("OwnerId") != AWS_ACCOUNT_ID
            or not isinstance(snapshot_payload.get("VolumeSize"), int)
            or not isinstance(source_payload.get("Size"), int)
            or snapshot_payload["VolumeSize"] > source_payload["Size"]
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS snapshot is not an exact usable encrypted source"
            )

    def _retained_volume_snapshot_restore_validate(
        self, *, snapshot_id: str, source_volume_id: str
    ) -> None:
        """Prove restore created a distinct current volume from the exact snapshot.

        Args:
            snapshot_id: Exact source snapshot.
            source_volume_id: Retained volume active before restore.
        """

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        restored_volume_id = output_by_name_map.get("RetainedVolumeId")
        if (
            not isinstance(restored_volume_id, str)
            or restored_volume_id == source_volume_id
        ):
            raise DevelopmentEnvironmentError(
                "Snapshot restore did not create a distinct retained volume"
            )
        payload = self._retained_volume_payload_get(volume_id=restored_volume_id)
        tag_by_name_map = {
            tag["Key"]: tag["Value"]
            for tag in payload.get("Tags", [])
            if isinstance(tag, dict)
            and isinstance(tag.get("Key"), str)
            and isinstance(tag.get("Value"), str)
        }
        if (
            payload.get("SnapshotId") != snapshot_id
            or payload.get("Encrypted") is not True
            or tag_by_name_map.get("workflow-control-center-retained-backup")
            != "enabled"
        ):
            raise DevelopmentEnvironmentError(
                "Restored retained volume does not match the exact snapshot contract"
            )

    def _retained_volume_backup_disable(self, *, volume_id: str) -> None:
        """Stop daily snapshots for one retained but no-longer-current volume.

        Args:
            volume_id: Previous retained volume left by the Retain policy.
        """

        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if state != "available" or attachment_list:
            raise DevelopmentEnvironmentError(
                "Previous retained volume cannot leave the backup set while attached"
            )
        self._aws_run(
            [
                "ec2",
                "delete-tags",
                "--resources",
                volume_id,
                "--tags",
                "Key=workflow-control-center-retained-backup",
            ]
        )
        payload = self._retained_volume_payload_get(volume_id=volume_id)
        if any(
            isinstance(tag, dict)
            and tag.get("Key") == "workflow-control-center-retained-backup"
            for tag in payload.get("Tags", [])
        ):
            raise DevelopmentEnvironmentError(
                "Previous retained volume still belongs to the daily backup set"
            )

    def _retired_retained_volume_cleanup(self, *, current_volume_id: str) -> None:
        """Delete stale rollback volumes before creating the next bounded rollback.

        Args:
            current_volume_id: Current stack-owned retained volume that must survive.
        """

        self._retained_volume_id_validate(current_volume_id)
        current_volume_payload = self._retained_volume_payload_get(
            volume_id=current_volume_id
        )
        payload = self._aws_json_get(
            [
                "ec2",
                "describe-volumes",
                "--filters",
                "Name=tag:Name,Values=workflow-control-center-development-retained",
                "Name=tag:Project,Values=workflow-control-center",
                "Name=tag:Environment,Values=development",
                f"Name=tag:EnvironmentName,Values={self._identity.environment_name}",
                "Name=tag:ManagedBy,Values=CloudFormation",
            ]
        )
        volume_list = payload.get("Volumes", [])
        if not isinstance(volume_list, list) or any(
            not isinstance(volume, dict) for volume in volume_list
        ):
            raise DevelopmentEnvironmentError(
                "Retained rollback volume inventory is malformed"
            )
        for volume_payload in volume_list:
            volume_id = volume_payload.get("VolumeId")
            if volume_id == current_volume_id:
                continue
            if not isinstance(volume_id, str):
                raise DevelopmentEnvironmentError(
                    "Retained rollback volume identity is malformed"
                )
            self._retained_volume_id_validate(volume_id)
            tag_by_name_map = {
                tag["Key"]: tag["Value"]
                for tag in volume_payload.get("Tags", [])
                if isinstance(tag, dict)
                and isinstance(tag.get("Key"), str)
                and isinstance(tag.get("Value"), str)
            }
            required_tag_by_name_map = {
                "Environment": "development",
                "EnvironmentName": self._identity.environment_name,
                "ManagedBy": "CloudFormation",
                "Name": "workflow-control-center-development-retained",
                "Project": "workflow-control-center",
                "aws:cloudformation:stack-name": self._identity.compute_stack_name,
            }
            if any(
                tag_by_name_map.get(tag_name) != tag_value
                for tag_name, tag_value in required_tag_by_name_map.items()
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained rollback volume {volume_id} ownership is ambiguous"
                )
            if (
                volume_payload.get("State") != "available"
                or volume_payload.get("Attachments") != []
                or volume_payload.get("Encrypted") is not True
                or volume_payload.get("Size") != current_volume_payload.get("Size")
                or volume_payload.get("KmsKeyId")
                != current_volume_payload.get("KmsKeyId")
                or "workflow-control-center-retained-backup" in tag_by_name_map
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained rollback volume {volume_id} is not safe to replace"
                )
            self._aws_run(["ec2", "delete-volume", "--volume-id", volume_id])
            self._aws_run(["ec2", "wait", "volume-deleted", "--volume-ids", volume_id])
            print(f"OK: stale retained rollback volume {volume_id} deleted")

    def _retained_volume_payload_get(self, *, volume_id: str) -> dict[str, object]:
        """Return one exact retained EBS volume payload.

        Args:
            volume_id: Exact EBS volume identifier.

        Returns:
            Validated volume payload.
        """

        payload = self._aws_json_get(
            ["ec2", "describe-volumes", "--volume-ids", volume_id]
        )
        volume_list = payload.get("Volumes", [])
        if (
            not isinstance(volume_list, list)
            or len(volume_list) != 1
            or not isinstance(volume_list[0], dict)
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume response is malformed"
            )
        return volume_list[0]

    def _retained_snapshot_policy_status_get(self) -> dict[str, str]:
        """Return safe provider status for the exact stack-owned DLM policy."""

        resource_id_by_logical_name_map = (
            self._stack_resource_id_by_logical_name_map_get(
                self._identity.compute_stack_name
            )
        )
        policy_id = resource_id_by_logical_name_map.get(
            "RetainedSnapshotLifecyclePolicy"
        )
        if not policy_id:
            raise DevelopmentEnvironmentError(
                "Compute stack has no retained snapshot lifecycle policy"
            )
        payload = self._aws_json_get(
            ["dlm", "get-lifecycle-policy", "--policy-id", policy_id]
        )
        policy = payload.get("Policy")
        if not isinstance(policy, dict):
            raise DevelopmentEnvironmentError(
                "DLM retained snapshot policy response is malformed"
            )
        state = policy.get("State")
        status_message = policy.get("StatusMessage", "")
        if not isinstance(state, str) or not isinstance(status_message, str):
            raise DevelopmentEnvironmentError(
                "DLM retained snapshot policy status is malformed"
            )
        return {
            "policy_id": policy_id,
            "state": state,
            "status_message": status_message,
        }

    def _retained_snapshot_policy_validate(self) -> None:
        """Require the stack-owned snapshot policy to be operational, not only in sync."""

        status = self._retained_snapshot_policy_status_get()
        if status["state"] != "ENABLED":
            raise DevelopmentEnvironmentError(
                "Retained snapshot lifecycle policy is "
                f"{status['state']}: {status['status_message'] or 'no provider detail'}"
            )

    def _latest_snapshot_id_get(self, volume_id: str) -> str:
        payload = self._aws_json_get(
            [
                "ec2",
                "describe-snapshots",
                "--owner-ids",
                "self",
                "--filters",
                f"Name=volume-id,Values={volume_id}",
            ]
        )
        snapshot_list = payload.get("Snapshots", [])
        if not isinstance(snapshot_list, list) or not snapshot_list:
            return ""
        snapshot_payload = max(
            (item for item in snapshot_list if isinstance(item, dict)),
            key=lambda item: str(item.get("StartTime", "")),
            default={},
        )
        snapshot_id = snapshot_payload.get("SnapshotId", "")
        return snapshot_id if isinstance(snapshot_id, str) else ""

    def _local_operator_context_validate(self) -> None:
        payload = self._aws_json_get(["sts", "get-caller-identity"])
        if payload.get("Account") != AWS_ACCOUNT_ID:
            raise DevelopmentEnvironmentError(
                f"AWS profile {AWS_PROFILE} targets {payload.get('Account')}, expected {AWS_ACCOUNT_ID}"
            )
        region_result = self._runner.run(
            ["aws", "configure", "get", "region", "--profile", AWS_PROFILE]
        )
        if region_result.stdout.strip() != AWS_REGION:
            raise DevelopmentEnvironmentError(
                f"AWS profile {AWS_PROFILE} region is {region_result.stdout.strip()}, expected {AWS_REGION}"
            )
        self._service_readiness_validate()

    def _service_readiness_validate(self) -> None:
        """Prove the development account can reach every required AWS control plane."""

        readiness_command_list = [
            ["s3api", "list-buckets"],
            ["kms", "list-keys", "--limit", "1"],
            ["athena", "list-work-groups", "--max-results", "1"],
            [
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                self._identity.data_plane_stack_name,
            ],
        ]
        for aws_argument_list in readiness_command_list:
            self._aws_run(aws_argument_list)

    def _product_recovery_acceptance_run(self) -> None:
        self._ssm_shell_run(
            [
                "sudo "
                + shlex.join(
                    self._current_product_tool_command_list_get("recovery-acceptance")
                )
            ]
        )

    def _product_recovery_status_get(self) -> str:
        """Return the exact retained Product recovery state from the active host."""

        result_payload = self._ssm_shell_result_get(
            [
                "sudo "
                + shlex.join(
                    self._current_infrastructure_tool_command_list_get(
                        "host-product-recovery-status"
                    )
                )
            ],
            timeout_seconds=HOST_STATUS_COMMAND_TIMEOUT_SECONDS,
        )
        output_text = result_payload.get("StandardOutputContent")
        if not isinstance(output_text, str):
            raise DevelopmentEnvironmentError(
                "Product recovery status output is malformed"
            )
        try:
            payload = json.loads(output_text)
            status = payload["status"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Product recovery status payload is malformed"
            ) from error
        if status not in {"absent", "pending", "ready"}:
            raise DevelopmentEnvironmentError("Product recovery status is unsupported")
        return status

    def _product_recovery_is_pending(self) -> bool:
        """Return whether interrupted retained Product recovery must resume."""

        return self._product_recovery_status_get() == "pending"

    def _product_recovery_begin(self) -> None:
        """Persist the retained Product recovery savepoint on the active host."""

        self._ssm_shell_run(
            [
                "sudo "
                + shlex.join(
                    self._current_infrastructure_tool_command_list_get(
                        "host-product-recovery-begin"
                    )
                )
            ]
        )

    def _product_recovery_complete(self) -> None:
        """Clear the retained Product recovery savepoint after acceptance."""

        self._ssm_shell_run(
            [
                "sudo "
                + shlex.join(
                    self._current_infrastructure_tool_command_list_get(
                        "host-product-recovery-complete"
                    )
                )
            ]
        )

    def _retained_product_release_link_restore(self) -> None:
        """Restore `/opt` access only after trusted infrastructure validates retained source."""

        self._ssm_shell_run(
            [
                "sudo "
                + shlex.join(
                    self._current_infrastructure_tool_command_list_get(
                        "host-product-release-restore"
                    )
                )
            ]
        )

    def _product_recovery_apply_run(self) -> None:
        """Reapply the exact retained Product release and reinstall its host service."""

        self._ssm_shell_run(
            [
                "sudo "
                + shlex.join(self._current_product_tool_command_list_get("recover"))
            ]
        )
        self._ssm_shell_run(
            [
                "sudo "
                + shlex.join(
                    self._current_product_tool_command_list_get("host-install")
                )
            ]
        )

    def _price_usd_get(
        self,
        filter_by_field_map: dict[str, str],
        *,
        service_code: str = "AmazonEC2",
        unit: str,
        usage_type: str,
    ) -> Decimal:
        price_dimension_list = self._price_dimension_list_get(
            service_code=service_code,
            filter_by_field_map=filter_by_field_map,
            usage_type=usage_type,
        )
        price_set = {
            Decimal(price_dimension["price_per_unit_usd"])
            for price_dimension in price_dimension_list
            if price_dimension["unit"] == unit
        }
        if len(price_set) != 1:
            raise DevelopmentEnvironmentError(
                f"AWS Pricing returned {len(price_set)} distinct {unit} prices "
                f"for usage type {usage_type or 'instance'}"
            )
        return next(iter(price_set))

    def _price_dimension_list_get(
        self,
        *,
        service_code: str,
        filter_by_field_map: dict[str, str],
        usage_type: str,
    ) -> list[dict[str, str]]:
        """Return every exact current on-demand price tier for one AWS meter."""

        aws_argument_list = [
            "pricing",
            "get-products",
            "--service-code",
            service_code,
            "--max-results",
            "100",
        ]
        if filter_by_field_map:
            aws_argument_list.append("--filters")
            for field, value in sorted(filter_by_field_map.items()):
                aws_argument_list.append(f"Type=TERM_MATCH,Field={field},Value={value}")
        payload = self._aws_json_get(aws_argument_list)
        price_list = payload.get("PriceList", [])
        if not isinstance(price_list, list):
            raise DevelopmentEnvironmentError("AWS Pricing response is malformed")
        price_dimension_set: set[tuple[str, str, str, str]] = set()
        for product_text in price_list:
            if not isinstance(product_text, str):
                raise DevelopmentEnvironmentError("AWS Pricing product is malformed")
            try:
                product_payload = json.loads(product_text)
            except json.JSONDecodeError as error:
                raise DevelopmentEnvironmentError(
                    "AWS Pricing product is invalid"
                ) from error
            if not isinstance(product_payload, dict):
                raise DevelopmentEnvironmentError("AWS Pricing product is malformed")
            product = product_payload.get("product", {})
            attribute_by_name_map = (
                product.get("attributes", {}) if isinstance(product, dict) else {}
            )
            if not isinstance(attribute_by_name_map, dict):
                continue
            if usage_type and attribute_by_name_map.get("usagetype") != usage_type:
                continue
            term_root = product_payload.get("terms", {})
            term_by_code_map = (
                term_root.get("OnDemand", {}) if isinstance(term_root, dict) else {}
            )
            if not isinstance(term_by_code_map, dict):
                continue
            for term_payload in term_by_code_map.values():
                if not isinstance(term_payload, dict):
                    continue
                dimension_by_code_map = term_payload.get("priceDimensions", {})
                if not isinstance(dimension_by_code_map, dict):
                    continue
                for dimension_payload in dimension_by_code_map.values():
                    if not isinstance(dimension_payload, dict):
                        continue
                    price_per_unit = dimension_payload.get("pricePerUnit", {})
                    price_text = (
                        price_per_unit.get("USD")
                        if isinstance(price_per_unit, dict)
                        else None
                    )
                    begin_range = dimension_payload.get("beginRange")
                    end_range = dimension_payload.get("endRange")
                    dimension_unit = dimension_payload.get("unit")
                    if not all(
                        isinstance(value, str)
                        for value in (
                            begin_range,
                            end_range,
                            price_text,
                            dimension_unit,
                        )
                    ):
                        continue
                    try:
                        Decimal(begin_range)
                        Decimal(price_text)
                    except (InvalidOperation, ValueError) as error:
                        raise DevelopmentEnvironmentError(
                            "AWS Pricing dimension is invalid"
                        ) from error
                    price_dimension_set.add(
                        (begin_range, end_range, price_text, dimension_unit)
                    )
        if not price_dimension_set:
            raise DevelopmentEnvironmentError(
                f"AWS Pricing returned no price dimensions for {service_code} "
                f"usage type {usage_type or 'unspecified'}"
            )
        return [
            {
                "begin_range": begin_range,
                "end_range": end_range,
                "price_per_unit_usd": price_text,
                "unit": unit,
            }
            for begin_range, end_range, price_text, unit in sorted(
                price_dimension_set,
                key=lambda price_dimension: Decimal(price_dimension[0]),
            )
        ]

    def _remote_text_write(
        self, *, remote_path: Path, text: str, ssh_control_path: Path
    ) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as file:
            file.write(text)
            local_path = Path(file.name)
        try:
            self._runner.run(
                [
                    "scp",
                    "-o",
                    f"ControlPath={ssh_control_path}",
                    str(local_path),
                    f"{self._identity.instance_name}:/tmp/{remote_path.name}",
                ]
            )
            self._ssh_run(
                [
                    "sudo",
                    "install",
                    "-D",
                    "-m",
                    "0644",
                    f"/tmp/{remote_path.name}",
                    str(remote_path),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                ["rm", "-f", f"/tmp/{remote_path.name}"],
                ssh_control_path=ssh_control_path,
            )
        finally:
            local_path.unlink(missing_ok=True)

    def _runtime_platform_get(self, ssh_control_path: Path) -> str:
        result = self._ssh_run(
            [
                "sudo",
                "k3s",
                "kubectl",
                "get",
                "nodes",
                "-l",
                "apwid.com/workflow-run-eligible=true",
                "-o",
                "json",
            ],
            ssh_control_path=ssh_control_path,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Kubernetes node platform response is invalid"
            ) from error
        item_list = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(item_list, list) or not item_list:
            raise DevelopmentEnvironmentError(
                "No WorkflowRun-eligible Kubernetes nodes exist"
            )
        platform_set: set[str] = set()
        for item in item_list:
            if not isinstance(item, dict):
                raise DevelopmentEnvironmentError(
                    "Kubernetes node platform response is malformed"
                )
            node_info = item.get("status", {}).get("nodeInfo", {})
            operating_system = node_info.get("operatingSystem")
            architecture = node_info.get("architecture")
            if operating_system != "linux" or architecture not in {"amd64", "arm64"}:
                raise DevelopmentEnvironmentError(
                    f"Unsupported WorkflowRun node platform {operating_system}/{architecture}"
                )
            platform_set.add(f"{operating_system}/{architecture}")
        if len(platform_set) != 1:
            raise DevelopmentEnvironmentError(
                f"WorkflowRun node platforms are mixed: {sorted(platform_set)}"
            )
        return next(iter(platform_set))

    def _moving_source_archive_create(
        self,
        *,
        archive_path: Path,
        exact_override_commit: str,
        manifest_path: Path,
        repository_name: str,
    ) -> dict[str, object]:
        """Resolve and export one remote moving source into an immutable archive.

        Args:
            archive_path: Destination deterministic source archive.
            exact_override_commit: Optional exact one-deploy commit override.
            manifest_path: Destination immutable source manifest.
            repository_name: Configured moving-source repository name.

        Returns:
            Immutable resolved source manifest.
        """

        repository_url = REPOSITORY_URL_BY_NAME_MAP[repository_name]
        if (
            exact_override_commit
            and re.fullmatch(r"[0-9a-f]{40}", exact_override_commit) is None
        ):
            raise DevelopmentEnvironmentError(
                "workflow-container-contract override must be one lowercase 40-character commit SHA"
            )
        for _attempt_index in range(MOVING_SOURCE_RESOLUTION_ATTEMPT_COUNT):
            remote_head_by_field_map = self._moving_source_head_by_field_map_get(
                repository_url=repository_url
            )
            resolved_ref = remote_head_by_field_map["resolved_ref"]
            commit_sha = exact_override_commit or remote_head_by_field_map["commit_sha"]
            with tempfile.TemporaryDirectory() as temporary_directory:
                repository_path = Path(temporary_directory) / repository_name
                self._runner.run(["git", "init", "--quiet", str(repository_path)])
                self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "fetch",
                        "--depth=1",
                        "--no-tags",
                        repository_url,
                        commit_sha if exact_override_commit else resolved_ref,
                    ]
                )
                fetched_commit_sha = self._git_stdout_get(
                    repository_path, ["rev-parse", "FETCH_HEAD"]
                )
                if fetched_commit_sha != commit_sha:
                    if exact_override_commit:
                        raise DevelopmentEnvironmentError(
                            "workflow-container-contract override resolved to another commit"
                        )
                    continue
                if (
                    self._git_stdout_get(
                        repository_path,
                        ["cat-file", "-t", commit_sha],
                    )
                    != "commit"
                ):
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract source identity is not a commit"
                    )
                remote_head_after_by_field_map = (
                    self._moving_source_head_by_field_map_get(
                        repository_url=repository_url
                    )
                )
                if remote_head_after_by_field_map["resolved_ref"] != resolved_ref or (
                    not exact_override_commit
                    and remote_head_after_by_field_map["commit_sha"] != commit_sha
                ):
                    continue
                tree_result = self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "ls-tree",
                        "-r",
                        "--full-tree",
                        commit_sha,
                    ]
                )
                if any(
                    line.startswith("160000 ")
                    for line in tree_result.stdout.splitlines()
                ):
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract moving source must not contain submodules"
                    )
                pyproject_result = self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "show",
                        f"{commit_sha}:pyproject.toml",
                    ]
                )
                try:
                    pyproject = tomllib.loads(pyproject_result.stdout)
                except tomllib.TOMLDecodeError as error:
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract pyproject.toml is malformed"
                    ) from error
                project = pyproject.get("project")
                package_version = (
                    project.get("version") if isinstance(project, dict) else None
                )
                if (
                    not isinstance(project, dict)
                    or project.get("name") != "workflow-container-contract"
                    or not isinstance(package_version, str)
                    or not package_version
                ):
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract package identity is malformed"
                    )
                self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "archive",
                        "--format=tar",
                        f"--output={archive_path}",
                        commit_sha,
                    ]
                )
            manifest: dict[str, object] = {
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "commit_sha": commit_sha,
                "file_sha256_by_path_map": self._source_file_sha256_by_path_map_get(
                    archive_path=archive_path
                ),
                "package_version": package_version,
                "repository_url": repository_url,
                "requested_selector": MOVING_SOURCE_SELECTOR,
                "resolved_ref": resolved_ref,
                "source_kind": "resolved_moving_source",
                "submodule_by_path_map": {},
            }
            if exact_override_commit:
                manifest.update(
                    {
                        "override_identity": exact_override_commit,
                        "override_reason": "explicit operator deploy argument",
                    }
                )
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            return manifest
        raise DevelopmentEnvironmentError(
            "workflow-container-contract default branch changed during every bounded resolution attempt"
        )

    def _moving_source_archive_publish(
        self,
        *,
        exact_override_commit: str,
        release_name: str,
        remote_release_root_path: Path,
        repository_name: str,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Resolve, transfer, and verify one moving source exactly once.

        Args:
            exact_override_commit: Optional exact one-deploy commit override.
            release_name: Exact Product release identity.
            remote_release_root_path: Remote root that owns immutable releases.
            repository_name: Configured moving-source repository name.
            ssh_control_path: Active SSH control socket path.

        Returns:
            Immutable resolved source manifest.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            archive_path = temporary_root_path / f"{repository_name}.tar"
            manifest_path = temporary_root_path / f"{repository_name}.json"
            manifest = self._moving_source_archive_create(
                archive_path=archive_path,
                exact_override_commit=exact_override_commit,
                manifest_path=manifest_path,
                repository_name=repository_name,
            )
            return self._source_archive_transfer(
                archive_path=archive_path,
                manifest=manifest,
                manifest_path=manifest_path,
                release_name=release_name,
                remote_release_root_path=remote_release_root_path,
                repository_name=repository_name,
                ssh_control_path=ssh_control_path,
            )

    def _moving_source_head_by_field_map_get(
        self, *, repository_url: str
    ) -> dict[str, str]:
        """Return the advertised symbolic remote HEAD and exact commit.

        Args:
            repository_url: Configured moving-source repository URL.

        Returns:
            Remote head fields keyed by `resolved_ref` and `commit_sha`.
        """

        result = self._runner.run(
            ["git", "ls-remote", "--symref", repository_url, MOVING_SOURCE_SELECTOR]
        )
        resolved_ref = ""
        commit_sha = ""
        for line in result.stdout.splitlines():
            field_list = line.split()
            if (
                len(field_list) == 3
                and field_list[0] == "ref:"
                and field_list[1].startswith("refs/heads/")
                and field_list[2] == MOVING_SOURCE_SELECTOR
            ):
                resolved_ref = field_list[1]
            elif (
                len(field_list) == 2
                and field_list[1] == MOVING_SOURCE_SELECTOR
                and re.fullmatch(r"[0-9a-f]{40}", field_list[0])
            ):
                commit_sha = field_list[0]
        if not resolved_ref or not commit_sha:
            raise DevelopmentEnvironmentError(
                "workflow-container-contract remote HEAD has no advertised symbolic branch and exact commit"
            )
        return {"commit_sha": commit_sha, "resolved_ref": resolved_ref}

    def _source_archive_publish(
        self,
        *,
        repository_name: str,
        repository_path: Path,
        release_name: str,
        remote_release_root_path: Path,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            archive_path = temporary_root_path / f"{repository_name}.tar"
            manifest_path = temporary_root_path / f"{repository_name}.json"
            manifest = self._source_archive_create(
                archive_path=archive_path,
                manifest_path=manifest_path,
                repository_name=repository_name,
                repository_path=repository_path,
            )
            return self._source_archive_transfer(
                archive_path=archive_path,
                manifest=manifest,
                manifest_path=manifest_path,
                release_name=release_name,
                remote_release_root_path=remote_release_root_path,
                repository_name=repository_name,
                ssh_control_path=ssh_control_path,
            )

    def _source_archive_transfer(
        self,
        *,
        archive_path: Path,
        manifest: dict[str, object],
        manifest_path: Path,
        release_name: str,
        remote_release_root_path: Path,
        repository_name: str,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Transfer and verify one already prepared immutable source archive.

        Args:
            archive_path: Prepared deterministic source archive.
            manifest: Prepared immutable source manifest.
            manifest_path: Serialized source manifest path.
            release_name: Exact Product release identity.
            remote_release_root_path: Remote root that owns immutable releases.
            repository_name: Source repository name.
            ssh_control_path: Active SSH control socket path.

        Returns:
            Transferred source manifest.
        """

        remote_staging_path = f"/tmp/workflow-source-{release_name}-{repository_name}"
        self._runner.run(
            [
                "rsync",
                "--archive",
                "--checksum",
                "--rsh",
                f"ssh -o ControlPath={ssh_control_path}",
                f"{archive_path}",
                f"{manifest_path}",
                f"{self._identity.instance_name}:{remote_staging_path}/",
            ]
        )
        remote_release_path = (
            remote_release_root_path / release_name / "sources" / repository_name
        )
        verification_code = f"""\
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

root_path = Path({remote_staging_path!r})
manifest = json.loads((root_path / {manifest_path.name!r}).read_text(encoding="utf-8"))
archive_path = root_path / {archive_path.name!r}
archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
if archive_sha256 != manifest["archive_sha256"]:
    raise RuntimeError("source archive digest mismatch")
target_path = Path({str(remote_release_path)!r})
target_path.parent.mkdir(parents=True, exist_ok=True)
target_path.mkdir()
with tarfile.open(archive_path) as source_archive:
    source_archive.extractall(target_path, filter="data")
actual_file_sha256_by_path_map = {{}}
for relative_path in manifest["file_sha256_by_path_map"]:
    source_path = target_path / relative_path
    if source_path.is_symlink():
        payload = os.readlink(source_path).encode()
    elif source_path.is_file():
        payload = source_path.read_bytes()
    else:
        raise RuntimeError(f"source path is missing: {{relative_path}}")
    actual_file_sha256_by_path_map[relative_path] = hashlib.sha256(payload).hexdigest()
if actual_file_sha256_by_path_map != manifest["file_sha256_by_path_map"]:
    raise RuntimeError("extracted source manifest mismatch")
shutil.rmtree(root_path)
"""
        self._ssh_run(
            [
                "sudo",
                "python3",
                "-c",
                verification_code,
            ],
            ssh_control_path=ssh_control_path,
        )
        return manifest

    def _source_archive_create(
        self,
        *,
        archive_path: Path,
        manifest_path: Path,
        repository_name: str,
        repository_path: Path,
    ) -> dict[str, object]:
        tracked_path_list = self._tracked_path_list_get(repository_path)
        file_sha256_by_path_map: dict[str, str] = {}
        with tarfile.open(archive_path, "w") as archive:
            for relative_path in tracked_path_list:
                source_path = repository_path / relative_path
                if source_path.is_dir():
                    continue
                archive_info = archive.gettarinfo(
                    str(source_path), arcname=relative_path.as_posix()
                )
                archive_info.gid = 0
                archive_info.gname = ""
                archive_info.mtime = 0
                archive_info.uid = 0
                archive_info.uname = ""
                if source_path.is_symlink():
                    archive.addfile(archive_info)
                    file_sha256_by_path_map[relative_path.as_posix()] = hashlib.sha256(
                        os.readlink(source_path).encode()
                    ).hexdigest()
                else:
                    with source_path.open("rb") as source_file:
                        payload = source_file.read()
                    archive.addfile(archive_info, fileobj=io.BytesIO(payload))
                    file_sha256_by_path_map[relative_path.as_posix()] = hashlib.sha256(
                        payload
                    ).hexdigest()
        manifest: dict[str, object] = {
            "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "commit_sha": self._git_stdout_get(repository_path, ["rev-parse", "HEAD"]),
            "file_sha256_by_path_map": file_sha256_by_path_map,
            "repository_url": REPOSITORY_URL_BY_NAME_MAP[repository_name],
            "source_kind": "exact_checkout",
            "submodule_by_path_map": self._submodule_by_path_map_get(repository_path),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        return manifest

    def _source_file_sha256_by_path_map_get(
        self, *, archive_path: Path
    ) -> dict[str, str]:
        """Return every file and symlink digest from one deterministic source archive.

        Args:
            archive_path: Candidate source tar archive.

        Returns:
            File SHA-256 values keyed by safe relative path.
        """

        file_sha256_by_path_map: dict[str, str] = {}
        member_name_set: set[str] = set()
        try:
            with tarfile.open(archive_path, "r") as archive:
                for member in archive.getmembers():
                    normalized_member_name = member.name.removesuffix("/")
                    relative_path = PurePosixPath(normalized_member_name)
                    if (
                        not normalized_member_name
                        or relative_path.is_absolute()
                        or relative_path.as_posix() != normalized_member_name
                        or any(
                            path_part in {"", ".", ".."}
                            for path_part in relative_path.parts
                        )
                        or normalized_member_name in member_name_set
                    ):
                        raise DevelopmentEnvironmentError(
                            "workflow-container-contract archive contains an unsafe or duplicate path"
                        )
                    member_name_set.add(normalized_member_name)
                    if member.isdir():
                        continue
                    if member.issym():
                        payload = member.linkname.encode()
                    elif member.isfile():
                        source_file = archive.extractfile(member)
                        if source_file is None:
                            raise DevelopmentEnvironmentError(
                                "workflow-container-contract archive file cannot be read"
                            )
                        payload = source_file.read()
                    else:
                        raise DevelopmentEnvironmentError(
                            "workflow-container-contract archive contains an unsupported entry"
                        )
                    file_sha256_by_path_map[normalized_member_name] = hashlib.sha256(
                        payload
                    ).hexdigest()
        except (OSError, tarfile.TarError) as error:
            raise DevelopmentEnvironmentError(
                "workflow-container-contract archive is malformed"
            ) from error
        return file_sha256_by_path_map

    def _source_repository_validate(
        self, repository_path: Path, repository_name: str
    ) -> None:
        expected_url = REPOSITORY_URL_BY_NAME_MAP[repository_name]
        actual_url = self._git_stdout_get(
            repository_path, ["remote", "get-url", "origin"]
        )
        if actual_url != expected_url:
            raise DevelopmentEnvironmentError(
                f"{repository_name} origin is {actual_url}, expected {expected_url}"
            )
        status = self._git_stdout_get(
            repository_path,
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
        )
        if status:
            raise DevelopmentEnvironmentError(
                f"{repository_name} worktree is not clean"
            )
        head_sha = self._git_stdout_get(repository_path, ["rev-parse", "HEAD"])
        branch_name = self._git_stdout_get(
            repository_path, ["branch", "--show-current"]
        )
        if not branch_name:
            raise DevelopmentEnvironmentError(f"{repository_name} is detached")
        remote_result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{branch_name}",
            ]
        )
        remote_field_list = remote_result.stdout.strip().split()
        if len(remote_field_list) != 2 or remote_field_list[0] != head_sha:
            raise DevelopmentEnvironmentError(
                f"{repository_name} HEAD is not exact origin/{branch_name}"
            )
        submodule_result = self._runner.run(
            ["git", "-C", str(repository_path), "submodule", "status", "--recursive"],
            check=False,
        )
        if submodule_result.returncode != 0:
            raise DevelopmentEnvironmentError(
                f"{repository_name} submodule status failed"
            )
        for status_line in submodule_result.stdout.splitlines():
            if status_line[:1] != " ":
                raise DevelopmentEnvironmentError(
                    f"{repository_name} has a non-exact submodule: {status_line}"
                )
        for submodule_path_text, submodule_payload in self._submodule_by_path_map_get(
            repository_path
        ).items():
            submodule_path = repository_path / submodule_path_text
            expected_submodule_url = submodule_payload["repository_url"]
            actual_submodule_url = self._git_stdout_get(
                submodule_path, ["remote", "get-url", "origin"]
            )
            if actual_submodule_url != expected_submodule_url:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} origin is {actual_submodule_url}, "
                    f"expected {expected_submodule_url}"
                )
            remote_result = self._runner.run(
                [
                    "git",
                    "-C",
                    str(submodule_path),
                    "ls-remote",
                    "--exit-code",
                    "origin",
                    "refs/heads/main",
                ]
            )
            remote_field_list = remote_result.stdout.strip().split()
            if len(remote_field_list) != 2:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} origin/main lookup is malformed"
                )
            ancestor_result = self._runner.run(
                [
                    "git",
                    "-C",
                    str(submodule_path),
                    "merge-base",
                    "--is-ancestor",
                    str(submodule_payload["commit_sha"]),
                    remote_field_list[0],
                ],
                check=False,
            )
            if ancestor_result.returncode != 0:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} commit is not published on origin/main"
                )

    def _ssh_control_session(self) -> "SshControlSession":
        return SshControlSession(environment=self)

    def _ssh_run(
        self,
        remote_command_list: Sequence[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        remote_command_text = shlex.join(remote_command_list)
        return self._runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={ssh_control_path}",
                self._identity.instance_name,
                remote_command_text,
            ],
            should_capture=should_capture,
        )

    def _ssm_command_start(self, shell_command_list: list[str]) -> str:
        payload = self._aws_json_get(
            [
                "ssm",
                "send-command",
                "--instance-ids",
                self._instance_id_get(),
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                json.dumps({"commands": shell_command_list}, separators=(",", ":")),
            ]
        )
        try:
            command_id = payload["Command"]["CommandId"]
        except (KeyError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "SSM send-command response is malformed"
            ) from error
        if not isinstance(command_id, str):
            raise DevelopmentEnvironmentError("SSM command ID is not text")
        return command_id

    def _ssm_shell_run(self, shell_command_list: list[str]) -> None:
        payload = self._ssm_shell_result_get(shell_command_list)
        print(payload.get("StandardOutputContent", ""), end="")
        error_text = payload.get("StandardErrorContent", "")
        if error_text:
            print(error_text, end="", file=os.sys.stderr)

    def _ssm_shell_result_get(
        self,
        shell_command_list: list[str],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        """Run one SSM shell command and return its successful invocation.

        Args:
            shell_command_list: Exact AWS-RunShellScript command list.
            timeout_seconds: Optional local polling deadline; the default is the
                current standard SSM timeout. Remote execution is never cancelled.

        Returns:
            Successful SSM command invocation payload.
        """

        if timeout_seconds is None:
            timeout_seconds = SSM_COMMAND_TIMEOUT_SECONDS
        command_id = self._ssm_command_start(shell_command_list)
        instance_id = self._instance_id_get()
        t_deadline = self._clock.monotonic() + timeout_seconds
        payload: dict[str, object] | None = None
        while self._clock.monotonic() < t_deadline:
            payload = self._ssm_command_invocation_payload_get(
                command_id=command_id,
                instance_id=instance_id,
            )
            if payload is None or payload.get("Status") in {
                "Delayed",
                "InProgress",
                "Pending",
            }:
                self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
                continue
            break
        if payload is None or payload.get("Status") in {
            "Delayed",
            "InProgress",
            "Pending",
        }:
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} did not finish within "
                f"{timeout_seconds} seconds; the remote command was not cancelled"
            )
        if payload.get("Status") != "Success":
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} failed with {payload.get('Status')}"
            )
        return payload

    def _ssm_command_invocation_payload_get(
        self, *, command_id: str, instance_id: str
    ) -> dict[str, object] | None:
        """Return one SSM invocation, tolerating its short registration delay.

        Args:
            command_id: Exact Run Command identifier.
            instance_id: Exact target instance identifier.

        Returns:
            Invocation payload, or ``None`` while the invocation is not registered.
        """

        result = self._aws_run(
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout).strip()
            if "InvocationDoesNotExist" in error_text:
                return None
            raise DevelopmentEnvironmentError(
                f"Unable to inspect SSM command {command_id}: "
                f"{error_text or f'exit {result.returncode}'}"
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} returned unexpected JSON"
            )
        return payload

    def _stack_apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
        protected_identity_logical_id_set: Collection[str] = (),
    ) -> None:
        stack_payload = self._stack_payload_get(stack_name, is_required=False)
        change_set_type = "UPDATE" if stack_payload else "CREATE"
        change_set_name = f"codex-{self._clock.now().strftime('%Y%m%d%H%M%S%f')}"
        command_list = [
            "cloudformation",
            "create-change-set",
            "--stack-name",
            stack_name,
            "--change-set-name",
            change_set_name,
            "--change-set-type",
            change_set_type,
            *self._cloudformation_template_argument_list_get(template_path),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--tags",
            "Key=Project,Value=workflow-control-center",
            "Key=Environment,Value=development",
            f"Key=EnvironmentName,Value={self._identity.environment_name}",
            "Key=ManagedBy,Value=CloudFormation",
        ]
        if stack_payload:
            current_parameter_by_name_map = self._stack_parameter_by_name_map_get(
                stack_name
            )
            current_parameter_by_name_map.update(parameter_by_name_map)
            parameter_by_name_map = current_parameter_by_name_map
        if parameter_by_name_map:
            command_list.append("--parameters")
            for parameter_name, parameter_value in sorted(
                parameter_by_name_map.items()
            ):
                command_list.append(
                    f"ParameterKey={parameter_name},ParameterValue={parameter_value}"
                )
        self._aws_run(command_list)
        wait_result = self._aws_run(
            [
                "cloudformation",
                "wait",
                "change-set-create-complete",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ],
            check=False,
        )
        change_set_payload = self._aws_json_get(
            [
                "cloudformation",
                "describe-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )
        if wait_result.returncode != 0:
            reason = str(change_set_payload.get("StatusReason", ""))
            if "didn't contain changes" in reason:
                self._aws_run(
                    [
                        "cloudformation",
                        "delete-change-set",
                        "--stack-name",
                        stack_name,
                        "--change-set-name",
                        change_set_name,
                    ]
                )
                print(f"OK: stack {stack_name} has no changes")
                return
            raise DevelopmentEnvironmentError(
                f"Change set {stack_name}/{change_set_name} failed: {reason}"
            )
        change_list = change_set_payload.get("Changes", [])
        if not isinstance(change_list, list):
            raise DevelopmentEnvironmentError(
                f"Change set {stack_name}/{change_set_name} is malformed"
            )
        change_summary_list: list[dict[str, object]] = []
        for change_payload in change_list:
            if not isinstance(change_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Change set {stack_name}/{change_set_name} is malformed"
                )
            resource_change = change_payload.get("ResourceChange", {})
            if not isinstance(resource_change, dict):
                raise DevelopmentEnvironmentError(
                    f"Change set {stack_name}/{change_set_name} is malformed"
                )
            summary = {
                "action": resource_change.get("Action"),
                "logical_resource_id": resource_change.get("LogicalResourceId"),
                "replacement": resource_change.get("Replacement", "False"),
                "resource_type": resource_change.get("ResourceType"),
                "detail_list": resource_change.get("Details", []),
            }
            change_summary_list.append(summary)
        print(
            json.dumps(
                {"change_set": change_set_name, "changes": change_summary_list},
                indent=2,
                sort_keys=True,
            )
        )
        if must_preserve_resource:
            violation_logical_id_list = self._stable_data_change_violation_list_get(
                change_summary_list
            )
            if violation_logical_id_list:
                self._aws_run(
                    [
                        "cloudformation",
                        "delete-change-set",
                        "--stack-name",
                        stack_name,
                        "--change-set-name",
                        change_set_name,
                    ]
                )
                raise DevelopmentEnvironmentError(
                    "Stable data-plane change would remove or replace "
                    + ", ".join(violation_logical_id_list)
                )
        protected_identity_violation_list = (
            self._protected_identity_change_violation_list_get(
                change_summary_list=change_summary_list,
                protected_identity_logical_id_set=protected_identity_logical_id_set,
            )
        )
        if protected_identity_violation_list:
            self._aws_run(
                [
                    "cloudformation",
                    "delete-change-set",
                    "--stack-name",
                    stack_name,
                    "--change-set-name",
                    change_set_name,
                ]
            )
            raise DevelopmentEnvironmentError(
                "Ordinary compute apply would replace a protected identity: "
                + ", ".join(protected_identity_violation_list)
            )
        self._aws_run(
            [
                "cloudformation",
                "execute-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )
        wait_name = (
            "stack-update-complete"
            if change_set_type == "UPDATE"
            else "stack-create-complete"
        )
        self._aws_run(["cloudformation", "wait", wait_name, "--stack-name", stack_name])
        if self._stack_payload_get(stack_name, is_required=True).get(
            "StackStatus"
        ) not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }:
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} did not reach a complete state"
            )

    @staticmethod
    def _protected_identity_change_violation_list_get(
        *,
        change_summary_list: Sequence[Mapping[str, object]],
        protected_identity_logical_id_set: Collection[str],
    ) -> list[str]:
        """Return protected identities whose planned action can change physical ID.

        Args:
            change_summary_list: Complete CloudFormation change summaries.
            protected_identity_logical_id_set: Identities forbidden to replace in this path.

        Returns:
            Sorted violating logical resource identities.
        """

        return sorted(
            str(summary.get("logical_resource_id"))
            for summary in change_summary_list
            if summary.get("logical_resource_id") in protected_identity_logical_id_set
            and (
                summary.get("action") == "Remove"
                or summary.get("replacement") != "False"
            )
        )

    def _stable_data_change_violation_list_get(
        self,
        change_summary_list: list[dict[str, object]],
    ) -> list[str]:
        """Return data-plane changes that are not proven identity-preserving.

        CloudFormation reports ``Conditional`` for a dependent resource when
        its property references another modified resource, even if that
        resource's physical identity cannot change. Such a change is safe only
        when every replacement-relevant detail is a dynamic resource-attribute
        reference to a change explicitly reported with ``Replacement=False``.

        Args:
            change_summary_list: Complete resource-change summaries.

        Returns:
            Sorted violating logical resource IDs.
        """

        summary_by_logical_id_map = {
            str(summary.get("logical_resource_id")): summary
            for summary in change_summary_list
        }
        conditional_safety_by_logical_id_map: dict[str, bool] = {}

        def conditional_change_is_safe(
            logical_resource_id: str,
            proving_logical_id_set: frozenset[str] = frozenset(),
        ) -> bool:
            """Prove one conditional replacement through its complete dependency chain."""

            if logical_resource_id in conditional_safety_by_logical_id_map:
                return conditional_safety_by_logical_id_map[logical_resource_id]
            if logical_resource_id in proving_logical_id_set:
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            summary = summary_by_logical_id_map.get(logical_resource_id)
            if (
                summary is None
                or summary.get("action") != "Modify"
                or summary.get("replacement") != "Conditional"
            ):
                return False
            detail_list = summary.get("detail_list")
            if not isinstance(detail_list, list) or not detail_list:
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            replacement_detail_list = []
            for detail in detail_list:
                if not isinstance(detail, dict):
                    replacement_detail_list.append(detail)
                    continue
                target = detail.get("Target")
                if isinstance(target, dict) and target.get("RequiresRecreation") in {
                    "Always",
                    "Conditionally",
                }:
                    replacement_detail_list.append(detail)
            if not replacement_detail_list:
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False

            next_proving_logical_id_set = proving_logical_id_set | {logical_resource_id}
            for detail in replacement_detail_list:
                if not isinstance(detail, dict):
                    conditional_safety_by_logical_id_map[logical_resource_id] = False
                    return False
                causing_entity = detail.get("CausingEntity")
                causing_logical_id = str(causing_entity).split(".", maxsplit=1)[0]
                causing_summary = summary_by_logical_id_map.get(causing_logical_id)
                if (
                    detail.get("Evaluation") != "Dynamic"
                    or detail.get("ChangeSource")
                    not in {"ResourceAttribute", "ResourceReference"}
                    or causing_summary is None
                    or causing_summary.get("action") != "Modify"
                ):
                    conditional_safety_by_logical_id_map[logical_resource_id] = False
                    return False
                causing_replacement = causing_summary.get("replacement")
                if causing_replacement == "False":
                    continue
                if causing_replacement == "Conditional" and (
                    conditional_change_is_safe(
                        causing_logical_id,
                        next_proving_logical_id_set,
                    )
                ):
                    continue
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            conditional_safety_by_logical_id_map[logical_resource_id] = True
            return True

        violation_logical_id_list: list[str] = []
        for summary in change_summary_list:
            logical_resource_id = str(summary.get("logical_resource_id"))
            action = summary.get("action")
            replacement = summary.get("replacement")
            if action == "Remove" or replacement == "True":
                violation_logical_id_list.append(logical_resource_id)
                continue
            if replacement != "Conditional":
                continue
            if not conditional_change_is_safe(logical_resource_id):
                violation_logical_id_list.append(logical_resource_id)
        return sorted(set(violation_logical_id_list))

    def _submodule_by_path_map_get(
        self, repository_path: Path
    ) -> dict[str, dict[str, str]]:
        gitmodules_path = repository_path / ".gitmodules"
        if not gitmodules_path.is_file():
            return {}
        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "config",
                "--file",
                ".gitmodules",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise DevelopmentEnvironmentError(
                f"{repository_path.name} .gitmodules lookup failed"
            )
        submodule_by_path_map: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            field_list = line.split(maxsplit=1)
            if len(field_list) != 2:
                raise DevelopmentEnvironmentError(
                    f"{repository_path.name} .gitmodules path is malformed"
                )
            path_key, submodule_path = field_list
            name = path_key.removeprefix("submodule.").removesuffix(".path")
            repository_url = self._git_stdout_get(
                repository_path,
                ["config", "--file", ".gitmodules", "--get", f"submodule.{name}.url"],
            )
            submodule_by_path_map[submodule_path] = {
                "commit_sha": self._git_stdout_get(
                    repository_path / submodule_path, ["rev-parse", "HEAD"]
                ),
                "repository_url": repository_url,
            }
        return submodule_by_path_map

    def _stack_drift_validate(
        self,
        stack_name: str,
    ) -> None:
        """Prove one stack is in sync.

        Args:
            stack_name: Exact CloudFormation stack name.
        """

        stack_payload = self._stack_payload_get(stack_name, is_required=True)
        if stack_payload.get("StackStatus") not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }:
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} is not in a complete operational state"
            )
        if not self._stack_output_by_name_map_get(stack_name):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} has no validated outputs"
            )
        payload = self._aws_json_get(
            ["cloudformation", "detect-stack-drift", "--stack-name", stack_name]
        )
        drift_detection_id = payload.get("StackDriftDetectionId")
        if not isinstance(drift_detection_id, str):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} drift detection ID is missing"
            )
        t_deadline = self._clock.monotonic() + STACK_TIMEOUT_SECONDS
        while self._clock.monotonic() < t_deadline:
            status_payload = self._aws_json_get(
                [
                    "cloudformation",
                    "describe-stack-drift-detection-status",
                    "--stack-drift-detection-id",
                    drift_detection_id,
                ]
            )
            detection_status = status_payload.get("DetectionStatus")
            if detection_status == "DETECTION_COMPLETE":
                if status_payload.get("StackDriftStatus") != "IN_SYNC":
                    raise DevelopmentEnvironmentError(
                        f"Stack {stack_name} is not IN_SYNC"
                    )
                print(f"OK: stack {stack_name} drift is IN_SYNC")
                return
            if detection_status == "DETECTION_FAILED":
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} drift detection failed"
                )
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(
            f"Stack {stack_name} drift detection timed out"
        )

    def _stack_output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        stack_payload = self._stack_payload_get(stack_name, is_required=True)
        output_list = stack_payload.get("Outputs", [])
        if not isinstance(output_list, list):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} Outputs are malformed"
            )
        output_by_name_map: dict[str, str] = {}
        for output_payload in output_list:
            if not isinstance(output_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} Outputs are malformed"
                )
            output_name = output_payload.get("OutputKey")
            output_value = output_payload.get("OutputValue")
            if not isinstance(output_name, str) or not isinstance(output_value, str):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} output is malformed"
                )
            output_by_name_map[output_name] = output_value
        return output_by_name_map

    def _stack_parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        stack_payload = self._stack_payload_get(stack_name, is_required=True)
        parameter_list = stack_payload.get("Parameters", [])
        if not isinstance(parameter_list, list):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} Parameters are malformed"
            )
        parameter_by_name_map: dict[str, str] = {}
        for parameter_payload in parameter_list:
            if not isinstance(parameter_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} Parameters are malformed"
                )
            parameter_name = parameter_payload.get("ParameterKey")
            parameter_value = parameter_payload.get("ParameterValue")
            if not isinstance(parameter_name, str) or not isinstance(
                parameter_value, str
            ):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} parameter is malformed"
                )
            parameter_by_name_map[parameter_name] = parameter_value
        return parameter_by_name_map

    def _stack_payload_get(
        self, stack_name: str, *, is_required: bool
    ) -> dict[str, object]:
        result = self._aws_run(
            [
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack_name,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            if not is_required and "does not exist" in result.stderr:
                return {}
            raise DevelopmentEnvironmentError(
                f"Unable to describe stack {stack_name}: {(result.stderr or result.stdout).strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} response is invalid"
            ) from error
        stack_list = payload.get("Stacks", []) if isinstance(payload, dict) else []
        if (
            not isinstance(stack_list, list)
            or len(stack_list) != 1
            or not isinstance(stack_list[0], dict)
        ):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} response is malformed"
            )
        return stack_list[0]

    def _stack_resource_id_by_logical_name_map_get(
        self, stack_name: str
    ) -> dict[str, str]:
        if not self._stack_payload_get(stack_name, is_required=False):
            return {}
        payload = self._aws_json_get(
            ["cloudformation", "list-stack-resources", "--stack-name", stack_name]
        )
        resource_list = payload.get("StackResourceSummaries", [])
        if not isinstance(resource_list, list):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} resources are malformed"
            )
        resource_id_by_logical_name_map: dict[str, str] = {}
        for resource_payload in resource_list:
            if not isinstance(resource_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} resource is malformed"
                )
            logical_name = resource_payload.get("LogicalResourceId")
            resource_id = resource_payload.get("PhysicalResourceId")
            if not isinstance(logical_name, str) or not isinstance(resource_id, str):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} resource identity is malformed"
                )
            resource_id_by_logical_name_map[logical_name] = resource_id
        return resource_id_by_logical_name_map

    @staticmethod
    def _existing_stack_resource_identity_validate(
        *,
        current_resource_id_by_logical_name_map: Mapping[str, str],
        previous_resource_id_by_logical_name_map: Mapping[str, str],
    ) -> None:
        """Prove that an update preserved every pre-existing physical identity.

        A stack update may add new owned resources. It must not remove or replace
        any resource that existed when the guarded operation began.

        Args:
            current_resource_id_by_logical_name_map: Physical identities after the update.
            previous_resource_id_by_logical_name_map: Physical identities before the update.

        Raises:
            DevelopmentEnvironmentError: If a pre-existing physical identity is absent or changed.
        """

        changed_logical_id_list = sorted(
            logical_id
            for logical_id, previous_physical_id in (
                previous_resource_id_by_logical_name_map.items()
            )
            if current_resource_id_by_logical_name_map.get(logical_id)
            != previous_physical_id
        )
        if changed_logical_id_list:
            raise DevelopmentEnvironmentError(
                "Stable data-plane physical resource identity changed: "
                + ", ".join(changed_logical_id_list)
            )

    def _stop_lease_delete(self) -> None:
        result = self._aws_run(
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
            raise DevelopmentEnvironmentError(
                f"Stop lease deletion failed: {result.stderr.strip()}"
            )

    def _stop_lease_payload_get(self) -> dict[str, object]:
        result = self._aws_run(
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
            raise DevelopmentEnvironmentError(
                f"Stop lease lookup failed: {result.stderr.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Stop lease response is invalid"
            ) from error
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

    def _stop_lease_upsert(self, *, lease_duration: timedelta = LEASE_DURATION) -> None:
        """Create or renew a lease that resolves the current instance at expiry."""

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.compute_stack_name
        )
        if lease_duration <= timedelta():
            raise DevelopmentEnvironmentError("Stop lease duration must be positive")
        t_stop = self._clock.now() + lease_duration
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
        result = self._aws_run(
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
        create_argument_list = ["scheduler", operation, *common_argument_list]
        self._aws_run(create_argument_list)
        lease_payload = self._stop_lease_payload_get()
        if (
            lease_payload.get("action_after_completion") != "DELETE"
            or lease_payload.get("schedule_expression") != schedule_expression
            or lease_payload.get("state") != "ENABLED"
            or lease_payload.get("target_arn") != target_arn
        ):
            raise DevelopmentEnvironmentError("Stop lease was not proven enabled")

    def _wait_until(self, t_deadline: datetime) -> None:
        """Wait until one UTC deadline without changing the operational policy."""

        while self._clock.now() < t_deadline:
            remaining_seconds = (t_deadline - self._clock.now()).total_seconds()
            self._clock.sleep(min(STACK_POLL_INTERVAL_SECONDS, remaining_seconds))

    def _instance_stopped_wait(self, *, instance_id: str, t_deadline: datetime) -> None:
        """Wait for the acceptance lease target to stop the exact instance."""

        while self._clock.now() < t_deadline:
            state = self._instance_state_get(instance_id)
            if state == "stopped":
                return
            if state not in {"running", "stopping"}:
                raise DevelopmentEnvironmentError(
                    f"Lifecycle acceptance reached unexpected instance state {state}"
                )
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(
            "Lifecycle acceptance lease did not stop the instance before its deadline"
        )

    def _stop_lease_absence_wait(self, *, t_deadline: datetime) -> None:
        """Wait for Scheduler to auto-delete the completed acceptance lease."""

        while self._clock.now() < t_deadline:
            if self._stop_lease_payload_get().get("state") == "absent":
                return
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(
            "Lifecycle acceptance lease was not auto-deleted"
        )

    def _lifecycle_acceptance_environment_restore(self, instance_id: str) -> None:
        """Best-effort restore of the ordinary controller and two-hour lease."""

        state = self._instance_state_get(instance_id)
        if state == "pending":
            self._instance_online_wait()
            state = "running"
        if state == "running":
            self._stop_lease_upsert()
            self._ssm_shell_run(
                ["sudo systemctl start workflow-control-center-host-controller"]
            )
            self._host_readiness_wait()
            return
        if state == "stopping":
            self._aws_run(
                ["ec2", "wait", "instance-stopped", "--instance-ids", instance_id]
            )
            state = "stopped"
        if state == "stopped":
            self.start()
            return
        raise DevelopmentEnvironmentError(
            "Lifecycle acceptance could not restore the environment from "
            f"instance state {state}"
        )

    def _cloudformation_template_argument_list_get(
        self,
        template_path: Path,
    ) -> list[str]:
        """Return an inline or content-addressed S3 CloudFormation template reference.

        Args:
            template_path: Exact local template selected for the operation.

        Returns:
            AWS CLI argument pair for ``TemplateBody`` or ``TemplateURL``.

        Raises:
            DevelopmentEnvironmentError: If the template or retained artifact
                object cannot be proven exact.
        """

        template_bytes = template_path.read_bytes()
        template_byte_count = len(template_bytes)
        if template_byte_count <= CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT:
            return ["--template-body", f"file://{template_path}"]
        if template_byte_count > CLOUDFORMATION_S3_TEMPLATE_MAX_BYTE_COUNT:
            raise DevelopmentEnvironmentError(
                f"CloudFormation template {template_path} exceeds the 1 MiB S3 limit"
            )

        output_by_name_map = self._stack_output_by_name_map_get(
            self._identity.data_plane_stack_name
        )
        bucket_name = output_by_name_map.get("ObservabilityBucketName")
        if not bucket_name:
            raise DevelopmentEnvironmentError(
                "Oversized CloudFormation template requires the retained "
                "Observability artifact bucket"
            )
        digest_bytes = hashlib.sha256(template_bytes).digest()
        digest = digest_bytes.hex()
        checksum_sha256 = base64.b64encode(digest_bytes).decode("ascii")
        object_key = (
            "cloudformation-template/"
            f"{self._identity.environment_name}/{digest}.yaml"
        )
        head_argument_list = [
            "s3api",
            "head-object",
            "--bucket",
            bucket_name,
            "--key",
            object_key,
            "--checksum-mode",
            "ENABLED",
            "--output",
            "json",
        ]
        head_result = self._aws_run(head_argument_list, check=False)
        if head_result.returncode != 0:
            error_text = (head_result.stderr or head_result.stdout).strip()
            if not any(
                marker in error_text for marker in ("(404)", "NoSuchKey", "Not Found")
            ):
                raise DevelopmentEnvironmentError(
                    "Unable to inspect CloudFormation template artifact: "
                    + (error_text or f"exit {head_result.returncode}")
                )
            self._aws_run(
                [
                    "s3api",
                    "put-object",
                    "--bucket",
                    bucket_name,
                    "--key",
                    object_key,
                    "--body",
                    str(template_path),
                    "--checksum-sha256",
                    checksum_sha256,
                    "--content-type",
                    "application/yaml",
                    "--metadata",
                    f"sha256={digest}",
                ]
            )

        head_payload = self._aws_json_get(head_argument_list)
        metadata = head_payload.get("Metadata")
        if (
            head_payload.get("ContentLength") != template_byte_count
            or head_payload.get("ChecksumSHA256") != checksum_sha256
            or not isinstance(metadata, dict)
            or metadata.get("sha256") != digest
        ):
            raise DevelopmentEnvironmentError(
                "CloudFormation template artifact identity does not match local bytes"
            )
        template_url = (
            f"https://{bucket_name}.s3.{AWS_REGION}.amazonaws.com/{object_key}"
        )
        return ["--template-url", template_url]

    def _template_validate(self, template_path: Path) -> None:
        self._runner.run(
            [
                str(self._project_root_path / ".venv" / "bin" / "cfn-lint"),
                str(template_path),
            ]
        )
        self._aws_run(
            [
                "cloudformation",
                "validate-template",
                *self._cloudformation_template_argument_list_get(template_path),
            ]
        )

    def _tracked_path_list_get(self, repository_path: Path) -> list[Path]:
        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-files",
                "--cached",
                "--recurse-submodules",
                "-z",
            ]
        )
        path_list = [Path(value) for value in result.stdout.split("\0") if value]
        return sorted(path_list, key=lambda path: path.as_posix())

    def _git_stdout_get(
        self, repository_path: Path, git_argument_list: Sequence[str]
    ) -> str:
        result = self._runner.run(
            ["git", "-C", str(repository_path), *git_argument_list]
        )
        return result.stdout.strip()


class DevelopmentEnvironmentError(RuntimeError):
    """Report one safe development-environment operation failure."""


class SshControlSession:
    """Own one ephemeral SSH key and multiplexed SSH-over-SSM control connection."""

    def __init__(self, *, environment: DevelopmentEnvironment) -> None:
        """Initialize the SSH session owner.

        Args:
            environment: Development environment owning AWS and process boundaries.
        """

        self._environment = environment
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        """Create an ephemeral key, publish it through EC2 Instance Connect, and open SSH.

        Returns:
            OpenSSH control-socket path.
        """

        self._temporary_directory = tempfile.TemporaryDirectory()
        temporary_root_path = Path(self._temporary_directory.name)
        private_key_path = temporary_root_path / "id_ed25519"
        control_path = temporary_root_path / "control"
        self._environment._runner.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(private_key_path),
            ]
        )
        instance_id = self._environment._instance_id_get()
        instance_payload = self._environment._aws_json_get(
            ["ec2", "describe-instances", "--instance-ids", instance_id]
        )
        try:
            availability_zone = instance_payload["Reservations"][0]["Instances"][0][
                "Placement"
            ]["AvailabilityZone"]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "EC2 availability zone response is malformed"
            ) from error
        if not isinstance(availability_zone, str):
            raise DevelopmentEnvironmentError("EC2 availability zone is not text")
        self._environment._aws_run(
            [
                "ec2-instance-connect",
                "send-ssh-public-key",
                "--instance-id",
                instance_id,
                "--instance-os-user",
                "ubuntu",
                "--ssh-public-key",
                f"file://{private_key_path}.pub",
                "--availability-zone",
                availability_zone,
            ]
        )
        proxy_command = (
            f"aws ssm start-session --profile {shlex.quote(AWS_PROFILE)} --region {shlex.quote(AWS_REGION)} "
            f"--target {shlex.quote(instance_id)} --document-name AWS-StartSSHSession "
            "--parameters 'portNumber=%p'"
        )
        config_path = temporary_root_path / "config"
        config_path.write_text(
            "\n".join(
                [
                    f"Host {self._environment._identity.instance_name}",
                    f"  HostName {instance_id}",
                    "  User ubuntu",
                    f"  IdentityFile {private_key_path}",
                    "  IdentitiesOnly yes",
                    "  StrictHostKeyChecking accept-new",
                    f"  UserKnownHostsFile {temporary_root_path / 'known_hosts'}",
                    f"  ProxyCommand {proxy_command}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._environment._runner.run(
            [
                "ssh",
                "-F",
                str(config_path),
                "-M",
                "-N",
                "-f",
                "-o",
                f"ControlPath={control_path}",
                "-o",
                "ControlPersist=600",
                self._environment._identity.instance_name,
            ]
        )
        return control_path

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Close the multiplexed connection and delete ephemeral key material.

        Args:
            exc_type: Active exception type.
            exc_value: Active exception value.
            traceback: Active traceback.
        """

        if self._temporary_directory is None:
            return
        control_path = Path(self._temporary_directory.name) / "control"
        self._environment._runner.run(
            [
                "ssh",
                "-S",
                str(control_path),
                "-O",
                "exit",
                self._environment._identity.instance_name,
            ],
            check=False,
        )
        self._temporary_directory.cleanup()
        self._temporary_directory = None
