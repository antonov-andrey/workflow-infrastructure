"""Verify development-environment orchestration and declarative infrastructure behavior."""

from __future__ import annotations

from contextlib import nullcontext
import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from cfnlint.decode import decode
from cfnlint.schema.manager import ProviderSchemaManager
import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from tool import development_environment_manage
from tool.lib import development_environment
from tool.lib.development_environment import (
    Clock,
    CommandRunner,
    DevelopmentEnvironment,
    DevelopmentEnvironmentError,
    DevelopmentEnvironmentIdentity,
)


class ClockFixed(Clock):
    """Provide deterministic UTC and monotonic time for lifecycle tests."""

    def __init__(self) -> None:
        """Initialize the fixed clock."""

        self.t_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
        self.monotonic_seconds = 0.0

    def monotonic(self) -> float:
        """Return deterministic monotonic seconds.

        Returns:
            Monotonic seconds.
        """

        return self.monotonic_seconds

    def now(self) -> datetime:
        """Return the deterministic UTC instant.

        Returns:
            Fixed UTC instant.
        """

        return self.t_now

    def sleep(self, delay_seconds: float) -> None:
        """Advance deterministic monotonic time.

        Args:
            delay_seconds: Duration in seconds.
        """

        self.monotonic_seconds += delay_seconds
        self.t_now += timedelta(seconds=delay_seconds)


def _environment_get(
    project_root_path: Path,
    *,
    environment_name: str = "primary",
) -> DevelopmentEnvironment:
    """Create one development environment with real commands and deterministic time.

    Args:
        project_root_path: Test project root.
        environment_name: Stable environment selector.

    Returns:
        Development environment.
    """

    return DevelopmentEnvironment(
        clock=ClockFixed(),
        environment_name=environment_name,
        project_root_path=project_root_path,
        runner=CommandRunner(),
    )


def _git_run(repository_path: Path, argument_list: list[str]) -> None:
    """Run one required Git fixture command.

    Args:
        repository_path: Fixture repository root.
        argument_list: Git arguments.
    """

    subprocess.run(
        ["git", "-C", str(repository_path), *argument_list],
        check=True,
        capture_output=True,
        text=True,
    )


def _template_get(project_root_path: Path, template_name: str) -> dict[str, object]:
    """Decode one CloudFormation template without discarding intrinsic functions.

    Args:
        project_root_path: Repository root.
        template_name: Template file name.

    Returns:
        Decoded template.
    """

    template, error_list = decode(
        str(project_root_path / "cloudformation" / template_name)
    )
    assert error_list == []
    assert isinstance(template, dict)
    return template


def _yaml_mapping_keys_unique_assert(
    node: yaml.Node,
    *,
    document_path: str,
) -> None:
    """Recursively reject duplicate YAML mapping keys before construction."""

    if isinstance(node, MappingNode):
        key_identity_set: set[tuple[str, str]] = set()
        for key_node, value_node in node.value:
            assert isinstance(
                key_node, ScalarNode
            ), f"{document_path} contains a non-scalar mapping key"
            key_identity = (key_node.tag, key_node.value)
            assert (
                key_identity not in key_identity_set
            ), f"{document_path} contains duplicate YAML key {key_node.value!r}"
            key_identity_set.add(key_identity)
            _yaml_mapping_keys_unique_assert(
                value_node,
                document_path=document_path,
            )
    elif isinstance(node, SequenceNode):
        for item_node in node.value:
            _yaml_mapping_keys_unique_assert(
                item_node,
                document_path=document_path,
            )


@pytest.mark.parametrize(
    "template_name",
    [
        "workflow-control-center-development.yaml",
        "workflow-control-center-development-compute.yaml",
    ],
)
def test_cloudformation_templates_have_no_duplicate_yaml_keys(
    template_name: str,
) -> None:
    """CloudFormation validation must not receive silently shadowed YAML keys."""

    template_path = (
        Path(__file__).resolve().parents[1] / "cloudformation" / template_name
    )
    root_node = yaml.compose(template_path.read_text(encoding="utf-8"))
    assert root_node is not None
    _yaml_mapping_keys_unique_assert(
        root_node,
        document_path=template_name,
    )


def test_cloudformation_template_transport_uses_verified_s3_for_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A template beyond the inline API limit uses one exact private S3 object."""

    environment = _environment_get(tmp_path)
    template_path = tmp_path / "template.yaml"
    template_bytes = b"x" * (
        development_environment.CLOUDFORMATION_INLINE_TEMPLATE_MAX_BYTE_COUNT + 1
    )
    template_path.write_bytes(template_bytes)
    digest_bytes = hashlib.sha256(template_bytes).digest()
    digest = digest_bytes.hex()
    checksum_sha256 = base64.b64encode(digest_bytes).decode("ascii")
    command_list: list[tuple[list[str], bool]] = []

    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "ObservabilityBucketName": "workflow-control-center-observability"
        },
    )

    def aws_run(
        argument_list: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_list.append((argument_list, check))
        if argument_list[:2] == ["s3api", "head-object"]:
            return subprocess.CompletedProcess(argument_list, 1, "", "404 Not Found")
        return subprocess.CompletedProcess(argument_list, 0, "{}", "")

    monkeypatch.setattr(environment, "_aws_run", aws_run)
    monkeypatch.setattr(
        environment,
        "_aws_json_get",
        lambda argument_list: {
            "ChecksumSHA256": checksum_sha256,
            "ContentLength": len(template_bytes),
            "Metadata": {"sha256": digest},
        },
    )

    argument_list = environment._cloudformation_template_argument_list_get(
        template_path
    )

    object_key = f"cloudformation-template/primary/{digest}.yaml"
    assert argument_list == [
        "--template-url",
        "https://workflow-control-center-observability.s3.us-east-1.amazonaws.com/"
        + object_key,
    ]
    put_argument_list = command_list[1][0]
    assert put_argument_list[:2] == ["s3api", "put-object"]
    assert put_argument_list[put_argument_list.index("--key") + 1] == object_key
    assert put_argument_list[put_argument_list.index("--checksum-sha256") + 1] == (
        checksum_sha256
    )
    assert put_argument_list[put_argument_list.index("--metadata") + 1] == (
        f"sha256={digest}"
    )


def test_cloudformation_template_transport_keeps_small_body_inline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A small template does not create an unnecessary S3 artifact."""

    environment = _environment_get(tmp_path)
    template_path = tmp_path / "template.yaml"
    template_path.write_text("Resources: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: pytest.fail("inline template must not inspect S3"),
    )

    assert environment._cloudformation_template_argument_list_get(template_path) == [
        "--template-body",
        f"file://{template_path}",
    ]


@pytest.mark.parametrize(
    "template_name",
    [
        "workflow-control-center-development.yaml",
        "workflow-control-center-development-compute.yaml",
    ],
)
def test_every_taggable_cloudformation_resource_has_environment_identity(
    template_name: str,
) -> None:
    """Every supported resource must carry the complete development identity."""

    template = _template_get(Path(__file__).resolve().parents[1], template_name)
    resource_by_name_map = template["Resources"]
    assert isinstance(resource_by_name_map, dict)
    provider_schema_manager = ProviderSchemaManager()
    required_tag_key_set = {
        "Project",
        "Environment",
        "EnvironmentName",
        "ManagedBy",
    }

    for resource_name, resource in resource_by_name_map.items():
        assert isinstance(resource, dict)
        resource_type = resource["Type"]
        assert isinstance(resource_type, str)
        resource_schema = provider_schema_manager.get_resource_schema(
            "us-east-1",
            resource_type,
        )
        if "Tags" not in resource_schema.schema.get("properties", {}):
            continue

        resource_property_map = resource.get("Properties", {})
        assert isinstance(resource_property_map, dict)
        tag_list = resource_property_map.get("Tags")

        assert isinstance(
            tag_list, list
        ), f"{template_name}:{resource_name} supports tags but declares none"
        tag_key_list = [tag["Key"] for tag in tag_list]
        assert len(tag_key_list) == len(
            set(tag_key_list)
        ), f"{template_name}:{resource_name} declares duplicate tag keys"
        assert required_tag_key_set <= set(
            tag_key_list
        ), f"{template_name}:{resource_name} lacks the development identity tags"


def test_compute_created_resources_preserve_environment_identity_tags() -> None:
    """Launch-created instance, volume, and DLM snapshot tags must remain complete."""

    template = _template_get(
        Path(__file__).resolve().parents[1],
        "workflow-control-center-development-compute.yaml",
    )
    resource_by_name_map = template["Resources"]
    required_tag_key_set = {
        "Project",
        "Environment",
        "EnvironmentName",
        "ManagedBy",
    }

    launch_template_data = resource_by_name_map["DevelopmentHostLaunchTemplate"][
        "Properties"
    ]["LaunchTemplateData"]
    assert [
        tag_specification["ResourceType"]
        for tag_specification in launch_template_data["TagSpecifications"]
    ] == ["volume"]
    for tag_specification in launch_template_data["TagSpecifications"]:
        tag_key_list = [tag["Key"] for tag in tag_specification["Tags"]]
        assert len(tag_key_list) == len(set(tag_key_list))
        assert required_tag_key_set <= set(tag_key_list)
    instance_tag_list = resource_by_name_map["DevelopmentInstance"]["Properties"][
        "Tags"
    ]
    instance_tag_key_list = [tag["Key"] for tag in instance_tag_list]
    assert len(instance_tag_key_list) == len(set(instance_tag_key_list))
    assert required_tag_key_set | {"Name", "ReplacementSlot"} == set(
        instance_tag_key_list
    )

    snapshot_schedule = resource_by_name_map["RetainedSnapshotLifecyclePolicy"][
        "Properties"
    ]["PolicyDetails"]["Schedules"][0]
    snapshot_tag_key_list = [tag["Key"] for tag in snapshot_schedule["TagsToAdd"]]
    assert len(snapshot_tag_key_list) == len(set(snapshot_tag_key_list))
    assert required_tag_key_set <= set(snapshot_tag_key_list)


def test_cli_keeps_standard_options_after_commands_and_only_forwards_ssh_arguments() -> (
    None
):
    """Restore and activation options must not be consumed by the SSH remainder."""

    restore_args = development_environment_manage._args_parse(
        ["restore", "--snapshot-id", "snap-0123456789abcdef0"]
    )
    activation_args = development_environment_manage._args_parse(
        [
            "host-product-release-activate",
            "--release",
            "20260728120000000000",
        ]
    )
    ssh_args = development_environment_manage._args_parse(
        ["ssh", "--", "-L", "8080:localhost:8080"]
    )
    host_status_args = development_environment_manage._args_parse(
        ["host-status", "--retained-volume-id", "vol-0123456789abcdef0"]
    )
    deploy_args = development_environment_manage._args_parse(
        [
            "deploy",
            "--environment-name",
            "feature1",
            "--workflow-container-contract-commit",
            "a" * 40,
        ]
    )

    assert restore_args.snapshot_id == "snap-0123456789abcdef0"
    assert restore_args.ssh_argument_list == []
    assert activation_args.release == "20260728120000000000"
    assert activation_args.ssh_argument_list == []
    assert ssh_args.ssh_argument_list == ["-L", "8080:localhost:8080"]
    assert host_status_args.retained_volume_id == "vol-0123456789abcdef0"
    assert host_status_args.ssh_argument_list == []
    assert deploy_args.workflow_container_contract_commit == "a" * 40
    assert deploy_args.environment_name == "feature1"
    assert deploy_args.ssh_argument_list == []


def test_environment_identity_preserves_primary_and_isolates_nonprimary() -> None:
    """One safe selector must preserve primary and disjoint every owned identity."""

    primary = DevelopmentEnvironmentIdentity()
    alternate = DevelopmentEnvironmentIdentity("feature1")

    assert (
        primary.data_plane_stack_name == development_environment.DATA_PLANE_STACK_NAME
    )
    assert primary.compute_stack_name == development_environment.COMPUTE_STACK_NAME
    assert primary.instance_name == development_environment.INSTANCE_NAME
    assert primary.lease_group_name == development_environment.LEASE_GROUP_NAME
    assert primary.lease_name == development_environment.LEASE_NAME
    assert primary.host_control_current_source_path == (
        development_environment.HOST_CONTROL_CURRENT_SOURCE_PATH
    )
    assert (
        primary.host_release_root_path == development_environment.HOST_RELEASE_ROOT_PATH
    )
    assert (
        primary.host_retained_root_path
        == development_environment.HOST_RETAINED_ROOT_PATH
    )

    primary_identity_set = {
        primary.compute_stack_name,
        primary.data_plane_stack_name,
        primary.instance_name,
        primary.lease_group_name,
        primary.lease_name,
        primary.host_control_current_source_path.as_posix(),
        primary.host_release_root_path.as_posix(),
        primary.host_retained_root_path.as_posix(),
        primary.host_state_root_path.as_posix(),
        primary.qualified_credential_identity,
        primary.qualified_product_database_identity,
        primary.qualified_registry_identity,
    }
    alternate_identity_set = {
        alternate.compute_stack_name,
        alternate.data_plane_stack_name,
        alternate.instance_name,
        alternate.lease_group_name,
        alternate.lease_name,
        alternate.host_control_current_source_path.as_posix(),
        alternate.host_release_root_path.as_posix(),
        alternate.host_retained_root_path.as_posix(),
        alternate.host_state_root_path.as_posix(),
        alternate.qualified_credential_identity,
        alternate.qualified_product_database_identity,
        alternate.qualified_registry_identity,
    }
    assert primary_identity_set.isdisjoint(alternate_identity_set)
    assert all("feature1" in identity for identity in alternate_identity_set)


