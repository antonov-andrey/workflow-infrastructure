"""Derive exact AWS and host identities for one development environment."""

from __future__ import annotations

import re
from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

COMPUTE_STACK_NAME = "workflow-control-center-development-compute"
DATA_PLANE_STACK_NAME = "workflow-control-center-development"
HOST_CONTROL_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/control/current")
HOST_CONTROL_RELEASE_ROOT_PATH = Path("/opt/workflow-infrastructure/control/releases")
HOST_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/current")
HOST_RETAINED_ROOT_PATH = Path("/srv/workflow-control-center")
HOST_RETAINED_RELEASE_ROOT_PATH = HOST_RETAINED_ROOT_PATH / "release"
HOST_RETAINED_CURRENT_RELEASE_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "current"
HOST_RELEASE_ROOT_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "releases"
HOST_STATE_ROOT_PATH = Path("/var/lib/workflow-infrastructure")
INSTANCE_NAME = "workflow-control-center-development"
LEASE_GROUP_NAME = "workflow-control-center-development"
LEASE_NAME = "workflow-control-center-development-stop"
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]{0,15}")


class DevelopmentEnvironmentIdentity:
    """Derive every physical development identity from one stable machine name."""

    def __init__(self, environment_name: str = "primary") -> None:
        """Validate and retain one stable lowercase environment name."""

        if _ENVIRONMENT_NAME_PATTERN.fullmatch(environment_name) is None:
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

        return f"{self.compute_stack_name}:apwid-workflow/workflow-image-registry"

    @property
    def qualified_product_database_identity(self) -> str:
        """Return the collision-proof logical Product database identity."""

        return f"{self.compute_stack_name}:apwid-db/apwid"

    @property
    def qualified_credential_identity(self) -> str:
        """Return the collision-proof logical renewable credential identity."""

        return f"{self.compute_stack_name}:apwid-platform/workflow-control-center-aws-credentials"
