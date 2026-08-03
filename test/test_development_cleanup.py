"""Verify exact, resumable task development-environment cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

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
from workflow_infrastructure.development_environment.cleanup.journal import (
    CleanupJournalStore,
)
from workflow_infrastructure.development_environment.cleanup.kms import KmsCleanup
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
    CleanupRequest,
)
from workflow_infrastructure.development_environment.cleanup.s3 import (
    VersionedBucketCleaner,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

COMMON_PREFIX = "2026-08-01-workflow-platform-hardening"
OPERATION_IDENTITY = "1" * 32


def test_cleanup_request_requires_exact_closed_stdin_identity() -> None:
    """Manual, extended, or cross-task cleanup input is rejected before AWS use."""

    request = CleanupRequest.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "common_prefix": COMMON_PREFIX,
                "operation_identity": OPERATION_IDENTITY,
            }
        ),
        expected_common_prefix=COMMON_PREFIX,
    )
    assert request.payload_get() == {
        "schema_version": 1,
        "common_prefix": COMMON_PREFIX,
        "operation_identity": OPERATION_IDENTITY,
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
    ):
        with pytest.raises(DevelopmentEnvironmentError):
            CleanupRequest.from_json(payload, expected_common_prefix=COMMON_PREFIX)


class _S3Aws:
    """Stateful AWS boundary for one versioned bucket cleanup."""

    def __init__(self, *, delete_error: bool = False) -> None:
        self.bucket_exists = True
        self.delete_error = delete_error
        self.upload_by_id = {"upload-1": "partial/file"}
        self.object_set = {("data/file", "v1"), ("data/file", "delete-marker")}

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
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
        del check
        operation = tuple(argument_list[:2])
        if operation == ("s3api", "head-bucket"):
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
        if operation == ("s3api", "abort-multipart-upload"):
            upload_id = argument_list[argument_list.index("--upload-id") + 1]
            self.upload_by_id.pop(upload_id)
        elif operation == ("s3api", "delete-objects"):
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
    cleaner.delete("task-bucket")
    cleaner.delete("task-bucket")
    assert not aws.bucket_exists


def test_versioned_bucket_cleanup_rejects_partial_http_200_deletion() -> None:
    """S3 per-object errors remain failures even when the request returned HTTP 200."""

    with pytest.raises(DevelopmentEnvironmentError, match="partially deleted"):
        VersionedBucketCleaner(_S3Aws(delete_error=True)).delete("task-bucket")


class _ComputeAws:
    """Return one EC2 tombstone and no active Session Manager sessions."""

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        assert argument_list[:3] == ["ssm", "describe-sessions", "--state"]
        return {"Sessions": []}

    def run(self, argument_list: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
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
                                    "State": {"Name": "terminated"},
                                }
                            ]
                        }
                    ]
                }
            ),
            "",
        )


def test_compute_cleanup_accepts_the_ec2_terminated_visibility_tombstone() -> None:
    """AWS's post-deletion EC2 tombstone does not block synchronized cleanup."""

    cleanup = ComputeCleanup(aws=_ComputeAws(), stack_cleanup=_Unused())
    cleanup.absence_validate(_cleanup_inventory_get())


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

    def __init__(self, *, alias_target_key_id: str | None, key_state: str) -> None:
        self.alias_target_key_id = alias_target_key_id
        self.key_state = key_state

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
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
                            "KeyState": self.key_state,
                        }
                    }
                ),
                "",
            )
        raise AssertionError(argument_list)


def _cleanup_inventory_get() -> CleanupInventory:
    return CleanupInventory(
        bucket_name_list=("bucket-a", "bucket-b", "bucket-c", "bucket-d"),
        common_prefix=COMMON_PREFIX,
        compute_stack_name=_Identity.compute_stack_name,
        data_stack_name=_Identity.data_plane_stack_name,
        environment_name=_Identity.environment_name,
        instance_id="i-0123456789abcdef0",
        kms_alias_name="alias/storage-w0123456789abcde",
        kms_key_arn="arn:aws:kms:us-east-1:463564115167:key/test",
        operation_identity=OPERATION_IDENTITY,
        retained_volume_id="vol-0123456789abcdef0",
    )


def test_kms_cleanup_accepts_physical_absence_after_pending_deletion() -> None:
    """A cleanup resume after AWS finishes deletion remains idempotent."""

    cleanup = KmsCleanup(_KmsAws(alias_target_key_id=None, key_state="absent"))
    inventory = _cleanup_inventory_get()
    cleanup.retire(inventory)
    cleanup.absence_validate(inventory)


