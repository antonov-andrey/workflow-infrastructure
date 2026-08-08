"""Verify exact, live-state task development-environment cleanup."""

from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

import development_environment_manage

from workflow_infrastructure.development_environment.aws import (
    aws_cli_error_get,
    aws_cli_error_matches,
)
from workflow_infrastructure.development_environment.cleanup.manager import (
    DevelopmentEnvironmentCleanupManager,
)
from workflow_infrastructure.development_environment.cleanup.compute import (
    ComputeCleanup,
)
from workflow_infrastructure.development_environment.cleanup.inventory import (
    CleanupInventoryResolver,
)
from workflow_infrastructure.development_environment.cleanup.kms import KmsCleanup
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.retained import (
    RetainedStorageCleanup,
)
from workflow_infrastructure.development_environment.cleanup.s3 import (
    VersionedBucketCleaner,
)
from workflow_infrastructure.development_environment.cleanup.verification import (
    CleanupAbsenceVerifier,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

COMMON_PREFIX = "2026-08-01-workflow-platform-hardening"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cleanup_request_requires_exact_closed_stdin_identity() -> None:
    """Manual, extended, or cross-task cleanup input is rejected before AWS use."""

    request = CleanupRequest.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "common_prefix": COMMON_PREFIX,
            }
        ),
        expected_common_prefix=COMMON_PREFIX,
    )
    assert request.payload_get() == {
        "schema_version": 1,
        "common_prefix": COMMON_PREFIX,
    }
    for payload in (
        "",
        "{}",
        json.dumps(
            {
                **request.payload_get(),
                "unexpected": True,
            }
        ),
        json.dumps(
            {
                **request.payload_get(),
                "common_prefix": "2026-08-01-another-task",
            }
        ),
        json.dumps(
            {
                **request.payload_get(),
                "operation_identity": "1" * 32,
            }
        ),
    ):
        with pytest.raises(DevelopmentEnvironmentError):
            CleanupRequest.from_json(payload, expected_common_prefix=COMMON_PREFIX)


def test_bootstrap_manifest_declares_only_the_registered_typed_cleanup_handler() -> None:
    """The repository manifest contains no executable cleanup command or fingerprint."""

    assert (PROJECT_ROOT / "worktree-bootstrap.yaml").read_text(encoding="utf-8") == """schema_version: 3
resource:
  copy_optional_path_list: []
  copy_required_path_list: []
  link_optional_path_list: []
  link_required_path_list: []
cleanup:
  handler_key_list:
    - workflow-infrastructure-development-environment
"""


