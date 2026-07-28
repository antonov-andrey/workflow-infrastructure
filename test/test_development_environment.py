"""Verify development-environment orchestration and declarative infrastructure behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tarfile

from cfnlint.decode import decode
import pytest

from tool.lib import development_environment
from tool.lib.development_environment import Clock, CommandRunner, DevelopmentEnvironment, DevelopmentEnvironmentError


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


def _environment_get(project_root_path: Path) -> DevelopmentEnvironment:
    """Create one development environment with real commands and deterministic time.

    Args:
        project_root_path: Test project root.

    Returns:
        Development environment.
    """

    return DevelopmentEnvironment(clock=ClockFixed(), project_root_path=project_root_path, runner=CommandRunner())


def _git_run(repository_path: Path, argument_list: list[str]) -> None:
    """Run one required Git fixture command.

    Args:
        repository_path: Fixture repository root.
        argument_list: Git arguments.
    """

    subprocess.run(["git", "-C", str(repository_path), *argument_list], check=True, capture_output=True, text=True)


def _template_get(project_root_path: Path, template_name: str) -> dict[str, object]:
    """Decode one CloudFormation template without discarding intrinsic functions.

    Args:
        project_root_path: Repository root.
        template_name: Template file name.

    Returns:
        Decoded template.
    """

    template, error_list = decode(str(project_root_path / "cloudformation" / template_name))
    assert error_list == []
    assert isinstance(template, dict)
    return template


def test_compute_template_owns_isolated_retained_recoverable_host() -> None:
    """Compute template must enforce the approved isolation, storage, and snapshot contract."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(project_root_path, "workflow-control-center-development-compute.yaml")
    resource_by_name_map = template["Resources"]
    assert isinstance(resource_by_name_map, dict)

    resource_type_set = {resource["Type"] for resource in resource_by_name_map.values() if isinstance(resource, dict)}
    assert "AWS::EC2::EIP" not in resource_type_set
    assert "AWS::EC2::NatGateway" not in resource_type_set
    security_group = resource_by_name_map["DevelopmentSecurityGroup"]
    assert "SecurityGroupIngress" not in security_group["Properties"]

    retained_volume = resource_by_name_map["RetainedVolume"]
    assert retained_volume["DeletionPolicy"] == "Retain"
    assert retained_volume["UpdateReplacePolicy"] == "Retain"
    assert retained_volume["Properties"]["Encrypted"] is True
    assert retained_volume["Properties"]["VolumeType"] == "gp3"

    snapshot_schedule = resource_by_name_map["RetainedSnapshotLifecyclePolicy"]["Properties"]["PolicyDetails"][
        "Schedules"
    ][0]
    assert snapshot_schedule["CreateRule"]["Interval"] == 24
    assert snapshot_schedule["RetainRule"]["Count"] == 7

    launch_template_data = resource_by_name_map["DevelopmentLaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert launch_template_data["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpPutResponseHopLimit": 1,
        "HttpTokens": "required",
        "InstanceMetadataTags": "disabled",
    }
    user_data = launch_template_data["UserData"]["Fn::Base64"]["Fn::Sub"][0]
    assert "apt upgrade" not in user_data
    assert "--disable traefik --secrets-encryption" in user_data
    assert "uv python install 3.14" in user_data

    launch_template_reference = {
        "LaunchTemplateId": {"Ref": "DevelopmentLaunchTemplate"},
        "Version": {"Fn::GetAtt": ["DevelopmentLaunchTemplate", "LatestVersionNumber"]},
    }
    assert resource_by_name_map["DevelopmentInstance"]["Properties"]["LaunchTemplate"] == launch_template_reference
    assert resource_by_name_map["RetainedVolumeAttachment"]["Properties"] == {
        "Device": "/dev/sdf",
        "InstanceId": {"Ref": "DevelopmentInstance"},
        "VolumeId": {"Ref": "RetainedVolume"},
    }