def test_current_product_tool_calls_preserve_nonprimary_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every infrastructure-to-Product command must retain its environment identity."""

    environment = _environment_get(tmp_path, environment_name="feature1")
    product_tool_path = (
        "/opt/workflow-infrastructure/environments/feature1/current/"
        "sources/workflow-control-center/tool/development_kubernetes_manage.py"
    )
    local_command_list_list: list[list[str]] = []
    ssm_command_batch_list: list[list[str]] = []

    def run(
        argument_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check, input_text, should_capture
        local_command_list_list.append(argument_list)
        stdout = (
            '{"reason_key_list":[],"status":"idle","t_observed":"2026-07-30T00:00:00Z"}\n'
            if argument_list[2:3] == ["activity"]
            else ""
        )
        return subprocess.CompletedProcess(argument_list, 0, stdout, "")

    monkeypatch.setattr(environment._runner, "run", run)
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: str(path) == product_tool_path,
    )
    monkeypatch.setattr(
        environment,
        "_ssm_shell_run",
        lambda command_list: ssm_command_batch_list.append(command_list),
    )
    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(environment, "status", lambda: None)
    monkeypatch.setattr(
        environment,
        "_instance_id_get",
        lambda: "i-0123456789abcdef0",
    )
    monkeypatch.setattr(
        environment,
        "_instance_state_get",
        lambda instance_id: "running",
    )

    assert environment._host_product_activity_get() == "idle"
    environment.host_shutdown()
    environment._product_recovery_apply_run()
    environment._product_recovery_acceptance_run()
    environment.diagnose()

    product_local_command_list = [
        command_list
        for command_list in local_command_list_list
        if product_tool_path in command_list
    ]
    assert product_local_command_list == [
        [
            "python3.14",
            product_tool_path,
            "activity",
            "--environment-name",
            "feature1",
        ],
        [
            "python3.14",
            product_tool_path,
            "shutdown",
            "--environment-name",
            "feature1",
        ],
    ]
    ssm_command_text = "\n".join(
        command for command_list in ssm_command_batch_list for command in command_list
    )
    for command in ("diagnose", "host-install", "recover", "recovery-acceptance"):
        assert (
            f"{product_tool_path} {command} --environment-name feature1"
            in ssm_command_text
        )
    assert "df -h / /srv/workflow-control-center-feature1" in ssm_command_text


def test_current_primary_product_tool_calls_keep_legacy_cli_compatibility(
    tmp_path: Path,
) -> None:
    """Primary lifecycle works before and after the environment selector was added."""

    environment = _environment_get(tmp_path)

    command_list = environment._current_product_tool_command_list_get("activity")

    assert command_list[-1] == "activity"
    assert "--environment-name" not in command_list


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (
            0,
            '{"reason_key_list":[],"status":"idle","t_observed":"2026-07-30T00:00:00Z"}',
            "idle",
        ),
        (
            0,
            (
                '{"reason_key_list":["persisted_work"],"status":"busy",'
                '"t_observed":"2026-07-30T00:00:00Z"}'
            ),
            "busy",
        ),
        (0, "idle", "busy"),
        (
            0,
            '{"reason_key_list":[],"status":"idle","t_observed":"2026-07-30T04:00:00+04:00"}',
            "busy",
        ),
        (
            0,
            '{"reason_key_list":[],"status":"busy","t_observed":"2026-07-30T00:00:00Z"}',
            "busy",
        ),
        (1, "", "busy"),
    ],
)
def test_host_product_activity_accepts_only_safe_utc_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    expected: str,
) -> None:
    """Lifecycle treats every malformed or unverifiable Product observation as busy."""

    environment = _environment_get(tmp_path)
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    monkeypatch.setattr(
        environment._runner,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode,
            stdout,
            "",
        ),
    )

    assert environment._host_product_activity_get() == expected


def test_data_plane_template_derives_query_role_from_environment_local_database_name() -> (
    None
):
    """Every non-primary Glue shard and IAM query role must share one exact namespace."""

    template = _template_get(
        Path(__file__).resolve().parents[1],
        "workflow-control-center-development.yaml",
    )
    resource_by_name_map = template["Resources"]

    assert resource_by_name_map["GlueDatabase"]["Properties"]["DatabaseInput"][
        "Name"
    ] == {
        "Fn::If": [
            "IsPrimaryEnvironment",
            "workflow_data_000",
            {"Fn::Sub": "workflow_data_000_${EnvironmentName}"},
        ]
    }
    assert resource_by_name_map["QueryRole"]["Properties"]["RoleName"] == {
        "Fn::If": [
            "IsPrimaryEnvironment",
            "workflow-data-workflow_data_000",
            {"Fn::Sub": "workflow-data-workflow_data_000_${EnvironmentName}"},
        ]
    }


def test_non_primary_environment_physical_names_fit_provider_limits() -> None:
    """The longest valid environment selector must still render valid AWS names."""

    project_root_path = Path(__file__).resolve().parents[1]
    data_template = _template_get(
        project_root_path,
        "workflow-control-center-development.yaml",
    )
    compute_template = _template_get(
        project_root_path,
        "workflow-control-center-development-compute.yaml",
    )
    environment_name = "a" * 16
    account_id = "1" * 12

    data_resource_by_name_map = data_template["Resources"]
    bucket_name_by_logical_id_map = {
        logical_id: resource["Properties"]["BucketName"]["Fn::If"][2]["Fn::Sub"]
        for logical_id, resource in data_resource_by_name_map.items()
        if resource["Type"] == "AWS::S3::Bucket"
    }
    rendered_bucket_name_by_logical_id_map = {
        logical_id: name_template.replace(
            "${EnvironmentName}", environment_name
        ).replace(
            "${AWS::AccountId}",
            account_id,
        )
        for logical_id, name_template in bucket_name_by_logical_id_map.items()
    }
    assert len(rendered_bucket_name_by_logical_id_map) == 4
    assert len(set(rendered_bucket_name_by_logical_id_map.values())) == 4
    assert all(
        len(bucket_name) <= 63
        for bucket_name in rendered_bucket_name_by_logical_id_map.values()
    )

    lake_formation_role_name = data_resource_by_name_map["LakeFormationDataAccessRole"][
        "Properties"
    ]["RoleName"]["Fn::If"][2]["Fn::Sub"].replace(
        "${EnvironmentName}",
        environment_name,
    )
    assert len(lake_formation_role_name) <= 64

    compute_resource_by_name_map = compute_template["Resources"]
    for logical_id, property_name, maximum_length in (
        ("LeaseStopFunction", "FunctionName", 64),
        ("LeaseStopFunctionExecutionRole", "RoleName", 64),
        ("InstanceCreationGuardSchedule", "Name", 64),
    ):
        name_template = compute_resource_by_name_map[logical_id]["Properties"][
            property_name
        ]["Fn::If"][2]["Fn::Sub"]
        rendered_name = name_template.replace(
            "${EnvironmentName}",
            environment_name,
        )
        assert len(rendered_name) <= maximum_length


def test_data_plane_keeps_primary_lake_formation_identity_and_isolates_non_primary() -> (
    None
):
    """Primary permissions stay byte-stable while other environments use distinct owners."""

    template = _template_get(
        Path(__file__).resolve().parents[1],
        "workflow-control-center-development.yaml",
    )
    resource_by_name_map = template["Resources"]
    data_lake_settings = resource_by_name_map["DataLakeSettings"]
    assert data_lake_settings["Condition"] == "IsPrimaryEnvironment"
    assert data_lake_settings["DeletionPolicy"] == "Retain"
    assert data_lake_settings["UpdateReplacePolicy"] == "Retain"
    assert data_lake_settings["Properties"]["Admins"] == [
        {"DataLakePrincipalIdentifier": {"Ref": "DeploymentPrincipalArn"}},
        {
            "DataLakePrincipalIdentifier": {
                "Fn::GetAtt": ["PlatformRole", "Arn"],
            }
        },
    ]
    assert data_lake_settings["Properties"]["MutationType"] == "REPLACE"

    platform_principal = {
        "DataLakePrincipalIdentifier": {
            "Fn::GetAtt": ["PlatformRole", "Arn"],
        }
    }
    non_primary_catalog_permission = resource_by_name_map[
        "NonPrimaryPlatformRoleCatalogPermission"
    ]
    assert non_primary_catalog_permission["Condition"] == ("IsNonPrimaryEnvironment")
    assert non_primary_catalog_permission["Properties"] == {
        "Permissions": ["CREATE_DATABASE"],
        "PermissionsWithGrantOption": [],
        "Principal": platform_principal,
        "Resource": {"Catalog": {}},
    }
    non_primary_data_location_permission = resource_by_name_map[
        "NonPrimaryPlatformRoleDataLocationPermission"
    ]
    assert non_primary_data_location_permission["Condition"] == (
        "IsNonPrimaryEnvironment"
    )
    assert non_primary_data_location_permission["Properties"]["Permissions"] == [
        "DATA_LOCATION_ACCESS"
    ]

    primary_database_permission = resource_by_name_map["PlatformRoleDatabasePermission"]
    assert primary_database_permission["Condition"] == "IsPrimaryEnvironment"
    assert primary_database_permission["DependsOn"] == "GlueDatabase"
    assert primary_database_permission["Properties"]["Principal"] == platform_principal
    assert primary_database_permission["Properties"]["Resource"] == {
        "Database": {
            "CatalogId": {"Ref": "AWS::AccountId"},
            "Name": "workflow_data_000",
        }
    }
    non_primary_database_permission = resource_by_name_map[
        "NonPrimaryPlatformRoleDatabasePermission"
    ]
    assert non_primary_database_permission["Condition"] == "IsNonPrimaryEnvironment"
    assert non_primary_database_permission["Properties"]["Resource"] == {
        "Database": {
            "CatalogId": {"Ref": "AWS::AccountId"},
            "Name": {"Ref": "GlueDatabase"},
        }
    }

    for primary_name, non_primary_name in (
        ("PlatformRoleTablePermission", "NonPrimaryPlatformRoleTablePermission"),
        ("QueryRoleDatabasePermission", "NonPrimaryQueryRoleDatabasePermission"),
        ("QueryRoleTablePermission", "NonPrimaryQueryRoleTablePermission"),
    ):
        assert resource_by_name_map[primary_name]["Condition"] == (
            "IsPrimaryEnvironment"
        )
        assert resource_by_name_map[non_primary_name]["Condition"] == (
            "IsNonPrimaryEnvironment"
        )


@pytest.mark.parametrize(
    "environment_name",
    ["", "Primary", "feature-1", "feature_1", "a" * 17, "../primary"],
)
def test_environment_identity_rejects_unsafe_name(environment_name: str) -> None:
    """Unsafe environment names cannot enter AWS resources or host paths."""

    with pytest.raises(DevelopmentEnvironmentError):
        DevelopmentEnvironmentIdentity(environment_name)


def test_compute_template_owns_isolated_retained_recoverable_host() -> None:
    """Compute template must enforce the approved isolation, storage, and snapshot contract."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(
        project_root_path, "workflow-control-center-development-compute.yaml"
    )
    resource_by_name_map = template["Resources"]
    assert isinstance(resource_by_name_map, dict)
    assert template["Parameters"]["RetainedVolumeSlot"]["AllowedValues"] == [
        "base",
        "a",
        "b",
    ]
    assert template["Rules"]["RetainedVolumeSourceIsConsistent"] == {
        "Assertions": [
            {
                "Assert": {
                    "Fn::Or": [
                        {
                            "Fn::And": [
                                {
                                    "Fn::Equals": [
                                        {"Ref": "RetainedVolumeSlot"},
                                        "base",
                                    ]
                                },
                                {
                                    "Fn::Equals": [
                                        {"Ref": "RetainedVolumeSnapshotId"},
                                        "",
                                    ]
                                },
                            ]
                        },
                        {
                            "Fn::And": [
                                {
                                    "Fn::Not": [
                                        {
                                            "Fn::Equals": [
                                                {"Ref": "RetainedVolumeSlot"},
                                                "base",
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "Fn::Not": [
                                        {
                                            "Fn::Equals": [
                                                {"Ref": "RetainedVolumeSnapshotId"},
                                                "",
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                    ]
                },
                "AssertDescription": (
                    "The base slot has no snapshot and restored slots require one "
                    "exact snapshot."
                ),
            }
        ]
    }

    resource_type_set = {
        resource["Type"]
        for resource in resource_by_name_map.values()
        if isinstance(resource, dict)
    }
    assert "AWS::EC2::EIP" not in resource_type_set
    assert "AWS::EC2::NatGateway" not in resource_type_set
    security_group = resource_by_name_map["DevelopmentSecurityGroup"]
    assert "SecurityGroupIngress" not in security_group["Properties"]

    retained_volume_by_name_map = {
        name: resource_by_name_map[name]
        for name in (
            "RetainedVolume",
            "RetainedVolumeRestoreA",
            "RetainedVolumeRestoreB",
        )
    }
    for retained_volume in retained_volume_by_name_map.values():
        assert retained_volume["DeletionPolicy"] == "Retain"
        assert retained_volume["UpdateReplacePolicy"] == "Retain"
        assert retained_volume["Properties"]["Encrypted"] is True
        assert retained_volume["Properties"]["VolumeType"] == "gp3"
    assert retained_volume_by_name_map["RetainedVolume"]["Condition"] == (
        "UseRetainedVolumeBase"
    )
    assert (
        "SnapshotId" not in retained_volume_by_name_map["RetainedVolume"]["Properties"]
    )
    assert retained_volume_by_name_map["RetainedVolumeRestoreA"]["Condition"] == (
        "UseRetainedVolumeRestoreA"
    )
    assert retained_volume_by_name_map["RetainedVolumeRestoreB"]["Condition"] == (
        "UseRetainedVolumeRestoreB"
    )
    assert retained_volume_by_name_map["RetainedVolumeRestoreA"]["Properties"][
        "SnapshotId"
    ] == {"Ref": "RetainedVolumeSnapshotId"}
    assert retained_volume_by_name_map["RetainedVolumeRestoreB"]["Properties"][
        "SnapshotId"
    ] == {"Ref": "RetainedVolumeSnapshotId"}

    snapshot_schedule = resource_by_name_map["RetainedSnapshotLifecyclePolicy"][
        "Properties"
    ]["PolicyDetails"]["Schedules"][0]
    assert snapshot_schedule["CreateRule"]["Interval"] == 24
    assert snapshot_schedule["RetainRule"]["Count"] == 7
    assert snapshot_schedule["CopyTags"] is False
    assert snapshot_schedule["TagsToAdd"] == [
        {
            "Key": "Name",
            "Value": "workflow-control-center-development-retained-snapshot",
        },
        {"Key": "Project", "Value": {"Ref": "NotificationTagValue"}},
        {"Key": "Environment", "Value": "development"},
        {"Key": "EnvironmentName", "Value": {"Ref": "EnvironmentName"}},
        {"Key": "ManagedBy", "Value": "DLM"},
    ]
    snapshot_role_statement_list = resource_by_name_map["RetainedSnapshotRole"][
        "Properties"
    ]["Policies"][0]["PolicyDocument"]["Statement"]
    assert snapshot_role_statement_list[1]["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:ec2:*::snapshot/*"
    }

    launch_template_data = resource_by_name_map["DevelopmentHostLaunchTemplate"][
        "Properties"
    ]["LaunchTemplateData"]
    assert launch_template_data["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpPutResponseHopLimit": 1,
        "HttpTokens": "required",
        "InstanceMetadataTags": "disabled",
    }
    user_data = launch_template_data["UserData"]["Fn::Base64"]["Fn::Sub"][0]
    parameter_by_name_map = template["Parameters"]
    assert "Default" not in parameter_by_name_map["K3sVersion"]
    assert "Default" not in parameter_by_name_map["K3sInstallScriptSha256"]
    assert "apt upgrade" not in user_data
    assert (
        "--cluster-cidr 10.42.0.0/16 --service-cidr 10.43.0.0/16 "
        "--disable traefik --secrets-encryption"
    ) in user_data
    assert "https://get.k3s.io" not in user_data
    assert "raw.githubusercontent.com/k3s-io/k3s" not in user_data
    assert "https://astral.sh/uv/install.sh" not in user_data
    assert "curl | sh" not in user_data
    assert "apt-get install --no-install-recommends -y docker-ce" not in user_data
    assert "${K3sInstallScriptUrl}" in user_data
    assert "${K3sBinaryUrl}" in user_data
    assert "K3sInstallScriptSha256" in user_data
    assert "K3sBinarySha256" in user_data
    assert "sha256sum --check --strict" in user_data
    assert "${PythonVersion}" in user_data
    assert "${PythonSha256}" in user_data
    assert "${UvVersion}" in user_data
    assert "${HelmVersion}" in user_data
    assert "HostArtifactManifestGzipBase64" in user_data
    assert "manifest_input" in user_data
    assert "direct_input" in user_data
    assert 'test "$docker_primary_key_count" = 1' in user_data
    assert '"$retained_root/product-tool"' in user_data
    assert '"$retained_root/release"' in user_data
    assert (
        'ln -s "$retained_root/product-tool" "$compatibility_root/product-tool"'
        in user_data
    )
    assert 'blockdev --getsize64 "$retained_device"' in user_data
    assert 'cmp --silent --bytes="$retained_device_size"' in user_data
    assert "has data but no recognized filesystem" in user_data
    assert "mkfs.xfs -f" not in user_data
    retained_root_identity = launch_template_data["UserData"]["Fn::Base64"]["Fn::Sub"][
        1
    ]["RetainedRootPath"]
    assert retained_root_identity == {
        "Fn::If": [
            "IsPrimaryEnvironment",
            "/srv/workflow-control-center",
            {
                "Fn::Sub": "/srv/workflow-control-center-${EnvironmentName}",
            },
        ]
    }

    launch_template_reference = {
        "LaunchTemplateId": {"Ref": "DevelopmentHostLaunchTemplate"},
        "Version": {"Ref": "InstanceLaunchTemplateVersion"},
    }
    assert (
        resource_by_name_map["DevelopmentInstance"]["Properties"]["LaunchTemplate"]
        == launch_template_reference
    )
    retained_volume_reference = {
        "Fn::If": [
            "UseRetainedVolumeBase",
            {"Ref": "RetainedVolume"},
            {
                "Fn::If": [
                    "UseRetainedVolumeRestoreA",
                    {"Ref": "RetainedVolumeRestoreA"},
                    {"Ref": "RetainedVolumeRestoreB"},
                ]
            },
        ]
    }
    assert resource_by_name_map["RetainedVolumeAttachment"]["Properties"] == {
        "Device": "/dev/sdf",
        "InstanceId": {"Ref": "DevelopmentInstance"},
        "VolumeId": retained_volume_reference,
    }
    assert (
        launch_template_data["UserData"]["Fn::Base64"]["Fn::Sub"][1]["RetainedVolumeId"]
        == retained_volume_reference
    )
    assert template["Outputs"]["RetainedVolumeId"]["Value"] == retained_volume_reference
    assert template["Outputs"]["RetainedVolumeSlot"]["Value"] == {
        "Ref": "RetainedVolumeSlot"
    }
    assert template["Outputs"]["RetainedVolumeSourceSnapshotId"]["Value"] == {
        "Ref": "RetainedVolumeSnapshotId"
    }
    assert template["Outputs"]["LatestLaunchTemplateVersion"]["Value"] == {
        "Fn::GetAtt": ["DevelopmentHostLaunchTemplate", "LatestVersionNumber"]
    }
    assert template["Outputs"]["InstanceLaunchTemplateVersion"]["Value"] == {
        "Ref": "InstanceLaunchTemplateVersion"
    }
    lease_stop_function = resource_by_name_map["LeaseStopFunction"]["Properties"]
    assert lease_stop_function["Runtime"] == "python3.14"
    assert lease_stop_function["Architectures"] == ["arm64"]
    assert lease_stop_function["Timeout"] == 30
    lease_stop_code = lease_stop_function["Code"]["ZipFile"]
    assert '"tag:aws:cloudformation:stack-name"' in lease_stop_code
    assert '"tag:aws:cloudformation:logical-id"' in lease_stop_code
    assert "ec2.stop_instances(InstanceIds=instance_ids)" in lease_stop_code
    scheduler_policy_statement = resource_by_name_map["SchedulerExecutionRole"][
        "Properties"
    ]["Policies"][0]["PolicyDocument"]["Statement"]
    assert scheduler_policy_statement == [
        {
            "Action": "lambda:InvokeFunction",
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": ["LeaseStopFunction", "Arn"]},
        }
    ]
    replacement_guard = resource_by_name_map["InstanceCreationGuardSchedule"][
        "Properties"
    ]
    assert "GroupName" not in replacement_guard
    assert replacement_guard["State"] == {"Ref": "ReplacementGuardScheduleState"}
    assert replacement_guard["Target"]["Arn"] == {
        "Fn::GetAtt": ["LeaseStopFunction", "Arn"]
    }
    assert resource_by_name_map["DevelopmentInstance"]["DependsOn"] == [
        "DevelopmentRoute",
        "InstanceCreationGuardSchedule",
    ]


def test_compute_bootstrap_is_syntactically_valid_and_fits_ec2_user_data() -> None:
    """Max-bounded exact launch inputs must fit the EC2 raw UserData limit."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(
        project_root_path,
        "workflow-control-center-development-compute.yaml",
    )
    user_data = template["Resources"]["DevelopmentHostLaunchTemplate"]["Properties"][
        "LaunchTemplateData"
    ]["UserData"]["Fn::Base64"]["Fn::Sub"][0]
    parameter_by_name_map = {
        "AwsCliSha256": "a" * 64,
        "AwsCliUrl": "https://example.invalid/aws-cli",
        "AwsCliVersion": "2.36.11",
        "ComputeArchitecture": "arm64",
        "DockerBuildxPackageSha256": "a" * 64,
        "DockerBuildxPackageUrl": "https://example.invalid/buildx.deb",
        "DockerBuildxPackageVersion": "0.36.0-1~ubuntu.24.04~noble",
        "DockerCeCliPackageSha256": "a" * 64,
        "DockerCeCliPackageUrl": "https://example.invalid/docker-cli.deb",
        "DockerCeCliPackageVersion": "5:29.6.2-1~ubuntu.24.04~noble",
        "DockerCePackageSha256": "a" * 64,
        "DockerCePackageUrl": "https://example.invalid/docker-ce.deb",
        "DockerCePackageVersion": "5:29.6.2-1~ubuntu.24.04~noble",
        "DockerContainerdPackageSha256": "a" * 64,
        "DockerContainerdPackageUrl": "https://example.invalid/containerd.deb",
        "DockerContainerdPackageVersion": "2.2.6-1~ubuntu.24.04~noble",
        "DockerSigningKeyFingerprint": "A" * 40,
        "DockerSigningKeySha256": "a" * 64,
        "DockerSigningKeyUrl": "https://example.invalid/docker.gpg",
        "HelmSha256": "a" * 64,
        "HelmUrl": "https://example.invalid/helm.tar.gz",
        "HelmVersion": "v4.2.3",
        "HostArtifactManifestGzipBase64": "A" * 3072,
        "HostArtifactManifestSha256": "a" * 64,
        "K3sBinarySha256": "a" * 64,
        "K3sBinaryUrl": "https://example.invalid/k3s",
        "K3sInstallScriptSha256": "a" * 64,
        "K3sInstallScriptUrl": "https://example.invalid/k3s-install",
        "K3sVersion": "v1.36.2+k3s1",
        "PythonBuild": "20260728",
        "PythonSha256": "a" * 64,
        "PythonUrl": "https://example.invalid/python.tar.gz",
        "PythonVersion": "3.14.6",
        "RetainedRootPath": "/srv/workflow-control-center",
        "RetainedVolumeId": "vol-0123456789abcdef0",
        "UvSha256": "a" * 64,
        "UvUrl": "https://example.invalid/uv.tar.gz",
        "UvVersion": "0.12.0",
    }
    rendered_user_data = user_data.replace("${!Version}", "${Version}")
    for parameter_name, value in parameter_by_name_map.items():
        rendered_user_data = rendered_user_data.replace(
            f"${{{parameter_name}}}",
            value,
        )
    assert "${" not in rendered_user_data.replace("${Version}", "")
    assert len(rendered_user_data.encode()) <= 16 * 1024
    result = subprocess.run(
        ["bash", "-n"],
        input=rendered_user_data,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_retained_snapshot_policy_requires_provider_enabled_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CloudFormation success must not hide an inoperable DLM provider policy."""

    environment = _environment_get(tmp_path)
    policy_payload = {
        "Policy": {
            "State": "ERROR",
            "StatusMessage": "Duplicate tag key 'ManagedBy' specified.",
        }
    }
    monkeypatch.setattr(
        environment,
        "_stack_resource_id_by_logical_name_map_get",
        lambda stack_name: {
            "RetainedSnapshotLifecyclePolicy": "policy-0123456789abcdef0"
        },
    )

    def aws_json_get(aws_argument_list: list[str]) -> dict[str, object]:
        """Return one controlled DLM provider status."""

        assert aws_argument_list == [
            "dlm",
            "get-lifecycle-policy",
            "--policy-id",
            "policy-0123456789abcdef0",
        ]
        return policy_payload

    monkeypatch.setattr(environment, "_aws_json_get", aws_json_get)

    assert environment._retained_snapshot_policy_status_get() == {
        "policy_id": "policy-0123456789abcdef0",
        "state": "ERROR",
        "status_message": "Duplicate tag key 'ManagedBy' specified.",
    }
    with pytest.raises(
        DevelopmentEnvironmentError,
        match="Retained snapshot lifecycle policy is ERROR",
    ):
        environment._retained_snapshot_policy_validate()

    policy_payload["Policy"] = {"State": "ENABLED", "StatusMessage": ""}
    environment._retained_snapshot_policy_validate()


def test_data_plane_template_adds_compute_trust_without_narrowing_platform_permissions() -> (
    None
):
    """Data-plane template must keep universal platform authority and add EC2 trust."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(
        project_root_path, "workflow-control-center-development.yaml"
    )
    platform_role = template["Resources"]["PlatformRole"]["Properties"]
    assert platform_role["ManagedPolicyArns"] == [
        {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AdministratorAccess"}
    ]
    trust_statement_list = platform_role["AssumeRolePolicyDocument"]["Statement"]
    assert any(
        statement.get("Principal") == {"Service": "ec2.amazonaws.com"}
        for statement in trust_statement_list
    )
    assert template["Parameters"]["UiOrigin"]["Default"] == "http://localhost:8080"


def test_data_plane_buckets_expose_ranges_and_reject_explicit_wrong_encryption() -> (
    None
):
    """Direct S3 may use bucket defaults, but explicit encryption must use the exact stack key."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(
        project_root_path, "workflow-control-center-development.yaml"
    )
    resource_by_name_map = template["Resources"]
    assert isinstance(resource_by_name_map, dict)
    expected_exposed_header_set = {
        "Accept-Ranges",
        "Content-Length",
        "Content-Range",
        "ETag",
        "x-amz-checksum-sha256",
        "x-amz-version-id",
    }
    for bucket_name in ("DataBucket", "SecretBucket", "ResultBucket"):
        cors_rule = resource_by_name_map[bucket_name]["Properties"][
            "CorsConfiguration"
        ]["CorsRules"][0]
        assert set(cors_rule["ExposedHeaders"]) == expected_exposed_header_set

    for bucket_name in (
        "DataBucket",
        "SecretBucket",
        "ResultBucket",
        "ObservabilityBucket",
    ):
        statement_by_sid_map = {
            statement["Sid"]: statement
            for statement in resource_by_name_map[f"{bucket_name}Policy"]["Properties"][
                "PolicyDocument"
            ]["Statement"]
        }
        assert statement_by_sid_map["DenyExplicitNonKmsEncryption"]["Condition"] == {
            "Null": {"s3:x-amz-server-side-encryption": "false"},
            "StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"},
        }
        assert statement_by_sid_map["DenyUnexpectedKmsKey"]["Condition"] == {
            "Null": {"s3:x-amz-server-side-encryption-aws-kms-key-id": "false"},
            "StringNotEquals": {
                "s3:x-amz-server-side-encryption-aws-kms-key-id": {
                    "Fn::GetAtt": ["StorageKmsKey", "Arn"]
                }
            },
        }
        assert statement_by_sid_map["DenyCustomerProvidedEncryption"]["Condition"] == {
            "Null": {"s3:x-amz-server-side-encryption-customer-algorithm": "false"}
        }


def test_data_plane_retains_state_and_delegates_history_cleanup_to_product() -> None:
    """Stack lifecycle cannot delete customer state or blanket-expire Data and Secret history."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(
        project_root_path, "workflow-control-center-development.yaml"
    )
    resource_by_name_map = template["Resources"]
    for resource_name in (
        "StorageKmsKey",
        "DataBucket",
        "SecretBucket",
        "ResultBucket",
        "ObservabilityBucket",
    ):
        resource = resource_by_name_map[resource_name]
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"

    for bucket_name in ("DataBucket", "SecretBucket"):
        lifecycle_rule_list = resource_by_name_map[bucket_name]["Properties"][
            "LifecycleConfiguration"
        ]["Rules"]
        assert lifecycle_rule_list == [
            {
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                "Id": "AbortIncompleteMultipartUploads",
                "Prefix": ".platform/staging/",
                "Status": "Enabled",
            }
        ]
        assert "NoncurrentVersionExpirationInDays" not in json.dumps(
            lifecycle_rule_list
        )

    result_lifecycle_text = json.dumps(
        resource_by_name_map["ResultBucket"]["Properties"]["LifecycleConfiguration"][
            "Rules"
        ]
    )
    observability_lifecycle_text = json.dumps(
        resource_by_name_map["ObservabilityBucket"]["Properties"][
            "LifecycleConfiguration"
        ]["Rules"]
    )
    assert "data-download/" in result_lifecycle_text
    assert "athena-result/" in result_lifecycle_text
    assert "source-map/" in observability_lifecycle_text
    assert "cloudformation-template/" in observability_lifecycle_text
    assert "ExpireCloudFormationTemplatesAfterThirtyDays" in (
        observability_lifecycle_text
    )


def test_data_plane_enforces_account_public_block_and_tag_derived_tenant_paths() -> (
    None
):
    """Permanent tenant authority is tag-scoped for every bucket and never bucket-wide."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(
        project_root_path, "workflow-control-center-development.yaml"
    )
    resource_by_name_map = template["Resources"]
    account_block = resource_by_name_map["AccountPublicAccessBlock"]
    assert account_block["Type"] == "Custom::S3AccountPublicAccessBlock"
    assert account_block["DeletionPolicy"] == "Retain"
    assert account_block["UpdateReplacePolicy"] == "Retain"
    assert account_block["Properties"] == {
        "AccountId": {"Ref": "AWS::AccountId"},
        "ServiceToken": {"Fn::GetAtt": ["AccountPublicAccessBlockFunction", "Arn"]},
    }
    function = resource_by_name_map["AccountPublicAccessBlockFunction"]["Properties"]
    assert function["Runtime"] == "python3.14"
    assert function["Architectures"] == ["arm64"]
    assert "put_public_access_block" in function["Code"]["ZipFile"]
    assert 'event["RequestType"] != "Delete"' in function["Code"]["ZipFile"]
    function_policy_text = json.dumps(
        resource_by_name_map["AccountPublicAccessBlockFunctionRole"]["Properties"][
            "Policies"
        ]
    )
    assert "s3:PutAccountPublicAccessBlock" in function_policy_text
    assert "s3:GetAccountPublicAccessBlock" in function_policy_text

    statement_list = resource_by_name_map["DataCredentialRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    for role_name in ("DataCredentialRole", "QueryRole"):
        trust_statement = resource_by_name_map[role_name]["Properties"][
            "AssumeRolePolicyDocument"
        ]["Statement"][0]
        assert trust_statement["Condition"]["StringLike"] == {
            "aws:RequestTag/UserDataRootId": "?" * 32
        }
        assert "StringNotLike" not in trust_statement["Condition"]
    statement_by_sid_map = {statement["Sid"]: statement for statement in statement_list}
    for sid in (
        "DataBucketOwnerList",
        "SecretBucketOwnerList",
        "ResultBucketOwnerList",
    ):
        condition_text = json.dumps(statement_by_sid_map[sid]["Condition"])
        assert "${aws:PrincipalTag/UserDataRootId}" in condition_text
    object_statement_name_set = {
        "CanonicalDataObjectsRead",
        "CanonicalSecretObjectsRead",
        "OwnerStagingObjects",
        "OwnerResultObjectsRead",
    }
    for sid in object_statement_name_set:
        resource_text = json.dumps(statement_by_sid_map[sid]["Resource"])
        assert "PrincipalTag/UserDataRootId" in resource_text
        assert resource_text not in {
            json.dumps({"Fn::Sub": "${DataBucket.Arn}/*"}),
            json.dumps({"Fn::Sub": "${SecretBucket.Arn}/*"}),
            json.dumps({"Fn::Sub": "${ResultBucket.Arn}/*"}),
        }
    serialized_policy = json.dumps(statement_list)
    assert '"Fn::Sub": "${DataBucket.Arn}/*"' not in serialized_policy
    assert '"Fn::Sub": "${SecretBucket.Arn}/*"' not in serialized_policy
    assert '"Fn::Sub": "${ResultBucket.Arn}/*"' not in serialized_policy
    assert statement_by_sid_map["StorageKms"]["Condition"] == {
        "StringEquals": {
            "kms:EncryptionContext:aws:s3:arn": [
                {"Fn::GetAtt": ["DataBucket", "Arn"]},
                {"Fn::GetAtt": ["SecretBucket", "Arn"]},
                {"Fn::GetAtt": ["ResultBucket", "Arn"]},
            ],
            "kms:ViaService": {"Fn::Sub": "s3.${AWS::Region}.${AWS::URLSuffix}"},
        }
    }

    query_statement_by_sid_map = {
        statement["Sid"]: statement
        for statement in resource_by_name_map["QueryRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
    }
    assert query_statement_by_sid_map["ResultKms"]["Condition"]["StringEquals"][
        "kms:ViaService"
    ] == {"Fn::Sub": "s3.${AWS::Region}.${AWS::URLSuffix}"}

    lake_formation_statement_by_sid_map = {
        statement["Sid"]: statement
        for statement in resource_by_name_map["LakeFormationDataAccessPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"]
    }
    assert lake_formation_statement_by_sid_map["StorageKms"]["Condition"] == {
        "StringEquals": {
            "kms:EncryptionContext:aws:s3:arn": {"Fn::GetAtt": ["DataBucket", "Arn"]},
            "kms:ViaService": {"Fn::Sub": "s3.${AWS::Region}.${AWS::URLSuffix}"},
        }
    }
    storage_key_statement_by_sid_map = {
        statement["Sid"]: statement
        for statement in resource_by_name_map["StorageKmsKey"]["Properties"][
            "KeyPolicy"
        ]["Statement"]
    }
    assert storage_key_statement_by_sid_map["AllowLakeFormationDataAccess"][
        "Condition"
    ] == {
        "StringEquals": {
            "kms:EncryptionContext:aws:s3:arn": {
                "Fn::If": [
                    "IsPrimaryEnvironment",
                    {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:s3:::workflow-control-center-devel-"
                            "${AWS::AccountId}-data"
                        )
                    },
                    {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:s3:::workflow-control-center-"
                            "${EnvironmentName}-${AWS::AccountId}-data"
                        )
                    },
                ]
            },
            "kms:ViaService": {"Fn::Sub": "s3.${AWS::Region}.${AWS::URLSuffix}"},
        }
    }


def test_runtime_platform_accepts_one_linux_arm64_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runtime platform discovery must normalize one homogeneous eligible node set."""

    environment = _environment_get(tmp_path)
    node_payload = {
        "items": [
            {
                "status": {
                    "nodeInfo": {"architecture": "arm64", "operatingSystem": "linux"}
                }
            },
            {
                "status": {
                    "nodeInfo": {"architecture": "arm64", "operatingSystem": "linux"}
                }
            },
        ]
    }

    def ssh_run(
        remote_command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Return the controlled node payload.

        Args:
            remote_command_list: Ignored remote command.
            ssh_control_path: Ignored control socket.
            should_capture: Ignored capture choice.

        Returns:
            Successful remote result.
        """

        del remote_command_list, ssh_control_path, should_capture
        return subprocess.CompletedProcess([], 0, json.dumps(node_payload), "")

    monkeypatch.setattr(environment, "_ssh_run", ssh_run)
    assert environment._runtime_platform_get(tmp_path / "control") == "linux/arm64"


def test_runtime_platform_rejects_mixed_eligible_nodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runtime platform discovery must reject mixed eligible architectures before build."""

    environment = _environment_get(tmp_path)
    node_payload = {
        "items": [
            {
                "status": {
                    "nodeInfo": {"architecture": "arm64", "operatingSystem": "linux"}
                }
            },
            {
                "status": {
                    "nodeInfo": {"architecture": "amd64", "operatingSystem": "linux"}
                }
            },
        ]
    }

    def ssh_run(
        remote_command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Return the controlled mixed node payload.

        Args:
            remote_command_list: Ignored remote command.
            ssh_control_path: Ignored control socket.
            should_capture: Ignored capture choice.

        Returns:
            Successful remote result.
        """

        del remote_command_list, ssh_control_path, should_capture
        return subprocess.CompletedProcess([], 0, json.dumps(node_payload), "")

    monkeypatch.setattr(environment, "_ssh_run", ssh_run)
    with pytest.raises(DevelopmentEnvironmentError, match="platforms are mixed"):
        environment._runtime_platform_get(tmp_path / "control")


def test_price_lookup_requires_one_exact_current_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cost review must select one exact current AWS price at the declared unit and usage type."""

    environment = _environment_get(tmp_path)
    product_payload = {
        "product": {"attributes": {"usagetype": "EBS:VolumeUsage.gp3"}},
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "endRange": "Inf",
                            "pricePerUnit": {"USD": "0.0800000000"},
                            "unit": "GB-Mo",
                        }
                    }
                }
            }
        },
    }
    monkeypatch.setattr(
        environment,
        "_aws_json_get",
        lambda aws_argument_list: {"PriceList": [json.dumps(product_payload)]},
    )

    price = environment._price_usd_get(
        {"regionCode": "us-east-1", "volumeApiName": "gp3"},
        unit="GB-Mo",
        usage_type="EBS:VolumeUsage.gp3",
    )
    assert price == Decimal("0.0800000000")


def test_price_dimension_lookup_preserves_every_tier_and_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Usage-based review must retain exact tier boundaries instead of cherry-picking one rate."""

    environment = _environment_get(tmp_path)
    product_payload = {
        "product": {"attributes": {"usagetype": "Requests-Tier1"}},
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "second": {
                            "beginRange": "1000",
                            "endRange": "Inf",
                            "pricePerUnit": {"USD": "0.0000040000"},
                            "unit": "Requests",
                        },
                        "first": {
                            "beginRange": "0",
                            "endRange": "1000",
                            "pricePerUnit": {"USD": "0.0000050000"},
                            "unit": "Requests",
                        },
                    }
                }
            }
        },
    }

    def aws_json_get(aws_argument_list: list[str]) -> dict[str, object]:
        """Verify the exact service and filters while returning controlled tiers."""

        assert aws_argument_list[:5] == [
            "pricing",
            "get-products",
            "--service-code",
            "AmazonS3",
            "--max-results",
        ]
        assert "Type=TERM_MATCH,Field=regionCode,Value=us-east-1" in aws_argument_list
        return {"PriceList": [json.dumps(product_payload)]}

    monkeypatch.setattr(environment, "_aws_json_get", aws_json_get)

    assert environment._price_dimension_list_get(
        service_code="AmazonS3",
        filter_by_field_map={"regionCode": "us-east-1"},
        usage_type="Requests-Tier1",
    ) == [
        {
            "begin_range": "0",
            "end_range": "1000",
            "price_per_unit_usd": "0.0000050000",
            "unit": "Requests",
        },
        {
            "begin_range": "1000",
            "end_range": "Inf",
            "price_per_unit_usd": "0.0000040000",
            "unit": "Requests",
        },
    ]


def test_cost_review_includes_one_bounded_retained_rollback_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The checkpoint must price root, current retained, and one previous rollback."""

    environment = _environment_get(tmp_path)

    def price_usd_get(
        filter_by_field_map: dict[str, str],
        *,
        unit: str,
        usage_type: str,
    ) -> Decimal:
        """Return controlled prices for each declared cost dimension."""

        del filter_by_field_map
        if unit == "Hrs":
            assert usage_type == ""
            return Decimal("0.10")
        if usage_type == "EBS:VolumeUsage.gp3":
            return Decimal("0.08")
        assert usage_type == "EBS:SnapshotUsage"
        return Decimal("0.05")

    monkeypatch.setattr(environment, "_price_usd_get", price_usd_get)

    def price_dimension_list_get(
        *,
        service_code: str,
        filter_by_field_map: dict[str, str],
        usage_type: str,
    ) -> list[dict[str, str]]:
        """Return one controlled current meter rate for every usage service."""

        del service_code, filter_by_field_map
        return [
            {
                "begin_range": "0",
                "end_range": "Inf",
                "price_per_unit_usd": (
                    "1.0000000000"
                    if usage_type == "us-east-1-KMS-Keys"
                    else "0.0100000000"
                ),
                "unit": ("Keys" if usage_type == "us-east-1-KMS-Keys" else "Requests"),
            }
        ]

    monkeypatch.setattr(
        environment,
        "_price_dimension_list_get",
        price_dimension_list_get,
    )

    environment._cost_review_record()

    payload = json.loads(
        (tmp_path / ".local/cost-review.json").read_text(encoding="utf-8")
    )
    assert payload["assumption"] == {
        "active_hour_count_monthly": 80,
        "gp3_gib_count_max": 260,
        "kms_customer_managed_key_count": 1,
        "snapshot_retention_count": 7,
        "snapshot_source_volume_gib_count_max": 80,
        "snapshot_stored_gib_count_max": 560,
    }
    assert payload["estimated_monthly_usd"] == {
        "compute": "8.00",
        "gp3_max": "20.80",
        "kms_customer_managed_key": "1.00",
        "snapshot_max": "28.00",
        "total_fixed_max": "57.80",
    }
    assert payload["architecture_delta_monthly_usd"] == {
        "bounded_retained_rollback_volume_max": "6.40",
        "total_max": "6.40",
    }
    assert set(payload["usage_based_service_by_name_map"]) == {
        "api_gateway",
        "athena",
        "data_transfer",
        "glue",
        "kms",
        "s3",
    }
    assert payload["usage_based_service_by_name_map"]["kms"]["price_meter_by_name_map"][
        "customer_managed_key"
    ]["price_dimension_list"][0] == {
        "begin_range": "0",
        "end_range": "Inf",
        "price_per_unit_usd": "1.0000000000",
        "unit": "Keys",
    }


def test_drift_preflight_requires_complete_stack_parameters_and_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation preflight must prove the complete live stack contract before drift."""

    environment = _environment_get(tmp_path)
    operation_list: list[str] = []
    monkeypatch.setattr(
        environment,
        "_stack_payload_get",
        lambda stack_name, is_required: {"StackStatus": "UPDATE_COMPLETE"},
    )
    monkeypatch.setattr(
        environment,
        "_stack_parameter_by_name_map_get",
        lambda stack_name: operation_list.append("parameters") or {"A": "B"},
    )
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: operation_list.append("outputs") or {"A": "B"},
    )

    def aws_json_get(argument_list: list[str]) -> dict[str, object]:
        """Return the controlled drift lifecycle."""

        if argument_list[1] == "detect-stack-drift":
            operation_list.append("detect")
            return {"StackDriftDetectionId": "drift-1"}
        operation_list.append("inspect")
        return {
            "DetectionStatus": "DETECTION_COMPLETE",
            "StackDriftStatus": "IN_SYNC",
        }

    monkeypatch.setattr(environment, "_aws_json_get", aws_json_get)

    environment._stack_drift_validate("stack-a")

    assert operation_list == ["parameters", "outputs", "detect", "inspect"]
    assert capsys.readouterr().out == "OK: stack stack-a drift is IN_SYNC\n"


def test_drift_preflight_allows_only_exact_retired_launch_template_tags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The interrupted cutover may reconcile two exact tags and no other drift."""

    environment = _environment_get(tmp_path)
    expected_property_by_name_map = {
        "Tags": [
            {"Key": "Project", "Value": "workflow-control-center"},
            {"Key": "ManagedBy", "Value": "CloudFormation"},
            {"Key": "EnvironmentName", "Value": "primary"},
            {"Key": "Environment", "Value": "development"},
        ],
        "Volumes": [
            {"VolumeId": "vol-retained", "Device": "/dev/sdf"},
            {"VolumeId": "vol-root", "Device": "/dev/sda1"},
        ],
    }
    actual_property_by_name_map = {
        "Tags": [
            {"Key": "Project", "Value": "workflow-control-center"},
            {"Key": "Environment", "Value": "development"},
            {"Key": "EnvironmentName", "Value": "primary"},
            {"Key": "ReplacementSlot", "Value": "a"},
            {"Key": "ManagedBy", "Value": "CloudFormation"},
            {"Key": "Name", "Value": "workflow-control-center-development"},
        ],
        "Volumes": list(reversed(expected_property_by_name_map["Volumes"])),
    }
    property_difference_list = [
        {
            "PropertyPath": "/Tags/5",
            "ExpectedValue": "null",
            "ActualValue": json.dumps(
                {
                    "Key": "Name",
                    "Value": "workflow-control-center-development",
                }
            ),
            "DifferenceType": "ADD",
        },
        {
            "PropertyPath": "/Tags/3",
            "ExpectedValue": "null",
            "ActualValue": json.dumps({"Key": "ReplacementSlot", "Value": "a"}),
            "DifferenceType": "ADD",
        },
    ]
    resource_drift_payload = {
        "StackResourceDrifts": [
            {
                "LogicalResourceId": "DevelopmentInstance",
                "ResourceType": "AWS::EC2::Instance",
                "ExpectedProperties": json.dumps(expected_property_by_name_map),
                "ActualProperties": json.dumps(actual_property_by_name_map),
                "PropertyDifferences": property_difference_list,
                "StackResourceDriftStatus": "MODIFIED",
            }
        ]
    }
    monkeypatch.setattr(
        environment,
        "_stack_payload_get",
        lambda stack_name, is_required: {"StackStatus": "UPDATE_COMPLETE"},
    )
    monkeypatch.setattr(
        environment,
        "_stack_parameter_by_name_map_get",
        lambda stack_name: {
            "InstanceSlot": "a",
            "NotificationTagValue": "workflow-control-center",
        },
    )
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"InstanceId": "i-test"},
    )

    def aws_json_get(argument_list: list[str]) -> dict[str, object]:
        """Return one exact drift lifecycle."""

        operation = argument_list[1]
        if operation == "detect-stack-drift":
            return {"StackDriftDetectionId": "drift-1"}
        if operation == "describe-stack-drift-detection-status":
            return {
                "DetectionStatus": "DETECTION_COMPLETE",
                "DriftedStackResourceCount": 1,
                "StackDriftStatus": "DRIFTED",
            }
        assert operation == "describe-stack-resource-drifts"
        return resource_drift_payload

    monkeypatch.setattr(environment, "_aws_json_get", aws_json_get)

    environment._stack_drift_validate(
        "workflow-control-center-development-compute",
        should_allow_instance_launch_template_tag_drift=True,
    )

    assert "has only the reconcilable instance launch-template tag drift" in (
        capsys.readouterr().out
    )
    property_difference_list[0]["ActualValue"] = json.dumps(
        {"Key": "Name", "Value": "unexpected"}
    )
    assert not environment._instance_launch_template_tag_drift_is_exact(
        stack_name="workflow-control-center-development-compute",
        parameter_by_name_map={
            "InstanceSlot": "a",
            "NotificationTagValue": "workflow-control-center",
        },
        drifted_resource_count=1,
    )


def test_source_archive_is_deterministic_and_excludes_untracked_files(
    tmp_path: Path,
) -> None:
    """Source archive must contain only deterministic tracked content and its exact manifest."""

    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    _git_run(repository_path, ["init", "--initial-branch=main"])
    _git_run(repository_path, ["config", "user.email", "test@example.com"])
    _git_run(repository_path, ["config", "user.name", "Test"])
    (repository_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repository_path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    _git_run(repository_path, ["add", "tracked.txt"])
    _git_run(repository_path, ["commit", "-m", "fixture"])

    environment = _environment_get(tmp_path)
    archive_path_a = tmp_path / "a.tar"
    archive_path_b = tmp_path / "b.tar"
    manifest_path_a = tmp_path / "a.json"
    manifest_path_b = tmp_path / "b.json"
    manifest_a = environment._source_archive_create(
        archive_path=archive_path_a,
        manifest_path=manifest_path_a,
        repository_name="workflow-infrastructure",
        repository_path=repository_path,
    )
    manifest_b = environment._source_archive_create(
        archive_path=archive_path_b,
        manifest_path=manifest_path_b,
        repository_name="workflow-infrastructure",
        repository_path=repository_path,
    )

    assert archive_path_a.read_bytes() == archive_path_b.read_bytes()
    assert manifest_a == manifest_b
    with tarfile.open(archive_path_a) as archive:
        assert archive.getnames() == ["tracked.txt"]
    assert manifest_a["file_sha256_by_path_map"] == {
        "tracked.txt": "e544535ca87c45bbcf3495423e0781d4fba16276f78a5e07a1049447cdc1e63e"
    }
    assert manifest_a["source_kind"] == "exact_checkout"


def test_source_archive_transfer_refuses_to_replace_immutable_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reused release/source identity must fail instead of deleting accepted bytes."""

    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    _git_run(repository_path, ["init", "--initial-branch=main"])
    _git_run(repository_path, ["config", "user.email", "test@example.com"])
    _git_run(repository_path, ["config", "user.name", "Test"])
    (repository_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git_run(repository_path, ["add", "tracked.txt"])
    _git_run(repository_path, ["commit", "-m", "fixture"])

    environment = _environment_get(tmp_path)
    archive_path = tmp_path / "source.tar"
    manifest_path = tmp_path / "source.json"
    manifest = environment._source_archive_create(
        archive_path=archive_path,
        manifest_path=manifest_path,
        repository_name="workflow-infrastructure",
        repository_path=repository_path,
    )
    release_name = f"transfer{tmp_path.name}"
    staging_path = Path(f"/tmp/workflow-source-{release_name}-workflow-infrastructure")
    remote_release_root_path = tmp_path / "remote-release"
    real_run = environment._runner.run

    def run(
        command_list: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Materialize the rsync payload locally and delegate ordinary commands."""

        if command_list[0] != "rsync":
            return real_run(command_list, **kwargs)
        destination_path = Path(command_list[-1].split(":", maxsplit=1)[1])
        destination_path.mkdir(parents=True)
        for source_path_text in command_list[-3:-1]:
            shutil.copy2(source_path_text, destination_path)
        return subprocess.CompletedProcess(command_list, 0, "", "")

    def ssh_run(
        command_list: list[str],
        *,
        ssh_control_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Execute the generated remote verification command on the test host."""

        assert ssh_control_path == tmp_path / "control"
        assert command_list[0] == "sudo"
        return subprocess.run(
            command_list[1:],
            check=True,
            capture_output=True,
            text=True,
        )

    monkeypatch.setattr(environment._runner, "run", run)
    monkeypatch.setattr(environment, "_ssh_run", ssh_run)
    try:
        environment._source_archive_transfer(
            archive_path=archive_path,
            manifest=manifest,
            manifest_path=manifest_path,
            release_name=release_name,
            remote_release_root_path=remote_release_root_path,
            repository_name="workflow-infrastructure",
            ssh_control_path=tmp_path / "control",
        )
        accepted_path = (
            remote_release_root_path
            / release_name
            / "sources"
            / "workflow-infrastructure"
            / "tracked.txt"
        )
        assert accepted_path.read_text(encoding="utf-8") == "tracked\n"

        with pytest.raises(subprocess.CalledProcessError):
            environment._source_archive_transfer(
                archive_path=archive_path,
                manifest=manifest,
                manifest_path=manifest_path,
                release_name=release_name,
                remote_release_root_path=remote_release_root_path,
                repository_name="workflow-infrastructure",
                ssh_control_path=tmp_path / "control",
            )
        assert accepted_path.read_text(encoding="utf-8") == "tracked\n"
    finally:
        shutil.rmtree(staging_path, ignore_errors=True)


def test_moving_source_resolution_retries_race_and_supports_one_exact_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resolve remote default HEAD once, retry a race, and keep override provenance explicit."""

    remote_path = tmp_path / "contract.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=trunk", str(remote_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    repository_path = tmp_path / "contract"
    subprocess.run(
        ["git", "clone", str(remote_path), str(repository_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(repository_path, ["config", "user.email", "test@example.com"])
    _git_run(repository_path, ["config", "user.name", "Test"])
    (repository_path / "pyproject.toml").write_text(
        '[project]\nname = "workflow-container-contract"\nversion = "0.6.0"\n',
        encoding="utf-8",
    )
    (repository_path / "workflow_container_contract").mkdir()
    (repository_path / "workflow_container_contract/__init__.py").write_text(
        'VERSION = "first"\n',
        encoding="utf-8",
    )
    _git_run(repository_path, ["add", "."])
    _git_run(repository_path, ["commit", "-m", "first"])
    first_commit_sha = subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git_run(repository_path, ["push", "-u", "origin", "trunk"])
    (repository_path / "pyproject.toml").write_text(
        '[project]\nname = "workflow-container-contract"\nversion = "0.7.0"\n',
        encoding="utf-8",
    )
    (repository_path / "workflow_container_contract/__init__.py").write_text(
        'VERSION = "second"\n',
        encoding="utf-8",
    )
    _git_run(repository_path, ["add", "."])
    _git_run(repository_path, ["commit", "-m", "second"])
    second_commit_sha = subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git_run(repository_path, ["push", "origin", "trunk"])
    (repository_path / "untracked.txt").write_text("not exported\n", encoding="utf-8")

    monkeypatch.setitem(
        development_environment.REPOSITORY_URL_BY_NAME_MAP,
        "workflow-container-contract",
        str(remote_path),
    )
    environment = _environment_get(tmp_path)
    remote_head_by_field_map_list = [
        {"commit_sha": first_commit_sha, "resolved_ref": "refs/heads/trunk"},
        {"commit_sha": second_commit_sha, "resolved_ref": "refs/heads/trunk"},
        {"commit_sha": second_commit_sha, "resolved_ref": "refs/heads/trunk"},
    ]

    def moving_source_head_by_field_map_get(*, repository_url: str) -> dict[str, str]:
        """Return one controlled remote HEAD observation.

        Args:
            repository_url: Expected fixture repository URL.

        Returns:
            Next controlled remote HEAD identity.
        """

        assert repository_url == str(remote_path)
        return remote_head_by_field_map_list.pop(0)

    real_head_get = environment._moving_source_head_by_field_map_get
    monkeypatch.setattr(
        environment,
        "_moving_source_head_by_field_map_get",
        moving_source_head_by_field_map_get,
    )
    latest_archive_path = tmp_path / "latest.tar"
    latest_manifest = environment._moving_source_archive_create(
        archive_path=latest_archive_path,
        exact_override_commit="",
        manifest_path=tmp_path / "latest.json",
        repository_name="workflow-container-contract",
    )

    assert remote_head_by_field_map_list == []
    assert latest_manifest["source_kind"] == "resolved_moving_source"
    assert latest_manifest["requested_selector"] == "HEAD"
    assert latest_manifest["resolved_ref"] == "refs/heads/trunk"
    assert latest_manifest["commit_sha"] == second_commit_sha
    assert latest_manifest["package_version"] == "0.7.0"
    assert "override_identity" not in latest_manifest
    with tarfile.open(latest_archive_path) as archive:
        assert set(archive.getnames()) == {
            "pyproject.toml",
            "workflow_container_contract",
            "workflow_container_contract/__init__.py",
        }

    monkeypatch.setattr(
        environment,
        "_moving_source_head_by_field_map_get",
        real_head_get,
    )
    override_manifest = environment._moving_source_archive_create(
        archive_path=tmp_path / "override.tar",
        exact_override_commit=first_commit_sha,
        manifest_path=tmp_path / "override.json",
        repository_name="workflow-container-contract",
    )

    assert override_manifest["commit_sha"] == first_commit_sha
    assert override_manifest["package_version"] == "0.6.0"
    assert override_manifest["override_identity"] == first_commit_sha
    assert override_manifest["override_reason"] == "explicit operator deploy argument"

    tree_sha = subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "rev-parse",
            f"{first_commit_sha}^{{tree}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(DevelopmentEnvironmentError, match="is not a commit"):
        environment._moving_source_archive_create(
            archive_path=tmp_path / "tree-override.tar",
            exact_override_commit=tree_sha,
            manifest_path=tmp_path / "tree-override.json",
            repository_name="workflow-container-contract",
        )


def test_source_repository_requires_clean_exact_published_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Source validation must reject dirty and unpublished repository states."""

    remote_path = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote_path)], check=True
    )
    repository_path = tmp_path / "repository"
    subprocess.run(
        ["git", "clone", str(remote_path), str(repository_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(repository_path, ["config", "user.email", "test@example.com"])
    _git_run(repository_path, ["config", "user.name", "Test"])
    (repository_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git_run(repository_path, ["add", "tracked.txt"])
    _git_run(repository_path, ["commit", "-m", "initial"])
    _git_run(repository_path, ["push", "-u", "origin", "main"])

    monkeypatch.setitem(
        development_environment.REPOSITORY_URL_BY_NAME_MAP,
        "workflow-infrastructure",
        str(remote_path),
    )
    environment = _environment_get(tmp_path)
    environment._source_repository_validate(repository_path, "workflow-infrastructure")

    (repository_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(DevelopmentEnvironmentError, match="worktree is not clean"):
        environment._source_repository_validate(
            repository_path, "workflow-infrastructure"
        )
    _git_run(repository_path, ["restore", "tracked.txt"])

    (repository_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git_run(repository_path, ["add", "tracked.txt"])
    _git_run(repository_path, ["commit", "-m", "unpublished"])
    with pytest.raises(DevelopmentEnvironmentError, match="not exact origin/main"):
        environment._source_repository_validate(
            repository_path, "workflow-infrastructure"
        )


def test_service_readiness_checks_every_required_aws_control_plane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Account readiness must prove S3, KMS, Athena, and CloudFormation access."""

    environment = _environment_get(tmp_path)
    command_list: list[list[str]] = []

    def aws_run(
        aws_argument_list: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Record one controlled readiness command.

        Args:
            aws_argument_list: AWS arguments.
            check: Required failure policy.

        Returns:
            Successful AWS result.
        """

        assert check is True
        command_list.append(aws_argument_list)
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(environment, "_aws_run", aws_run)

    environment._service_readiness_validate()
    assert command_list == [
        ["s3api", "list-buckets"],
        ["kms", "list-keys", "--limit", "1"],
        ["athena", "list-work-groups", "--max-results", "1"],
        [
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            "workflow-control-center-development",
        ],
    ]


def test_stable_data_change_allows_only_identity_preserving_conditional_dependencies(
    tmp_path: Path,
) -> None:
    """Conditional dependent updates are safe only when their source cannot be replaced."""

    environment = _environment_get(tmp_path)
    safe_detail = {
        "CausingEntity": "DataBucket.Arn",
        "ChangeSource": "ResourceAttribute",
        "Evaluation": "Dynamic",
        "Target": {"RequiresRecreation": "Conditionally"},
    }
    change_summary_list = [
        {
            "action": "Modify",
            "detail_list": [],
            "logical_resource_id": "DataBucket",
            "replacement": "False",
            "resource_type": "AWS::S3::Bucket",
        },
        {
            "action": "Modify",
            "detail_list": [safe_detail],
            "logical_resource_id": "DataLakeLocation",
            "replacement": "Conditional",
            "resource_type": "AWS::LakeFormation::Resource",
        },
    ]
    assert environment._stable_data_change_violation_list_get(change_summary_list) == []

    change_summary_list[0]["replacement"] = "True"
    assert environment._stable_data_change_violation_list_get(change_summary_list) == [
        "DataBucket",
        "DataLakeLocation",
    ]


def test_stable_data_change_proves_transitive_conditional_dependency_chain(
    tmp_path: Path,
) -> None:
    """A conditional child is safe when every transitive physical owner is stable."""

    environment = _environment_get(tmp_path)

    def dynamic_detail(
        causing_entity: str,
        *,
        change_source: str = "ResourceAttribute",
    ) -> dict[str, object]:
        return {
            "CausingEntity": causing_entity,
            "ChangeSource": change_source,
            "Evaluation": "Dynamic",
            "Target": {"RequiresRecreation": "Always"},
        }

    change_summary_list = [
        {
            "action": "Modify",
            "detail_list": [],
            "logical_resource_id": "VpnValidationRestApi",
            "replacement": "False",
            "resource_type": "AWS::ApiGateway::RestApi",
        },
        {
            "action": "Modify",
            "detail_list": [
                dynamic_detail("VpnValidationRestApi.RootResourceId"),
            ],
            "logical_resource_id": "VpnValidationIpResource",
            "replacement": "Conditional",
            "resource_type": "AWS::ApiGateway::Resource",
        },
        {
            "action": "Modify",
            "detail_list": [
                dynamic_detail(
                    "VpnValidationIpResource",
                    change_source="ResourceReference",
                ),
            ],
            "logical_resource_id": "VpnValidationGetMethod",
            "replacement": "Conditional",
            "resource_type": "AWS::ApiGateway::Method",
        },
    ]

    assert environment._stable_data_change_violation_list_get(change_summary_list) == []

    change_summary_list[0]["action"] = "Add"
    assert environment._stable_data_change_violation_list_get(change_summary_list) == [
        "VpnValidationGetMethod",
        "VpnValidationIpResource",
    ]


def test_ordinary_compute_apply_rejects_every_possible_stable_identity_replacement(
    tmp_path: Path,
) -> None:
    """Only the explicit replacement workflow may change instance or retained attachment identity."""

    environment = _environment_get(tmp_path)
    change_summary_list = [
        {
            "action": "Modify",
            "logical_resource_id": "DevelopmentHostLaunchTemplate",
            "replacement": "False",
        },
        {
            "action": "Modify",
            "logical_resource_id": "DevelopmentInstance",
            "replacement": "Conditional",
        },
        {
            "action": "Remove",
            "logical_resource_id": "RetainedVolumeAttachment",
            "replacement": "False",
        },
        {
            "action": "Remove",
            "logical_resource_id": "RetainedVolumeRestoreA",
            "replacement": "False",
        },
    ]

    assert environment._protected_identity_change_violation_list_get(
        change_summary_list=change_summary_list,
        protected_identity_logical_id_set=(
            development_environment.COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET
        ),
    ) == [
        "DevelopmentInstance",
        "RetainedVolumeAttachment",
        "RetainedVolumeRestoreA",
    ]


def test_existing_stack_resource_identity_validation_allows_only_additions(
    tmp_path: Path,
) -> None:
    """A guarded update may add resources but cannot remove or replace an existing identity."""

    environment = _environment_get(tmp_path)
    previous_identity_by_logical_id_map = {
        "DataBucket": "workflow-control-center-data",
        "StorageKey": "key-0123456789abcdef0",
    }

    environment._existing_stack_resource_identity_validate(
        current_resource_id_by_logical_name_map={
            **previous_identity_by_logical_id_map,
            "AccountPublicAccessBlock": "workflow-control-center-account-bpa",
        },
        previous_resource_id_by_logical_name_map=previous_identity_by_logical_id_map,
    )

    for current_identity_by_logical_id_map in (
        {"DataBucket": "workflow-control-center-data"},
        {
            "DataBucket": "workflow-control-center-data-replacement",
            "StorageKey": "key-0123456789abcdef0",
        },
    ):
        with pytest.raises(
            DevelopmentEnvironmentError,
            match="Stable data-plane physical resource identity changed",
        ):
            environment._existing_stack_resource_identity_validate(
                current_resource_id_by_logical_name_map=current_identity_by_logical_id_map,
                previous_resource_id_by_logical_name_map=previous_identity_by_logical_id_map,
            )


def test_replacement_parameters_select_next_slot_and_enable_creation_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit replacement makes future instance creation fail-safe before it starts."""

    environment = _environment_get(tmp_path)
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "InstanceSlot": "a",
            "LatestLaunchTemplateVersion": "7",
        },
    )

    assert environment._replacement_parameter_by_name_map_get() == {
        "InstanceSlot": "b",
        "InstanceLaunchTemplateVersion": "7",
        "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
        "ReplacementGuardScheduleState": "ENABLED",
    }


def test_replacement_detaches_retained_volume_only_after_proven_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The old stopped instance releases retained EBS before CloudFormation creates its successor."""

    environment = _environment_get(tmp_path)
    attachment = {
        "DeleteOnTermination": False,
        "Device": "/dev/sdf",
        "InstanceId": "i-old",
        "State": "attached",
        "VolumeId": "vol-retained",
    }
    state_list = [("in-use", [attachment]), ("available", [])]
    aws_argument_list_list: list[list[str]] = []
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "InstanceId": "i-old",
            "RetainedVolumeId": "vol-retained",
        },
    )
    monkeypatch.setattr(
        environment,
        "_instance_state_get",
        lambda instance_id: "stopped",
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_state_get",
        lambda **kwargs: state_list.pop(0),
    )
    monkeypatch.setattr(
        environment,
        "_aws_run",
        lambda argument_list, **kwargs: (
            aws_argument_list_list.append(argument_list)
            or subprocess.CompletedProcess(argument_list, 0, "{}", "")
        ),
    )

    environment._retained_volume_detach_for_replacement()

    assert aws_argument_list_list == [
        [
            "ec2",
            "detach-volume",
            "--device",
            "/dev/sdf",
            "--instance-id",
            "i-old",
            "--volume-id",
            "vol-retained",
        ],
        [
            "ec2",
            "wait",
            "volume-available",
            "--volume-ids",
            "vol-retained",
        ],
    ]


@pytest.mark.parametrize(
    ("current_slot", "next_slot"),
    [
        ("base", "a"),
        ("a", "b"),
        ("b", "a"),
    ],
)
def test_restore_plan_alternates_declarative_retained_volume_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    current_slot: str,
    next_slot: str,
) -> None:
    """Every restore selects a new logical volume while ordinary replacement does not."""

    environment = _environment_get(tmp_path)
    validation_argument_list: list[dict[str, str]] = []
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "RetainedVolumeId": "vol-source",
            "RetainedVolumeSlot": current_slot,
        },
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_snapshot_source_validate",
        lambda **kwargs: validation_argument_list.append(kwargs),
    )

    assert environment._retained_volume_restore_plan_get(
        snapshot_id="snap-0123456789abcdef0"
    ) == (
        "vol-source",
        {
            "RetainedVolumeSlot": next_slot,
            "RetainedVolumeSnapshotId": "snap-0123456789abcdef0",
        },
    )
    assert validation_argument_list == [
        {
            "snapshot_id": "snap-0123456789abcdef0",
            "source_volume_id": "vol-source",
        }
    ]


def test_restore_source_must_be_completed_owned_encrypted_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Restore preflight rejects a snapshot before stopping the current instance."""

    environment = _environment_get(tmp_path)
    snapshot_payload = {
        "Encrypted": True,
        "OwnerId": development_environment.AWS_ACCOUNT_ID,
        "SnapshotId": "snap-0123456789abcdef0",
        "State": "completed",
        "VolumeSize": 80,
    }
    monkeypatch.setattr(
        environment,
        "_retained_volume_payload_get",
        lambda **kwargs: {"Size": 80},
    )
    monkeypatch.setattr(
        environment,
        "_aws_json_get",
        lambda arguments: {"Snapshots": [snapshot_payload]},
    )

    environment._retained_volume_snapshot_source_validate(
        snapshot_id="snap-0123456789abcdef0",
        source_volume_id="vol-source",
    )

    snapshot_payload["State"] = "pending"
    with pytest.raises(
        DevelopmentEnvironmentError,
        match="not an exact usable encrypted source",
    ):
        environment._retained_volume_snapshot_source_validate(
            snapshot_id="snap-0123456789abcdef0",
            source_volume_id="vol-source",
        )


def test_restore_proves_distinct_snapshot_volume_and_retires_old_backup_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful restore must switch physical identity and one active DLM target."""

    environment = _environment_get(tmp_path)
    old_volume_payload = {
        "Attachments": [],
        "State": "available",
        "Tags": [
            {
                "Key": "workflow-control-center-retained-backup",
                "Value": "enabled",
            }
        ],
    }
    aws_argument_list_list: list[list[str]] = []
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"RetainedVolumeId": "vol-restored"},
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_payload_get",
        lambda **kwargs: (
            {
                "Encrypted": True,
                "SnapshotId": "snap-0123456789abcdef0",
                "Tags": [
                    {
                        "Key": "workflow-control-center-retained-backup",
                        "Value": "enabled",
                    }
                ],
            }
            if kwargs["volume_id"] == "vol-restored"
            else old_volume_payload
        ),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_state_get",
        lambda **kwargs: ("available", []),
    )

    def aws_run(
        argument_list: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        aws_argument_list_list.append(argument_list)
        old_volume_payload["Tags"] = []
        return subprocess.CompletedProcess(argument_list, 0, "{}", "")

    monkeypatch.setattr(environment, "_aws_run", aws_run)

    environment._retained_volume_snapshot_restore_validate(
        snapshot_id="snap-0123456789abcdef0",
        source_volume_id="vol-source",
    )
    environment._retained_volume_backup_disable(volume_id="vol-source")

    assert aws_argument_list_list == [
        [
            "ec2",
            "delete-tags",
            "--resources",
            "vol-source",
            "--tags",
            "Key=workflow-control-center-retained-backup",
        ]
    ]


def test_restore_cleanup_keeps_current_and_deletes_only_owned_stale_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A new restore bounds retained gp3 storage to current plus one rollback."""

    environment = _environment_get(tmp_path)
    current_volume_id = "vol-0123456789abcdef0"
    retired_volume_id = "vol-0fedcba9876543210"
    common_tag_list = [
        {"Key": "Environment", "Value": "development"},
        {"Key": "EnvironmentName", "Value": "primary"},
        {"Key": "ManagedBy", "Value": "CloudFormation"},
        {
            "Key": "Name",
            "Value": "workflow-control-center-development-retained",
        },
        {"Key": "Project", "Value": "workflow-control-center"},
        {
            "Key": "aws:cloudformation:stack-name",
            "Value": development_environment.COMPUTE_STACK_NAME,
        },
    ]
    current_volume_payload = {
        "Attachments": [{"InstanceId": "i-current"}],
        "Encrypted": True,
        "KmsKeyId": "arn:aws:kms:us-east-1:463564115167:key/test",
        "Size": 80,
        "State": "in-use",
        "Tags": [
            *common_tag_list,
            {
                "Key": "workflow-control-center-retained-backup",
                "Value": "enabled",
            },
        ],
        "VolumeId": current_volume_id,
    }
    retired_volume_payload = {
        "Attachments": [],
        "Encrypted": True,
        "KmsKeyId": "arn:aws:kms:us-east-1:463564115167:key/test",
        "Size": 80,
        "State": "available",
        "Tags": common_tag_list,
        "VolumeId": retired_volume_id,
    }
    aws_argument_list_list: list[list[str]] = []
    monkeypatch.setattr(
        environment,
        "_retained_volume_payload_get",
        lambda **kwargs: current_volume_payload,
    )
    monkeypatch.setattr(
        environment,
        "_aws_json_get",
        lambda arguments: {"Volumes": [current_volume_payload, retired_volume_payload]},
    )

    def aws_run(
        argument_list: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Record exact stale-volume deletion commands."""

        del check
        aws_argument_list_list.append(argument_list)
        return subprocess.CompletedProcess(argument_list, 0, "{}", "")

    monkeypatch.setattr(environment, "_aws_run", aws_run)

    environment._retired_retained_volume_cleanup(current_volume_id=current_volume_id)

    assert aws_argument_list_list == [
        ["ec2", "delete-volume", "--volume-id", retired_volume_id],
        [
            "ec2",
            "wait",
            "volume-deleted",
            "--volume-ids",
            retired_volume_id,
        ],
    ]


def test_instance_launch_template_version_must_match_stack_latest_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A completed replacement proves the concrete immutable version used by EC2."""

    environment = _environment_get(tmp_path)
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "InstanceId": "i-current",
            "InstanceLaunchTemplateVersion": "7",
            "LatestLaunchTemplateVersion": "7",
        },
    )
    monkeypatch.setattr(
        environment,
        "_aws_json_get",
        lambda argument_list: {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "Tags": [
                                {
                                    "Key": "aws:ec2launchtemplate:version",
                                    "Value": "7",
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    )

    environment._instance_launch_template_version_validate()


def test_failed_replacement_recovers_the_stack_declared_volume_attachment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rolled-back replacement leaves the old volume attached before returning failure."""

    environment = _environment_get(tmp_path)
    operation_list: list[str] = []
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"StopLeaseTargetArn": "arn:aws:lambda:target"},
    )
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: operation_list.append("lease"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_detach_for_replacement",
        lambda: operation_list.append("detach"),
    )
    monkeypatch.setattr(
        environment,
        "_stack_apply",
        lambda **kwargs: (_ for _ in ()).throw(
            DevelopmentEnvironmentError("replacement failed")
        ),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_attachment_ensure",
        lambda: operation_list.append("recover"),
    )
    monkeypatch.setattr(
        environment,
        "_stop_lease_delete",
        lambda: operation_list.append("delete-lease"),
    )

    with pytest.raises(DevelopmentEnvironmentError, match="replacement failed"):
        environment._replacement_stack_apply(
            parameter_by_name_map={
                "InstanceSlot": "b",
                "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
                "ReplacementGuardScheduleState": "ENABLED",
            }
        )

    assert operation_list == ["lease", "detach", "recover", "delete-lease"]


