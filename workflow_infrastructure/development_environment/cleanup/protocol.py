"""External boundaries shared by task-environment cleanup owners."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Protocol, Sequence


class AccountVerifierProtocol(Protocol):
    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class AwsClientProtocol(Protocol):
    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response."""

    def run(self, aws_argument_list: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command."""


class CleanupBindingProtocol(Protocol):
    def common_directory_get(self) -> Path:
        """Return the owning repository Git common directory."""


class EnvironmentIdentityProtocol(Protocol):
    compute_stack_name: str
    data_plane_stack_name: str
    environment_name: str
    git_worktree: str
    is_primary: bool


class StackManagerProtocol(Protocol):
    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return one stack's exact outputs."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return one stack's exact parameters."""

    def payload_get(self, stack_name: str, *, is_required: bool) -> dict[str, object]:
        """Return one stack object, or empty when absent."""