def test_kms_cleanup_rejects_expected_alias_retargeting() -> None:
    """The task alias cannot be deleted after somebody retargets it to another key."""

    cleanup = KmsCleanup(_KmsAws(alias_target_key_id="another-key", key_state="Enabled"))
    with pytest.raises(DevelopmentEnvironmentError, match="ownership is ambiguous"):
        cleanup.retire(_cleanup_inventory_get())


class _Account:
    def local_operator_context_validate(self) -> None:
        return None


class _Binding:
    def __init__(self, common_directory: Path) -> None:
        self.common_directory = common_directory

    def common_directory_get(self) -> Path:
        return self.common_directory


class _Identity:
    compute_stack_name = "compute-w0123456789abcde"
    data_plane_stack_name = "data-w0123456789abcde"
    environment_name = "w0123456789abcde"
    git_worktree = COMMON_PREFIX
    is_primary = False


class _Unused:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(name)


class _InventoryResolver:
    def __init__(self, inventory: CleanupInventory) -> None:
        self._inventory = inventory

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        assert request.common_prefix == self._inventory.common_prefix
        return self._inventory


class _InventoryCleanup:
    def __init__(self, operation_list: list[str], label: str) -> None:
        self._operation_list = operation_list
        self._label = label

    def delete(self, inventory: CleanupInventory) -> None:
        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append(self._label)


class _StorageCleanup:
    def __init__(self, operation_list: list[str]) -> None:
        self._operation_list = operation_list

    def delete(self, bucket_name: str) -> None:
        self._operation_list.append(f"bucket:{bucket_name}")


class _StackCleanup:
    def __init__(self, operation_list: list[str]) -> None:
        self._operation_list = operation_list

    def delete(self, stack_name: str) -> None:
        self._operation_list.append(f"stack:{stack_name}")


class _KmsCleanup:
    def __init__(self, operation_list: list[str]) -> None:
        self._operation_list = operation_list

    def retire(self, inventory: CleanupInventory) -> None:
        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("kms")


class _Verifier:
    def __init__(self, operation_list: list[str]) -> None:
        self._operation_list = operation_list

    def validate(self, inventory: CleanupInventory) -> None:
        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("verify")


def test_cleanup_journal_resumes_each_phase_and_binds_operation(
    tmp_path: Path,
) -> None:
    """A repeated hook resumes the same journal and never repeats completed phases."""

    inventory = CleanupInventory(
        bucket_name_list=("bucket-a", "bucket-b", "bucket-c", "bucket-d"),
        common_prefix=COMMON_PREFIX,
        compute_stack_name=_Identity.compute_stack_name,
        data_stack_name=_Identity.data_plane_stack_name,
        environment_name=_Identity.environment_name,
        instance_id="i-0123456789abcdef0",
        kms_alias_name="alias/storage-w0123456789abcde",
        kms_key_arn="arn:aws:kms:us-east-1:463564115167:key/test",
        operation_identity=OPERATION_IDENTITY,
        retained_volume_id="vol-0123456789abcdef0",
    )
    operation_list: list[str] = []
    inventory_resolver = _InventoryResolver(inventory)
    manager = DevelopmentEnvironmentCleanupManager(
        account=_Account(),
        compute=_InventoryCleanup(operation_list, "compute"),
        identity=_Identity(),
        inventory_resolver=inventory_resolver,
        journal=CleanupJournalStore(
            binding=_Binding(tmp_path),
            inventory_resolver=inventory_resolver,
        ),
        kms=_KmsCleanup(operation_list),
        retained=_InventoryCleanup(operation_list, "retained"),
        stack=_StackCleanup(operation_list),
        storage=_StorageCleanup(operation_list),
        verifier=_Verifier(operation_list),
    )
    request = CleanupRequest(
        common_prefix=COMMON_PREFIX,
        operation_identity=OPERATION_IDENTITY,
    )

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
        "verify",
        "verify",
    ]
    operation_list.clear()
    assert manager.destroy(request)["external_resources_absent"] is True
    assert operation_list == ["verify"]

    with pytest.raises(
        DevelopmentEnvironmentError,
        match="another operation",
    ):
        manager.destroy(
            CleanupRequest(
                common_prefix=COMMON_PREFIX,
                operation_identity="2" * 32,
            )
        )
