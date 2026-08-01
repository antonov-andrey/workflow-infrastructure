"""Validate and invoke the exact environment-owned host bootstrap document."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

BOOTSTRAP_COMMAND_TIMEOUT_SECONDS = 3600
BOOTSTRAP_POLL_INTERVAL_SECONDS = 5
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_IN_PROGRESS_STATUS_SET = frozenset({"Delayed", "InProgress", "Pending"})


class AwsClientProtocol(Protocol):
    """AWS operations required by exact document invocation."""

    def json_get(self, argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI operation and decode its object result."""

    def run(self, argument_list: Sequence[str], *, check: bool = True) -> object:
        """Run one AWS CLI operation."""


class ClockProtocol(Protocol):
    """Monotonic wait boundary required by invocation polling."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a bounded duration."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment values supplied to the bootstrap document."""

    environment_name: str
    host_retained_root_path: object


class StackManagerProtocol(Protocol):
    """Compute stack state required by document invocation."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack parameters."""


class DevelopmentHostBootstrapInvocation:
    """Own document verification, exact SendCommand, and completion proof."""

    def __init__(
        self,
        *,
        account_id: str,
        aws: AwsClientProtocol,
        clock: ClockProtocol,
        compute_stack_name: str,
        identity: EnvironmentIdentityProtocol,
        stack: StackManagerProtocol,
    ) -> None:
        """Bind invocation to one environment compute stack."""

        self._account_id = account_id
        self._aws = aws
        self._clock = clock
        self._compute_stack_name = compute_stack_name
        self._identity = identity
        self._stack = stack

    def run(self) -> None:
        """Validate and run the exact current document on the current instance."""

        output_by_name = self._stack.output_by_name_map_get(self._compute_stack_name)
        parameter_by_name = self._stack.parameter_by_name_map_get(
            self._compute_stack_name
        )
        document_identity = self._document_identity_validate(
            output_by_name=output_by_name,
            parameter_by_name=parameter_by_name,
        )
        command_id = self._command_start(
            document_identity=document_identity,
            output_by_name=output_by_name,
            parameter_by_name=parameter_by_name,
        )
        self._command_success_wait(
            command_id=command_id,
            instance_id=self._required_text(output_by_name, "InstanceId"),
        )

    def _document_identity_validate(
        self,
        *,
        output_by_name: Mapping[str, str],
        parameter_by_name: Mapping[str, str],
    ) -> tuple[str, str, str]:
        """Return exact name, numeric version, and system SHA-256."""

        document_name = self._required_text(output_by_name, "HostBootstrapDocumentName")
        description = self._aws.json_get(
            ["ssm", "describe-document", "--name", document_name]
        ).get("Document")
        if not isinstance(description, dict):
            raise DevelopmentEnvironmentError(
                "Bootstrap document description is malformed"
            )
        version = description.get("LatestVersion")
        document_hash = description.get("Hash")
        if (
            description.get("Name") != document_name
            or description.get("Owner") != self._account_id
            or description.get("Status") != "Active"
            or description.get("DocumentType") != "Command"
            or description.get("DocumentFormat") != "YAML"
            or not isinstance(version, str)
            or not version.isdigit()
            or description.get("DocumentVersion") != version
            or description.get("DefaultVersion") != version
            or description.get("HashType") != "Sha256"
            or not isinstance(document_hash, str)
            or _SHA256_PATTERN.fullmatch(document_hash) is None
            or description.get("VersionName") not in {None, ""}
        ):
            raise DevelopmentEnvironmentError(
                "Bootstrap document is not the exact active latest/default Command version"
            )
        document_payload = self._aws.json_get(
            [
                "ssm",
                "get-document",
                "--name",
                document_name,
                "--document-version",
                version,
                "--document-format",
                "JSON",
            ]
        )
        if (
            document_payload.get("Name") != document_name
            or document_payload.get("DocumentVersion") != version
            or document_payload.get("Status") != "Active"
            or document_payload.get("DocumentType") != "Command"
            or document_payload.get("DocumentFormat") != "JSON"
            or document_payload.get("VersionName") not in {None, ""}
        ):
            raise DevelopmentEnvironmentError(
                "Bootstrap document version response is malformed"
            )
        content_text = document_payload.get("Content")
        if not isinstance(content_text, str):
            raise DevelopmentEnvironmentError("Bootstrap document content is not text")
        try:
            content = json.loads(content_text)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Bootstrap document content is not JSON"
            ) from error
        self._document_content_validate(content, parameter_by_name=parameter_by_name)
        return document_name, version, document_hash

    def _document_content_validate(
        self,
        content: object,
        *,
        parameter_by_name: Mapping[str, str],
    ) -> None:
        """Validate the closed security-relevant document content."""

        if not isinstance(content, dict) or set(content) != {
            "description",
            "mainSteps",
            "parameters",
            "schemaVersion",
        }:
            raise DevelopmentEnvironmentError(
                "Bootstrap document content has an unexpected shape"
            )
        if content.get("schemaVersion") != "2.2":
            raise DevelopmentEnvironmentError("Bootstrap document schema is unexpected")
        if content.get("description") != (
            "Install one exact content-addressed development host release."
        ):
            raise DevelopmentEnvironmentError(
                "Bootstrap document description is unexpected"
            )
        parameter_map = content.get("parameters")
        if parameter_map != {
            "Architecture": {
                "type": "String",
                "allowedPattern": "^(amd64|arm64)$",
            },
            "EnvironmentName": {
                "type": "String",
                "allowedPattern": "^[a-z][a-z0-9]{0,15}$",
            },
            "RetainedRootPath": {
                "type": "String",
                "allowedPattern": "^/srv/workflow-control-center(-w[0-9a-f]{15})?$",
            },
            "RetainedVolumeId": {
                "type": "String",
                "allowedPattern": "^vol-[0-9a-f]+$",
            },
        }:
            raise DevelopmentEnvironmentError(
                "Bootstrap document parameter contract is unexpected"
            )
        step_list = content.get("mainSteps")
        if not isinstance(step_list, list) or len(step_list) != 3:
            raise DevelopmentEnvironmentError(
                "Bootstrap document step contract is unexpected"
            )
        expected_download_list = (
            (
                "downloadPythonRuntime",
                "/var/lib/development-bootstrap/download/python.tar.gz",
                self._required_text(parameter_by_name, "HostPythonArtifactSourceInfo"),
            ),
            (
                "downloadBootstrapBundle",
                "/var/lib/development-bootstrap/download/bootstrap.tar.gz",
                self._required_text(parameter_by_name, "HostBootstrapBundleSourceInfo"),
            ),
        )
        for step, (name, destination_path, source_info) in zip(
            step_list[:2],
            expected_download_list,
            strict=True,
        ):
            if not isinstance(step, dict) or set(step) != {"action", "inputs", "name"}:
                raise DevelopmentEnvironmentError(
                    "Bootstrap download step shape is unexpected"
                )
            if step.get("action") != "aws:downloadContent" or step.get("name") != name:
                raise DevelopmentEnvironmentError(
                    "Bootstrap download step identity is unexpected"
                )
            if step.get("inputs") != {
                "destinationPath": destination_path,
                "sourceInfo": source_info,
                "sourceType": "S3",
            }:
                raise DevelopmentEnvironmentError(
                    "Bootstrap download source is unexpected"
                )
        run_step = step_list[2]
        if (
            not isinstance(run_step, dict)
            or set(run_step) != {"action", "inputs", "name"}
            or run_step.get("action") != "aws:runShellScript"
            or run_step.get("name") != "runVerifiedBootstrap"
        ):
            raise DevelopmentEnvironmentError(
                "Bootstrap launcher step identity is unexpected"
            )
        inputs = run_step.get("inputs")
        if not isinstance(inputs, dict) or set(inputs) != {
            "runCommand",
            "timeoutSeconds",
        }:
            raise DevelopmentEnvironmentError(
                "Bootstrap launcher inputs are unexpected"
            )
        command_list = inputs.get("runCommand")
        if (
            inputs.get("timeoutSeconds") != "3600"
            or not isinstance(command_list, list)
            or len(command_list) != 1
            or not isinstance(command_list[0], str)
        ):
            raise DevelopmentEnvironmentError(
                "Bootstrap launcher command is unexpected"
            )
        launcher = command_list[0]
        if launcher != self._launcher_get(parameter_by_name):
            raise DevelopmentEnvironmentError(
                "Bootstrap launcher differs from the exact current contract"
            )

    def _launcher_get(self, parameter_by_name: Mapping[str, str]) -> str:
        """Render the one accepted minimal checksum-verifying launcher."""

        python_sha256 = self._required_sha256(
            parameter_by_name, "HostPythonArtifactSha256"
        )
        bundle_sha256 = self._required_sha256(
            parameter_by_name, "HostBootstrapBundleSha256"
        )
        manifest_sha256 = self._required_sha256(
            parameter_by_name, "HostBootstrapManifestSha256"
        )
        return f"""set -eu
umask 077
root=/var/lib/development-bootstrap
python_archive="$root/download/python.tar.gz"
bundle_archive="$root/download/bootstrap.tar.gz"
printf '%s  %s\\n' '{python_sha256}' "$python_archive" | sha256sum --check --strict
printf '%s  %s\\n' '{bundle_sha256}' "$bundle_archive" | sha256sum --check --strict
python_root="/opt/python/bootstrap-{python_sha256}"
bundle_root="$root/release/{bundle_sha256}"
if [ ! -x "$python_root/bin/python3.14" ]; then
  python_new="$python_root.new"
  rm -rf "$python_root"
  rm -rf "$python_new"
  install -d -m 0755 "$python_new"
  tar --extract --gzip --file "$python_archive" --directory "$python_new" --strip-components=1
  mv "$python_new" "$python_root"
fi
if [ ! -x "$bundle_root/host_bootstrap.py" ]; then
  bundle_new="$bundle_root.new"
  rm -rf "$bundle_root"
  rm -rf "$bundle_new"
  install -d -m 0755 "$bundle_new"
  tar --extract --gzip --file "$bundle_archive" --directory "$bundle_new"
  mv "$bundle_new" "$bundle_root"
fi
exec "$python_root/bin/python3.14" -B "$bundle_root/host_bootstrap.py" \\
  --architecture '{{{{ Architecture }}}}' \\
  --bundle-root "$bundle_root" \\
  --bundle-manifest-sha256 '{manifest_sha256}' \\
  --environment-name '{{{{ EnvironmentName }}}}' \\
  --python-runtime "$python_root" \\
  --retained-root '{{{{ RetainedRootPath }}}}' \\
  --retained-volume-id '{{{{ RetainedVolumeId }}}}'
"""

    def _command_start(
        self,
        *,
        document_identity: tuple[str, str, str],
        output_by_name: Mapping[str, str],
        parameter_by_name: Mapping[str, str],
    ) -> str:
        """Start one exact hash-bound invocation."""

        document_name, version, document_hash = document_identity
        instance_id = self._required_text(output_by_name, "InstanceId")
        architecture = self._required_text(output_by_name, "ComputeArchitecture")
        retained_volume_id = self._required_text(output_by_name, "RetainedVolumeId")
        bundle_sha256 = self._required_sha256(
            parameter_by_name,
            "HostBootstrapBundleSha256",
        )
        payload = self._aws.json_get(
            [
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--document-name",
                document_name,
                "--document-version",
                version,
                "--document-hash",
                document_hash,
                "--document-hash-type",
                "Sha256",
                "--comment",
                f"bootstrap:{self._identity.environment_name}:{bundle_sha256[:16]}",
                "--parameters",
                json.dumps(
                    {
                        "Architecture": [architecture],
                        "EnvironmentName": [self._identity.environment_name],
                        "RetainedRootPath": [
                            str(self._identity.host_retained_root_path)
                        ],
                        "RetainedVolumeId": [retained_volume_id],
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        command = payload.get("Command")
        command_id = command.get("CommandId") if isinstance(command, dict) else None
        if not isinstance(command_id, str) or not command_id:
            raise DevelopmentEnvironmentError(
                "Bootstrap SendCommand response is malformed"
            )
        return command_id

    def _command_success_wait(self, *, command_id: str, instance_id: str) -> None:
        """Require one successful invocation without emitting remote content."""

        t_deadline = self._clock.monotonic() + BOOTSTRAP_COMMAND_TIMEOUT_SECONDS
        status: object = "Pending"
        while self._clock.monotonic() < t_deadline:
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
            returncode = getattr(result, "returncode", None)
            stdout = getattr(result, "stdout", "")
            stderr = getattr(result, "stderr", "")
            if returncode != 0:
                error_text = f"{stderr or stdout}"
                if "InvocationDoesNotExist" in error_text:
                    self._clock.sleep(BOOTSTRAP_POLL_INTERVAL_SECONDS)
                    continue
                raise DevelopmentEnvironmentError(
                    f"Unable to inspect bootstrap command {command_id}"
                )
            try:
                payload = json.loads(stdout or "{}")
            except json.JSONDecodeError as error:
                raise DevelopmentEnvironmentError(
                    "Bootstrap command invocation response is malformed"
                ) from error
            if not isinstance(payload, dict):
                raise DevelopmentEnvironmentError(
                    "Bootstrap command invocation response is malformed"
                )
            status = payload.get("Status")
            if status in _IN_PROGRESS_STATUS_SET:
                self._clock.sleep(BOOTSTRAP_POLL_INTERVAL_SECONDS)
                continue
            if status == "Success":
                print(
                    f"OK: exact host bootstrap command {command_id} completed on {instance_id}"
                )
                return
            break
        if status in _IN_PROGRESS_STATUS_SET:
            raise DevelopmentEnvironmentError(
                f"Bootstrap command {command_id} did not finish within "
                f"{BOOTSTRAP_COMMAND_TIMEOUT_SECONDS} seconds"
            )
        raise DevelopmentEnvironmentError(
            f"Bootstrap command {command_id} failed with status {status}"
        )

    @staticmethod
    def _required_text(value_by_name: Mapping[str, str], name: str) -> str:
        """Return one required non-empty text value."""

        value = value_by_name.get(name)
        if not isinstance(value, str) or not value:
            raise DevelopmentEnvironmentError(
                f"Required bootstrap value {name} is missing"
            )
        return value

    @classmethod
    def _required_sha256(cls, value_by_name: Mapping[str, str], name: str) -> str:
        """Return one required lowercase SHA-256 value."""

        value = cls._required_text(value_by_name, name)
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise DevelopmentEnvironmentError(
                f"Bootstrap value {name} is not a SHA-256"
            )
        return value
