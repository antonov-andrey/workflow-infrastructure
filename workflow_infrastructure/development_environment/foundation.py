"""Own the single account-global development foundation stack."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class AccountVerifierProtocol(Protocol):
    """Declare the account verifier interface."""

    def account_foundation_validate(self) -> None:
        """Validate exact live account-global state."""


class EnvironmentIdentityProtocol(Protocol):
    """Declare the environment identity interface."""

    is_primary: bool


class StackManagerProtocol(Protocol):
    """Declare the stack manager interface."""

    def apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
        allowed_primary_platform_data_lake_admin_arn: str | None = None,
        versioned_document_logical_id_set: Collection[str] = (),
    ) -> None:
        """Apply one exact CloudFormation stack transition.

        Args:
            stack_name: Stack name.
            template_path: Exact filesystem path for template.
            parameter_by_name_map: Parameter by name mapping.
            must_preserve_resource: Must preserve resource.
            allowed_primary_platform_data_lake_admin_arn: Exact permitted fresh-primary administrator ARN.
            versioned_document_logical_id_set: Explicitly versioned SSM document logical identities.
        """

    def drift_validate(self, stack_name: str) -> None:
        """Validate one existing stack's drift.

        Args:
            stack_name: Stack name.
        """

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack outputs.

        Args:
            stack_name: Stack name.

        Returns:
            The stack outputs.
        """

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack parameters.

        Args:
            stack_name: Stack name.

        Returns:
            The stack parameters.
        """

    def payload_get(self, stack_name: str, *, is_required: bool) -> Mapping[str, object]:
        """Return an existing stack or an empty mapping.

        Args:
            stack_name: Stack name.
            is_required: Whether required.

        Returns:
            An existing stack or an empty mapping.
        """

    def template_validate(self, template_path: Path) -> None:
        """Validate a local CloudFormation template.

        Args:
            template_path: Exact filesystem path for template.
        """


class DevelopmentAccountFoundationManager:
    """Create/reconcile foundation only for primary; task environments verify it."""

    STACK_NAME = "account-foundation"

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        identity: EnvironmentIdentityProtocol,
        stack: StackManagerProtocol,
        template_path: Path,
    ) -> None:
        """Initialize the development account foundation manager dependencies.

        Args:
            account: Account.
            identity: Identity.
            stack: Stack.
            template_path: Exact filesystem path for template.
        """

        self._account = account
        self._identity = identity
        self._stack = stack
        self._template_path = template_path

    def ensure(
        self,
        *,
        primary_platform_role_arn: str | None = None,
        primary_retained_volume_arn: str | None = None,
    ) -> None:
        """Apply the primary-owned state or validate it without competing writes.

        Args:
            primary_platform_role_arn: Primary platform role arn.
            primary_retained_volume_arn: Primary retained volume arn.
        """

        if not self._identity.is_primary:
            if not self.exists():
                raise DevelopmentEnvironmentError("Account-foundation stack is unavailable")
            self._stack.drift_validate(self.STACK_NAME)
            self._account.account_foundation_validate()
            return
        parameter_by_name_map = {
            "PrimaryPlatformRoleArn": "",
            "PrimaryRetainedVolumeArn": "",
        }
        if self.exists():
            current = self._stack.parameter_by_name_map_get(self.STACK_NAME)
            parameter_by_name_map.update({name: current.get(name, "") for name in parameter_by_name_map})
        if primary_platform_role_arn is not None:
            parameter_by_name_map["PrimaryPlatformRoleArn"] = primary_platform_role_arn
        if primary_retained_volume_arn is not None:
            parameter_by_name_map["PrimaryRetainedVolumeArn"] = primary_retained_volume_arn
        self._stack.template_validate(self._template_path)
        self._stack.apply(
            stack_name=self.STACK_NAME,
            template_path=self._template_path,
            parameter_by_name_map=parameter_by_name_map,
            must_preserve_resource=True,
            allowed_primary_platform_data_lake_admin_arn=primary_platform_role_arn,
            versioned_document_logical_id_set={"SessionManagerRunShellPreferences"},
        )
        self._stack.drift_validate(self.STACK_NAME)
        self._account.account_foundation_validate()

    def exists(self) -> bool:
        """Report whether the unique account-foundation stack currently exists.

        Returns:
            Whether the account-foundation stack exists.
        """

        return bool(self._stack.payload_get(self.STACK_NAME, is_required=False))
