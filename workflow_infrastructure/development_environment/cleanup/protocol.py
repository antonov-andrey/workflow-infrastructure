"""External boundaries shared by task-environment cleanup owners."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Protocol, Sequence


class AccountVerifierProtocol(Protocol):
    """Declare the account verifier interface."""

    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class AwsClientProtocol(Protocol):
    """Declare the AWS client interface."""

    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response.

        Args:
            aws_argument_list: Ordered AWS argument values.

        Returns:
            Decoded AWS response object.
        """

    def run(self, aws_argument_list: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command.

        Args:
            aws_argument_list: Ordered AWS argument values.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """


class CleanupBindingProtocol(Protocol):
    """Declare the cleanup binding interface."""

    def common_directory_get(self) -> Path:
        """Return the owning repository Git common directory.

        Returns:
            The owning repository Git common directory.
        """


class EnvironmentIdentityProtocol(Protocol):
    """Declare the environment identity interface."""

    compute_stack_name: str
    data_plane_stack_name: str
    environment_name: str
    git_worktree: str
    is_primary: bool


class StackManagerProtocol(Protocol):
    """Declare the stack manager interface."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return one stack's exact outputs.

        Args:
            stack_name: Stack name.

        Returns:
            One stack's exact outputs.
        """

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return one stack's exact parameters.

        Args:
            stack_name: Stack name.

        Returns:
            One stack's exact parameters.
        """

    def payload_get(self, stack_name: str, *, is_required: bool) -> dict[str, object]:
        """Return one stack object, or empty when absent.

        Args:
            stack_name: Stack name.
            is_required: Whether required.

        Returns:
            One stack object, or empty when absent.
        """
