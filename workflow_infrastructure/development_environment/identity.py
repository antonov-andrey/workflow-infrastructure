"""Derive exact AWS and host identities for one development environment."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

ACCOUNT_FOUNDATION_STACK_NAME = "account-foundation"
COMPUTE_STACK_NAME = "compute-primary"
DATA_PLANE_STACK_NAME = "data-primary"
HOST_CONTROL_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/control/current")
HOST_CONTROL_RELEASE_ROOT_PATH = Path("/opt/workflow-infrastructure/control/releases")
INFRASTRUCTURE_SOURCE_RELATIVE_PATH = Path("sources/workflow-infrastructure")
INFRASTRUCTURE_ENTRYPOINT_RELATIVE_PATH = (
    INFRASTRUCTURE_SOURCE_RELATIVE_PATH / "development_environment_manage.py"
)
HOST_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/current")
HOST_RETAINED_ROOT_PATH = Path("/srv/workflow-control-center")
HOST_RETAINED_RELEASE_ROOT_PATH = HOST_RETAINED_ROOT_PATH / "release"
HOST_RETAINED_CURRENT_RELEASE_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "current"
HOST_RELEASE_ROOT_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "releases"
HOST_STATE_ROOT_PATH = Path("/var/lib/workflow-infrastructure")
INSTANCE_NAME = "compute-primary"
LEASE_GROUP_NAME = "scheduler-primary"
LEASE_NAME = "stop-primary"
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]{0,15}")
_GIT_WORKTREE_PATTERN = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]*")


class DevelopmentEnvironmentIdentity:
    """Derive every physical development identity from one stable machine name."""

    def __init__(
        self, environment_name: str = "primary", *, git_worktree: str = ""
    ) -> None:
        """Validate and retain one stable lowercase environment name."""

        if git_worktree:
            if (
                _GIT_WORKTREE_PATTERN.fullmatch(git_worktree) is None
                or len(git_worktree) > 120
            ):
                raise DevelopmentEnvironmentError(
                    "git_worktree must be one canonical dated common prefix"
                )
            derived_environment_name = (
                "w" + hashlib.sha256(git_worktree.encode("utf-8")).hexdigest()[:15]
            )
            if environment_name not in {"primary", derived_environment_name}:
                raise DevelopmentEnvironmentError(
                    "git_worktree and environment_name identify different environments"
                )
            environment_name = derived_environment_name
        if _ENVIRONMENT_NAME_PATTERN.fullmatch(environment_name) is None:
            raise DevelopmentEnvironmentError(
                "environment_name must match [a-z][a-z0-9]{0,15}"
            )
        self.environment_name = environment_name
        self.git_worktree = git_worktree
        self._git_worktree_sha256 = (
            hashlib.sha256(git_worktree.encode("utf-8")).hexdigest()
            if git_worktree
            else ""
        )

    @property
    def is_primary(self) -> bool:
        """Return whether this is the stable primary development environment."""

        return self.environment_name == "primary"

    @property
    def local_http_port(self) -> int:
        """Return the deterministic local SSM tunnel port for this environment."""

        if self.is_primary:
            return 8080
        return 18000 + int(self._git_worktree_sha256[:8], 16) % 20000

    @property
    def data_plane_stack_name(self) -> str:
        """Return the exact data-plane stack identity."""

        if self.is_primary:
            return DATA_PLANE_STACK_NAME
        return f"data-{self.environment_name}"

    @property
    def compute_stack_name(self) -> str:
        """Return the exact compute stack identity."""

        if self.is_primary:
            return COMPUTE_STACK_NAME
        return f"compute-{self.environment_name}"

    @property
    def instance_name(self) -> str:
        """Return the local SSH alias and EC2 Name identity."""

        if self.is_primary:
            return INSTANCE_NAME
        return f"compute-{self.environment_name}"

    @property
    def lease_group_name(self) -> str:
        """Return the environment-owned Scheduler group name."""

        if self.is_primary:
            return LEASE_GROUP_NAME
        return f"scheduler-{self.environment_name}"

    @property
    def lease_name(self) -> str:
        """Return the renewable stop-lease schedule name."""

        if self.is_primary:
            return LEASE_NAME
        return f"stop-{self.environment_name}"

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
    def host_control_infrastructure_source_path(self) -> Path:
        """Return the current workflow-infrastructure repository root."""

        return (
            self.host_control_current_source_path / INFRASTRUCTURE_SOURCE_RELATIVE_PATH
        )

    @property
    def host_control_entrypoint_path(self) -> Path:
        """Return the current primary development-environment entrypoint."""

        return (
            self.host_control_current_source_path
            / INFRASTRUCTURE_ENTRYPOINT_RELATIVE_PATH
        )

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

        return f"{self.compute_stack_name}:apwid-workflow/workflow-image-registry"

    @property
    def qualified_product_database_identity(self) -> str:
        """Return the collision-proof logical Product database identity."""

        return f"{self.compute_stack_name}:apwid-db/apwid"

    @property
    def qualified_credential_identity(self) -> str:
        """Return the collision-proof logical renewable credential identity."""

        return f"{self.compute_stack_name}:apwid-platform/workflow-control-center-aws-credentials"
