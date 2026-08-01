"""Publish and activate one exact Product release on a development host."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.manager import (
    PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
)
from workflow_infrastructure.development_environment.identity import (
    INFRASTRUCTURE_ENTRYPOINT_RELATIVE_PATH,
)
from workflow_infrastructure.development_environment.product.release import (
    PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
    SOURCE_MANIFEST_VERSION,
)


class AccountVerifierProtocol(Protocol):
    """Operator-account boundary consumed by Product deployment."""

    def local_operator_context_validate(self) -> None:
        """Validate exact local AWS operator context."""


class ClockProtocol(Protocol):
    """UTC clock consumed by immutable release creation."""

    def now(self) -> datetime:
        """Return current UTC time."""


class ComputeManagerProtocol(Protocol):
    """Compute boundary consumed by Product deployment."""

    def launch_template_version_validate(
        self,
        *,
        require_latest: bool = True,
    ) -> None:
        """Validate exact active launch-template version."""

    def online_wait(self) -> None:
        """Wait for the host to become SSM Online."""

    def remote_text_write(
        self,
        *,
        remote_path: Path,
        text: str,
        ssh_control_path: Path,
    ) -> None:
        """Install one generated non-secret remote file."""

    def runtime_platform_get(self, ssh_control_path: Path) -> str:
        """Return the one WorkflowRun-eligible node platform."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment paths and names consumed by Product deployment."""

    compute_stack_name: str
    data_plane_stack_name: str
    environment_name: str
    host_control_entrypoint_path: Path
    host_control_current_source_path: Path
    host_control_release_root_path: Path
    host_current_source_path: Path
    host_release_root_path: Path


class HostArtifactProtocol(Protocol):
    """Immutable host-artifact provenance boundary."""

    def manifest_payload_get(self) -> dict[str, object]:
        """Return exact compute launch provenance."""


class ProductRecoveryProtocol(Protocol):
    """Retained Product recovery boundary."""

    def status_get(self) -> str:
        """Return current retained recovery state."""


class PublicEcrAuthProtocol(Protocol):
    """Ephemeral upstream-registry authentication boundary."""

    def session(
        self,
        *,
        release_name: str,
        ssh_control_path: Path,
    ) -> AbstractContextManager[Path]:
        """Yield one release-local authenticated Docker config."""


class ProductResetProtocol(Protocol):
    """Destructive candidate-reset boundary."""

    def reset(
        self,
        *,
        expected_role_key_list: Sequence[str],
        release_name: str,
        release_root_path: Path,
        ssh_control_path: Path,
        target_platform: str,
        user_email: str,
    ) -> None:
        """Reset disposable Product state before deploying one candidate."""


