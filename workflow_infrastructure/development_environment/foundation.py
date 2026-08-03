"""Own the single account-global development foundation stack."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class AccountVerifierProtocol(Protocol):
    def account_foundation_validate(self) -> None:
        """Validate exact live account-global state."""


class EnvironmentIdentityProtocol(Protocol):
    is_primary: bool


class StackManagerProtocol(Protocol):
    def apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
    ) -> None:
        """Apply one exact CloudFormation stack transition."""

    def drift_validate(self, stack_name: str) -> None:
        """Validate one existing stack's drift."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack outputs."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack parameters."""

    def payload_get(self, stack_name: str, *, is_required: bool) -> Mapping[str, object]:
        """Return an existing stack or an empty mapping."""

    def template_validate(self, template_path: Path) -> None:
        """Validate a local CloudFormation template."""


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
        """Apply the primary-owned state or validate it without competing writes."""

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
        )
        self._stack.drift_validate(self.STACK_NAME)
        self._account.account_foundation_validate()

    def exists(self) -> bool:
        return bool(self._stack.payload_get(self.STACK_NAME, is_required=False))
