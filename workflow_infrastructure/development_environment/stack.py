"""CloudFormation planning, safety guards, application, and drift validation."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.aws import (
    aws_cli_error_get,
    aws_cli_error_matches,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT = 51_200
CLOUDFORMATION_S3_TEMPLATE_MAX_BYTE_COUNT = 1_048_576
STACK_DRIFT_CHECKABLE_STATUS_SET = frozenset(
    {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
    }
)
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
        """Run one AWS CLI command.

        Args:
            aws_argument_list: Ordered AWS argument values.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """

    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response.

        Args:
            aws_argument_list: Ordered AWS argument values.

        Returns:
            Decoded AWS response object.
        """


class ClockProtocol(Protocol):
    """UTC and monotonic time surface required by stack operations."""

    def now(self):
        """Return the current UTC instant."""

    def monotonic(self) -> float:
        """Return monotonic seconds.

        Returns:
            The monotonic seconds.
        """

    def sleep(self, delay_seconds: float) -> None:
        """Wait for one duration.

        Args:
            delay_seconds: Delay in seconds.
        """


class EnvironmentIdentityProtocol(Protocol):
    """Environment identities required by stack operations."""

    data_plane_stack_name: str
    environment_name: str
    git_worktree: str


class CommandRunnerProtocol(Protocol):
    """Local process boundary required by template linting."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one local command.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """


def _can_add_primary_platform_data_lake_admin(
    *,
    allowed_primary_platform_data_lake_admin_arn: str | None,
    aws_account_id: str,
    existing_parameter_by_name_map: Mapping[str, str],
    parameter_by_name_map: Mapping[str, str],
    stack_name: str,
) -> bool:
    """Validate and report one exact fresh-primary administrator transition.

    Args:
        allowed_primary_platform_data_lake_admin_arn: Exact permitted fresh-primary administrator ARN.
        aws_account_id: Exact AWS account identity.
        existing_parameter_by_name_map: Existing stack parameters by name.
        parameter_by_name_map: Requested effective stack parameters by name.
        stack_name: Stack name.

    Returns:
        Whether the transition adds the primary platform administrator.

    Raises:
        DevelopmentEnvironmentError: The explicit transition permission does not match exact fresh-primary state.
    """

    if allowed_primary_platform_data_lake_admin_arn is None:
        return False
    expected_primary_platform_role_arn = f"arn:aws:iam::{aws_account_id}:role/platform-primary"
    current_primary_platform_role_arn = existing_parameter_by_name_map.get("PrimaryPlatformRoleArn")
    if (
        stack_name != "account-foundation"
        or allowed_primary_platform_data_lake_admin_arn != expected_primary_platform_role_arn
        or parameter_by_name_map.get("PrimaryPlatformRoleArn") != allowed_primary_platform_data_lake_admin_arn
        or current_primary_platform_role_arn not in {"", allowed_primary_platform_data_lake_admin_arn}
    ):
        raise DevelopmentEnvironmentError(
            "Primary platform Lake Formation administrator transition is not the exact fresh-primary addition"
        )
    return current_primary_platform_role_arn == ""


def _is_data_lake_settings_parameter_change_safe(summary: Mapping[str, object]) -> bool:
    """Return whether one change is the documented in-place Parameters update.

    Args:
        summary: Summary.

    Returns:
        Whether one change is the documented in-place Parameters update.
    """

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


def _is_data_lake_settings_primary_platform_admin_change_safe(
    summary: Mapping[str, object],
    *,
    can_add_primary_platform_data_lake_admin: bool,
) -> bool:
    """Return whether one change is the exact approved primary-admin addition.

    Args:
        summary: One CloudFormation resource-change summary.
        can_add_primary_platform_data_lake_admin: Whether the exact parameter transition was proven.

    Returns:
        Whether the change is the exact approved primary-admin addition.
    """

    if (
        not can_add_primary_platform_data_lake_admin
        or summary.get("action") != "Modify"
        or summary.get("logical_resource_id") != "DataLakeSettings"
        or summary.get("replacement") != "Conditional"
        or summary.get("resource_type") != "AWS::LakeFormation::DataLakeSettings"
    ):
        return False
    detail_list = summary.get("detail_list")
    if not isinstance(detail_list, list):
        return False
    expected_target = {
        "Attribute": "Properties",
        "Name": "Admins",
        "RequiresRecreation": "Conditionally",
    }
    expected_detail_list = [
        {
            "CausingEntity": "PrimaryPlatformRoleArn",
            "ChangeSource": "ParameterReference",
            "Evaluation": "Static",
            "Target": expected_target,
        },
        {
            "ChangeSource": "DirectModification",
            "Evaluation": "Dynamic",
            "Target": expected_target,
        },
    ]
    return sorted(json.dumps(detail, sort_keys=True) for detail in detail_list) == sorted(
        json.dumps(detail, sort_keys=True) for detail in expected_detail_list
    )