class SourcePublisherProtocol(Protocol):
    """Exact source publication boundary."""

    def archive_publish(
        self,
        *,
        repository_name: str,
        repository_path: Path,
        release_name: str,
        remote_release_root_path: Path,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Publish one exact local repository archive."""

    def moving_archive_publish(
        self,
        *,
        exact_override_commit: str,
        release_name: str,
        remote_release_root_path: Path,
        repository_name: str,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Resolve and publish one moving external source."""

    def validate_repository(
        self,
        repository_path: Path,
        repository_name: str,
    ) -> None:
        """Validate one exact local source repository."""


class StackManagerProtocol(Protocol):
    """CloudFormation boundary consumed by Product deployment."""

    def drift_validate(self, stack_name: str) -> None:
        """Require a drift-free stack."""


class SsmTransportProtocol(Protocol):
    """SSH-over-SSM boundary consumed by Product deployment."""

    def ssh_control_session(self) -> AbstractContextManager[Path]:
        """Open one reusable SSH-over-SSM control session."""

    def ssh_run(
        self,
        command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> object:
        """Run one command through SSH-over-SSM."""


class DevelopmentProductDeploymentManager:
    """Own immutable Product source publication, deployment, and activation."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        clock: ClockProtocol,
        compute: ComputeManagerProtocol,
        host_artifact: HostArtifactProtocol,
        identity: EnvironmentIdentityProtocol,
        product_recovery: ProductRecoveryProtocol,
        product_reset: ProductResetProtocol,
        project_root_path: Path,
        public_ecr_auth: PublicEcrAuthProtocol,
        source_publisher: SourcePublisherProtocol,
        stack: StackManagerProtocol,
        transport: SsmTransportProtocol,
        workspace_root_path: Path,
    ) -> None:
        """Initialize Product deployment from explicit owner boundaries."""

        self._account = account
        self._clock = clock
        self._compute = compute
        self._host_artifact = host_artifact
        self._identity = identity
        self._product_recovery = product_recovery
        self._product_reset = product_reset
        self._project_root_path = project_root_path
        self._public_ecr_auth = public_ecr_auth
        self._source_publisher = source_publisher
        self._stack = stack
        self._transport = transport
        self._workspace_root_path = workspace_root_path

    def deploy(
        self,
        *,
        expected_role_key_list: Sequence[str] = (),
        should_reset_product_state: bool = False,
        user_email: str = "",
        workflow_container_contract_commit: str = "",
    ) -> None:
        """Publish, deploy, activate, and install one exact Product release.

        Args:
            expected_role_key_list: Exact preserved Product roles for destructive reset.
            should_reset_product_state: Whether to run the approved pre-production reset.
            user_email: Preserved ZITADEL user for destructive reset.
            workflow_container_contract_commit: Optional exact one-deploy contract override.
        """

        if should_reset_product_state and not user_email:
            raise DevelopmentEnvironmentError(
                "Destructive Product reset requires one preserved ZITADEL user"
            )
        if not should_reset_product_state and (user_email or expected_role_key_list):
            raise DevelopmentEnvironmentError(
                "Preserved Product identity inputs require destructive Product reset"
            )
        self._precondition_validate(
            should_reset_product_state=should_reset_product_state
        )
        release_name = self._clock.now().strftime("%Y%m%d%H%M%S%f")
        source_manifest_by_repository_name_map: dict[str, dict[str, object]] = {}
        with self._transport.ssh_control_session() as ssh_control_path:
            self._source_publish(
                release_name=release_name,
                source_manifest_by_repository_name_map=(
                    source_manifest_by_repository_name_map
                ),
                ssh_control_path=ssh_control_path,
                workflow_container_contract_commit=(workflow_container_contract_commit),
            )
            release_root_path = self._identity.host_release_root_path / release_name
            self._release_manifest_write(
                release_name=release_name,
                release_root_path=release_root_path,
                source_manifest_by_repository_name_map=(
                    source_manifest_by_repository_name_map
                ),
                ssh_control_path=ssh_control_path,
            )
            self._host_prepare(
                release_root_path=release_root_path,
                ssh_control_path=ssh_control_path,
            )
            platform = self._compute.runtime_platform_get(ssh_control_path)
            if should_reset_product_state:
                self._product_reset.reset(
                    expected_role_key_list=expected_role_key_list,
                    release_name=release_name,
                    release_root_path=release_root_path,
                    ssh_control_path=ssh_control_path,
                    target_platform=platform,
                    user_email=user_email,
                )
            with self._public_ecr_auth.session(
                release_name=release_name,
                ssh_control_path=ssh_control_path,
            ) as docker_config_path:
                self._product_deploy(
                    docker_config_path=docker_config_path,
                    platform=platform,
                    release_name=release_name,
                    release_root_path=release_root_path,
                    ssh_control_path=ssh_control_path,
                )
            self._release_activate(
                release_name=release_name,
                release_root_path=release_root_path,
                ssh_control_path=ssh_control_path,
            )
            self._host_service_install(ssh_control_path=ssh_control_path)
        print(f"OK: exact Product release {release_name} is deployed for {platform}")

    def _precondition_validate(self, *, should_reset_product_state: bool) -> None:
        """Validate operator, source, stack, host, and recovery preconditions.

        Args:
            should_reset_product_state: Whether pending legacy recovery will be destroyed.
        """

        self._account.local_operator_context_validate()
        self._source_publisher.validate_repository(
            self._project_root_path,
            "workflow-infrastructure",
        )
        for repository_name in PRODUCT_SOURCE_REPOSITORY_NAME_LIST:
            self._source_publisher.validate_repository(
                self._workspace_root_path / repository_name,
                repository_name,
            )
        self._stack.drift_validate(self._identity.data_plane_stack_name)
        self._stack.drift_validate(self._identity.compute_stack_name)
        self._compute.online_wait()
        self._compute.launch_template_version_validate(require_latest=True)
        if (
            self._product_recovery.status_get() == "pending"
            and not should_reset_product_state
        ):
            raise DevelopmentEnvironmentError(
                "Pending retained Product recovery must complete before a new deploy"
            )

    def _source_publish(
        self,
        *,
        release_name: str,
        source_manifest_by_repository_name_map: dict[str, dict[str, object]],
        ssh_control_path: Path,
        workflow_container_contract_commit: str,
    ) -> None:
        """Publish the exact local and moving source graph."""

        for repository_name in [
            "workflow-infrastructure",
            *PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
        ]:
            repository_path = (
                self._project_root_path
                if repository_name == "workflow-infrastructure"
                else self._workspace_root_path / repository_name
            )
            source_manifest_by_repository_name_map[repository_name] = (
                self._source_publisher.archive_publish(
                    repository_name=repository_name,
                    repository_path=repository_path,
                    release_name=release_name,
                    remote_release_root_path=self._identity.host_release_root_path,
                    ssh_control_path=ssh_control_path,
                )
            )
        source_manifest_by_repository_name_map["workflow-container-contract"] = (
            self._source_publisher.moving_archive_publish(
                exact_override_commit=workflow_container_contract_commit,
                release_name=release_name,
                remote_release_root_path=self._identity.host_release_root_path,
                repository_name="workflow-container-contract",
                ssh_control_path=ssh_control_path,
            )
        )

    def _release_manifest_write(
        self,
        *,
        release_name: str,
        release_root_path: Path,
        source_manifest_by_repository_name_map: dict[str, dict[str, object]],
        ssh_control_path: Path,
    ) -> None:
        """Write immutable release source and host provenance."""

        release_manifest_text = json.dumps(
            {
                "environment_name": self._identity.environment_name,
                "host_artifact_manifest": self._host_artifact.manifest_payload_get(),
                "python_bytecode_write_disabled": True,
                "release": release_name,
                "repository_by_name_map": (source_manifest_by_repository_name_map),
                "source_manifest_version": SOURCE_MANIFEST_VERSION,
                "t_deploy": self._clock.now().isoformat().replace("+00:00", "Z"),
            },
            indent=2,
            sort_keys=True,
        )
        self._compute.remote_text_write(
            remote_path=release_root_path / "source-manifest.json",
            text=release_manifest_text,
            ssh_control_path=ssh_control_path,
        )

    def _host_prepare(
        self,
        *,
        release_root_path: Path,
        ssh_control_path: Path,
    ) -> None:
        """Validate host inputs from the exact release before Product execution."""

        self._transport.ssh_run(
            [
                "sudo",
                "env",
                PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                "python3.14",
                "-B",
                str(release_root_path / INFRASTRUCTURE_ENTRYPOINT_RELATIVE_PATH),
                "host-prepare",
                "--environment-name",
                self._identity.environment_name,
            ],
            ssh_control_path=ssh_control_path,
        )

    def _product_deploy(
        self,
        *,
        docker_config_path: Path,
        platform: str,
        release_name: str,
        release_root_path: Path,
        ssh_control_path: Path,
    ) -> None:
        """Invoke Product-owned deployment from the immutable release."""

        self._transport.ssh_run(
            [
                "sudo",
                "env",
                PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                f"DOCKER_CONFIG={docker_config_path}",
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
            ],
            ssh_control_path=ssh_control_path,
            should_capture=False,
        )

    def _release_activate(
        self,
        *,
        release_name: str,
        release_root_path: Path,
        ssh_control_path: Path,
    ) -> None:
        """Activate the accepted Product and infrastructure source pointers."""

        self._transport.ssh_run(
            [
                "sudo",
                "env",
                PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                "python3.14",
                "-B",
                str(release_root_path / INFRASTRUCTURE_ENTRYPOINT_RELATIVE_PATH),
                "host-product-release-activate",
                "--environment-name",
                self._identity.environment_name,
                "--release",
                release_name,
            ],
            ssh_control_path=ssh_control_path,
        )
        self._product_release_root_complete(
            release_name=release_name,
            release_root_path=release_root_path,
            ssh_control_path=ssh_control_path,
        )
        self._transport.ssh_run(
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
        self._transport.ssh_run(
            [
                "sudo",
                "ln",
                "-sfn",
                str(release_root_path),
                str(self._identity.host_control_current_source_path),
            ],
            ssh_control_path=ssh_control_path,
        )

    def _product_release_root_complete(
        self,
        *,
        release_name: str,
        release_root_path: Path,
        ssh_control_path: Path,
    ) -> None:
        """Release Product registry admission after exact retained-pointer proof."""

        self._transport.ssh_run(
            [
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
                "product-release-root-complete",
                "--release",
                release_name,
                "--environment-name",
                self._identity.environment_name,
            ],
            ssh_control_path=ssh_control_path,
        )

    def _host_service_install(self, *, ssh_control_path: Path) -> None:
        """Install Product and infrastructure host services from active pointers."""

        self._transport.ssh_run(
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
        self._transport.ssh_run(
            [
                "sudo",
                "env",
                PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                "python3.14",
                "-B",
                str(self._identity.host_control_entrypoint_path),
                "host-install",
                "--environment-name",
                self._identity.environment_name,
            ],
            ssh_control_path=ssh_control_path,
        )