def test_first_replacement_relies_on_cloudformation_guard_before_target_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The initial legacy-stack replacement does not require the future Lambda output."""

    environment = _environment_get(tmp_path)
    operation_list: list[str] = []
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"InstanceSlot": "a"},
    )
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: pytest.fail("The not-yet-created renewable target cannot be used"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_detach_for_replacement",
        lambda: operation_list.append("detach"),
    )
    monkeypatch.setattr(
        environment,
        "_stack_apply",
        lambda **kwargs: operation_list.append(
            ("apply", kwargs["protected_identity_logical_id_set"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_attachment_validate",
        lambda: operation_list.append("attachment"),
    )
    monkeypatch.setattr(
        environment,
        "_instance_launch_template_version_validate",
        lambda: operation_list.append("version"),
    )

    environment._replacement_stack_apply(
        parameter_by_name_map={
            "InstanceSlot": "b",
            "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
            "ReplacementGuardScheduleState": "ENABLED",
        }
    )

    assert operation_list == [
        "detach",
        (
            "apply",
            development_environment.COMPUTE_RETAINED_VOLUME_LOGICAL_ID_SET,
        ),
        "attachment",
        "version",
    ]


def test_replace_uses_controlled_detach_and_creation_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Public replace disables the creation guard only after renewable lease and start."""

    environment = _environment_get(tmp_path)
    operation_list: list[object] = []
    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        environment,
        "_replacement_parameter_by_name_map_get",
        lambda: {
            "InstanceSlot": "b",
            "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
            "ReplacementGuardScheduleState": "ENABLED",
        },
    )
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: operation_list.append("drift"),
    )
    monkeypatch.setattr(
        environment,
        "stop",
        lambda **kwargs: operation_list.append(
            ("stop", kwargs["should_validate_drift"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_stack_apply",
        lambda **kwargs: operation_list.append(
            (
                kwargs["parameter_by_name_map"],
                kwargs.get("allow_retained_volume_transition", False),
            )
        ),
    )
    monkeypatch.setattr(
        environment,
        "start",
        lambda **kwargs: operation_list.append(
            ("start", kwargs["should_publish_infrastructure_source"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_guard_disable",
        lambda: operation_list.append("disable-guard"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_product_release_link_restore",
        lambda: operation_list.append("link"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_apply_run",
        lambda: operation_list.append("recover"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_acceptance_run",
        lambda: operation_list.append("accept"),
    )

    environment.replace()

    assert operation_list == [
        "drift",
        ("stop", False),
        (
            {
                "InstanceSlot": "b",
                "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
                "ReplacementGuardScheduleState": "ENABLED",
            },
            False,
        ),
        ("start", True),
        "disable-guard",
        "link",
        "recover",
        "accept",
    ]


def test_restore_combines_snapshot_and_creation_guard_in_controlled_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Snapshot restore uses the same exact replacement boundary as retained-volume reuse."""

    environment = _environment_get(tmp_path)
    operation_list: list[object] = []
    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        environment,
        "_replacement_parameter_by_name_map_get",
        lambda: {
            "InstanceSlot": "b",
            "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
            "ReplacementGuardScheduleState": "ENABLED",
        },
    )
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: operation_list.append("drift"),
    )
    monkeypatch.setattr(
        environment,
        "stop",
        lambda **kwargs: operation_list.append(
            ("stop", kwargs["should_validate_drift"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_restore_plan_get",
        lambda **kwargs: (
            "vol-source",
            {
                "RetainedVolumeSlot": "a",
                "RetainedVolumeSnapshotId": kwargs["snapshot_id"],
            },
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_stack_apply",
        lambda **kwargs: operation_list.append(
            (
                kwargs["parameter_by_name_map"],
                kwargs["allow_retained_volume_transition"],
            )
        ),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_snapshot_restore_validate",
        lambda **kwargs: operation_list.append(
            ("validate-volume", kwargs["snapshot_id"], kwargs["source_volume_id"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_retained_volume_backup_disable",
        lambda **kwargs: operation_list.append(("disable-backup", kwargs["volume_id"])),
    )
    monkeypatch.setattr(
        environment,
        "_retired_retained_volume_cleanup",
        lambda **kwargs: operation_list.append(
            ("cleanup-retired", kwargs["current_volume_id"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "start",
        lambda **kwargs: operation_list.append(
            ("start", kwargs["should_publish_infrastructure_source"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_guard_disable",
        lambda: operation_list.append("disable-guard"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_product_release_link_restore",
        lambda: operation_list.append("link"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_apply_run",
        lambda: operation_list.append("recover"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_acceptance_run",
        lambda: operation_list.append("accept"),
    )

    environment.restore("snap-0123456789abcdef0")

    assert operation_list == [
        "drift",
        ("cleanup-retired", "vol-source"),
        ("stop", False),
        (
            {
                "InstanceSlot": "b",
                "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
                "ReplacementGuardScheduleState": "ENABLED",
                "RetainedVolumeSlot": "a",
                "RetainedVolumeSnapshotId": "snap-0123456789abcdef0",
            },
            True,
        ),
        (
            "validate-volume",
            "snap-0123456789abcdef0",
            "vol-source",
        ),
        ("disable-backup", "vol-source"),
        ("start", True),
        "disable-guard",
        "link",
        "recover",
        "accept",
    ]


def test_ssh_remote_arguments_are_shell_quoted_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SSH must transport one exact remote argument vector through the remote shell."""

    environment = _environment_get(tmp_path)
    command_list: list[str] = []

    def run(
        argument_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Record one controlled process call.

        Args:
            argument_list: Complete command.
            check: Ignored failure policy.
            input_text: Ignored input.
            should_capture: Ignored capture policy.

        Returns:
            Successful command result.
        """

        del check, input_text, should_capture
        command_list.extend(argument_list)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(environment._runner, "run", run)
    environment._ssh_run(
        ["python3", "-c", "print('two words')"],
        ssh_control_path=tmp_path / "control",
    )

    assert command_list[-1] == "python3 -c 'print('\"'\"'two words'\"'\"')'"


def test_start_creates_stop_lease_before_ec2_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Start must prove the external lease before invoking EC2 StartInstances."""

    environment = _environment_get(tmp_path)
    operation_list: list[str] = []

    def aws_run(
        aws_argument_list: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Record the controlled AWS operation.

        Args:
            aws_argument_list: AWS arguments.
            check: Ignored failure policy.

        Returns:
            Successful AWS result.
        """

        del check
        operation_list.append(" ".join(aws_argument_list))
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: operation_list.append("drift"),
    )
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda *args, **kwargs: operation_list.append("validate-source"),
    )
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-0123456789abcdef0")
    monkeypatch.setattr(
        environment, "_instance_state_get", lambda instance_id: "stopped"
    )
    monkeypatch.setattr(
        environment, "_instance_online_wait", lambda: operation_list.append("online")
    )
    monkeypatch.setattr(
        environment,
        "_ssm_shell_result_get",
        lambda *args, **kwargs: operation_list.append("cloud-init") or {},
    )
    monkeypatch.setattr(
        environment,
        "_infrastructure_source_publish",
        lambda: operation_list.append("publish-source"),
    )
    monkeypatch.setattr(
        environment,
        "_host_readiness_wait",
        lambda: operation_list.append("ready"),
    )
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: operation_list.append("lease"),
    )
    monkeypatch.setattr(environment, "_aws_run", aws_run)

    environment.start(should_publish_infrastructure_source=True)
    assert operation_list[0] == "drift"
    assert operation_list[1] == "validate-source"
    assert operation_list[2] == "lease"
    assert operation_list[3].startswith("ec2 start-instances")
    assert operation_list[4:] == [
        "online",
        "cloud-init",
        "publish-source",
        "ready",
    ]


def test_ordinary_start_reuses_installed_controller_without_source_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A lifecycle-only start must not become an implicit source deployment."""

    environment = _environment_get(tmp_path)
    operation_list: list[str] = []
    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: operation_list.append("drift"),
    )
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda *args, **kwargs: pytest.fail("source validation is not required"),
    )
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-running")
    monkeypatch.setattr(
        environment, "_instance_state_get", lambda instance_id: "running"
    )
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: operation_list.append("lease"),
    )
    monkeypatch.setattr(
        environment,
        "_instance_online_wait",
        lambda: operation_list.append("online"),
    )
    monkeypatch.setattr(
        environment,
        "_ssm_shell_result_get",
        lambda *args, **kwargs: operation_list.append("cloud-init") or {},
    )
    monkeypatch.setattr(
        environment,
        "_infrastructure_source_publish",
        lambda: pytest.fail("ordinary start must not publish source"),
    )
    monkeypatch.setattr(
        environment,
        "_host_readiness_wait",
        lambda: operation_list.append("ready"),
    )

    environment.start()

    assert operation_list == ["drift", "lease", "online", "cloud-init", "ready"]


def test_host_readiness_waits_for_foundation_and_controller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Final readiness requires both the host foundation and lifecycle controller."""

    environment = _environment_get(tmp_path)
    operation_list: list[str] = []
    common_status_payload = {
        "current_release": "",
        "host_status_probe": "ok",
        "k3s_service_status": "active",
        "kubernetes_node_status": "ready",
        "retained_mount_status": "ready",
        "wcc_activity": "busy",
    }
    status_payload_list = [
        {
            **common_status_payload,
            "kubernetes_node_status": "not-ready",
            "host_controller_service_status": "active",
            "host_controller_unit_status": "loaded",
        },
        {
            **common_status_payload,
            "host_controller_service_status": "active",
            "host_controller_unit_status": "loaded",
        },
    ]
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"RetainedVolumeId": "vol-0123456789abcdef0"},
    )

    def host_status_payload_get(*, retained_volume_id: str) -> dict[str, str]:
        """Return one controlled readiness transition."""

        assert retained_volume_id == "vol-0123456789abcdef0"
        operation_list.append("probe")
        return status_payload_list.pop(0)

    monkeypatch.setattr(
        environment, "_host_status_payload_get", host_status_payload_get
    )

    environment._host_readiness_wait()

    assert operation_list == ["probe", "probe"]
    assert status_payload_list == []
    assert environment._clock.monotonic() == 5


def test_host_status_probe_normalizes_only_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remote status accepts the fixed safe schema and uses a bounded SSM command."""

    environment = _environment_get(tmp_path)
    expected_payload = {
        "current_release": "20260728123456789012",
        "host_controller_service_status": "active",
        "host_controller_unit_status": "loaded",
        "host_status_probe": "ok",
        "k3s_service_status": "active",
        "kubernetes_node_status": "ready",
        "retained_mount_status": "ready",
        "wcc_activity": "idle",
    }
    command_list_list: list[list[str]] = []

    def ssm_shell_result_get(
        shell_command_list: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        """Capture the exact status command and return safe output."""

        assert (
            timeout_seconds
            == development_environment.HOST_STATUS_COMMAND_TIMEOUT_SECONDS
        )
        command_list_list.append(shell_command_list)
        return {
            "StandardOutputContent": json.dumps(expected_payload),
            "Status": "Success",
        }

    monkeypatch.setattr(environment, "_ssm_shell_result_get", ssm_shell_result_get)

    assert (
        environment._host_status_payload_get(retained_volume_id="vol-0123456789abcdef0")
        == expected_payload
    )
    assert len(command_list_list) == 1
    assert command_list_list[0][0].startswith(
        "python3.14 /opt/workflow-infrastructure/control/current/"
    )
    assert (
        "host-status --environment-name primary --retained-volume-id vol-0123456789abcdef0"
        in command_list_list[0][0]
    )


def test_host_status_collects_exact_mount_node_release_and_activity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Host-side status proves the retained device and reports no secret content."""

    environment = _environment_get(tmp_path)
    retained_root_path = tmp_path / "srv"
    release_root_path = retained_root_path / "release" / "releases"
    current_release_path = retained_root_path / "release" / "current"
    release_path = release_root_path / "20260728123456789012"
    release_path.mkdir(parents=True)
    current_release_path.symlink_to(release_path)
    device_root_path = tmp_path / "device-by-id"
    device_root_path.mkdir()
    actual_device_path = tmp_path / "nvme1n1"
    actual_device_path.touch()
    expected_device_path = device_root_path / (
        "nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0"
    )
    expected_device_path.symlink_to(actual_device_path)
    monkeypatch.setattr(
        development_environment,
        "HOST_EBS_DEVICE_BY_ID_ROOT_PATH",
        device_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_ROOT_PATH",
        retained_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_RELEASE_ROOT_PATH",
        retained_root_path / "release",
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_CURRENT_RELEASE_PATH",
        current_release_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RELEASE_ROOT_PATH",
        release_root_path,
    )

    def run(
        argument_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Return controlled host command results."""

        del check, input_text, should_capture
        if argument_list[0] == "findmnt":
            output = f"{actual_device_path} xfs {retained_root_path}\n"
        elif argument_list[:2] == ["systemctl", "is-active"]:
            output = "active\n"
        elif argument_list[:3] == ["k3s", "kubectl", "get"]:
            output = json.dumps(
                {
                    "items": [
                        {
                            "status": {
                                "conditions": [{"status": "True", "type": "Ready"}]
                            }
                        }
                    ]
                }
            )
        elif argument_list[:2] == ["systemctl", "show"]:
            output = "loaded\n"
        else:
            pytest.fail(f"Unexpected host status command: {argument_list}")
        return subprocess.CompletedProcess(argument_list, 0, output, "")

    monkeypatch.setattr(environment._runner, "run", run)
    monkeypatch.setattr(environment, "_host_product_activity_get", lambda: "idle")

    assert environment._host_status_local_payload_get(
        retained_volume_id="vol-0123456789abcdef0"
    ) == {
        "current_release": "20260728123456789012",
        "host_controller_service_status": "active",
        "host_controller_unit_status": "loaded",
        "host_status_probe": "ok",
        "k3s_service_status": "active",
        "kubernetes_node_status": "ready",
        "retained_mount_status": "ready",
        "wcc_activity": "idle",
    }


def test_status_includes_remote_host_readiness_and_activity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Status must expose safe runtime state, not only CloudFormation resources."""

    environment = _environment_get(tmp_path)
    output_by_name_map = {
        "InstanceId": "i-0123456789abcdef0",
        "InstanceType": "m7g.xlarge",
        "RetainedVolumeId": "vol-0123456789abcdef0",
        "RetainedVolumeSlot": "a",
        "RetainedVolumeSourceSnapshotId": "snap-source",
    }
    host_status_payload = {
        "current_release": "20260728123456789012",
        "host_controller_service_status": "active",
        "host_controller_unit_status": "loaded",
        "host_status_probe": "ok",
        "k3s_service_status": "active",
        "kubernetes_node_status": "ready",
        "retained_mount_status": "ready",
        "wcc_activity": "busy",
    }
    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_stack_payload_get",
        lambda stack_name, is_required: {"StackStatus": "UPDATE_COMPLETE"},
    )
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: output_by_name_map,
    )
    monkeypatch.setattr(
        environment, "_instance_state_get", lambda instance_id: "running"
    )
    monkeypatch.setattr(
        environment,
        "_instance_ssm_ping_status_get",
        lambda instance_id: "Online",
    )
    monkeypatch.setattr(environment, "_active_session_count_get", lambda instance_id: 1)
    monkeypatch.setattr(
        environment,
        "_latest_snapshot_id_get",
        lambda volume_id: "snap-latest",
    )
    monkeypatch.setattr(
        environment,
        "_retained_snapshot_policy_status_get",
        lambda: {
            "policy_id": "policy-0123456789abcdef0",
            "state": "ENABLED",
            "status_message": "",
        },
    )
    monkeypatch.setattr(environment, "_stop_lease_payload_get", lambda: {"state": "on"})
    monkeypatch.setattr(
        environment,
        "_host_status_payload_get",
        lambda **kwargs: host_status_payload,
    )

    environment.status()

    payload = json.loads(capsys.readouterr().out)
    assert payload["compute_stack_status"] == "UPDATE_COMPLETE"
    assert payload["current_release"] == "20260728123456789012"
    assert payload["host_status_probe"] == "ok"
    assert payload["kubernetes_node_status"] == "ready"
    assert payload["retained_mount_status"] == "ready"
    assert payload["retained_snapshot_policy"]["state"] == "ENABLED"
    assert payload["ssm_ping_status"] == "Online"
    assert payload["wcc_activity"] == "busy"


def test_stop_lease_uses_renewable_tag_resolving_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lease target must protect a future replacement whose instance ID is not known yet."""

    environment = _environment_get(tmp_path)
    aws_argument_list_list: list[list[str]] = []

    def aws_run(
        aws_argument_list: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Return controlled schedule lookup and creation results.

        Args:
            aws_argument_list: AWS arguments.
            check: Ignored failure policy.

        Returns:
            Controlled AWS result.
        """

        del check
        aws_argument_list_list.append(aws_argument_list)
        if aws_argument_list[:2] == ["scheduler", "get-schedule"]:
            return subprocess.CompletedProcess([], 255, "", "ResourceNotFoundException")
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "SchedulerExecutionRoleArn": "arn:aws:iam::463564115167:role/scheduler",
            "StopLeaseTargetArn": (
                "arn:aws:lambda:us-east-1:463564115167:"
                "function:workflow-control-center-development-stop-current-instance"
            ),
        },
    )
    monkeypatch.setattr(environment, "_aws_run", aws_run)
    monkeypatch.setattr(
        environment,
        "_stop_lease_payload_get",
        lambda: {
            "action_after_completion": "DELETE",
            "schedule_expression": "at(2026-07-28T14:00:00)",
            "state": "ENABLED",
            "target_arn": (
                "arn:aws:lambda:us-east-1:463564115167:"
                "function:workflow-control-center-development-stop-current-instance"
            ),
        },
    )

    environment._stop_lease_upsert()
    create_argument_list = aws_argument_list_list[-1]
    assert create_argument_list[:2] == ["scheduler", "create-schedule"]
    assert "at(2026-07-28T14:00:00)" in create_argument_list
    assert "DELETE" in create_argument_list
    target_payload = json.loads(
        create_argument_list[create_argument_list.index("--target") + 1]
    )
    assert target_payload["Arn"].endswith(
        ":function:workflow-control-center-development-stop-current-instance"
    )
    assert json.loads(target_payload["Input"]) == {}


def test_connect_forwards_the_remote_ingress_port_to_local_8080(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Session Manager must target the actual hostPort without public ingress."""

    environment = _environment_get(tmp_path)
    command_list: list[str] = []

    def run(
        argument_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check, input_text, should_capture
        command_list.extend(argument_list)
        return subprocess.CompletedProcess(argument_list, 0, "", "")

    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_instance_id_get",
        lambda: "i-0123456789abcdef0",
    )
    monkeypatch.setattr(environment._runner, "run", run)

    assert environment.connect() == 0
    parameter_payload = json.loads(command_list[command_list.index("--parameters") + 1])
    assert parameter_payload == {
        "localPortNumber": ["8080"],
        "portNumber": ["8080"],
    }


def test_cli_interrupt_returns_standard_status_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An operator Ctrl+C must stop a foreground command without a Python traceback."""

    def connect_interrupted(_environment: DevelopmentEnvironment) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(DevelopmentEnvironment, "connect", connect_interrupted)

    assert development_environment_manage.main(["connect"]) == 130
    assert capsys.readouterr() == ("", "")


def _retained_product_release_prepare(
    release_root_path: Path,
    *,
    release_name: str,
    source_manifest_version: int | None = None,
) -> None:
    """Create one internally consistent retained Product release fixture."""

    source_identity_by_name_map: dict[str, dict[str, str]] = {}
    repository_by_name_map: dict[str, dict[str, object]] = {}
    for index, repository_name in enumerate(
        [
            "workflow-infrastructure",
            *development_environment.PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
            "workflow-container-contract",
        ],
        start=1,
    ):
        source_path = release_root_path / "sources" / repository_name / "tracked.txt"
        source_path.parent.mkdir(parents=True)
        source_payload = f"{repository_name}-source\n".encode()
        source_path.write_bytes(source_payload)
        source_identity = {
            "archive_sha256": f"{index:x}".zfill(64),
            "commit_sha": f"{index:x}".zfill(40),
            "repository_url": development_environment.REPOSITORY_URL_BY_NAME_MAP[
                repository_name
            ],
        }
        repository_payload: dict[str, object] = {
            **source_identity,
            "file_sha256_by_path_map": {
                "tracked.txt": hashlib.sha256(source_payload).hexdigest()
            },
            "submodule_by_path_map": {},
        }
        if source_manifest_version is not None:
            source_kind = (
                "resolved_moving_source"
                if repository_name == "workflow-container-contract"
                else "exact_checkout"
            )
            source_identity["source_kind"] = source_kind
            repository_payload["source_kind"] = source_kind
            if repository_name == "workflow-container-contract":
                moving_identity = {
                    "package_version": "0.7.0",
                    "requested_selector": "HEAD",
                    "resolved_ref": "refs/heads/main",
                }
                source_identity.update(moving_identity)
                repository_payload.update(moving_identity)
        source_identity_by_name_map[repository_name] = source_identity
        repository_by_name_map[repository_name] = repository_payload
    source_manifest = {
        "release": release_name,
        "repository_by_name_map": repository_by_name_map,
    }
    if source_manifest_version is not None:
        source_manifest.update(
            {
                "environment_name": "primary",
                "source_manifest_version": source_manifest_version,
            }
        )
    source_manifest_bytes = (
        json.dumps(source_manifest, indent=2, sort_keys=True) + "\n"
    ).encode()
    (release_root_path / "source-manifest.json").write_bytes(source_manifest_bytes)
    render_bytes = b"apiVersion: v1\nkind: List\nitems: []\n"
    (release_root_path / "render.yaml").write_bytes(render_bytes)
    (release_root_path / "release-manifest.json").write_text(
        json.dumps(
            {
                **(
                    {
                        "environment_name": "primary",
                        "product_release_manifest_version": 2,
                    }
                    if source_manifest_version is not None
                    else {}
                ),
                "release": release_name,
                "render_sha256": hashlib.sha256(render_bytes).hexdigest(),
                "source_by_name_map": source_identity_by_name_map,
                "source_manifest_sha256": hashlib.sha256(
                    source_manifest_bytes
                ).hexdigest(),
                "target_platform": "linux/arm64",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_host_product_release_activation_is_verified_retained_and_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a byte-exact retained release becomes current on retained and root volumes."""

    retained_release_root_path = tmp_path / "retained/release"
    release_root_path = retained_release_root_path / "releases/20260728120000000000"
    current_release_path = retained_release_root_path / "current"
    current_source_path = tmp_path / "root/current"
    _retained_product_release_prepare(
        release_root_path,
        release_name=release_root_path.name,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_RELEASE_ROOT_PATH",
        retained_release_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_CURRENT_RELEASE_PATH",
        current_release_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RELEASE_ROOT_PATH",
        retained_release_root_path / "releases",
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_CURRENT_SOURCE_PATH",
        current_source_path,
    )
    environment = _environment_get(
        tmp_path / "control/current/sources/workflow-infrastructure"
    )
    environment._is_host = True

    environment.host_product_release_activate(release_root_path.name)

    assert current_release_path.is_symlink()
    assert current_release_path.resolve(strict=True) == release_root_path
    assert current_source_path.is_symlink()
    assert current_source_path.resolve(strict=True) == release_root_path


def test_legacy_product_tool_runtime_is_resolved_before_atomic_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The first replacement must retain a portable runtime while the old root exists."""

    control_release_root_path = tmp_path / "control/releases"
    current_source_path = tmp_path / "product/current"
    legacy_runtime_root_path = tmp_path / "root/product-tool"
    retained_root_path = tmp_path / "retained"
    host_python_path = Path("/opt/exact-python/bin/python3.14")
    requirement_path = (
        current_source_path
        / "sources/workflow-control-center/tool/requirements-development-kubernetes.txt"
    )
    product_tool_path = (
        current_source_path
        / "sources/workflow-control-center/tool/development_kubernetes_manage.py"
    )
    requirement_path.parent.mkdir(parents=True)
    requirement_path.write_text("pydantic==2.13.4\nPyYAML==6.0.3\n", encoding="utf-8")
    product_tool_path.write_text("# exact retained Product tool\n", encoding="utf-8")
    requirement_digest = hashlib.sha256(requirement_path.read_bytes()).hexdigest()
    legacy_runtime_path = legacy_runtime_root_path / requirement_digest
    command_list_list: list[list[str]] = []

    monkeypatch.setattr(
        development_environment,
        "HOST_CONTROL_RELEASE_ROOT_PATH",
        control_release_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_CURRENT_SOURCE_PATH",
        current_source_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_LEGACY_PRODUCT_TOOL_RUNTIME_ROOT_PATH",
        legacy_runtime_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_PYTHON_PATH",
        host_python_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_ROOT_PATH",
        retained_root_path,
    )
    environment = _environment_get(
        control_release_root_path
        / "20260730120000000000/sources/workflow-infrastructure"
    )

    def run(
        argument_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check, input_text, should_capture
        command_list_list.append(argument_list)
        if argument_list == ["python3.14", str(product_tool_path), "--help"]:
            (legacy_runtime_path / "bin").mkdir(parents=True)
            (legacy_runtime_path / "pyvenv.cfg").write_text(
                (
                    "home = /old-root/python\n"
                    "implementation = CPython\n"
                    "executable = /old-root/python3.14\n"
                ),
                encoding="utf-8",
            )
            (legacy_runtime_path / "bin/python").symlink_to("/old-root/python3.14")
        return subprocess.CompletedProcess(argument_list, 0, "", "")

    monkeypatch.setattr(environment._runner, "run", run)

    environment.host_product_runtime_retain()
    environment.host_product_runtime_retain()

    retained_runtime_path = retained_root_path / "product-tool" / requirement_digest
    assert retained_runtime_path.is_dir()
    retained_pyvenv_text = (retained_runtime_path / "pyvenv.cfg").read_text(
        encoding="utf-8"
    )
    assert f"home = {host_python_path.parent}\n" in retained_pyvenv_text
    assert f"executable = {host_python_path}\n" in retained_pyvenv_text
    assert (
        f"command = {host_python_path} -m venv {retained_runtime_path}\n"
        in retained_pyvenv_text
    )
    assert "implementation = CPython\n" in retained_pyvenv_text
    assert "/old-root" not in retained_pyvenv_text
    for python_name in ("python", "python3", "python3.14"):
        assert (retained_runtime_path / "bin" / python_name).readlink() == (
            host_python_path
        )
    assert (
        command_list_list.count(["python3.14", str(product_tool_path), "--help"]) == 1
    )
    assert [
        str(retained_runtime_path / "bin/python"),
        "-c",
        "import pydantic; import yaml",
    ] in command_list_list
    assert ["sync", "-f", str(retained_root_path / "product-tool")] in (
        command_list_list
    )


def test_legacy_runtime_transition_publishes_control_before_host_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The old root is touched only after exact new control source readiness."""

    environment = _environment_get(tmp_path)
    event_list: list[object] = []
    monkeypatch.setattr(
        environment,
        "_host_start_foundation",
        lambda *, should_validate_source: event_list.append(
            ("start-foundation", should_validate_source)
        ),
    )
    monkeypatch.setattr(
        environment,
        "_infrastructure_source_publish",
        lambda *, should_install_host_controller=True: event_list.append(
            ("publish-source", should_install_host_controller)
        ),
    )
    monkeypatch.setattr(
        environment,
        "_host_readiness_wait",
        lambda: event_list.append("ready"),
    )
    monkeypatch.setattr(
        environment,
        "_ssh_control_session",
        lambda: nullcontext(Path("/tmp/test-ssh-control")),
    )
    monkeypatch.setattr(
        environment,
        "_ssh_run",
        lambda command_list, *, ssh_control_path, should_capture=True: (
            event_list.append(
                (
                    "ssh",
                    command_list,
                    ssh_control_path,
                    should_capture,
                )
            )
            or subprocess.CompletedProcess(command_list, 0, "", "")
        ),
    )

    environment._legacy_product_tool_runtime_transition_prepare()

    assert event_list[:3] == [
        ("start-foundation", True),
        ("publish-source", False),
        "ready",
    ]
    assert event_list[3] == (
        "ssh",
        [
            "sudo",
            "python3.14",
            (
                "/opt/workflow-infrastructure/control/current/sources/"
                "workflow-infrastructure/tool/development_environment_manage.py"
            ),
            "host-product-runtime-retain",
            "--environment-name",
            "primary",
        ],
        Path("/tmp/test-ssh-control"),
        True,
    )


def test_legacy_runtime_transition_is_required_only_for_pre_manifest_stack() -> None:
    """Only a pre-hardening compute stack needs the one-time root-volume bridge."""

    assert DevelopmentEnvironment._legacy_product_tool_runtime_transition_is_required(
        {"InstanceType": "m7g.xlarge"}
    )
    assert not (
        DevelopmentEnvironment._legacy_product_tool_runtime_transition_is_required(
            {
                "HostArtifactManifestSha256": "a" * 64,
                "InstanceType": "m7g.xlarge",
            }
        )
    )


def test_apply_uses_one_controlled_replacement_for_legacy_launch_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The legacy latest-version owner moves once to hardened numeric version one."""

    environment = _environment_get(tmp_path)
    event_list: list[object] = []

    class HostArtifactResolutionStub:
        """Expose the one launch input needed to prove parameter propagation."""

        @staticmethod
        def cloudformation_parameter_by_name_map_get() -> dict[str, str]:
            return {"HostArtifactManifestSha256": "a" * 64}

    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda repository_path, repository_name: None,
    )
    monkeypatch.setattr(environment, "_cost_review_record", lambda: None)
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name, **keyword_argument_by_name_map: event_list.append(
            ("drift", stack_name, keyword_argument_by_name_map)
        ),
    )
    monkeypatch.setattr(
        environment,
        "_stack_payload_get",
        lambda stack_name, *, is_required: {"StackStatus": "UPDATE_COMPLETE"},
    )
    monkeypatch.setattr(
        environment,
        "_stack_parameter_by_name_map_get",
        lambda stack_name: {"InstanceSlot": "b"},
    )
    monkeypatch.setattr(
        environment,
        "_host_artifact_resolution_get",
        lambda *, compute_stack_exists: HostArtifactResolutionStub(),
    )
    monkeypatch.setattr(
        environment,
        "_stack_resource_id_by_logical_name_map_get",
        lambda stack_name: {},
    )
    monkeypatch.setattr(
        environment,
        "_template_validate",
        lambda template_path: None,
    )
    monkeypatch.setattr(
        environment,
        "_stack_apply",
        lambda **keyword_argument_by_name_map: event_list.append(
            ("ordinary-apply", keyword_argument_by_name_map["stack_name"])
        ),
    )
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {
            "PlatformRoleArn": (
                "arn:aws:iam::463564115167:role/workflow-control-center-platform"
            )
        },
    )
    monkeypatch.setattr(
        environment,
        "_legacy_product_tool_runtime_transition_prepare",
        lambda: event_list.append("retain-runtime"),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_parameter_by_name_map_get",
        lambda: {
            "InstanceLaunchTemplateVersion": "8",
            "InstanceSlot": "a",
            "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
            "ReplacementGuardScheduleState": "ENABLED",
        },
    )
    monkeypatch.setattr(
        environment,
        "stop",
        lambda *, should_validate_drift: event_list.append(
            ("stop", should_validate_drift)
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_stack_apply",
        lambda *, parameter_by_name_map: event_list.append(
            ("controlled-replacement", parameter_by_name_map)
        ),
    )
    monkeypatch.setattr(
        environment,
        "start",
        lambda *, should_publish_infrastructure_source, should_allow_instance_launch_template_tag_drift=False: event_list.append(
            (
                "start",
                should_publish_infrastructure_source,
                should_allow_instance_launch_template_tag_drift,
            )
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_guard_disable",
        lambda: event_list.append("disable-guard"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_product_release_link_restore",
        lambda: event_list.append("restore-link"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_apply_run",
        lambda: event_list.append("recover"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_acceptance_run",
        lambda: event_list.append("recovery-acceptance"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_snapshot_policy_validate",
        lambda: event_list.append("snapshot-policy"),
    )

    environment.apply()

    ordinary_compute_apply_list = [
        event
        for event in event_list
        if event
        == (
            "ordinary-apply",
            "workflow-control-center-development-compute",
        )
    ]
    assert ordinary_compute_apply_list == []
    controlled_replacement = next(
        event for event in event_list if event[0] == "controlled-replacement"
    )
    assert controlled_replacement[1] == {
        "EnvironmentName": "primary",
        "HostArtifactManifestSha256": "a" * 64,
        "InstanceLaunchTemplateVersion": "1",
        "InstanceSlot": "a",
        "PlatformRoleName": "workflow-control-center-platform",
        "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
        "ReplacementGuardScheduleState": "ENABLED",
    }
    assert (
        event_list.index("retain-runtime")
        < event_list.index(("stop", False))
        < event_list.index(controlled_replacement)
    )
    assert (
        event_list.index(controlled_replacement)
        < event_list.index(("start", True, False))
        < event_list.index("disable-guard")
    )


def test_replacement_recovery_finish_resumes_exact_interrupted_cutover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resume installs control before disabling the guard and recovering Product."""

    environment = _environment_get(tmp_path)
    event_list: list[object] = []
    monkeypatch.setattr(
        environment,
        "start",
        lambda **keyword_argument_by_name_map: event_list.append(
            ("start", keyword_argument_by_name_map)
        ),
    )
    monkeypatch.setattr(
        environment,
        "_replacement_guard_disable",
        lambda: event_list.append("disable-guard"),
    )
    monkeypatch.setattr(
        environment,
        "_retained_product_release_link_restore",
        lambda: event_list.append("restore-link"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_apply_run",
        lambda: event_list.append("recover"),
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_acceptance_run",
        lambda: event_list.append("recovery-acceptance"),
    )

    environment._replacement_recovery_finish(
        should_allow_instance_launch_template_tag_drift=True
    )

    assert event_list == [
        (
            "start",
            {
                "should_allow_instance_launch_template_tag_drift": True,
                "should_publish_infrastructure_source": True,
            },
        ),
        "disable-guard",
        "restore-link",
        "recover",
        "recovery-acceptance",
    ]


@pytest.mark.parametrize(
    "source_manifest_version",
    [2, development_environment.SOURCE_MANIFEST_VERSION],
)
def test_host_product_release_activation_accepts_supported_typed_source_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_manifest_version: int,
) -> None:
    """Activation accepts both the previous and current typed source graph."""

    retained_release_root_path = tmp_path / "retained/release"
    release_root_path = retained_release_root_path / "releases/20260730120000000000"
    current_release_path = retained_release_root_path / "current"
    current_source_path = tmp_path / "root/current"
    _retained_product_release_prepare(
        release_root_path,
        release_name=release_root_path.name,
        source_manifest_version=source_manifest_version,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_RELEASE_ROOT_PATH",
        retained_release_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_CURRENT_RELEASE_PATH",
        current_release_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RELEASE_ROOT_PATH",
        retained_release_root_path / "releases",
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_CURRENT_SOURCE_PATH",
        current_source_path,
    )
    environment = _environment_get(
        tmp_path / "control/current/sources/workflow-infrastructure"
    )
    environment._is_host = True

    environment.host_product_release_activate(release_root_path.name)

    assert current_release_path.resolve(strict=True) == release_root_path
    assert current_source_path.resolve(strict=True) == release_root_path


def test_legacy_runtime_source_publish_skips_new_host_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The transition publishes exact control bytes without using new host artifacts."""

    environment = _environment_get(tmp_path)
    command_list_list: list[list[str]] = []
    monkeypatch.setattr(
        environment,
        "_source_archive_publish",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        environment,
        "_ssh_control_session",
        lambda: nullcontext(tmp_path / "control"),
    )

    def ssh_run(
        command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del ssh_control_path, should_capture
        command_list_list.append(command_list)
        return subprocess.CompletedProcess(command_list, 0, "", "")

    monkeypatch.setattr(environment, "_ssh_run", ssh_run)

    environment._infrastructure_source_publish(
        should_install_host_controller=False,
    )

    assert any(
        command_list[1:3] == ["install", "-d"] for command_list in command_list_list
    )
    assert any(
        command_list[1:3] == ["ln", "-sfn"] for command_list in command_list_list
    )
    assert not any("host-install" in command_list for command_list in command_list_list)


def test_host_product_release_restore_rejects_changed_tracked_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A snapshot cannot execute Product recovery after one retained tracked byte changes."""

    retained_release_root_path = tmp_path / "retained/release"
    release_root_path = retained_release_root_path / "releases/20260728120000000000"
    current_release_path = retained_release_root_path / "current"
    current_source_path = tmp_path / "root/current"
    _retained_product_release_prepare(
        release_root_path,
        release_name=release_root_path.name,
    )
    current_release_path.parent.mkdir(parents=True, exist_ok=True)
    current_release_path.symlink_to(release_root_path)
    (
        release_root_path / "sources" / "workflow-control-center" / "tracked.txt"
    ).write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_RELEASE_ROOT_PATH",
        retained_release_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_CURRENT_RELEASE_PATH",
        current_release_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RELEASE_ROOT_PATH",
        retained_release_root_path / "releases",
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_CURRENT_SOURCE_PATH",
        current_source_path,
    )
    environment = _environment_get(
        tmp_path / "control/current/sources/workflow-infrastructure"
    )
    environment._is_host = True

    with pytest.raises(
        DevelopmentEnvironmentError,
        match="source file graph differs",
    ):
        environment.host_product_release_restore()

    assert not current_source_path.exists()


def test_host_product_release_restore_rejects_unmanifested_source_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery must reject added source bytes before executing retained Product code."""

    retained_release_root_path = tmp_path / "retained/release"
    release_root_path = retained_release_root_path / "releases/20260730120000000000"
    current_release_path = retained_release_root_path / "current"
    current_source_path = tmp_path / "root/current"
    _retained_product_release_prepare(
        release_root_path,
        release_name=release_root_path.name,
        source_manifest_version=development_environment.SOURCE_MANIFEST_VERSION,
    )
    current_release_path.parent.mkdir(parents=True, exist_ok=True)
    current_release_path.symlink_to(release_root_path)
    (
        release_root_path / "sources" / "workflow-control-center" / "unmanifested.py"
    ).write_text("raise RuntimeError('must never execute')\n", encoding="utf-8")
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_RELEASE_ROOT_PATH",
        retained_release_root_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RETAINED_CURRENT_RELEASE_PATH",
        current_release_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RELEASE_ROOT_PATH",
        retained_release_root_path / "releases",
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_CURRENT_SOURCE_PATH",
        current_source_path,
    )
    environment = _environment_get(
        tmp_path / "control/current/sources/workflow-infrastructure"
    )
    environment._is_host = True

    with pytest.raises(
        DevelopmentEnvironmentError,
        match="source file graph differs",
    ):
        environment.host_product_release_restore()

    assert not current_source_path.exists()


def test_deploy_activates_release_before_installing_product_and_host_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accepted source becomes current before either current-path systemd owner is installed."""

    environment = _environment_get(tmp_path / "workflow-infrastructure")
    remote_command_list_list: list[list[str]] = []
    remote_release_root_path_list: list[Path] = []

    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: None,
    )
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda repository_path, repository_name: None,
    )
    monkeypatch.setattr(environment, "_instance_online_wait", lambda: None)
    monkeypatch.setattr(
        environment,
        "_instance_launch_template_version_validate",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        environment,
        "_host_artifact_manifest_payload_get",
        lambda: {
            "architecture": "arm64",
            "artifact_by_name_map": {},
        },
    )

    def source_archive_publish(**kwargs: object) -> dict[str, object]:
        remote_release_root_path = kwargs["remote_release_root_path"]
        assert isinstance(remote_release_root_path, Path)
        remote_release_root_path_list.append(remote_release_root_path)
        repository_name = kwargs["repository_name"]
        assert isinstance(repository_name, str)
        return {
            "archive_sha256": "a" * 64,
            "commit_sha": "b" * 40,
            "file_sha256_by_path_map": {},
            "repository_url": development_environment.REPOSITORY_URL_BY_NAME_MAP[
                repository_name
            ],
            "submodule_by_path_map": {},
        }

    monkeypatch.setattr(
        environment,
        "_source_archive_publish",
        source_archive_publish,
    )

    def moving_source_archive_publish(**kwargs: object) -> dict[str, object]:
        """Return one exact resolved moving-source entry.

        Args:
            kwargs: Moving-source publication arguments.

        Returns:
            Resolved moving-source manifest.
        """

        remote_release_root_path = kwargs["remote_release_root_path"]
        assert isinstance(remote_release_root_path, Path)
        remote_release_root_path_list.append(remote_release_root_path)
        assert kwargs["repository_name"] == "workflow-container-contract"
        assert kwargs["exact_override_commit"] == ""
        return {
            "archive_sha256": "c" * 64,
            "commit_sha": "d" * 40,
            "file_sha256_by_path_map": {},
            "package_version": "0.7.0",
            "repository_url": development_environment.REPOSITORY_URL_BY_NAME_MAP[
                "workflow-container-contract"
            ],
            "requested_selector": "HEAD",
            "resolved_ref": "refs/heads/main",
            "source_kind": "resolved_moving_source",
            "submodule_by_path_map": {},
        }

    monkeypatch.setattr(
        environment,
        "_moving_source_archive_publish",
        moving_source_archive_publish,
    )
    monkeypatch.setattr(
        environment,
        "_ssh_control_session",
        lambda: nullcontext(tmp_path / "control"),
    )
    monkeypatch.setattr(
        environment,
        "_remote_text_write",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        environment,
        "_runtime_platform_get",
        lambda ssh_control_path: "linux/arm64",
    )

    def ssh_run(
        remote_command_list: list[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del ssh_control_path, should_capture
        remote_command_list_list.append(remote_command_list)
        return subprocess.CompletedProcess(remote_command_list, 0, "", "")

    monkeypatch.setattr(environment, "_ssh_run", ssh_run)

    environment.deploy()

    host_prepare_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if "host-prepare" in command_list
        and "/sources/workflow-infrastructure/" in " ".join(command_list)
    )
    product_deploy_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if "linux/arm64" in command_list
        and "development_kubernetes_manage.py" in " ".join(command_list)
    )
    current_activation_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if "host-product-release-activate" in command_list
    )
    product_host_install_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if "host-install" in command_list
        and str(development_environment.HOST_CURRENT_SOURCE_PATH)
        in " ".join(command_list)
    )
    controller_host_install_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if "host-install" in command_list
        and str(development_environment.HOST_CONTROL_CURRENT_SOURCE_PATH)
        in " ".join(command_list)
    )
    assert (
        host_prepare_index
        < product_deploy_index
        < current_activation_index
        < product_host_install_index
        < controller_host_install_index
    )
    assert (
        remote_release_root_path_list
        == [development_environment.HOST_RELEASE_ROOT_PATH] * 6
    )


def test_ssm_shell_run_waits_for_real_operation_and_registration_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run Command polling must outlive the AWS CLI default waiter."""

    environment = _environment_get(tmp_path)
    response_list = [
        subprocess.CompletedProcess(
            [],
            255,
            "",
            "InvocationDoesNotExist: command is not registered yet",
        ),
        subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"Status": "Pending"}),
            "",
        ),
        subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"Status": "InProgress"}),
            "",
        ),
        subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "StandardErrorContent": "",
                    "StandardOutputContent": "accepted\n",
                    "Status": "Success",
                }
            ),
            "",
        ),
    ]
    monkeypatch.setattr(environment, "_ssm_command_start", lambda commands: "cmd-1")
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-123")
    monkeypatch.setattr(
        environment,
        "_aws_run",
        lambda arguments, check=True: response_list.pop(0),
    )

    environment._ssm_shell_run(["long recovery"])

    assert response_list == []
    assert environment._clock.monotonic() == 15
    assert capsys.readouterr().out == "accepted\n"


def test_ssm_shell_run_timeout_preserves_remote_command_for_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A local wait timeout must report but not cancel the remote operation."""

    environment = _environment_get(tmp_path)
    monkeypatch.setattr(development_environment, "SSM_COMMAND_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(environment, "_ssm_command_start", lambda commands: "cmd-2")
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-123")
    monkeypatch.setattr(
        environment,
        "_aws_run",
        lambda arguments, check=True: subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"Status": "InProgress"}),
            "",
        ),
    )

    with pytest.raises(
        DevelopmentEnvironmentError,
        match=(
            "SSM command cmd-2 did not finish within 10 seconds; "
            "the remote command was not cancelled"
        ),
    ):
        environment._ssm_shell_run(["long recovery"])

    assert environment._clock.monotonic() == 10


def test_host_prepare_validates_preinstalled_helm_against_launch_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Host preparation never downloads and accepts only launch-bound Helm."""

    helm_version = "v4.2.3"
    helm_path = tmp_path / "bin/helm"
    helm_path.parent.mkdir()
    helm_path.write_bytes(b"\x7fELFtest-helm-binary")
    manifest_payload = {
        "architecture": "arm64",
        "artifact_by_name_map": {
            "helm": {
                "name": "helm",
                "selector": "4",
                "sha256": "a" * 64,
                "size": 1,
                "url": "https://get.helm.sh/helm-v4.2.3-linux-arm64.tar.gz",
                "version": helm_version,
            }
        },
    }
    manifest_bytes = json.dumps(
        manifest_payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    manifest_path = tmp_path / "host-artifact-manifest.json.gz.b64"
    manifest_digest_path = tmp_path / "host-artifact-manifest.sha256"
    manifest_path.write_text(
        base64.b64encode(gzip.compress(manifest_bytes, mtime=0)).decode(),
        encoding="utf-8",
    )
    manifest_digest_path.write_text(
        hashlib.sha256(manifest_bytes).hexdigest(),
        encoding="utf-8",
    )
    monkeypatch.setattr(development_environment, "HELM_BINARY_PATH", helm_path)
    monkeypatch.setattr(
        development_environment,
        "HOST_ARTIFACT_MANIFEST_PATH",
        manifest_path,
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_ARTIFACT_MANIFEST_SHA256_PATH",
        manifest_digest_path,
    )

    class HelmRunner:
        """Report only the already installed exact binary version."""

        def run(
            self,
            command_list: list[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            should_capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            del check, input_text, should_capture
            assert command_list[0] == str(helm_path)
            return subprocess.CompletedProcess(
                command_list,
                0,
                helm_version + "\n",
                "",
            )

    environment = DevelopmentEnvironment(
        clock=ClockFixed(),
        project_root_path=tmp_path,
        runner=HelmRunner(),  # type: ignore[arg-type]
    )
    environment._is_host = True

    environment.host_prepare()

    assert helm_path.read_bytes() == b"\x7fELFtest-helm-binary"


def test_host_prepare_rejects_product_release_built_for_another_host_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Product release provenance must equal the launch input installed on its host."""

    host_release_root_path = tmp_path / "retained/release/releases"
    project_root_path = (
        host_release_root_path
        / "20260730120000000000"
        / "sources"
        / "workflow-infrastructure"
    )
    project_root_path.mkdir(parents=True)
    source_manifest_path = project_root_path.parent.parent / "source-manifest.json"
    source_manifest_path.write_text(
        json.dumps(
            {
                "environment_name": "primary",
                "host_artifact_manifest": {"architecture": "amd64"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        development_environment,
        "HOST_RELEASE_ROOT_PATH",
        host_release_root_path,
    )
    environment = _environment_get(project_root_path)
    monkeypatch.setattr(
        environment,
        "_host_artifact_manifest_get",
        lambda: {"architecture": "arm64"},
    )

    with pytest.raises(
        DevelopmentEnvironmentError,
        match="active host artifact identities differ",
    ):
        environment.host_prepare()


def test_host_controller_renews_beyond_two_hours_then_stops_after_proven_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Useful access has no hard deadline; a fresh 30-minute idle proof is still required."""

    clock = ClockFixed()
    environment = DevelopmentEnvironment(
        clock=clock,
        project_root_path=tmp_path,
        runner=CommandRunner(),
    )
    command_list_list: list[list[str]] = []
    lease_time_list: list[datetime] = []
    shutdown_time_list: list[datetime] = []
    t_active_until = clock.t_now + timedelta(hours=2, minutes=10)

    def run(
        argument_list: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check, input_text, should_capture
        command_list_list.append(argument_list)
        return subprocess.CompletedProcess(argument_list, 0, "", "")

    monkeypatch.setattr(
        development_environment,
        "HOST_STATE_ROOT_PATH",
        tmp_path / "state",
    )
    monkeypatch.setattr(environment._runner, "run", run)
    monkeypatch.setattr(
        environment,
        "_instance_metadata_get",
        lambda path: "i-0123456789abcdef0",
    )
    monkeypatch.setattr(environment, "_host_node_name_get", lambda: "node-a")
    monkeypatch.setattr(
        environment,
        "_host_active_session_count_get",
        lambda instance_id: int(clock.t_now < t_active_until),
    )
    monkeypatch.setattr(environment, "_host_product_activity_get", lambda: "idle")
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: lease_time_list.append(clock.t_now),
    )
    monkeypatch.setattr(
        environment,
        "host_shutdown",
        lambda: shutdown_time_list.append(clock.t_now),
    )

    environment.host_controller()

    assert command_list_list[0] == [
        "k3s",
        "kubectl",
        "uncordon",
        "node-a",
    ]
    assert lease_time_list[0] == datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert lease_time_list[-1] >= datetime(2026, 7, 28, 14, 0, tzinfo=UTC)
    assert shutdown_time_list[0] >= t_active_until + timedelta(minutes=30)


def test_host_controller_discards_stale_idle_proof_on_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A controller restart begins a new uninterrupted idle interval."""

    clock = ClockFixed()
    environment = DevelopmentEnvironment(
        clock=clock,
        project_root_path=tmp_path,
        runner=CommandRunner(),
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    idle_start_path = state_root / "idle-start"
    idle_start_path.write_text(
        (clock.t_now - timedelta(days=1)).isoformat(),
        encoding="utf-8",
    )
    shutdown_time_list: list[datetime] = []

    monkeypatch.setattr(
        development_environment,
        "HOST_STATE_ROOT_PATH",
        state_root,
    )
    monkeypatch.setattr(
        environment._runner,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(
        environment,
        "_instance_metadata_get",
        lambda path: "i-0123456789abcdef0",
    )
    monkeypatch.setattr(environment, "_host_node_name_get", lambda: "node-a")
    monkeypatch.setattr(
        environment,
        "_host_active_session_count_get",
        lambda instance_id: 0,
    )
    monkeypatch.setattr(environment, "_host_product_activity_get", lambda: "idle")
    monkeypatch.setattr(
        environment,
        "host_shutdown",
        lambda: shutdown_time_list.append(clock.t_now),
    )

    environment.host_controller()

    assert shutdown_time_list == [datetime(2026, 7, 28, 12, 30, tzinfo=UTC)]


def test_host_controller_treats_unknown_session_state_as_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An SSM API failure must renew the lease and restart the idle proof."""

    clock = ClockFixed()
    environment = DevelopmentEnvironment(
        clock=clock,
        project_root_path=tmp_path,
        runner=CommandRunner(),
    )
    session_probe_count = 0
    lease_time_list: list[datetime] = []
    shutdown_time_list: list[datetime] = []

    def session_count_get(instance_id: str) -> int:
        nonlocal session_probe_count
        del instance_id
        session_probe_count += 1
        if session_probe_count == 1:
            raise DevelopmentEnvironmentError("Session Manager is unavailable")
        return 0

    monkeypatch.setattr(
        development_environment,
        "HOST_STATE_ROOT_PATH",
        tmp_path / "state",
    )
    monkeypatch.setattr(
        environment._runner,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(
        environment,
        "_instance_metadata_get",
        lambda path: "i-0123456789abcdef0",
    )
    monkeypatch.setattr(environment, "_host_node_name_get", lambda: "node-a")
    monkeypatch.setattr(
        environment,
        "_host_active_session_count_get",
        session_count_get,
    )
    monkeypatch.setattr(environment, "_host_product_activity_get", lambda: "idle")
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: lease_time_list.append(clock.t_now),
    )
    monkeypatch.setattr(
        environment,
        "host_shutdown",
        lambda: shutdown_time_list.append(clock.t_now),
    )

    environment.host_controller()

    assert lease_time_list == [datetime(2026, 7, 28, 12, 0, tzinfo=UTC)]
    assert shutdown_time_list == [datetime(2026, 7, 28, 12, 31, tzinfo=UTC)]


def test_lifecycle_acceptance_uses_short_real_lease_then_restores_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Short acceptance must prove renewal and fail-safe stop without changing policy."""

    clock = ClockFixed()
    environment = DevelopmentEnvironment(
        clock=clock,
        project_root_path=tmp_path,
        runner=CommandRunner(),
    )
    event_list: list[object] = []
    lease_duration_list: list[timedelta] = []

    monkeypatch.setattr(
        environment,
        "_local_operator_context_validate",
        lambda: event_list.append("context"),
    )
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda *args: event_list.append("source"),
    )
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: event_list.append(("drift", stack_name)),
    )
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-0123456789abcdef0")
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"RetainedVolumeId": "vol-0123456789abcdef0"},
    )
    monkeypatch.setattr(
        environment,
        "_host_status_payload_get",
        lambda **kwargs: {"wcc_activity": "idle"},
    )
    monkeypatch.setattr(
        environment,
        "_ssm_shell_run",
        lambda command_list: event_list.append(("ssm", command_list)),
    )

    def stop_lease_upsert(
        *, lease_duration: timedelta = development_environment.LEASE_DURATION
    ) -> None:
        lease_duration_list.append(lease_duration)
        event_list.append(("lease", lease_duration, clock.t_now))

    monkeypatch.setattr(environment, "_stop_lease_upsert", stop_lease_upsert)

    def stop_lease_payload_get() -> dict[str, object]:
        if clock.t_now >= datetime(2026, 7, 28, 12, 7, 30, tzinfo=UTC):
            return {"state": "absent"}
        return {
            "schedule_expression": (
                "initial" if len(lease_duration_list) == 1 else "renewed"
            ),
            "state": "ENABLED",
        }

    monkeypatch.setattr(environment, "_stop_lease_payload_get", stop_lease_payload_get)

    def instance_state_get(instance_id: str) -> str:
        assert instance_id == "i-0123456789abcdef0"
        if clock.t_now >= datetime(2026, 7, 28, 12, 7, 30, tzinfo=UTC):
            return "stopped"
        return "running"

    monkeypatch.setattr(environment, "_instance_state_get", instance_state_get)
    monkeypatch.setattr(
        environment, "start", lambda: event_list.append(("start", clock.t_now))
    )
    monkeypatch.setattr(
        environment,
        "_product_recovery_acceptance_run",
        lambda: event_list.append("product-acceptance"),
    )

    environment.lifecycle_acceptance()

    assert lease_duration_list == [
        development_environment.LIFECYCLE_ACCEPTANCE_INITIAL_LEASE_DURATION,
        development_environment.LIFECYCLE_ACCEPTANCE_RENEWED_LEASE_DURATION,
    ]
    assert ("start", datetime(2026, 7, 28, 12, 7, 30, tzinfo=UTC)) in event_list
    assert event_list[-1] == "product-acceptance"


def test_lifecycle_acceptance_failure_restores_controller_and_production_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed acceptance probe must not leave the fail-safe controller disabled."""

    environment = _environment_get(tmp_path)
    event_list: list[object] = []
    lease_call_count = 0

    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(environment, "_source_repository_validate", lambda *args: None)
    monkeypatch.setattr(environment, "_stack_drift_validate", lambda stack_name: None)
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-0123456789abcdef0")
    monkeypatch.setattr(
        environment, "_instance_state_get", lambda instance_id: "running"
    )
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"RetainedVolumeId": "vol-0123456789abcdef0"},
    )
    monkeypatch.setattr(
        environment,
        "_host_status_payload_get",
        lambda **kwargs: {"wcc_activity": "idle"},
    )
    monkeypatch.setattr(
        environment,
        "_ssm_shell_run",
        lambda command_list: event_list.append(("ssm", command_list)),
    )

    def stop_lease_upsert(
        *, lease_duration: timedelta = development_environment.LEASE_DURATION
    ) -> None:
        nonlocal lease_call_count
        lease_call_count += 1
        event_list.append(("lease", lease_duration))
        if lease_call_count == 1:
            raise DevelopmentEnvironmentError("acceptance scheduler failure")

    monkeypatch.setattr(environment, "_stop_lease_upsert", stop_lease_upsert)
    monkeypatch.setattr(
        environment,
        "_host_readiness_wait",
        lambda: event_list.append("readiness"),
    )

    with pytest.raises(
        DevelopmentEnvironmentError, match="acceptance scheduler failure"
    ):
        environment.lifecycle_acceptance()

    assert event_list[-3:] == [
        ("lease", development_environment.LEASE_DURATION),
        (
            "ssm",
            ["sudo systemctl start workflow-control-center-host-controller"],
        ),
        "readiness",
    ]


def test_start_never_calls_ec2_when_initial_stop_lease_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The external failure lease is a precondition, not best-effort cleanup."""

    environment = _environment_get(tmp_path)
    monkeypatch.setattr(environment, "_local_operator_context_validate", lambda: None)
    monkeypatch.setattr(
        environment,
        "_stack_drift_validate",
        lambda stack_name: None,
    )
    monkeypatch.setattr(
        environment,
        "_source_repository_validate",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        environment,
        "_instance_id_get",
        lambda: "i-0123456789abcdef0",
    )
    monkeypatch.setattr(
        environment,
        "_stop_lease_upsert",
        lambda: (_ for _ in ()).throw(
            DevelopmentEnvironmentError("scheduler unavailable")
        ),
    )
    monkeypatch.setattr(
        environment,
        "_instance_state_get",
        lambda instance_id: pytest.fail("EC2 state must not be read"),
    )

    with pytest.raises(DevelopmentEnvironmentError, match="scheduler unavailable"):
        environment.start()
