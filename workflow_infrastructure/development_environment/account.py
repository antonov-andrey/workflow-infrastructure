"""Development-account operator and account-foundation verification."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Protocol

from workflow_infrastructure.development_environment.aws import DevelopmentAwsClient
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class CommandRunnerProtocol(Protocol):
    """External process boundary required by account verification."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command."""


class StackReaderProtocol(Protocol):
    """CloudFormation state required by account-foundation verification."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack outputs."""

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return stack parameters."""


class DevelopmentAccountVerifier:
    """Own local operator identity and account-global guard verification."""

    def __init__(
        self,
        *,
        account_id: str,
        aws: DevelopmentAwsClient,
        foundation_stack_name: str,
        profile: str,
        region: str,
        runner: CommandRunnerProtocol,
        stack: StackReaderProtocol,
    ) -> None:
        """Bind verification to one exact development account and environment."""

        self._account_id = account_id
        self._aws = aws
        self._foundation_stack_name = foundation_stack_name
        self._profile = profile
        self._region = region
        self._runner = runner
        self._stack = stack

    def local_operator_context_validate(self) -> None:
        """Prove the selected profile, region, and required AWS services."""

        payload = self._aws.json_get(["sts", "get-caller-identity"])
        if payload.get("Account") != self._account_id:
            raise DevelopmentEnvironmentError(
                f"AWS profile {self._profile} targets {payload.get('Account')}, "
                f"expected {self._account_id}"
            )
        region_result = self._runner.run(
            [
                "aws",
                "configure",
                "get",
                "region",
                "--profile",
                self._profile,
            ]
        )
        actual_region = region_result.stdout.strip()
        if actual_region != self._region:
            raise DevelopmentEnvironmentError(
                f"AWS profile {self._profile} region is {actual_region}, "
                f"expected {self._region}"
            )
        self.service_readiness_validate()

    def account_foundation_validate(self) -> None:
        """Verify account-global guards owned only by the primary stack."""

        self._public_access_block_validate()
        self._data_lake_settings_validate()
        self._session_manager_preferences_validate()

    def service_readiness_validate(self) -> None:
        """Prove every required AWS control plane is reachable."""

        for aws_argument_list in (
            ["s3api", "list-buckets"],
            ["kms", "list-keys", "--limit", "1"],
            ["athena", "list-work-groups", "--max-results", "1"],
            ["cloudformation", "list-stacks"],
        ):
            self._aws.run(aws_argument_list)

    def _public_access_block_validate(self) -> None:
        """Require the primary-owned account-global S3 public-access guard."""

        payload = self._aws.json_get(
            [
                "s3control",
                "get-public-access-block",
                "--account-id",
                self._account_id,
            ]
        )
        if payload.get("PublicAccessBlockConfiguration") != {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }:
            raise DevelopmentEnvironmentError(
                "Account-level S3 Block Public Access is not fully enabled"
            )

    def _data_lake_settings_validate(self) -> None:
        """Require the exact primary-owned Lake Formation account foundation."""

        parameter_by_name_map = self._stack.parameter_by_name_map_get(
            self._foundation_stack_name
        )
        deployment_principal_arn = parameter_by_name_map.get("DeploymentPrincipalArn")
        primary_platform_role_arn = parameter_by_name_map.get("PrimaryPlatformRoleArn")
        if not deployment_principal_arn:
            raise DevelopmentEnvironmentError(
                "Account-foundation deployment principal is unavailable"
            )
        payload = self._aws.json_get(
            [
                "lakeformation",
                "get-data-lake-settings",
                "--catalog-id",
                self._account_id,
            ]
        )
        settings = payload.get("DataLakeSettings")
        if not isinstance(settings, Mapping):
            raise DevelopmentEnvironmentError(
                "Lake Formation account settings are unavailable"
            )
        admin_list = settings.get("DataLakeAdmins")
        if not isinstance(admin_list, list):
            raise DevelopmentEnvironmentError(
                "Lake Formation account administrator list is unavailable"
            )
        actual_admin_arn_set = {
            item.get("DataLakePrincipalIdentifier")
            for item in admin_list
            if isinstance(item, Mapping)
        }
        expected_admin_arn_set = {deployment_principal_arn}
        if primary_platform_role_arn:
            expected_admin_arn_set.add(primary_platform_role_arn)
        if actual_admin_arn_set != expected_admin_arn_set or len(admin_list) != len(
            expected_admin_arn_set
        ):
            raise DevelopmentEnvironmentError(
                "Lake Formation account administrators differ from the primary owner"
            )
        required_value_by_name_map: dict[str, object] = {
            "AllowExternalDataFiltering": False,
            "CreateDatabaseDefaultPermissions": [],
            "CreateTableDefaultPermissions": [],
            "ExternalDataFilteringAllowList": [],
            "Parameters": {
                "CROSS_ACCOUNT_VERSION": "4",
                "SET_CONTEXT": "TRUE",
                "SET_SOURCE_IDENTITY": "FALSE",
            },
            "TrustedResourceOwners": [],
        }
        if settings.get("AllowFullTableExternalDataAccess", False) is not False:
            raise DevelopmentEnvironmentError(
                "Lake Formation account setting AllowFullTableExternalDataAccess differs from the primary owner"
            )
        for name, expected_value in required_value_by_name_map.items():
            if settings.get(name) != expected_value:
                raise DevelopmentEnvironmentError(
                    f"Lake Formation account setting {name} differs from the primary owner"
                )
        for optional_empty_name in (
            "AuthorizedSessionTagValueList",
            "ReadOnlyAdmins",
        ):
            if settings.get(optional_empty_name, []) != []:
                raise DevelopmentEnvironmentError(
                    f"Lake Formation account setting {optional_empty_name} must be empty"
                )

    def _session_manager_preferences_validate(self) -> None:
        """Require encrypted ordinary-shell command/output logging preferences."""

        output_by_name_map = self._stack.output_by_name_map_get(
            self._foundation_stack_name
        )
        expected_log_group_name = output_by_name_map.get("SessionShellLogGroupName")
        expected_key_arn = output_by_name_map.get("SessionShellLogKeyArn")
        if expected_log_group_name != "/session-manager/shell":
            raise DevelopmentEnvironmentError(
                "Account-foundation Session Manager log group is unavailable"
            )
        if not isinstance(expected_key_arn, str) or not expected_key_arn.startswith(
            f"arn:aws:kms:{self._region}:{self._account_id}:key/"
        ):
            raise DevelopmentEnvironmentError(
                "Account-foundation Session Manager KMS key is unavailable"
            )
        log_payload = self._aws.json_get(
            [
                "logs",
                "describe-log-groups",
                "--log-group-name-prefix",
                expected_log_group_name,
            ]
        )
        log_group_list = log_payload.get("logGroups")
        if (
            not isinstance(log_group_list, list)
            or len(log_group_list) != 1
            or not isinstance(log_group_list[0], Mapping)
            or log_group_list[0].get("logGroupName") != expected_log_group_name
            or log_group_list[0].get("kmsKeyId") != expected_key_arn
            or log_group_list[0].get("retentionInDays") != 30
        ):
            raise DevelopmentEnvironmentError(
                "Session Manager shell log group differs from account-foundation"
            )
        key_payload = self._aws.json_get(
            ["kms", "describe-key", "--key-id", expected_key_arn]
        )
        key_metadata = key_payload.get("KeyMetadata")
        if (
            not isinstance(key_metadata, Mapping)
            or key_metadata.get("Arn") != expected_key_arn
            or key_metadata.get("Enabled") is not True
            or key_metadata.get("KeyState") != "Enabled"
        ):
            raise DevelopmentEnvironmentError(
                "Session Manager shell log KMS key is not active"
            )
        payload = self._aws.json_get(
            [
                "ssm",
                "describe-document",
                "--name",
                "SSM-SessionManagerRunShell",
                "--document-version",
                "$LATEST",
            ]
        )
        document = payload.get("Document")
        if (
            not isinstance(document, Mapping)
            or document.get("DocumentType") != "Session"
            or document.get("Status") != "Active"
            or document.get("Name") != "SSM-SessionManagerRunShell"
        ):
            raise DevelopmentEnvironmentError(
                "Session Manager shell preferences document is unavailable"
            )
        content_payload = self._aws.json_get(
            [
                "ssm",
                "get-document",
                "--name",
                "SSM-SessionManagerRunShell",
                "--document-version",
                "$LATEST",
                "--document-format",
                "JSON",
            ]
        )
        content_text = content_payload.get("Content")
        try:
            content = (
                json.loads(content_text) if isinstance(content_text, str) else None
            )
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Session Manager shell preferences content is malformed"
            ) from error
        expected_content = {
            "schemaVersion": "1.0",
            "description": "Development account Session Manager shell preferences.",
            "sessionType": "Standard_Stream",
            "inputs": {
                "cloudWatchEncryptionEnabled": True,
                "cloudWatchLogGroupName": expected_log_group_name,
                "idleSessionTimeout": "20",
                "kmsKeyId": expected_key_arn,
                "maxSessionDuration": "",
                "runAsDefaultUser": "",
                "runAsEnabled": False,
                "s3BucketName": "",
                "s3EncryptionEnabled": True,
                "s3KeyPrefix": "",
                "shellProfile": {"linux": "", "windows": ""},
            },
        }
        if content != expected_content:
            raise DevelopmentEnvironmentError(
                "Session Manager shell preferences differ from account-foundation"
            )
