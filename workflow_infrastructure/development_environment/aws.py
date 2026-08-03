"""AWS CLI boundary shared by development-environment owners."""

from __future__ import annotations

import json
from dataclasses import dataclass
import re
import subprocess
from collections.abc import Sequence
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_AWS_CLI_ERROR_PATTERN = re.compile(
    r"^An error occurred \((?P<code>[A-Za-z0-9][A-Za-z0-9._-]*)\) "
    r"when calling the (?P<operation>[A-Za-z0-9]+) operation"
    r"(?: \([^\r\n]*\))?: (?P<message>.+)$"
)


@dataclass(frozen=True, slots=True)
class AwsCliError:
    """One unambiguous structured AWS CLI service error."""

    code: str
    message: str
    operation: str


def aws_cli_error_get(result: subprocess.CompletedProcess[str]) -> AwsCliError | None:
    """Decode exactly one standard AWS CLI error line, or fail closed."""

    returncode = getattr(result, "returncode", None)
    stderr = getattr(result, "stderr", None)
    if not isinstance(returncode, int) or returncode == 0 or not isinstance(stderr, str):
        return None
    diagnostic_line_list = [line.strip() for line in stderr.splitlines() if line.strip()]
    if len(diagnostic_line_list) != 1:
        return None
    match = _AWS_CLI_ERROR_PATTERN.fullmatch(diagnostic_line_list[0])
    return AwsCliError(**match.groupdict()) if match is not None else None


def aws_cli_error_matches(
    result: subprocess.CompletedProcess[str],
    *,
    code_set: frozenset[str],
    operation: str,
) -> bool:
    """Return whether a failed command proves one exact service error identity."""

    error = aws_cli_error_get(result)
    return error is not None and error.operation == operation and error.code in code_set


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
