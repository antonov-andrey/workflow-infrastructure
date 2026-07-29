"""Verify development-environment orchestration and declarative infrastructure behavior."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

from cfnlint.decode import decode
import pytest

from tool import development_environment_manage
from tool.lib import development_environment
from tool.lib.development_environment import (
    Clock,
    CommandRunner,
    DevelopmentEnvironment,
    DevelopmentEnvironmentError,
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


def _environment_get(project_root_path: Path) -> DevelopmentEnvironment:
    """Create one development environment with real commands and deterministic time.

    Args:
        project_root_path: Test project root.

    Returns:
        Development environment.
    """

    return DevelopmentEnvironment(
        clock=ClockFixed(), project_root_path=project_root_path, runner=CommandRunner()
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

    assert restore_args.snapshot_id == "snap-0123456789abcdef0"
    assert restore_args.ssh_argument_list == []
    assert activation_args.release == "20260728120000000000"
    assert activation_args.ssh_argument_list == []
    assert ssh_args.ssh_argument_list == ["-L", "8080:localhost:8080"]
    assert host_status_args.retained_volume_id == "vol-0123456789abcdef0"
    assert host_status_args.ssh_argument_list == []


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
    assert "SnapshotId" not in retained_volume_by_name_map["RetainedVolume"]["Properties"]
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

    launch_template_data = resource_by_name_map["DevelopmentLaunchTemplate"][
        "Properties"
    ]["LaunchTemplateData"]
    assert launch_template_data["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpPutResponseHopLimit": 1,
        "HttpTokens": "required",
        "InstanceMetadataTags": "disabled",
    }
    user_data = launch_template_data["UserData"]["Fn::Base64"]["Fn::Sub"][0]
    assert "apt upgrade" not in user_data
    assert "--disable traefik --secrets-encryption" in user_data
    assert "https://get.k3s.io" not in user_data
    assert (
        "https://raw.githubusercontent.com/k3s-io/k3s/${K3sVersion}/install.sh"
        in user_data
    )
    assert "K3sInstallScriptSha256" in user_data
    assert "sha256sum --check --strict" in user_data
    assert "uv python install 3.14" in user_data
    assert "/srv/workflow-control-center/release" in user_data

    launch_template_reference = {
        "LaunchTemplateId": {"Ref": "DevelopmentLaunchTemplate"},
        "Version": {"Fn::GetAtt": ["DevelopmentLaunchTemplate", "LatestVersionNumber"]},
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
    assert launch_template_data["UserData"]["Fn::Base64"]["Fn::Sub"][1][
        "RetainedVolumeId"
    ] == retained_volume_reference
    assert template["Outputs"]["RetainedVolumeId"]["Value"] == retained_volume_reference
    assert template["Outputs"]["RetainedVolumeSlot"]["Value"] == {
        "Ref": "RetainedVolumeSlot"
    }
    assert template["Outputs"]["RetainedVolumeSourceSnapshotId"]["Value"] == {
        "Ref": "RetainedVolumeSnapshotId"
    }
    assert template["Outputs"]["LatestLaunchTemplateVersion"]["Value"] == {
        "Fn::GetAtt": ["DevelopmentLaunchTemplate", "LatestVersionNumber"]
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

    environment._cost_review_record()

    payload = json.loads(
        (tmp_path / ".local/cost-review.json").read_text(encoding="utf-8")
    )
    assert payload["assumption"] == {
        "active_hour_count_monthly": 80,
        "gp3_gib_count_max": 260,
        "snapshot_gib_count_max": 80,
    }
    assert payload["estimated_monthly_usd"] == {
        "compute": "8.00",
        "gp3_max": "20.80",
        "snapshot_max": "4.00",
        "total_fixed_max": "32.80",
    }
    assert payload["architecture_delta_monthly_usd"] == {
        "bounded_retained_rollback_volume_max": "6.40",
        "total_max": "6.40",
    }
    assert set(payload["unchanged_usage_based_service_by_name_map"]) == {
        "api_gateway",
        "athena",
        "data_transfer",
        "glue",
        "kms",
        "s3",
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
        lambda stack_name, is_required: {
            "StackStatus": "UPDATE_COMPLETE"
        },
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


def test_ordinary_compute_apply_rejects_every_possible_stable_identity_replacement(
    tmp_path: Path,
) -> None:
    """Only the explicit replacement workflow may change instance or retained attachment identity."""

    environment = _environment_get(tmp_path)
    change_summary_list = [
        {
            "action": "Modify",
            "logical_resource_id": "DevelopmentLaunchTemplate",
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


def test_replacement_parameters_select_next_slot_and_enable_creation_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit replacement makes future instance creation fail-safe before it starts."""

    environment = _environment_get(tmp_path)
    monkeypatch.setattr(
        environment,
        "_stack_output_by_name_map_get",
        lambda stack_name: {"InstanceSlot": "a"},
    )

    assert environment._replacement_parameter_by_name_map_get() == {
        "InstanceSlot": "b",
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
        lambda arguments: {
            "Volumes": [current_volume_payload, retired_volume_payload]
        },
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

    environment._retired_retained_volume_cleanup(
        current_volume_id=current_volume_id
    )

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
        lambda **kwargs: operation_list.append("apply"),
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

    assert operation_list == ["detach", "apply", "attachment", "version"]


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
        lambda **kwargs: operation_list.append(kwargs["parameter_by_name_map"]),
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
        {
            "InstanceSlot": "b",
            "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
            "ReplacementGuardScheduleState": "ENABLED",
        },
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
        lambda **kwargs: operation_list.append(kwargs["parameter_by_name_map"]),
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
        lambda **kwargs: operation_list.append(
            ("disable-backup", kwargs["volume_id"])
        ),
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
        {
            "InstanceSlot": "b",
            "ReplacementGuardScheduleExpression": "at(2026-07-28T14:00:00)",
            "ReplacementGuardScheduleState": "ENABLED",
            "RetainedVolumeSlot": "a",
            "RetainedVolumeSnapshotId": "snap-0123456789abcdef0",
        },
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

    monkeypatch.setattr(
        environment, "_ssm_shell_result_get", ssm_shell_result_get
    )

    assert (
        environment._host_status_payload_get(
            retained_volume_id="vol-0123456789abcdef0"
        )
        == expected_payload
    )
    assert len(command_list_list) == 1
    assert command_list_list[0][0].startswith(
        "python3.14 /opt/workflow-infrastructure/control/current/"
    )
    assert (
        "host-status --retained-volume-id vol-0123456789abcdef0"
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
                                "conditions": [
                                    {"status": "True", "type": "Ready"}
                                ]
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
    monkeypatch.setattr(
        environment, "_host_product_activity_get", lambda: "idle"
    )

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
    monkeypatch.setattr(
        environment, "_active_session_count_get", lambda instance_id: 1
    )
    monkeypatch.setattr(
        environment,
        "_latest_snapshot_id_get",
        lambda volume_id: "snap-latest",
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


def _retained_product_release_prepare(
    release_root_path: Path,
    *,
    release_name: str,
) -> None:
    """Create one internally consistent retained Product release fixture."""

    source_identity_by_name_map: dict[str, dict[str, str]] = {}
    repository_by_name_map: dict[str, dict[str, object]] = {}
    for index, repository_name in enumerate(
        [
            "workflow-infrastructure",
            *development_environment.PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
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
        source_identity_by_name_map[repository_name] = source_identity
        repository_by_name_map[repository_name] = {
            **source_identity,
            "file_sha256_by_path_map": {
                "tracked.txt": hashlib.sha256(source_payload).hexdigest()
            },
            "submodule_by_path_map": {},
        }
    source_manifest = {
        "release": release_name,
        "repository_by_name_map": repository_by_name_map,
    }
    source_manifest_bytes = (
        json.dumps(source_manifest, indent=2, sort_keys=True) + "\n"
    ).encode()
    (release_root_path / "source-manifest.json").write_bytes(source_manifest_bytes)
    render_bytes = b"apiVersion: v1\nkind: List\nitems: []\n"
    (release_root_path / "render.yaml").write_bytes(render_bytes)
    (release_root_path / "release-manifest.json").write_text(
        json.dumps(
            {
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
        match="source file digest differs",
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
        if command_list[-1] == "host-prepare"
        and "/sources/workflow-infrastructure/" in " ".join(command_list)
    )
    product_deploy_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if command_list[-1] == "linux/arm64"
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
        if command_list[-1] == "host-install"
        and str(development_environment.HOST_CURRENT_SOURCE_PATH)
        in " ".join(command_list)
    )
    controller_host_install_index = next(
        index
        for index, command_list in enumerate(remote_command_list_list)
        if command_list[-1] == "host-install"
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


def test_host_prepare_installs_checksum_pinned_helm_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Host preparation verifies and installs the exact native Helm binary."""

    binary_payload = b"\x7fELFtest-helm-binary"
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("linux-arm64/helm")
        member.size = len(binary_payload)
        archive.addfile(member, io.BytesIO(binary_payload))
    archive_payload = archive_buffer.getvalue()
    helm_path = tmp_path / "bin/helm"
    helm_path.parent.mkdir()
    monkeypatch.setattr(development_environment, "HELM_BINARY_PATH", helm_path)
    monkeypatch.setattr(development_environment.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        development_environment,
        "HELM_RELEASE_BY_MACHINE_MAP",
        {
            "aarch64": (
                "arm64",
                hashlib.sha256(archive_payload).hexdigest(),
            )
        },
    )

    class HelmRunner:
        """Provide the pinned archive and report the installed binary version."""

        def run(
            self,
            command_list: list[str],
            *,
            check: bool = True,
            input_text: str | None = None,
            should_capture: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            del check, input_text, should_capture
            if command_list[0] == "curl":
                output_path = Path(command_list[command_list.index("-o") + 1])
                output_path.write_bytes(archive_payload)
                return subprocess.CompletedProcess(command_list, 0, "", "")
            assert command_list[0] == str(helm_path)
            assert helm_path.read_bytes() == binary_payload
            return subprocess.CompletedProcess(
                command_list,
                0,
                development_environment.HELM_VERSION + "\n",
                "",
            )

    environment = DevelopmentEnvironment(
        clock=ClockFixed(),
        project_root_path=tmp_path,
        runner=HelmRunner(),  # type: ignore[arg-type]
    )
    environment._is_host = True

    environment.host_prepare()

    assert helm_path.read_bytes() == binary_payload
    assert helm_path.stat().st_mode & 0o777 == 0o755


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
        environment, "_local_operator_context_validate", lambda: event_list.append("context")
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
    monkeypatch.setattr(
        environment, "_instance_id_get", lambda: "i-0123456789abcdef0"
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
        lease_duration_list.append(lease_duration)
        event_list.append(("lease", lease_duration, clock.t_now))

    monkeypatch.setattr(environment, "_stop_lease_upsert", stop_lease_upsert)

    def stop_lease_payload_get() -> dict[str, object]:
        if clock.t_now >= datetime(2026, 7, 28, 12, 5, 30, tzinfo=UTC):
            return {"state": "absent"}
        return {
            "schedule_expression": (
                "initial" if len(lease_duration_list) == 1 else "renewed"
            ),
            "state": "ENABLED",
        }

    monkeypatch.setattr(
        environment, "_stop_lease_payload_get", stop_lease_payload_get
    )

    def instance_state_get(instance_id: str) -> str:
        assert instance_id == "i-0123456789abcdef0"
        if clock.t_now >= datetime(2026, 7, 28, 12, 5, 30, tzinfo=UTC):
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
    assert ("start", datetime(2026, 7, 28, 12, 5, 30, tzinfo=UTC)) in event_list
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
    monkeypatch.setattr(
        environment, "_source_repository_validate", lambda *args: None
    )
    monkeypatch.setattr(environment, "_stack_drift_validate", lambda stack_name: None)
    monkeypatch.setattr(
        environment, "_instance_id_get", lambda: "i-0123456789abcdef0"
    )
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
