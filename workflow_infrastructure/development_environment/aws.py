"""AWS CLI boundary shared by development-environment owners."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class CommandRunnerProtocol(Protocol):
    """Minimal process boundary required by the AWS CLI client."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command."""


class DevelopmentAwsClient:
    """Own AWS CLI invocation and JSON response validation for one environment."""

    def __init__(
        self,
        *,
        is_host: bool,
        profile: str,
        region: str,
        runner: CommandRunnerProtocol,
    ) -> None:
        """Bind the client to one explicit account access path."""

        if not profile or not region:
            raise DevelopmentEnvironmentError("AWS profile and region are required")
        self._is_host = is_host
        self._profile = profile
        self._region = region
        self._runner = runner

    def run(
        self,
        aws_argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI operation through the configured identity boundary."""

        command_list = ["aws", *aws_argument_list, "--region", self._region]
        if not self._is_host:
            command_list.extend(["--profile", self._profile])
        return self._runner.run(command_list, check=check)

    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI operation and require one object JSON response."""

        result = self.run([*aws_argument_list, "--output", "json"])
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(f"AWS {aws_argument_list[0]} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(f"AWS {aws_argument_list[0]} returned unexpected JSON")
        return payload
