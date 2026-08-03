"""Shared retained Product release lifecycle interfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence


class RetainedProductReleaseHostIdentity(Protocol):
    """Host paths and identity required by retained Product release lifecycle."""

    environment_name: str
    local_http_port: int
    host_current_source_path: Path
    host_product_recovery_marker_path: Path
    host_release_root_path: Path
    host_retained_current_release_path: Path
    host_retained_product_tool_path: Path
    host_retained_release_root_path: Path
    host_retained_rollback_release_path: Path


class RetainedProductReleaseCommandResult(Protocol):
    """Command result fields required by retained Product release lifecycle."""

    returncode: int


class RetainedProductReleaseCommandRunner(Protocol):
    """External command boundary required by retained Product release lifecycle."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> RetainedProductReleaseCommandResult:
        """Run one host command.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.
            should_capture: Whether stdout and stderr should be captured.

        Returns:
            Resulting retained product release command result.
        """