def test_data_plane_template_adds_compute_trust_without_narrowing_platform_permissions() -> None:
    """Data-plane template must keep universal platform authority and add EC2 trust."""

    project_root_path = Path(__file__).resolve().parents[1]
    template = _template_get(project_root_path, "workflow-control-center-development.yaml")
    platform_role = template["Resources"]["PlatformRole"]["Properties"]
    assert platform_role["ManagedPolicyArns"] == [
        {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AdministratorAccess"}
    ]
    trust_statement_list = platform_role["AssumeRolePolicyDocument"]["Statement"]
    assert any(statement.get("Principal") == {"Service": "ec2.amazonaws.com"} for statement in trust_statement_list)
    assert template["Parameters"]["UiOrigin"]["Default"] == "http://localhost:8080"


def test_runtime_platform_accepts_one_linux_arm64_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runtime platform discovery must normalize one homogeneous eligible node set."""

    environment = _environment_get(tmp_path)
    node_payload = {
        "items": [
            {"status": {"nodeInfo": {"architecture": "arm64", "operatingSystem": "linux"}}},
            {"status": {"nodeInfo": {"architecture": "arm64", "operatingSystem": "linux"}}},
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
            {"status": {"nodeInfo": {"architecture": "arm64", "operatingSystem": "linux"}}},
            {"status": {"nodeInfo": {"architecture": "amd64", "operatingSystem": "linux"}}},
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


def test_source_archive_is_deterministic_and_excludes_untracked_files(tmp_path: Path) -> None:
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
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote_path)], check=True)
    repository_path = tmp_path / "repository"
    subprocess.run(["git", "clone", str(remote_path), str(repository_path)], check=True, capture_output=True, text=True)
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
        environment._source_repository_validate(repository_path, "workflow-infrastructure")
    _git_run(repository_path, ["restore", "tracked.txt"])

    (repository_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git_run(repository_path, ["add", "tracked.txt"])
    _git_run(repository_path, ["commit", "-m", "unpublished"])
    with pytest.raises(DevelopmentEnvironmentError, match="not exact origin/main"):
        environment._source_repository_validate(repository_path, "workflow-infrastructure")


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
        ["cloudformation", "describe-stacks", "--stack-name", "workflow-control-center-development"],
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
    monkeypatch.setattr(environment, "_instance_id_get", lambda: "i-0123456789abcdef0")
    monkeypatch.setattr(environment, "_instance_state_get", lambda instance_id: "stopped")
    monkeypatch.setattr(environment, "_instance_online_wait", lambda: operation_list.append("online"))
    monkeypatch.setattr(environment, "_stop_lease_upsert", lambda instance_id: operation_list.append("lease"))
    monkeypatch.setattr(environment, "_aws_run", aws_run)

    environment.start()
    assert operation_list[0] == "lease"
    assert operation_list[1].startswith("ec2 start-instances")
    assert operation_list[2] == "online"


def test_stop_lease_uses_renewable_direct_stop_instances_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lease renewal must create the approved two-hour one-time direct EC2 stop target."""

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
        lambda stack_name: {"SchedulerExecutionRoleArn": "arn:aws:iam::463564115167:role/scheduler"},
    )
    monkeypatch.setattr(environment, "_aws_run", aws_run)
    monkeypatch.setattr(
        environment,
        "_stop_lease_payload_get",
        lambda: {"state": "ENABLED"},
    )

    environment._stop_lease_upsert(instance_id="i-0123456789abcdef0")
    create_argument_list = aws_argument_list_list[-1]
    assert create_argument_list[:2] == ["scheduler", "create-schedule"]
    assert "at(2026-07-28T14:00:00)" in create_argument_list
    assert "DELETE" in create_argument_list
    target_payload = json.loads(create_argument_list[create_argument_list.index("--target") + 1])
    assert target_payload["Arn"] == "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    assert json.loads(target_payload["Input"]) == {"InstanceIds": ["i-0123456789abcdef0"]}
