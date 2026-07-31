"""CloudFormation planning, safety guards, application, and drift validation."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from tool.lib.development_environment_error import DevelopmentEnvironmentError

CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT = 51_200
CLOUDFORMATION_S3_TEMPLATE_MAX_BYTE_COUNT = 1_048_576
STACK_POLL_INTERVAL_SECONDS = 5
STACK_TIMEOUT_SECONDS = 3600


class AwsClientProtocol(Protocol):
    """AWS CLI surface required by CloudFormation management."""

    def run(
        self,
        aws_argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command."""

    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response."""


class ClockProtocol(Protocol):
    """UTC and monotonic time surface required by stack operations."""

    def now(self):
        """Return the current UTC instant."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for one duration."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment identities required by stack operations."""

    data_plane_stack_name: str
    environment_name: str


class CommandRunnerProtocol(Protocol):
    """Local process boundary required by template linting."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one local command."""


def _is_data_lake_settings_parameter_change_safe(summary: Mapping[str, object]) -> bool:
    """Return whether one change is the documented in-place Parameters update."""

    return (
        summary.get("action") == "Modify"
        and summary.get("replacement") == "Conditional"
        and summary.get("resource_type") == "AWS::LakeFormation::DataLakeSettings"
        and summary.get("detail_list")
        == [
            {
                "ChangeSource": "DirectModification",
                "Evaluation": "Static",
                "Target": {
                    "Attribute": "Properties",
                    "Name": "Parameters",
                    "RequiresRecreation": "Conditionally",
                },
            }
        ]
    )