def _is_versioned_ssm_document_content_change_safe(
    summary: Mapping[str, object],
    *,
    versioned_document_logical_id_set: Collection[str],
) -> bool:
    """Return whether one explicitly versioned SSM document changes in place.

    CloudFormation reports ``AWS::SSM::Document.Content`` as conditionally
    replacing even when the template selects ``UpdateMethod: NewVersion``.
    Callers must explicitly designate the versioned document, and the change
    set must contain only the exact provider details produced by a content
    update and the optional first transition to ``NewVersion``.

    Args:
        summary: One CloudFormation resource-change summary.
        versioned_document_logical_id_set: Explicitly versioned SSM document logical identities.

    Returns:
        Whether the change is the narrowly supported versioned-content update.
    """

    if (
        summary.get("logical_resource_id") not in versioned_document_logical_id_set
        or summary.get("action") != "Modify"
        or summary.get("replacement") != "Conditional"
        or summary.get("resource_type") != "AWS::SSM::Document"
    ):
        return False
    detail_list = summary.get("detail_list")
    if not isinstance(detail_list, list) or not detail_list:
        return False
    expected_recreation_by_property_map = {
        "Content": "Conditionally",
        "UpdateMethod": "Never",
    }
    actual_property_name_set: set[str] = set()
    for detail in detail_list:
        if not isinstance(detail, dict):
            return False
        target = detail.get("Target")
        if not isinstance(target, dict):
            return False
        property_name = target.get("Name")
        if (
            detail.get("ChangeSource") != "DirectModification"
            or detail.get("Evaluation") != "Static"
            or target.get("Attribute") != "Properties"
            or not isinstance(property_name, str)
            or property_name in actual_property_name_set
            or target.get("RequiresRecreation") != expected_recreation_by_property_map.get(property_name)
        ):
            return False
        actual_property_name_set.add(property_name)
    return "Content" in actual_property_name_set


def _protected_identity_change_violation_list_get(
    *,
    change_summary_list: Sequence[Mapping[str, object]],
    protected_identity_logical_id_set: Collection[str],
) -> list[str]:
    """Return protected resources whose physical identity may change.

    Args:
        change_summary_list: Ordered change summary values.
        protected_identity_logical_id_set: Unique protected identity logical identity values.

    Returns:
        The protected resources whose physical identity may change.
    """

    return sorted(
        str(summary.get("logical_resource_id"))
        for summary in change_summary_list
        if summary.get("logical_resource_id") in protected_identity_logical_id_set
        and (summary.get("action") == "Remove" or summary.get("replacement") != "False")
    )


def _stable_data_change_violation_list_get(
    change_summary_list: list[dict[str, object]],
    *,
    can_add_primary_platform_data_lake_admin: bool = False,
    versioned_document_logical_id_set: Collection[str] = (),
) -> list[str]:
    """Return data-plane changes not proven identity-preserving.

    Args:
        change_summary_list: Ordered change summary values.
        can_add_primary_platform_data_lake_admin: Whether the exact parameter transition was proven.
        versioned_document_logical_id_set: Explicitly versioned SSM document logical identities.

    Returns:
        The data-plane changes not proven identity-preserving.
    """

    summary_by_logical_id_map = {str(summary.get("logical_resource_id")): summary for summary in change_summary_list}
    conditional_safety_by_logical_id_map: dict[str, bool] = {}

    def conditional_change_is_safe(
        logical_resource_id: str,
        proving_logical_id_set: frozenset[str] = frozenset(),
    ) -> bool:
        """Prove one conditional change is supported by unchanged identity owners.

        Args:
            logical_resource_id: Exact logical resource identity.
            proving_logical_id_set: Unique proving logical identity values.

        Returns:
            Whether all recursive identity proofs establish an in-place update.
        """

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
        if _is_data_lake_settings_primary_platform_admin_change_safe(
            summary,
            can_add_primary_platform_data_lake_admin=can_add_primary_platform_data_lake_admin,
        ):
            conditional_safety_by_logical_id_map[logical_resource_id] = True
            return True
        if _is_versioned_ssm_document_content_change_safe(
            summary,
            versioned_document_logical_id_set=versioned_document_logical_id_set,
        ):
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


