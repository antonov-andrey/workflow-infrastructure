"""SSM and SSH-over-SSM transport for one development environment."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

SSM_COMMAND_TIMEOUT_SECONDS = 3600
STACK_POLL_INTERVAL_SECONDS = 5


class AwsClientProtocol(Protocol):
    """AWS command surface required by the transport."""

    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response."""

    def run(
        self,
        aws_argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command."""


class ClockProtocol(Protocol):
    """Monotonic time surface required by command polling."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, delay_seconds: float) -> None:
        """Advance or wait by one duration."""


class EnvironmentIdentityProtocol(Protocol):
    """Stable environment identity required by SSH."""

    instance_name: str


class CommandRunnerProtocol(Protocol):
    """External process boundary required by transport."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one local command."""


class DevelopmentSsmTransport:
    """Own SSM commands and ephemeral SSH-over-SSM sessions."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        aws_profile: str,
        aws_region: str,
        clock: ClockProtocol,
        identity: EnvironmentIdentityProtocol,
        instance_id_get: Callable[[], str],
        runner: CommandRunnerProtocol,
    ) -> None:
        """Bind every transport operation to explicit environment dependencies."""

        self._aws = aws
        self._aws_profile = aws_profile
        self._aws_region = aws_region
        self._clock = clock
        self._identity = identity
        self._instance_id_get = instance_id_get
        self._runner = runner

    def ssh_control_session(self) -> SshControlSession:
        """Return one not-yet-opened ephemeral SSH control session."""

        return SshControlSession(transport=self)

    def ssh_run(
        self,
        remote_command_list: Sequence[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one exact remote command through an open control socket."""

        return self._runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={ssh_control_path}",
                self._identity.instance_name,
                shlex.join(remote_command_list),
            ],
            should_capture=should_capture,
        )

    def ssm_command_start(self, shell_command_list: list[str]) -> str:
        """Start one bounded AWS-RunShellScript invocation."""

        payload = self._aws.json_get(
            [
                "ssm",
                "send-command",
                "--instance-ids",
                self._instance_id_get(),
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                json.dumps(
                    {"commands": shell_command_list},
                    separators=(",", ":"),
                ),
            ]
        )
        try:
            command_id = payload["Command"]["CommandId"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise DevelopmentEnvironmentError("SSM send-command response is malformed") from error
        if not isinstance(command_id, str):
            raise DevelopmentEnvironmentError("SSM command ID is not text")
        return command_id

    def ssm_shell_run(self, shell_command_list: list[str]) -> None:
        """Run one SSM shell command and stream its completed output."""

        payload = self.ssm_shell_result_get(shell_command_list)
        print(payload.get("StandardOutputContent", ""), end="")
        error_text = payload.get("StandardErrorContent", "")
        if error_text:
            print(error_text, end="", file=os.sys.stderr)

    def ssm_shell_result_get(
        self,
        shell_command_list: list[str],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        """Run one SSM shell command and return its successful invocation."""

        effective_timeout_seconds = SSM_COMMAND_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        command_id = self.ssm_command_start(shell_command_list)
        instance_id = self._instance_id_get()
        t_deadline = self._clock.monotonic() + effective_timeout_seconds
        payload: dict[str, object] | None = None
        while self._clock.monotonic() < t_deadline:
            payload = self._ssm_command_invocation_payload_get(
                command_id=command_id,
                instance_id=instance_id,
            )
            if payload is None or payload.get("Status") in {
                "Delayed",
                "InProgress",
                "Pending",
            }:
                self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
                continue
            break
        if payload is None or payload.get("Status") in {
            "Delayed",
            "InProgress",
            "Pending",
        }:
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} did not finish within "
                f"{effective_timeout_seconds} seconds; the remote command was "
                "not cancelled"
            )
        if payload.get("Status") != "Success":
            raise DevelopmentEnvironmentError(f"SSM command {command_id} failed with {payload.get('Status')}")
        return payload

    def _ssm_command_invocation_payload_get(
        self,
        *,
        command_id: str,
        instance_id: str,
    ) -> dict[str, object] | None:
        """Inspect one invocation while tolerating its registration delay."""

        result = self._aws.run(
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout).strip()
            if "InvocationDoesNotExist" in error_text:
                return None
            raise DevelopmentEnvironmentError(
                f"Unable to inspect SSM command {command_id}: " f"{error_text or f'exit {result.returncode}'}"
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(f"SSM command {command_id} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(f"SSM command {command_id} returned unexpected JSON")
        return payload


class SshControlSession:
    """Own one ephemeral key and multiplexed SSH-over-SSM control connection."""

    def __init__(self, *, transport: DevelopmentSsmTransport) -> None:
        """Bind the session to one transport owner."""

        self._transport = transport
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        """Publish an ephemeral key and open one multiplexed SSH session."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        temporary_root_path = Path(self._temporary_directory.name)
        private_key_path = temporary_root_path / "id_ed25519"
        control_path = temporary_root_path / "control"
        self._transport._runner.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(private_key_path),
            ]
        )
        instance_id = self._transport._instance_id_get()
        instance_payload = self._transport._aws.json_get(["ec2", "describe-instances", "--instance-ids", instance_id])
        try:
            availability_zone = instance_payload["Reservations"][0]["Instances"][0]["Placement"][
                "AvailabilityZone"
            ]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError("EC2 availability zone response is malformed") from error
        if not isinstance(availability_zone, str):
            raise DevelopmentEnvironmentError("EC2 availability zone is not text")
        self._transport._aws.run(
            [
                "ec2-instance-connect",
                "send-ssh-public-key",
                "--instance-id",
                instance_id,
                "--instance-os-user",
                "ubuntu",
                "--ssh-public-key",
                f"file://{private_key_path}.pub",
                "--availability-zone",
                availability_zone,
            ]
        )
        proxy_command = (
            "aws ssm start-session "
            f"--profile {shlex.quote(self._transport._aws_profile)} "
            f"--region {shlex.quote(self._transport._aws_region)} "
            f"--target {shlex.quote(instance_id)} "
            "--document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
        )
        config_path = temporary_root_path / "config"
        config_path.write_text(
            "\n".join(
                [
                    f"Host {self._transport._identity.instance_name}",
                    f"  HostName {instance_id}",
                    "  User ubuntu",
                    f"  IdentityFile {private_key_path}",
                    "  IdentitiesOnly yes",
                    "  StrictHostKeyChecking accept-new",
                    ("  UserKnownHostsFile " f"{temporary_root_path / 'known_hosts'}"),
                    f"  ProxyCommand {proxy_command}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._transport._runner.run(
            [
                "ssh",
                "-F",
                str(config_path),
                "-M",
                "-N",
                "-f",
                "-o",
                f"ControlPath={control_path}",
                "-o",
                "ControlPersist=600",
                self._transport._identity.instance_name,
            ]
        )
        return control_path

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Close the control connection and delete ephemeral key material."""

        del exc_type, exc_value, traceback
        if self._temporary_directory is None:
            return
        control_path = Path(self._temporary_directory.name) / "control"
        self._transport._runner.run(
            [
                "ssh",
                "-S",
                str(control_path),
                "-O",
                "exit",
                self._transport._identity.instance_name,
            ],
            check=False,
        )
        self._temporary_directory.cleanup()
        self._temporary_directory = None