def test_cleanup_cli_consumes_only_natural_identity_and_returns_exact_inventory(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixed Product boundary needs no receipt, fingerprint, or operation identity."""

    expected_response = {
        "schema_version": 1,
        "common_prefix": COMMON_PREFIX,
        "environment_name": "w0123456789abcde",
        "external_resources_absent": False,
        "resource_identity_list": ["compute-w0123456789abcde"],
    }

    class _Cleanup:
        @staticmethod
        def inventory(request: CleanupRequest) -> dict[str, object]:
            assert request == CleanupRequest(common_prefix=COMMON_PREFIX)
            return expected_response

    class _Environment:
        cleanup = _Cleanup()

        def __init__(self, **kwargs: object) -> None:
            del kwargs

    monkeypatch.setattr(development_environment_manage, "DevelopmentEnvironment", _Environment)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"schema_version": 1, "common_prefix": COMMON_PREFIX})),
    )

    assert development_environment_manage.main(["destroy-inventory", "--git-worktree", COMMON_PREFIX]) == 0
    assert json.loads(capsys.readouterr().out) == expected_response


class _S3Aws:
    """Stateful AWS boundary for one versioned bucket cleanup."""

    def __init__(self, *, delete_error: bool = False) -> None:
        """Initialize the S3 AWS dependencies.

        Args:
            delete_error: Delete error.
        """

        self.bucket_exists = True
        self.delete_error = delete_error
        self.upload_by_id = {"upload-1": "partial/file"}
        self.object_set = {("data/file", "v1"), ("data/file", "delete-marker")}
        self.mutation_argument_list: list[list[str]] = []

    @staticmethod
    def _expected_owner_require(argument_list: list[str]) -> None:
        """Require the account fence on every S3 bucket operation."""

        assert argument_list[argument_list.index("--expected-bucket-owner") + 1] == "463564115167"

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return the scripted cleanup-inventory AWS response.

        Args:
            argument_list: Exact command arguments.

        Returns:
            Decoded JSON object.
        """

        self._expected_owner_require(argument_list)
        if argument_list[:2] == ["s3api", "list-multipart-uploads"]:
            return {"Uploads": [{"Key": key, "UploadId": upload_id} for upload_id, key in self.upload_by_id.items()]}
        if argument_list[:2] == ["s3api", "list-object-versions"]:
            return {
                "Versions": [
                    {"Key": key, "VersionId": version} for key, version in self.object_set if version != "delete-marker"
                ],
                "DeleteMarkers": [
                    {"Key": key, "VersionId": version} for key, version in self.object_set if version == "delete-marker"
                ],
            }
        raise AssertionError(argument_list)

    def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run the S3 AWS operation.

        Args:
            argument_list: Exact command arguments.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """

        del check
        operation = tuple(argument_list[:2])
        expected_owner = argument_list[argument_list.index("--expected-bucket-owner") + 1]
        if operation == ("s3api", "head-bucket"):
            if expected_owner != "463564115167":
                return subprocess.CompletedProcess(
                    argument_list,
                    1,
                    "",
                    "An error occurred (AccessDenied) when calling the HeadBucket operation: Access Denied",
                )
            return subprocess.CompletedProcess(
                argument_list,
                0 if self.bucket_exists else 1,
                "",
                (
                    ""
                    if self.bucket_exists
                    else "An error occurred (NoSuchBucket) when calling the "
                    "HeadBucket operation: The specified bucket does not exist"
                ),
            )
        self._expected_owner_require(argument_list)
        if operation == ("s3api", "abort-multipart-upload"):
            self.mutation_argument_list.append(argument_list)
            upload_id = argument_list[argument_list.index("--upload-id") + 1]
            self.upload_by_id.pop(upload_id)
        elif operation == ("s3api", "delete-objects"):
            self.mutation_argument_list.append(argument_list)
            if self.delete_error:
                return subprocess.CompletedProcess(
                    argument_list,
                    0,
                    json.dumps({"Errors": [{"Code": "AccessDenied", "Key": "data/file"}]}),
                    "",
                )
            payload = json.loads(argument_list[argument_list.index("--delete") + 1])
            for item in payload["Objects"]:
                self.object_set.remove((item["Key"], item["VersionId"]))
        elif operation == ("s3api", "delete-bucket"):
            self.mutation_argument_list.append(argument_list)
            assert not self.upload_by_id
            assert not self.object_set
            self.bucket_exists = False
        else:
            raise AssertionError(argument_list)
        return subprocess.CompletedProcess(argument_list, 0, "{}", "")


def test_versioned_bucket_cleanup_removes_uploads_versions_and_markers() -> None:
    """S3 deletion uses the service identities instead of assuming an empty bucket."""

    aws = _S3Aws()
    cleaner = VersionedBucketCleaner(aws)
    cleaner.delete("task-bucket", expected_owner="463564115167")
    cleaner.delete("task-bucket", expected_owner="463564115167")
    assert not aws.bucket_exists
    assert cleaner.absent_get("task-bucket", expected_owner="463564115167")


def test_versioned_bucket_cleanup_rejects_partial_http_200_deletion() -> None:
    """S3 per-object errors remain failures even when the request returned HTTP 200."""

    with pytest.raises(DevelopmentEnvironmentError, match="partially deleted"):
        VersionedBucketCleaner(_S3Aws(delete_error=True)).delete(
            "task-bucket",
            expected_owner="463564115167",
        )


def test_versioned_bucket_cleanup_rejects_foreign_owner_before_any_mutation() -> None:
    """Every S3 lifecycle operation is fenced by the exact expected account."""

    aws = _S3Aws()

    with pytest.raises(DevelopmentEnvironmentError, match="ownership cannot be observed"):
        VersionedBucketCleaner(aws).delete("task-bucket", expected_owner="000000000000")

    assert aws.mutation_argument_list == []
    assert aws.bucket_exists
    assert aws.upload_by_id == {"upload-1": "partial/file"}
    assert aws.object_set == {("data/file", "v1"), ("data/file", "delete-marker")}


class _ComputeAws:
    """Return one EC2 tombstone and no active Session Manager sessions."""

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return the scripted versioned-bucket AWS response.

        Args:
            argument_list: Exact command arguments.

        Returns:
            Decoded JSON object.
        """

        assert argument_list[:3] == ["ssm", "describe-sessions", "--state"]
        return {"Sessions": []}

    def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run the compute AWS operation.

        Args:
            argument_list: Exact command arguments.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """

        del check
        assert argument_list[:2] == ["ec2", "describe-instances"]
        return subprocess.CompletedProcess(
            argument_list,
            0,
            json.dumps(
                {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": "i-0123456789abcdef0",
                                    "Placement": {"AvailabilityZone": "us-east-1a"},
                                    "State": {"Name": "terminated"},
                                    "Tags": _task_tag_list_get(),
                                }
                            ],
                            "OwnerId": "463564115167",
                        }
                    ]
                }
            ),
            "",
        )


def test_compute_cleanup_accepts_the_ec2_terminated_visibility_tombstone() -> None:
    """AWS's post-deletion EC2 tombstone does not block idempotent cleanup."""

    cleanup = ComputeCleanup(aws=_ComputeAws(), stack_cleanup=_Unused())
    cleanup.absence_validate(_cleanup_inventory_get())
    assert cleanup.absent_get(_cleanup_inventory_get())


@pytest.mark.parametrize(
    ("owner_id", "availability_zone", "task_owned", "error_pattern"),
    [
        ("463564115167", "us-east-1a", False, "another ownership identity"),
        ("000000000000", "us-east-1a", True, "inventory is malformed"),
        ("463564115167", "us-west-2a", True, "another region"),
    ],
)
def test_compute_cleanup_rejects_foreign_instance_before_any_mutation(
    owner_id: str,
    availability_zone: str,
    task_owned: bool,
    error_pattern: str,
) -> None:
    """A stale instance ID cannot authorize SSM, stop, stack, or termination mutations."""

    class _ForeignComputeAws:
        mutation_argument_list: list[list[str]] = []

        @staticmethod
        def json_get(argument_list: list[str]) -> dict[str, object]:
            raise AssertionError(argument_list)

        def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
            del check
            if argument_list[:2] != ["ec2", "describe-instances"]:
                self.mutation_argument_list.append(argument_list)
                return subprocess.CompletedProcess(argument_list, 0, "{}", "")
            tag_list = _task_tag_list_get()
            if not task_owned:
                tag_list = [item for item in tag_list if item["Key"] != "git-worktree"]
            return subprocess.CompletedProcess(
                argument_list,
                0,
                json.dumps(
                    {
                        "Reservations": [
                            {
                                "OwnerId": owner_id,
                                "Instances": [
                                    {
                                        "InstanceId": "i-0123456789abcdef0",
                                        "Placement": {"AvailabilityZone": availability_zone},
                                        "State": {"Name": "running"},
                                        "Tags": tag_list,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                "",
            )

    aws = _ForeignComputeAws()
    cleanup = ComputeCleanup(aws=aws, stack_cleanup=_Unused())

    with pytest.raises(DevelopmentEnvironmentError, match=error_pattern):
        cleanup.delete(_cleanup_inventory_get())

    assert aws.mutation_argument_list == []


def test_aws_absence_errors_require_exact_code_and_operation() -> None:
    """Incidental error text must never become destructive absence proof."""

    exact = subprocess.CompletedProcess(
        [],
        255,
        "",
        "An error occurred (InvalidVolume.NotFound) when calling the "
        "DescribeVolumes operation: The volume does not exist",
    )
    assert aws_cli_error_get(exact) is not None
    assert aws_cli_error_matches(
        exact,
        code_set=frozenset({"InvalidVolume.NotFound"}),
        operation="DescribeVolumes",
    )
    current_cli = subprocess.CompletedProcess(
        [],
        255,
        "",
        "\naws: [ERROR]: An error occurred (ResourceNotFoundException) when calling the "
        "GetSchedule operation: Schedule group does not exist.\n",
    )
    assert aws_cli_error_matches(
        current_cli,
        code_set=frozenset({"ResourceNotFoundException"}),
        operation="GetSchedule",
    )
    for diagnostic in (
        "404 Not Found",
        "AccessDenied: the requested object does not exist",
        "An error occurred (InvalidVolume.NotFound) when calling the "
        "DescribeInstances operation: misleading operation",
        "An error occurred (AccessDenied) when calling the DescribeVolumes "
        "operation: message mentions InvalidVolume.NotFound",
        "warning\nAn error occurred (InvalidVolume.NotFound) when calling the "
        "DescribeVolumes operation: The volume does not exist",
        "An error occurred (InvalidVolume.NotFound) when calling the "
        "DescribeVolumes operation: first\n"
        "An error occurred (InvalidVolume.NotFound) when calling the "
        "DescribeVolumes operation: second",
    ):
        assert not aws_cli_error_matches(
            subprocess.CompletedProcess([], 255, "", diagnostic),
            code_set=frozenset({"InvalidVolume.NotFound"}),
            operation="DescribeVolumes",
        )


class _KmsAws:
    """Stateful KMS boundary for alias-fencing and delayed physical deletion."""

    def __init__(
        self,
        *,
        alias_target_key_id: str | None,
        account_id: str = "463564115167",
        key_state: str,
        key_manager: str = "CUSTOMER",
        task_owned: bool = True,
    ) -> None:
        """Initialize the KMS AWS dependencies.

        Args:
            alias_target_key_id: Exact alias target key identity.
            account_id: Current key account identity.
            key_state: Key state.
            key_manager: Current key manager type.
            task_owned: Whether the current key retains exact task tags.
        """

        self.alias_target_key_id = alias_target_key_id
        self.account_id = account_id
        self.key_state = key_state
        self.key_manager = key_manager
        self.task_owned = task_owned
        self.mutation_argument_list: list[list[str]] = []

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return the scripted KMS cleanup AWS response.

        Args:
            argument_list: Exact command arguments.

        Returns:
            Decoded JSON object.
        """

        if argument_list[:2] == ["kms", "list-resource-tags"]:
            tag_list = _kms_task_tag_list_get()
            if not self.task_owned:
                tag_list = [item for item in tag_list if item["TagKey"] != "git-worktree"]
            return {"Tags": tag_list}
        assert argument_list == ["kms", "list-aliases"]
        alias_list = []
        if self.alias_target_key_id is not None:
            alias_list.append(
                {
                    "AliasName": "alias/storage-w0123456789abcde",
                    "TargetKeyId": self.alias_target_key_id,
                }
            )
        return {"Aliases": alias_list}

    def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run the KMS AWS operation.

        Args:
            argument_list: Exact command arguments.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Completed text-mode subprocess result.
        """

        del check
        if argument_list[:2] == ["kms", "describe-key"]:
            if self.key_state == "absent":
                return subprocess.CompletedProcess(
                    argument_list,
                    1,
                    "",
                    "An error occurred (NotFoundException) when calling the " "DescribeKey operation: Key not found",
                )
            return subprocess.CompletedProcess(
                argument_list,
                0,
                json.dumps(
                    {
                        "KeyMetadata": {
                            "Arn": "arn:aws:kms:us-east-1:463564115167:key/test",
                            "AWSAccountId": self.account_id,
                            "KeyManager": self.key_manager,
                            "KeyState": self.key_state,
                        }
                    }
                ),
                "",
            )
        if argument_list[:2] == ["kms", "delete-alias"]:
            self.mutation_argument_list.append(argument_list)
            self.alias_target_key_id = None
        elif argument_list[:2] == ["kms", "disable-key"]:
            self.mutation_argument_list.append(argument_list)
            self.key_state = "Disabled"
        elif argument_list[:2] == ["kms", "schedule-key-deletion"]:
            self.mutation_argument_list.append(argument_list)
            self.key_state = "PendingDeletion"
        else:
            raise AssertionError(argument_list)
        return subprocess.CompletedProcess(argument_list, 0, "{}", "")


def _cleanup_inventory_get() -> CleanupInventory:
    """Build one complete task-environment cleanup inventory fixture.

    Returns:
        The cleanup inventory.
    """

    return CleanupInventory(
        account_id="463564115167",
        bucket_name_list=("bucket-a", "bucket-b", "bucket-c", "bucket-d"),
        common_prefix=COMMON_PREFIX,
        compute_stack_name=_Identity.compute_stack_name,
        data_stack_name=_Identity.data_plane_stack_name,
        environment_name=_Identity.environment_name,
        instance_id_list=("i-0123456789abcdef0",),
        kms_alias_name="alias/storage-w0123456789abcde",
        kms_key_arn_list=("arn:aws:kms:us-east-1:463564115167:key/test",),
        region="us-east-1",
        retained_volume_id_list=("vol-0123456789abcdef0",),
    )


def test_cleanup_inventory_rejects_mutation_identities_outside_its_closed_shape() -> None:
    """An invalid runtime identity cannot broaden the AWS mutation scope."""

    with pytest.raises(DevelopmentEnvironmentError, match="inventory is malformed"):
        replace(_cleanup_inventory_get(), instance_id_list=("primary-instance",))


def test_kms_cleanup_accepts_physical_absence_after_pending_deletion() -> None:
    """A cleanup resume after AWS finishes deletion remains idempotent."""

    cleanup = KmsCleanup(_KmsAws(alias_target_key_id=None, key_state="absent"))
    inventory = _cleanup_inventory_get()
    cleanup.retire(inventory)
    cleanup.absence_validate(inventory)
    assert cleanup.absent_get(inventory)


def test_kms_cleanup_rejects_expected_alias_retargeting() -> None:
    """The task alias cannot be deleted after somebody retargets it to another key."""

    cleanup = KmsCleanup(_KmsAws(alias_target_key_id="another-key", key_state="Enabled"))
    with pytest.raises(DevelopmentEnvironmentError, match="ownership is ambiguous"):
        cleanup.retire(_cleanup_inventory_get())


def test_kms_cleanup_retires_only_the_freshly_reattested_task_key() -> None:
    """Every KMS mutation is fenced by current key identity and task tags."""

    aws = _KmsAws(alias_target_key_id="test", key_state="Enabled")
    cleanup = KmsCleanup(aws)

    cleanup.retire(_cleanup_inventory_get())

    assert [argument_list[:2] for argument_list in aws.mutation_argument_list] == [
        ["kms", "delete-alias"],
        ["kms", "disable-key"],
        ["kms", "schedule-key-deletion"],
    ]
    assert cleanup.absent_get(_cleanup_inventory_get())


@pytest.mark.parametrize(
    ("account_id", "key_manager", "task_owned", "error_pattern"),
    [
        ("463564115167", "CUSTOMER", False, "another ownership identity"),
        ("000000000000", "CUSTOMER", True, "identity is malformed"),
        ("463564115167", "AWS", True, "identity is malformed"),
    ],
)
def test_kms_cleanup_rejects_foreign_key_before_any_mutation(
    account_id: str,
    key_manager: str,
    task_owned: bool,
    error_pattern: str,
) -> None:
    """A stale key ARN cannot authorize alias, disable, or deletion-schedule mutations."""

    aws = _KmsAws(
        account_id=account_id,
        alias_target_key_id="test",
        key_manager=key_manager,
        key_state="Enabled",
        task_owned=task_owned,
    )

    with pytest.raises(DevelopmentEnvironmentError, match=error_pattern):
        KmsCleanup(aws).retire(_cleanup_inventory_get())

    assert aws.mutation_argument_list == []


class _RetainedAws:
    """Expose freshly changeable EBS volume and snapshot ownership."""

    def __init__(
        self,
        *,
        account_id: str = "463564115167",
        availability_zone: str = "us-east-1a",
        snapshot_owner_id: str = "463564115167",
        snapshot_present: bool = False,
        state: str = "available",
        task_owned: bool = True,
        volume_type: str = "gp3",
        wait_deleted_returns_not_found: bool = False,
    ) -> None:
        """Initialize one retained-storage service double."""

        self.account_id = account_id
        self.availability_zone = availability_zone
        self.snapshot_owner_id = snapshot_owner_id
        self.snapshot_present = snapshot_present
        self.state = state
        self.task_owned = task_owned
        self.volume_type = volume_type
        self.wait_deleted_returns_not_found = wait_deleted_returns_not_found
        self.mutation_argument_list: list[list[str]] = []
        self.wait_argument_list: list[list[str]] = []

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return caller identity or the task snapshot discovery list."""

        if argument_list[:2] == ["sts", "get-caller-identity"]:
            return {"Account": self.account_id}
        if argument_list[:2] == ["ec2", "describe-snapshots"]:
            return {
                "Snapshots": (
                    [
                        {
                            "SnapshotId": "snap-0123456789abcdef0",
                            "Tags": _task_tag_list_get(),
                        }
                    ]
                    if self.snapshot_present
                    else []
                )
            }
        raise AssertionError(argument_list)

    def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Return fresh EBS identity reads and record only destructive calls."""

        assert isinstance(check, bool)
        if argument_list[:2] == ["ec2", "describe-volumes"]:
            if self.state == "not-found":
                return _volume_not_found_result(argument_list, operation="DescribeVolumes")
            tag_list = _task_tag_list_get(name=f"retained-{_Identity.environment_name}")
            if not self.task_owned:
                tag_list = [item for item in tag_list if item["Key"] != "git-worktree"]
            attachments = []
            if self.state == "in-use":
                attachments = [
                    {
                        "State": "attached",
                        "VolumeId": "vol-0123456789abcdef0",
                    }
                ]
            return subprocess.CompletedProcess(
                argument_list,
                0,
                json.dumps(
                    {
                        "Volumes": [
                            {
                                "Attachments": attachments,
                                "AvailabilityZone": self.availability_zone,
                                "State": self.state,
                                "Tags": tag_list,
                                "VolumeId": "vol-0123456789abcdef0",
                                "VolumeType": self.volume_type,
                            }
                        ]
                    }
                ),
                "",
            )
        if argument_list[:2] == ["ec2", "describe-snapshots"]:
            return subprocess.CompletedProcess(
                argument_list,
                0,
                json.dumps(
                    {
                        "Snapshots": [
                            {
                                "OwnerId": self.snapshot_owner_id,
                                "SnapshotId": "snap-0123456789abcdef0",
                                "State": "completed",
                                "Tags": _task_tag_list_get(),
                            }
                        ]
                    }
                ),
                "",
            )
        if tuple(argument_list[:2]) in {
            ("ec2", "delete-snapshot"),
            ("ec2", "delete-volume"),
        }:
            self.mutation_argument_list.append(argument_list)
            if argument_list[:2] == ["ec2", "delete-volume"]:
                if self.state == "not-found":
                    return _volume_not_found_result(argument_list, operation="DeleteVolume")
                self.state = "deleting"
        elif argument_list[:2] == ["ec2", "wait"]:
            self.wait_argument_list.append(argument_list)
            waiter_name = argument_list[2]
            if waiter_name == "volume-available":
                if self.state == "not-found":
                    return _volume_not_found_result(argument_list, operation="DescribeVolumes")
                if self.state == "in-use":
                    self.state = "available"
            elif waiter_name == "volume-deleted":
                if self.state == "deleting":
                    self.state = "not-found"
                if self.wait_deleted_returns_not_found:
                    return _volume_not_found_result(argument_list, operation="DescribeVolumes")
            else:
                raise AssertionError(argument_list)
        else:
            raise AssertionError(argument_list)
        return subprocess.CompletedProcess(argument_list, 0, "{}", "")


def _volume_not_found_result(
    argument_list: list[str],
    *,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    """Return one exact AWS CLI EBS absence diagnostic."""

    return subprocess.CompletedProcess(
        argument_list,
        255,
        "",
        f"An error occurred (InvalidVolume.NotFound) when calling the {operation} operation: Volume absent",
    )


def test_retained_storage_deletes_only_freshly_reattested_task_resources() -> None:
    """Snapshot and volume mutations follow exact current ownership reads."""

    aws = _RetainedAws(snapshot_present=True)

    RetainedStorageCleanup(aws).delete(_cleanup_inventory_get())

    assert [argument_list[:2] for argument_list in aws.mutation_argument_list] == [
        ["ec2", "delete-snapshot"],
        ["ec2", "delete-volume"],
    ]
    assert [argument_list[2] for argument_list in aws.wait_argument_list] == ["volume-deleted"]


@pytest.mark.parametrize(
    ("state", "expected_delete_count", "expected_waiter_name_list"),
    [
        ("available", 1, ["volume-deleted"]),
        ("in-use", 1, ["volume-available", "volume-deleted"]),
        ("deleting", 0, ["volume-deleted"]),
        ("deleted", 0, []),
        ("not-found", 0, []),
    ],
)
def test_retained_storage_converges_from_each_live_volume_state(
    state: str,
    expected_delete_count: int,
    expected_waiter_name_list: list[str],
) -> None:
    """Available, attached, deleting, deleted, and absent volumes use distinct transitions."""

    aws = _RetainedAws(state=state)

    cleanup = RetainedStorageCleanup(aws)
    cleanup.delete(_cleanup_inventory_get())

    assert sum(item[:2] == ["ec2", "delete-volume"] for item in aws.mutation_argument_list) == expected_delete_count
    assert [item[2] for item in aws.wait_argument_list] == expected_waiter_name_list
    assert "volume-available" not in expected_waiter_name_list or state == "in-use"
    assert cleanup.absent_get(_cleanup_inventory_get())


def test_retained_storage_accepts_not_found_while_waiting_for_deletion() -> None:
    """A deletion waiter NotFound is exact convergence rather than an availability retry."""

    aws = _RetainedAws(state="deleting", wait_deleted_returns_not_found=True)

    RetainedStorageCleanup(aws).delete(_cleanup_inventory_get())

    assert [item[2] for item in aws.wait_argument_list] == ["volume-deleted"]
    assert aws.mutation_argument_list == []


@pytest.mark.parametrize(
    ("kwargs", "error_pattern"),
    [
        ({"account_id": "000000000000"}, "another AWS account"),
        ({"availability_zone": "us-west-2a"}, "ownership is malformed"),
        ({"volume_type": "io2"}, "ownership is malformed"),
        ({"task_owned": False}, "another ownership identity"),
        ({"state": "error"}, "not safely deletable"),
    ],
)
def test_retained_storage_rejects_foreign_or_changed_volume_before_mutation(
    kwargs: dict[str, object],
    error_pattern: str,
) -> None:
    """A stale retained-volume ID never authorizes deletion after identity drift."""

    aws = _RetainedAws(**kwargs)

    with pytest.raises(DevelopmentEnvironmentError, match=error_pattern):
        RetainedStorageCleanup(aws).delete(_cleanup_inventory_get())

    assert aws.mutation_argument_list == []


def test_retained_storage_rejects_foreign_snapshot_before_mutation() -> None:
    """Snapshot inventory is re-attested to the exact account at deletion time."""

    aws = _RetainedAws(snapshot_owner_id="000000000000", snapshot_present=True)

    with pytest.raises(DevelopmentEnvironmentError, match="ownership is malformed"):
        RetainedStorageCleanup(aws).delete(_cleanup_inventory_get())

    assert aws.mutation_argument_list == []


class _Account:
    """Accept the fixed test operator context without external AWS access."""

    def local_operator_context_validate(self) -> None:
        """Accept the fixed test account and region without external I/O."""

        return None


class _Identity:
    """Declare every exact identity expected in the cleanup inventory."""

    compute_stack_name = "compute-w0123456789abcde"
    data_plane_stack_name = "data-w0123456789abcde"
    environment_name = "w0123456789abcde"
    git_worktree = COMMON_PREFIX
    is_primary = False


class _PrimaryIdentity(_Identity):
    """Expose an invalid primary identity at the typed cleanup boundary."""

    git_worktree = ""
    is_primary = True


def _task_tag_list_get(*, name: str = "") -> list[dict[str, str]]:
    """Return exact task ownership tags for one fake AWS resource.

    Args:
        name: Optional Name tag.

    Returns:
        Exact task ownership tags.
    """

    tag_by_name_map = {
        "EnvironmentClass": "development",
        "EnvironmentName": _Identity.environment_name,
        "ManagedBy": "CloudFormation",
        "git-worktree": COMMON_PREFIX,
    }
    if name:
        tag_by_name_map["Name"] = name
    return [{"Key": key, "Value": value} for key, value in sorted(tag_by_name_map.items())]


def _kms_task_tag_list_get() -> list[dict[str, str]]:
    """Return exact KMS list-resource-tags field names.

    Returns:
        Exact KMS ownership tags.
    """

    return [{"TagKey": item["Key"], "TagValue": item["Value"]} for item in _task_tag_list_get()]


class _CleanupInventoryAws:
    """Expose a configurable partially deleted task environment."""

    def __init__(
        self,
        *,
        alias_key_arn: str = "",
        foreign_kms_key_arn_list: tuple[str, ...] = (),
        instance_id_list: tuple[str, ...] = (),
        kms_key_arn_list: tuple[str, ...] = (),
        retained_volume_id_list: tuple[str, ...] = (),
    ) -> None:
        """Initialize the fake task resource inventory.

        Args:
            alias_key_arn: Key targeted by the deterministic alias.
            foreign_kms_key_arn_list: Account keys without this task's ownership tags.
            instance_id_list: Remaining instance identities.
            kms_key_arn_list: Remaining tagged KMS identities.
            retained_volume_id_list: Remaining retained-volume identities.
        """

        self.alias_key_arn = alias_key_arn
        self.foreign_kms_key_arn_list = foreign_kms_key_arn_list
        self.instance_id_list = instance_id_list
        self.kms_key_arn_list = kms_key_arn_list
        self.retained_volume_id_list = retained_volume_id_list

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return one configured discovery response.

        Args:
            argument_list: Exact AWS arguments.

        Returns:
            Configured discovery response.
        """

        if argument_list[:2] == ["ec2", "describe-instances"]:
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": instance_id, "Tags": _task_tag_list_get()}
                            for instance_id in self.instance_id_list
                        ]
                    }
                ]
            }
        if argument_list[:2] == ["ec2", "describe-volumes"]:
            return {
                "Volumes": [
                    {
                        "VolumeId": volume_id,
                        "Tags": _task_tag_list_get(name=f"retained-{_Identity.environment_name}"),
                    }
                    for volume_id in self.retained_volume_id_list
                ]
            }
        if argument_list[:2] == ["kms", "list-keys"]:
            return {
                "Keys": [
                    {"KeyArn": key_arn, "KeyId": key_arn.rsplit("/", maxsplit=1)[-1]}
                    for key_arn in (*self.kms_key_arn_list, *self.foreign_kms_key_arn_list)
                ],
                "Truncated": False,
            }
        if argument_list[:2] == ["kms", "list-resource-tags"]:
            key_arn = argument_list[argument_list.index("--key-id") + 1]
            if key_arn in self.foreign_kms_key_arn_list:
                return {"Tags": []}
            return {"Tags": _kms_task_tag_list_get()}
        raise AssertionError(argument_list)

    def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Resolve the optional deterministic KMS alias.

        Args:
            argument_list: Exact AWS arguments.
            check: Whether a nonzero command exit raises an error.

        Returns:
            Configured command result.
        """

        del check
        if argument_list[:2] == ["ec2", "describe-instances"]:
            instance_id = argument_list[-1]
            return subprocess.CompletedProcess(
                argument_list,
                0,
                json.dumps(
                    {
                        "Reservations": [
                            {
                                "OwnerId": "463564115167",
                                "Instances": [
                                    {
                                        "InstanceId": instance_id,
                                        "Placement": {"AvailabilityZone": "us-east-1a"},
                                        "State": {"Name": "running"},
                                        "Tags": _task_tag_list_get(),
                                    }
                                ],
                            }
                        ]
                    }
                ),
                "",
            )
        if argument_list[:2] == ["ec2", "describe-volumes"]:
            volume_id = argument_list[-1]
            return subprocess.CompletedProcess(
                argument_list,
                0,
                json.dumps(
                    {
                        "Volumes": [
                            {
                                "VolumeId": volume_id,
                                "AvailabilityZone": "us-east-1a",
                                "Tags": _task_tag_list_get(name=f"retained-{_Identity.environment_name}"),
                            }
                        ]
                    }
                ),
                "",
            )
        assert argument_list[:2] == ["kms", "describe-key"]
        key_id = argument_list[-1]
        key_arn = self.alias_key_arn if key_id.startswith("alias/") else key_id
        known_key_arn_set = {*self.kms_key_arn_list, *self.foreign_kms_key_arn_list}
        if self.alias_key_arn:
            known_key_arn_set.add(self.alias_key_arn)
        if not key_arn or (not key_id.startswith("alias/") and key_arn not in known_key_arn_set):
            return subprocess.CompletedProcess(
                argument_list,
                1,
                "",
                "An error occurred (NotFoundException) when calling the DescribeKey operation: Key not found",
            )
        return subprocess.CompletedProcess(
            argument_list,
            0,
            json.dumps(
                {
                    "KeyMetadata": {
                        "Arn": key_arn,
                        "AWSAccountId": "463564115167",
                        "KeyManager": "CUSTOMER",
                        "KeyState": "Enabled",
                    }
                }
            ),
            "",
        )


class _CleanupInventoryStack:
    """Expose optional exact stack payloads without requiring both stacks."""

    def __init__(self, payload_by_name: dict[str, dict[str, object]] | None = None) -> None:
        """Initialize stack payloads.

        Args:
            payload_by_name: Stack payload by exact name.
        """

        self.payload_by_name = payload_by_name or {}

    def payload_get(self, stack_name: str, *, is_required: bool) -> dict[str, object]:
        """Return an optional exact stack payload.

        Args:
            stack_name: Stack name.
            is_required: Whether the stack is required.

        Returns:
            Stack payload or an empty mapping.
        """

        assert not is_required
        return self.payload_by_name.get(stack_name, {})


def _owned_stack_payload_get(output_by_name_map: dict[str, str]) -> dict[str, object]:
    """Build one exact stack payload with arbitrary available outputs.

    Args:
        output_by_name_map: Output values by logical name.

    Returns:
        Exact fake stack payload.
    """

    return {
        "Outputs": [{"OutputKey": name, "OutputValue": value} for name, value in sorted(output_by_name_map.items())],
        "Parameters": [
            {
                "ParameterKey": "EnvironmentName",
                "ParameterValue": _Identity.environment_name,
            },
            {"ParameterKey": "GitWorktree", "ParameterValue": COMMON_PREFIX},
        ],
        "Tags": _task_tag_list_get(),
    }


def test_cleanup_inventory_accepts_an_already_absent_environment() -> None:
    """No stack or retained resource is a successful empty cleanup inventory."""

    inventory = CleanupInventoryResolver(
        account_id="463564115167",
        aws=_CleanupInventoryAws(),
        identity=_Identity(),
        region="us-east-1",
        stack=_CleanupInventoryStack(),
    ).resolve(CleanupRequest(common_prefix=COMMON_PREFIX))

    assert inventory.instance_id_list == ()
    assert inventory.kms_key_arn_list == ()
    assert inventory.retained_volume_id_list == ()
    assert inventory.bucket_name_list == (
        "463564115167-us-east-1-w0123456789abcde-data",
        "463564115167-us-east-1-w0123456789abcde-observability",
        "463564115167-us-east-1-w0123456789abcde-result",
        "463564115167-us-east-1-w0123456789abcde-secret",
    )


def test_cleanup_inventory_unions_stack_outputs_with_every_tagged_orphan() -> None:
    """Partial stack deletion cannot hide additional exact task-owned resources."""

    stack_instance_id = "i-0123456789abcdef0"
    tagged_instance_id = "i-1123456789abcdef0"
    stack_volume_id = "vol-0123456789abcdef0"
    tagged_volume_id = "vol-1123456789abcdef0"
    stack_key_arn = "arn:aws:kms:us-east-1:463564115167:key/stack"
    tagged_key_arn = "arn:aws:kms:us-east-1:463564115167:key/tagged"
    foreign_key_arn = "arn:aws:kms:us-east-1:463564115167:key/foreign"
    inventory = CleanupInventoryResolver(
        account_id="463564115167",
        aws=_CleanupInventoryAws(
            alias_key_arn=stack_key_arn,
            foreign_kms_key_arn_list=(foreign_key_arn,),
            instance_id_list=(tagged_instance_id,),
            kms_key_arn_list=(tagged_key_arn,),
            retained_volume_id_list=(tagged_volume_id,),
        ),
        identity=_Identity(),
        region="us-east-1",
        stack=_CleanupInventoryStack(
            {
                _Identity.compute_stack_name: _owned_stack_payload_get(
                    {
                        "InstanceId": stack_instance_id,
                        "RetainedVolumeId": stack_volume_id,
                    }
                ),
            }
        ),
    ).resolve(CleanupRequest(common_prefix=COMMON_PREFIX))

    assert inventory.instance_id_list == (stack_instance_id, tagged_instance_id)
    assert inventory.kms_key_arn_list == (stack_key_arn, tagged_key_arn)
    assert inventory.retained_volume_id_list == (stack_volume_id, tagged_volume_id)


def test_cleanup_inventory_fully_enumerates_regional_kms_keys_before_task_filtering() -> None:
    """Final live proof cannot miss an exact-tagged task key on a later KMS page."""

    first_key_arn = "arn:aws:kms:us-east-1:463564115167:key/first"
    second_key_arn = "arn:aws:kms:us-east-1:463564115167:key/second"

    class _PaginatedKmsInventoryAws(_CleanupInventoryAws):
        list_key_argument_list: list[list[str]] = []

        def json_get(self, argument_list: list[str]) -> dict[str, object]:
            if argument_list[:2] != ["kms", "list-keys"]:
                return super().json_get(argument_list)
            self.list_key_argument_list.append(argument_list)
            if "--marker" not in argument_list:
                return {
                    "Keys": [{"KeyArn": first_key_arn, "KeyId": "first"}],
                    "Truncated": True,
                    "NextMarker": "next-page",
                }
            assert argument_list[-2:] == ["--marker", "next-page"]
            return {
                "Keys": [{"KeyArn": second_key_arn, "KeyId": "second"}],
                "Truncated": False,
            }

    aws = _PaginatedKmsInventoryAws(kms_key_arn_list=(first_key_arn, second_key_arn))
    inventory = CleanupInventoryResolver(
        account_id="463564115167",
        aws=aws,
        identity=_Identity(),
        region="us-east-1",
        stack=_CleanupInventoryStack(),
    ).resolve(CleanupRequest(common_prefix=COMMON_PREFIX))

    assert inventory.kms_key_arn_list == (first_key_arn, second_key_arn)
    assert aws.list_key_argument_list == [
        ["kms", "list-keys", "--no-paginate"],
        ["kms", "list-keys", "--no-paginate", "--marker", "next-page"],
    ]


class _Unused:
    """Fail immediately when cleanup reaches an unexpected dependency."""

    def __getattr__(self, name: str) -> object:
        """Provide the requested test-double attribute.

        Args:
            name: Canonical name.

        Returns:
            Requested unused test-double attribute value.
        """

        raise AssertionError(name)


class _InventoryResolver:
    """Return one exact cleanup inventory after validating its request identity."""

    def __init__(self, inventory: CleanupInventory) -> None:
        """Initialize the inventory resolver dependencies.

        Args:
            inventory: Inventory.
        """

        self._inventory = inventory
        self.resolve_count = 0

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Resolve the inventory resolver result.

        Args:
            request: Validated operation request.

        Returns:
            The inventory resolver result.
        """

        assert request.common_prefix == self._inventory.common_prefix
        self.resolve_count += 1
        return self._inventory


class _InventoryCleanup:
    """Append one named cleanup phase for a singleton task resource."""

    def __init__(self, operation_list: list[str], label: str) -> None:
        """Initialize the inventory cleanup dependencies.

        Args:
            operation_list: Ordered operation values.
            label: Diagnostic owner label.
        """

        self._operation_list = operation_list
        self._label = label

    def delete(self, inventory: CleanupInventory) -> None:
        """Delete the inventory cleanup target.

        Args:
            inventory: Inventory.
        """

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append(self._label)

    def session_absence_validate(self, inventory: CleanupInventory, instance_id_list: list[str]) -> None:
        """Record the final Session Manager absence readback.

        Args:
            inventory: Current task inventory.
            instance_id_list: Invocation-owned instance identities.
        """

        assert self._label == "compute"
        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append(f"verify:session:{','.join(instance_id_list)}")

    def session_list_terminate(self, inventory: CleanupInventory, instance_id_list: list[str]) -> None:
        """Record final active and disconnected session termination.

        Args:
            inventory: Current task inventory.
            instance_id_list: Invocation-owned instance identities.
        """

        assert self._label == "compute"
        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append(f"session:{','.join(instance_id_list)}")

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Record service-native absence proof for the inventory target.

        Args:
            inventory: Inventory.
        """

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append(f"verify:{self._label}")

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Record exact non-mutating absence readback for the inventory target."""

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append(f"read:{self._label}")
        return True


class _StorageCleanup:
    """Record each task bucket deleted by the storage phase."""

    def __init__(self, operation_list: list[str]) -> None:
        """Initialize the storage cleanup dependencies.

        Args:
            operation_list: Ordered operation values.
        """

        self._operation_list = operation_list

    def delete(self, bucket_name: str, *, expected_owner: str) -> None:
        """Delete the storage cleanup target.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
        """

        assert expected_owner == "463564115167"
        self._operation_list.append(f"bucket:{bucket_name}")

    def absence_validate(self, bucket_name: str, *, expected_owner: str) -> None:
        """Record service-native absence proof for one bucket.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
        """

        assert expected_owner == "463564115167"
        self._operation_list.append(f"verify:bucket:{bucket_name}")

    def absent_get(self, bucket_name: str, *, expected_owner: str) -> bool:
        """Record exact non-mutating absence readback for one bucket."""

        assert expected_owner == "463564115167"
        self._operation_list.append(f"read:bucket:{bucket_name}")
        return True


class _StackCleanup:
    """Record each task stack deleted by a stack phase."""

    def __init__(self, operation_list: list[str]) -> None:
        """Initialize the stack cleanup dependencies.

        Args:
            operation_list: Ordered operation values.
        """

        self._operation_list = operation_list

    def delete(self, stack_name: str) -> None:
        """Delete the stack cleanup target.

        Args:
            stack_name: Stack name.
        """

        self._operation_list.append(f"stack:{stack_name}")

    def absence_validate(self, stack_name: str) -> None:
        """Record CloudFormation-native absence proof for one stack.

        Args:
            stack_name: Stack name.
        """

        self._operation_list.append(f"verify:stack:{stack_name}")

    def absent_get(self, stack_name: str) -> bool:
        """Record exact non-mutating absence readback for one stack."""

        self._operation_list.append(f"read:stack:{stack_name}")
        return True


class _KmsCleanup:
    """Record retirement of the task-owned KMS key."""

    def __init__(self, operation_list: list[str]) -> None:
        """Initialize the KMS cleanup dependencies.

        Args:
            operation_list: Ordered operation values.
        """

        self._operation_list = operation_list

    def retire(self, inventory: CleanupInventory) -> None:
        """Retire the KMS cleanup target.

        Args:
            inventory: Inventory.
        """

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("kms")

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Record KMS-native accepted-retirement proof.

        Args:
            inventory: Inventory.
        """

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("verify:kms")

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Record exact non-mutating accepted-retirement readback."""

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("read:kms")
        return True


class _Verifier:
    """Record final absence verification after all cleanup phases."""

    def __init__(self, operation_list: list[str], *, absent: bool = True) -> None:
        """Initialize the verifier dependencies.

        Args:
            operation_list: Ordered operation values.
        """

        self._operation_list = operation_list
        self.absent = absent

    def validate(self, inventory: CleanupInventory) -> None:
        """Validate the verifier contract.

        Args:
            inventory: Inventory.
        """

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("verify")

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Return the configured exact aggregate absence readback."""

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("readback")
        return self.absent


def test_cleanup_absence_uses_every_service_native_owner() -> None:
    """A freshly enumerated task inventory still requires each service-native proof."""

    operation_list: list[str] = []
    inventory = _cleanup_inventory_get()
    CleanupAbsenceVerifier(
        compute=_InventoryCleanup(operation_list, "compute"),
        kms=_KmsCleanup(operation_list),
        retained=_InventoryCleanup(operation_list, "retained"),
        stack=_StackCleanup(operation_list),
        storage=_StorageCleanup(operation_list),
    ).validate(inventory)

    assert operation_list == [
        f"verify:stack:{inventory.compute_stack_name}",
        f"verify:stack:{inventory.data_stack_name}",
        "verify:compute",
        *(f"verify:bucket:{bucket_name}" for bucket_name in inventory.bucket_name_list),
        "verify:retained",
        "verify:kms",
    ]

    operation_list.clear()
    assert CleanupAbsenceVerifier(
        compute=_InventoryCleanup(operation_list, "compute"),
        kms=_KmsCleanup(operation_list),
        retained=_InventoryCleanup(operation_list, "retained"),
        stack=_StackCleanup(operation_list),
        storage=_StorageCleanup(operation_list),
    ).absent_get(inventory)
    assert operation_list == [
        f"read:stack:{inventory.compute_stack_name}",
        f"read:stack:{inventory.data_stack_name}",
        "read:compute",
        *(f"read:bucket:{bucket_name}" for bucket_name in inventory.bucket_name_list),
        "read:retained",
        "read:kms",
    ]


def test_cleanup_restart_ignores_stale_progress_and_replays_live_owners(
    tmp_path: Path,
) -> None:
    """A restart ignores old progress bytes and replays every owner from live state.

    Args:
        tmp_path: Temporary directory path.
    """

    inventory = CleanupInventory(
        account_id="463564115167",
        bucket_name_list=("bucket-a", "bucket-b", "bucket-c", "bucket-d"),
        common_prefix=COMMON_PREFIX,
        compute_stack_name=_Identity.compute_stack_name,
        data_stack_name=_Identity.data_plane_stack_name,
        environment_name=_Identity.environment_name,
        instance_id_list=("i-0123456789abcdef0",),
        kms_alias_name="alias/storage-w0123456789abcde",
        kms_key_arn_list=("arn:aws:kms:us-east-1:463564115167:key/test",),
        region="us-east-1",
        retained_volume_id_list=("vol-0123456789abcdef0",),
    )
    stale_progress_path = tmp_path / ".git" / "agent-workflows" / "external-cleanup" / f"{COMMON_PREFIX}.json"
    stale_progress_path.parent.mkdir(parents=True)
    stale_progress_path.write_text('{"schema_version":2,"phase":"foreign-partial"}\n', encoding="utf-8")
    operation_list: list[str] = []
    inventory_resolver = _InventoryResolver(inventory)
    verifier = _Verifier(operation_list, absent=False)
    manager = DevelopmentEnvironmentCleanupManager(
        account=_Account(),
        compute=_InventoryCleanup(operation_list, "compute"),
        identity=_Identity(),
        inventory_resolver=inventory_resolver,
        kms=_KmsCleanup(operation_list),
        retained=_InventoryCleanup(operation_list, "retained"),
        stack=_StackCleanup(operation_list),
        storage=_StorageCleanup(operation_list),
        verifier=verifier,
    )
    request = CleanupRequest(common_prefix=COMMON_PREFIX)

    assert manager.destroy(request) == {
        **request.payload_get(),
        "external_resources_absent": True,
    }
    assert operation_list == [
        "compute",
        f"stack:{_Identity.data_plane_stack_name}",
        "bucket:bucket-a",
        "bucket:bucket-b",
        "bucket:bucket-c",
        "bucket:bucket-d",
        "retained",
        "kms",
        "session:i-0123456789abcdef0",
        "verify",
        "verify:session:i-0123456789abcdef0",
    ]
    assert inventory_resolver.resolve_count == 7
    operation_list.clear()
    assert manager.destroy(request)["external_resources_absent"] is True
    assert operation_list == [
        "compute",
        f"stack:{_Identity.data_plane_stack_name}",
        "bucket:bucket-a",
        "bucket:bucket-b",
        "bucket:bucket-c",
        "bucket:bucket-d",
        "retained",
        "kms",
        "session:i-0123456789abcdef0",
        "verify",
        "verify:session:i-0123456789abcdef0",
    ]
    assert inventory_resolver.resolve_count == 14

    operation_list.clear()
    retained = manager.inventory(request)
    assert retained["external_resources_absent"] is False
    assert retained["common_prefix"] == COMMON_PREFIX
    assert retained["resource_identity_list"] == sorted(
        [
            inventory.compute_stack_name,
            inventory.data_stack_name,
            *inventory.instance_id_list,
            inventory.kms_alias_name,
            *inventory.kms_key_arn_list,
            *inventory.retained_volume_id_list,
            *inventory.bucket_name_list,
        ]
    )
    assert operation_list == ["readback"]
    verifier.absent = True
    assert manager.inventory(request)["external_resources_absent"] is True
    assert stale_progress_path.read_text(encoding="utf-8") == ('{"schema_version":2,"phase":"foreign-partial"}\n')


def test_cleanup_terminates_late_disconnected_session_after_instance_leaves_inventory() -> None:
    """Invocation-owned instance progress closes SSM after EC2 inventory drops the target."""

    instance_id = "i-0123456789abcdef0"
    session_id = "andrey-late-disconnected-session"

    class _LiveState:
        instance_visible = True
        late_session_visible = False
        session_target_argument_list: list[str] = []
        terminated_session_id_list: list[str] = []

    state = _LiveState()

    class _LateSessionAws:
        @staticmethod
        def json_get(argument_list: list[str]) -> dict[str, object]:
            assert argument_list[:3] == ["ssm", "describe-sessions", "--state"]
            assert argument_list[3] == "Active"
            assert argument_list[4:6] == ["--filters", f"key=Target,value={instance_id}"]
            state.session_target_argument_list.append(argument_list[5])
            if not state.late_session_visible:
                return {"Sessions": []}
            return {
                "Sessions": [
                    {
                        "SessionId": session_id,
                        "Status": "Disconnected",
                        "Target": instance_id,
                    }
                ]
            }

        @staticmethod
        def run(argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
            del check
            if argument_list[:2] == ["ec2", "describe-instances"]:
                if not state.instance_visible:
                    return subprocess.CompletedProcess(
                        argument_list,
                        255,
                        "",
                        "An error occurred (InvalidInstanceID.NotFound) when calling the DescribeInstances "
                        "operation: The instance does not exist",
                    )
                return subprocess.CompletedProcess(
                    argument_list,
                    0,
                    json.dumps(
                        {
                            "Reservations": [
                                {
                                    "Instances": [
                                        {
                                            "InstanceId": instance_id,
                                            "Placement": {"AvailabilityZone": "us-east-1a"},
                                            "State": {"Name": "running"},
                                            "Tags": _task_tag_list_get(),
                                        }
                                    ],
                                    "OwnerId": "463564115167",
                                }
                            ]
                        }
                    ),
                    "",
                )
            if tuple(argument_list[:2]) in {("ec2", "stop-instances"), ("ec2", "wait")}:
                return subprocess.CompletedProcess(argument_list, 0, "{}", "")
            if argument_list[:2] == ["ssm", "terminate-session"]:
                assert argument_list == ["ssm", "terminate-session", "--session-id", session_id]
                state.terminated_session_id_list.append(session_id)
                state.late_session_visible = False
                return subprocess.CompletedProcess(argument_list, 0, "{}", "")
            raise AssertionError(argument_list)

    class _ComputeStackCleanup:
        @staticmethod
        def delete(stack_name: str) -> None:
            assert stack_name == _Identity.compute_stack_name
            state.instance_visible = False
            state.late_session_visible = True

    base_inventory = replace(_cleanup_inventory_get(), instance_id_list=())

    class _LiveInventoryResolver:
        @staticmethod
        def resolve(request: CleanupRequest) -> CleanupInventory:
            assert request.common_prefix == COMMON_PREFIX
            return replace(base_inventory, instance_id_list=(instance_id,) if state.instance_visible else ())

    class _FinalVerifier:
        @staticmethod
        def validate(inventory: CleanupInventory) -> None:
            assert inventory.instance_id_list == ()
            assert not state.late_session_visible

        @staticmethod
        def absent_get(inventory: CleanupInventory) -> bool:
            return not inventory.instance_id_list and not state.late_session_visible

    operation_list: list[str] = []
    manager = DevelopmentEnvironmentCleanupManager(
        account=_Account(),
        compute=ComputeCleanup(aws=_LateSessionAws(), stack_cleanup=_ComputeStackCleanup()),
        identity=_Identity(),
        inventory_resolver=_LiveInventoryResolver(),
        kms=_KmsCleanup(operation_list),
        retained=_InventoryCleanup(operation_list, "retained"),
        stack=_StackCleanup(operation_list),
        storage=_StorageCleanup(operation_list),
        verifier=_FinalVerifier(),
    )

    assert manager.destroy(CleanupRequest(common_prefix=COMMON_PREFIX))["external_resources_absent"] is True
    assert state.terminated_session_id_list == [session_id]
    assert state.session_target_argument_list == [
        f"key=Target,value={instance_id}",
        f"key=Target,value={instance_id}",
        f"key=Target,value={instance_id}",
    ]


def test_cleanup_restart_deletes_resource_that_became_visible_after_its_first_owner() -> None:
    """Final fresh enumeration blocks success and the next restart replays the owner."""
    base_inventory = replace(
        _cleanup_inventory_get(),
        instance_id_list=(),
        kms_key_arn_list=(),
        retained_volume_id_list=(),
    )

    class _LiveState:
        visible = False
        resolve_count = 0
        deleted_instance_id_list: list[str] = []

    state = _LiveState()

    class _LateInventoryResolver:
        @staticmethod
        def resolve(request: CleanupRequest) -> CleanupInventory:
            assert request.common_prefix == COMMON_PREFIX
            state.resolve_count += 1
            if state.resolve_count == 2:
                state.visible = True
            return replace(
                base_inventory,
                instance_id_list=("i-0123456789abcdef0",) if state.visible else (),
            )

    class _LateCompute:
        @staticmethod
        def delete(inventory: CleanupInventory) -> None:
            if inventory.instance_id_list:
                state.deleted_instance_id_list.extend(inventory.instance_id_list)
                state.visible = False

        @staticmethod
        def session_absence_validate(inventory: CleanupInventory, instance_id_list: list[str]) -> None:
            assert inventory.common_prefix == COMMON_PREFIX
            assert instance_id_list == sorted(instance_id_list)

        @staticmethod
        def session_list_terminate(inventory: CleanupInventory, instance_id_list: list[str]) -> None:
            assert inventory.common_prefix == COMMON_PREFIX
            assert instance_id_list == sorted(instance_id_list)

    class _FreshVerifier:
        @staticmethod
        def validate(inventory: CleanupInventory) -> None:
            if inventory.instance_id_list:
                raise DevelopmentEnvironmentError("Fresh live task resource remains")

        @staticmethod
        def absent_get(inventory: CleanupInventory) -> bool:
            return not inventory.instance_id_list

    operation_list: list[str] = []
    manager = DevelopmentEnvironmentCleanupManager(
        account=_Account(),
        compute=_LateCompute(),
        identity=_Identity(),
        inventory_resolver=_LateInventoryResolver(),
        kms=_KmsCleanup(operation_list),
        retained=_InventoryCleanup(operation_list, "retained"),
        stack=_StackCleanup(operation_list),
        storage=_StorageCleanup(operation_list),
        verifier=_FreshVerifier(),
    )
    request = CleanupRequest(common_prefix=COMMON_PREFIX)

    with pytest.raises(DevelopmentEnvironmentError, match="Fresh live task resource remains"):
        manager.destroy(request)

    assert state.deleted_instance_id_list == []

    assert manager.destroy(request)["external_resources_absent"] is True
    assert state.deleted_instance_id_list == ["i-0123456789abcdef0"]
    assert state.resolve_count == 14


def test_cleanup_inventory_rejects_primary_environment_before_discovery() -> None:
    """Typed cleanup inventory is available only for one isolated task identity."""

    manager = DevelopmentEnvironmentCleanupManager(
        account=_Account(),
        compute=_Unused(),
        identity=_PrimaryIdentity(),
        inventory_resolver=_Unused(),
        kms=_Unused(),
        retained=_Unused(),
        stack=_Unused(),
        storage=_Unused(),
        verifier=_Unused(),
    )

    with pytest.raises(DevelopmentEnvironmentError, match="cannot target the primary"):
        manager.inventory(CleanupRequest(common_prefix=COMMON_PREFIX))