class DevelopmentStackManager:
    """Own all CloudFormation API behavior and physical-identity guards."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        aws_account_id: str,
        aws_region: str,
        clock: ClockProtocol,
        identity: EnvironmentIdentityProtocol,
        project_root_path: Path,
        runner: CommandRunnerProtocol,
    ) -> None:
        """Bind stack management to one exact AWS development environment.

        Args:
            aws: Aws.
            aws_account_id: Exact AWS account identity.
            aws_region: Aws region.
            clock: Clock.
            identity: Identity.
            project_root_path: Exact filesystem path for project root.
            runner: Explicit command execution boundary.
        """

        self._aws = aws
        self._aws_account_id = aws_account_id
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
        allowed_primary_platform_data_lake_admin_arn: str | None = None,
        protected_identity_logical_id_set: Collection[str] = (),
        versioned_document_logical_id_set: Collection[str] = (),
    ) -> None:
        """Plan, guard, execute, and verify one CloudFormation change set.

        Args:
            stack_name: Stack name.
            template_path: Exact filesystem path for template.
            parameter_by_name_map: Parameter by name mapping.
            must_preserve_resource: Must preserve resource.
            allowed_primary_platform_data_lake_admin_arn: Exact permitted fresh-primary administrator ARN.
            protected_identity_logical_id_set: Unique protected identity logical identity values.
            versioned_document_logical_id_set: Explicitly versioned SSM document logical identities.
        """

        stack_payload = self.payload_get(stack_name, is_required=False)
        change_set_type = "UPDATE" if stack_payload else "CREATE"
        change_set_name = f"codex-{self._clock.now().strftime('%Y%m%d%H%M%S%f')}"
        template_argument_list = self._template_argument_list_get(template_path)
        command_list = [
            "cloudformation",
            "create-change-set",
            "--stack-name",
            stack_name,
            "--change-set-name",
            change_set_name,
            "--change-set-type",
            change_set_type,
            *template_argument_list,
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--tags",
            "Key=EnvironmentClass,Value=development",
            f"Key=EnvironmentName,Value={self._identity.environment_name}",
            "Key=ManagedBy,Value=CloudFormation",
        ]
        if self._identity.git_worktree:
            command_list.append(f"Key=git-worktree,Value={self._identity.git_worktree}")
        existing_parameter_by_name_map: dict[str, str] = {}
        if stack_payload:
            existing_parameter_by_name_map = self.parameter_by_name_map_get(stack_name)
            template_parameter_name_set = self._template_parameter_name_set_get(template_argument_list)
            merged_parameter_by_name_map = {
                parameter_name: parameter_value
                for parameter_name, parameter_value in existing_parameter_by_name_map.items()
                if parameter_name in template_parameter_name_set
            }
            merged_parameter_by_name_map.update(parameter_by_name_map)
            parameter_by_name_map = merged_parameter_by_name_map
        can_add_primary_platform_data_lake_admin = _can_add_primary_platform_data_lake_admin(
            allowed_primary_platform_data_lake_admin_arn=allowed_primary_platform_data_lake_admin_arn,
            aws_account_id=self._aws_account_id,
            existing_parameter_by_name_map=existing_parameter_by_name_map,
            parameter_by_name_map=parameter_by_name_map,
            stack_name=stack_name,
        )
        if parameter_by_name_map:
            command_list.extend(
                [
                    "--parameters",
                    json.dumps(
                        [
                            {
                                "ParameterKey": parameter_name,
                                "ParameterValue": parameter_value,
                            }
                            for parameter_name, parameter_value in sorted(parameter_by_name_map.items())
                        ],
                        separators=(",", ":"),
                    ),
                ]
            )
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
            violation_logical_id_list = _stable_data_change_violation_list_get(
                change_summary_list,
                can_add_primary_platform_data_lake_admin=can_add_primary_platform_data_lake_admin,
                versioned_document_logical_id_set=versioned_document_logical_id_set,
            )
            if violation_logical_id_list:
                self._change_set_delete(stack_name, change_set_name)
                raise DevelopmentEnvironmentError(
                    "Stable data-plane change would remove or replace " + ", ".join(violation_logical_id_list)
                )
        protected_violation_list = _protected_identity_change_violation_list_get(
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
        """Prove one stable stack is available for recovery and in sync.

        Args:
            stack_name: Stack name.
        """

        stack_payload = self.payload_get(stack_name, is_required=True)
        if stack_payload.get("StackStatus") not in STACK_DRIFT_CHECKABLE_STATUS_SET:
            raise DevelopmentEnvironmentError(f"Stack {stack_name} is not in a stable operational state")
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
        """Return validated stack outputs keyed by logical output name.

        Args:
            stack_name: Stack name.

        Returns:
            The validated stack outputs keyed by logical output name.
        """

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
        """Return validated effective stack parameters.

        Args:
            stack_name: Stack name.

        Returns:
            The validated effective stack parameters.
        """

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
        """Return one exact stack object, or empty when optional and absent.

        Args:
            stack_name: Stack name.
            is_required: Whether required.

        Returns:
            One exact stack object, or empty when optional and absent.
        """

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
            error = aws_cli_error_get(result)
            if (
                not is_required
                and error is not None
                and error.code == "ValidationError"
                and error.operation == "DescribeStacks"
                and error.message == f"Stack with id {stack_name} does not exist"
            ):
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
        """Return physical resource IDs keyed by logical resource IDs.

        Args:
            stack_name: Stack name.

        Returns:
            The physical resource IDs keyed by logical resource IDs.
        """

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
        """Run local schema lint and remote CloudFormation validation.

        Args:
            template_path: Exact filesystem path for template.
        """

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

    def existing_resource_identity_validate(
        self,
        *,
        current_resource_id_by_logical_name_map: Mapping[str, str],
        previous_resource_id_by_logical_name_map: Mapping[str, str],
    ) -> None:
        """Prove an update preserved every pre-existing physical identity.

        Args:
            current_resource_id_by_logical_name_map: Current resource identity by logical name mapping.
            previous_resource_id_by_logical_name_map: Previous resource identity by logical name mapping.
        """

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
        """Return inline or content-addressed S3 template arguments.

        Args:
            template_path: Exact filesystem path for template.

        Returns:
            The inline or content-addressed S3 template arguments.
        """

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
            if not aws_cli_error_matches(
                head_result,
                code_set=frozenset({"404", "NoSuchKey"}),
                operation="HeadObject",
            ):
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

    def _template_parameter_name_set_get(self, template_argument_list: Sequence[str]) -> set[str]:
        """Return the parameter names declared by the submitted template.

        Args:
            template_argument_list: Ordered template argument values.

        Returns:
            The parameter names declared by the submitted template.
        """

        payload = self._aws.json_get(
            [
                "cloudformation",
                "get-template-summary",
                *template_argument_list,
            ]
        )
        parameter_list = payload.get("Parameters", [])
        if not isinstance(parameter_list, list):
            raise DevelopmentEnvironmentError("CloudFormation template Parameters are malformed")
        parameter_name_set: set[str] = set()
        for parameter_payload in parameter_list:
            parameter_name = parameter_payload.get("ParameterKey") if isinstance(parameter_payload, Mapping) else None
            if not isinstance(parameter_name, str):
                raise DevelopmentEnvironmentError("CloudFormation template Parameters are malformed")
            parameter_name_set.add(parameter_name)
        return parameter_name_set

    def _change_set_delete(
        self,
        stack_name: str,
        change_set_name: str,
    ) -> None:
        """Delete one unexecuted deterministic change set.

        Args:
            stack_name: Stack name.
            change_set_name: Change set name.
        """

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