class DevelopmentStackManager:
    """Own all CloudFormation API behavior and physical-identity guards."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        aws_region: str,
        clock: ClockProtocol,
        identity: EnvironmentIdentityProtocol,
        project_root_path: Path,
        runner: CommandRunnerProtocol,
    ) -> None:
        """Bind stack management to one exact AWS development environment."""

        self._aws = aws
        self._aws_region = aws_region
        self._clock = clock
        self._identity = identity
        self._project_root_path = project_root_path
        self._runner = runner

    def apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
        protected_identity_logical_id_set: Collection[str] = (),
    ) -> None:
        """Plan, guard, execute, and verify one CloudFormation change set."""

        stack_payload = self.payload_get(stack_name, is_required=False)
        change_set_type = "UPDATE" if stack_payload else "CREATE"
        change_set_name = f"codex-{self._clock.now().strftime('%Y%m%d%H%M%S%f')}"
        command_list = [
            "cloudformation",
            "create-change-set",
            "--stack-name",
            stack_name,
            "--change-set-name",
            change_set_name,
            "--change-set-type",
            change_set_type,
            *self._template_argument_list_get(template_path),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--tags",
            "Key=Project,Value=workflow-control-center",
            "Key=Environment,Value=development",
            f"Key=EnvironmentName,Value={self._identity.environment_name}",
            "Key=ManagedBy,Value=CloudFormation",
        ]
        if stack_payload:
            current_parameter_by_name_map = self.parameter_by_name_map_get(stack_name)
            current_parameter_by_name_map.update(parameter_by_name_map)
            parameter_by_name_map = current_parameter_by_name_map
        if parameter_by_name_map:
            command_list.append("--parameters")
            for parameter_name, parameter_value in sorted(parameter_by_name_map.items()):
                command_list.append(f"ParameterKey={parameter_name}," f"ParameterValue={parameter_value}")
        self._aws.run(command_list)
        wait_result = self._aws.run(
            [
                "cloudformation",
                "wait",
                "change-set-create-complete",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ],
            check=False,
        )
        change_set_payload = self._aws.json_get(
            [
                "cloudformation",
                "describe-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )
        if wait_result.returncode != 0:
            reason = str(change_set_payload.get("StatusReason", ""))
            if "didn't contain changes" in reason:
                self._change_set_delete(stack_name, change_set_name)
                print(f"OK: stack {stack_name} has no changes")
                return
            raise DevelopmentEnvironmentError(f"Change set {stack_name}/{change_set_name} failed: {reason}")
        change_list = change_set_payload.get("Changes", [])
        if not isinstance(change_list, list):
            raise DevelopmentEnvironmentError(f"Change set {stack_name}/{change_set_name} is malformed")
        change_summary_list: list[dict[str, object]] = []
        for change_payload in change_list:
            resource_change = change_payload.get("ResourceChange") if isinstance(change_payload, dict) else None
            if not isinstance(resource_change, dict):
                raise DevelopmentEnvironmentError(f"Change set {stack_name}/{change_set_name} is malformed")
            change_summary_list.append(
                {
                    "action": resource_change.get("Action"),
                    "logical_resource_id": resource_change.get("LogicalResourceId"),
                    "replacement": resource_change.get(
                        "Replacement",
                        "False",
                    ),
                    "resource_type": resource_change.get("ResourceType"),
                    "detail_list": resource_change.get("Details", []),
                }
            )
        print(
            json.dumps(
                {
                    "change_set": change_set_name,
                    "changes": change_summary_list,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if must_preserve_resource:
            violation_logical_id_list = self._stable_data_change_violation_list_get(change_summary_list)
            if violation_logical_id_list:
                self._change_set_delete(stack_name, change_set_name)
                raise DevelopmentEnvironmentError(
                    "Stable data-plane change would remove or replace " + ", ".join(violation_logical_id_list)
                )
        protected_violation_list = self._protected_identity_change_violation_list_get(
            change_summary_list=change_summary_list,
            protected_identity_logical_id_set=(protected_identity_logical_id_set),
        )
        if protected_violation_list:
            self._change_set_delete(stack_name, change_set_name)
            raise DevelopmentEnvironmentError(
                "Ordinary compute apply would replace a protected identity: " + ", ".join(protected_violation_list)
            )
        self._aws.run(
            [
                "cloudformation",
                "execute-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )
        wait_name = "stack-update-complete" if change_set_type == "UPDATE" else "stack-create-complete"
        self._aws.run(
            [
                "cloudformation",
                "wait",
                wait_name,
                "--stack-name",
                stack_name,
            ]
        )
        if self.payload_get(
            stack_name,
            is_required=True,
        ).get("StackStatus") not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }:
            raise DevelopmentEnvironmentError(f"Stack {stack_name} did not reach a complete state")

    def drift_validate(self, stack_name: str) -> None:
        """Prove one stack is complete and in sync."""

        stack_payload = self.payload_get(stack_name, is_required=True)
        if stack_payload.get("StackStatus") not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }:
            raise DevelopmentEnvironmentError(f"Stack {stack_name} is not in a complete operational state")
        if not self.output_by_name_map_get(stack_name):
            raise DevelopmentEnvironmentError(f"Stack {stack_name} has no validated outputs")
        payload = self._aws.json_get(
            [
                "cloudformation",
                "detect-stack-drift",
                "--stack-name",
                stack_name,
            ]
        )
        drift_detection_id = payload.get("StackDriftDetectionId")
        if not isinstance(drift_detection_id, str):
            raise DevelopmentEnvironmentError(f"Stack {stack_name} drift detection ID is missing")
        t_deadline = self._clock.monotonic() + STACK_TIMEOUT_SECONDS
        while self._clock.monotonic() < t_deadline:
            status_payload = self._aws.json_get(
                [
                    "cloudformation",
                    "describe-stack-drift-detection-status",
                    "--stack-drift-detection-id",
                    drift_detection_id,
                ]
            )
            detection_status = status_payload.get("DetectionStatus")
            if detection_status == "DETECTION_COMPLETE":
                if status_payload.get("StackDriftStatus") != "IN_SYNC":
                    raise DevelopmentEnvironmentError(f"Stack {stack_name} is not IN_SYNC")
                print(f"OK: stack {stack_name} drift is IN_SYNC")
                return
            if detection_status == "DETECTION_FAILED":
                raise DevelopmentEnvironmentError(f"Stack {stack_name} drift detection failed")
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(f"Stack {stack_name} drift detection timed out")

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return validated stack outputs keyed by logical output name."""

        stack_payload = self.payload_get(stack_name, is_required=True)
        output_list = stack_payload.get("Outputs", [])
        if not isinstance(output_list, list):
            raise DevelopmentEnvironmentError(f"Stack {stack_name} Outputs are malformed")
        output_by_name_map: dict[str, str] = {}
        for output_payload in output_list:
            if not isinstance(output_payload, dict):
                raise DevelopmentEnvironmentError(f"Stack {stack_name} Outputs are malformed")
            output_name = output_payload.get("OutputKey")
            output_value = output_payload.get("OutputValue")
            if not isinstance(output_name, str) or not isinstance(
                output_value,
                str,
            ):
                raise DevelopmentEnvironmentError(f"Stack {stack_name} output is malformed")
            output_by_name_map[output_name] = output_value
        return output_by_name_map

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return validated effective stack parameters."""

        stack_payload = self.payload_get(stack_name, is_required=True)
        parameter_list = stack_payload.get("Parameters", [])
        if not isinstance(parameter_list, list):
            raise DevelopmentEnvironmentError(f"Stack {stack_name} Parameters are malformed")
        parameter_by_name_map: dict[str, str] = {}
        for parameter_payload in parameter_list:
            if not isinstance(parameter_payload, dict):
                raise DevelopmentEnvironmentError(f"Stack {stack_name} Parameters are malformed")
            parameter_name = parameter_payload.get("ParameterKey")
            parameter_value = parameter_payload.get("ParameterValue")
            if not isinstance(parameter_name, str) or not isinstance(
                parameter_value,
                str,
            ):
                raise DevelopmentEnvironmentError(f"Stack {stack_name} parameter is malformed")
            parameter_by_name_map[parameter_name] = parameter_value
        return parameter_by_name_map

    def payload_get(
        self,
        stack_name: str,
        *,
        is_required: bool,
    ) -> dict[str, object]:
        """Return one exact stack object, or empty when optional and absent."""

        result = self._aws.run(
            [
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack_name,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            if not is_required and "does not exist" in result.stderr:
                return {}
            raise DevelopmentEnvironmentError(
                f"Unable to describe stack {stack_name}: " f"{(result.stderr or result.stdout).strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(f"Stack {stack_name} response is invalid") from error
        stack_list = payload.get("Stacks", []) if isinstance(payload, dict) else []
        if not isinstance(stack_list, list) or len(stack_list) != 1 or not isinstance(stack_list[0], dict):
            raise DevelopmentEnvironmentError(f"Stack {stack_name} response is malformed")
        return stack_list[0]

    def resource_id_by_logical_name_map_get(
        self,
        stack_name: str,
    ) -> dict[str, str]:
        """Return physical resource IDs keyed by logical resource IDs."""

        if not self.payload_get(stack_name, is_required=False):
            return {}
        payload = self._aws.json_get(
            [
                "cloudformation",
                "list-stack-resources",
                "--stack-name",
                stack_name,
            ]
        )
        resource_list = payload.get("StackResourceSummaries", [])
        if not isinstance(resource_list, list):
            raise DevelopmentEnvironmentError(f"Stack {stack_name} resources are malformed")
        resource_id_by_logical_name_map: dict[str, str] = {}
        for resource_payload in resource_list:
            if not isinstance(resource_payload, dict):
                raise DevelopmentEnvironmentError(f"Stack {stack_name} resource is malformed")
            logical_name = resource_payload.get("LogicalResourceId")
            resource_id = resource_payload.get("PhysicalResourceId")
            if not isinstance(logical_name, str) or not isinstance(
                resource_id,
                str,
            ):
                raise DevelopmentEnvironmentError(f"Stack {stack_name} resource identity is malformed")
            resource_id_by_logical_name_map[logical_name] = resource_id
        return resource_id_by_logical_name_map

    def template_validate(self, template_path: Path) -> None:
        """Run local schema lint and remote CloudFormation validation."""

        self._runner.run(
            [
                str(self._project_root_path / ".venv" / "bin" / "cfn-lint"),
                str(template_path),
            ]
        )
        self._aws.run(
            [
                "cloudformation",
                "validate-template",
                *self._template_argument_list_get(template_path),
            ]
        )

    @staticmethod
    def existing_resource_identity_validate(
        *,
        current_resource_id_by_logical_name_map: Mapping[str, str],
        previous_resource_id_by_logical_name_map: Mapping[str, str],
    ) -> None:
        """Prove an update preserved every pre-existing physical identity."""

        changed_logical_id_list = sorted(
            logical_id
            for logical_id, previous_physical_id in (previous_resource_id_by_logical_name_map.items())
            if current_resource_id_by_logical_name_map.get(logical_id) != previous_physical_id
        )
        if changed_logical_id_list:
            raise DevelopmentEnvironmentError(
                "Stable data-plane physical resource identity changed: " + ", ".join(changed_logical_id_list)
            )

    def _template_argument_list_get(
        self,
        template_path: Path,
    ) -> list[str]:
        """Return inline or content-addressed S3 template arguments."""

        template_bytes = template_path.read_bytes()
        template_byte_count = len(template_bytes)
        if template_byte_count <= CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT:
            return ["--template-body", f"file://{template_path}"]
        if template_byte_count > CLOUDFORMATION_S3_TEMPLATE_MAX_BYTE_COUNT:
            raise DevelopmentEnvironmentError(f"CloudFormation template {template_path} exceeds the 1 MiB S3 limit")
        output_by_name_map = self.output_by_name_map_get(self._identity.data_plane_stack_name)
        bucket_name = output_by_name_map.get("ObservabilityBucketName")
        if not bucket_name:
            raise DevelopmentEnvironmentError(
                "Oversized CloudFormation template requires the retained " "Observability artifact bucket"
            )
        digest_bytes = hashlib.sha256(template_bytes).digest()
        digest = digest_bytes.hex()
        checksum_sha256 = base64.b64encode(digest_bytes).decode("ascii")
        object_key = "cloudformation-template/" f"{self._identity.environment_name}/{digest}.yaml"
        head_argument_list = [
            "s3api",
            "head-object",
            "--bucket",
            bucket_name,
            "--key",
            object_key,
            "--checksum-mode",
            "ENABLED",
            "--output",
            "json",
        ]
        head_result = self._aws.run(head_argument_list, check=False)
        if head_result.returncode != 0:
            error_text = (head_result.stderr or head_result.stdout).strip()
            if not any(marker in error_text for marker in ("(404)", "NoSuchKey", "Not Found")):
                raise DevelopmentEnvironmentError(
                    "Unable to inspect CloudFormation template artifact: "
                    + (error_text or f"exit {head_result.returncode}")
                )
            self._aws.run(
                [
                    "s3api",
                    "put-object",
                    "--bucket",
                    bucket_name,
                    "--key",
                    object_key,
                    "--body",
                    str(template_path),
                    "--checksum-sha256",
                    checksum_sha256,
                    "--content-type",
                    "application/yaml",
                    "--metadata",
                    f"sha256={digest}",
                ]
            )
        head_payload = self._aws.json_get(head_argument_list)
        metadata = head_payload.get("Metadata")
        if (
            head_payload.get("ContentLength") != template_byte_count
            or head_payload.get("ChecksumSHA256") != checksum_sha256
            or not isinstance(metadata, dict)
            or metadata.get("sha256") != digest
        ):
            raise DevelopmentEnvironmentError("CloudFormation template artifact identity does not match local bytes")
        template_url = f"https://{bucket_name}.s3.{self._aws_region}.amazonaws.com/" f"{object_key}"
        return ["--template-url", template_url]

    def _change_set_delete(
        self,
        stack_name: str,
        change_set_name: str,
    ) -> None:
        """Delete one unexecuted deterministic change set."""

        self._aws.run(
            [
                "cloudformation",
                "delete-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )

    @staticmethod
    def _protected_identity_change_violation_list_get(
        *,
        change_summary_list: Sequence[Mapping[str, object]],
        protected_identity_logical_id_set: Collection[str],
    ) -> list[str]:
        """Return protected resources whose physical identity may change."""

        return sorted(
            str(summary.get("logical_resource_id"))
            for summary in change_summary_list
            if summary.get("logical_resource_id") in protected_identity_logical_id_set
            and (summary.get("action") == "Remove" or summary.get("replacement") != "False")
        )

    @staticmethod
    def _stable_data_change_violation_list_get(
        change_summary_list: list[dict[str, object]],
    ) -> list[str]:
        """Return data-plane changes not proven identity-preserving."""

        summary_by_logical_id_map = {
            str(summary.get("logical_resource_id")): summary for summary in change_summary_list
        }
        conditional_safety_by_logical_id_map: dict[str, bool] = {}

        def conditional_change_is_safe(
            logical_resource_id: str,
            proving_logical_id_set: frozenset[str] = frozenset(),
        ) -> bool:
            if logical_resource_id in conditional_safety_by_logical_id_map:
                return conditional_safety_by_logical_id_map[logical_resource_id]
            if logical_resource_id in proving_logical_id_set:
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            summary = summary_by_logical_id_map.get(logical_resource_id)
            if summary is None or summary.get("action") != "Modify" or summary.get("replacement") != "Conditional":
                return False
            detail_list = summary.get("detail_list")
            if not isinstance(detail_list, list) or not detail_list:
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            if _is_data_lake_settings_parameter_change_safe(summary):
                conditional_safety_by_logical_id_map[logical_resource_id] = True
                return True
            replacement_detail_list = [
                detail
                for detail in detail_list
                if not isinstance(detail, dict)
                or (
                    isinstance(detail.get("Target"), dict)
                    and detail["Target"].get("RequiresRecreation") in {"Always", "Conditionally"}
                )
            ]
            if not replacement_detail_list:
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            next_proving_logical_id_set = proving_logical_id_set | {logical_resource_id}
            for detail in replacement_detail_list:
                if not isinstance(detail, dict):
                    conditional_safety_by_logical_id_map[logical_resource_id] = False
                    return False
                causing_logical_id = str(detail.get("CausingEntity")).split(".", maxsplit=1)[0]
                causing_summary = summary_by_logical_id_map.get(causing_logical_id)
                if (
                    detail.get("Evaluation") != "Dynamic"
                    or detail.get("ChangeSource") not in {"ResourceAttribute", "ResourceReference"}
                    or causing_summary is None
                    or causing_summary.get("action") != "Modify"
                ):
                    conditional_safety_by_logical_id_map[logical_resource_id] = False
                    return False
                causing_replacement = causing_summary.get("replacement")
                if causing_replacement == "False":
                    continue
                if causing_replacement == "Conditional" and conditional_change_is_safe(
                    causing_logical_id,
                    next_proving_logical_id_set,
                ):
                    continue
                conditional_safety_by_logical_id_map[logical_resource_id] = False
                return False
            conditional_safety_by_logical_id_map[logical_resource_id] = True
            return True

        violation_logical_id_list: list[str] = []
        for summary in change_summary_list:
            logical_resource_id = str(summary.get("logical_resource_id"))
            action = summary.get("action")
            replacement = summary.get("replacement")
            if action == "Remove" or replacement == "True":
                violation_logical_id_list.append(logical_resource_id)
            elif replacement == "Conditional" and not conditional_change_is_safe(logical_resource_id):
                violation_logical_id_list.append(logical_resource_id)
        return sorted(set(violation_logical_id_list))
