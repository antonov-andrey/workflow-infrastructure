"""Verify exact, resumable task development-environment cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from workflow_infrastructure.development_environment.cleanup.manager import (
    DevelopmentEnvironmentCleanupManager,
)
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

    def __init__(self) -> None:
        self.bucket_exists = True
        self.upload_by_id = {"upload-1": "partial/file"}
        self.object_set = {("data/file", "v1"), ("data/file", "delete-marker")}

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        if argument_list[:2] == ["s3api", "list-multipart-uploads"]:
            return {
                "Uploads": [
                    {"Key": key, "UploadId": upload_id}
                    for upload_id, key in self.upload_by_id.items()
                ]
            }
        if argument_list[:2] == ["s3api", "list-object-versions"]:
            return {
                "Versions": [
                    {"Key": key, "VersionId": version}
                    for key, version in self.object_set
                    if version != "delete-marker"
                ],
                "DeleteMarkers": [
                    {"Key": key, "VersionId": version}
                    for key, version in self.object_set
                    if version == "delete-marker"
                ],
            }
        raise AssertionError(argument_list)

    def run(
        self, argument_list: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del check
        operation = tuple(argument_list[:2])
        if operation == ("s3api", "head-bucket"):
            return subprocess.CompletedProcess(
                argument_list,
                0 if self.bucket_exists else 1,
                "",
                "" if self.bucket_exists else "NoSuchBucket",
            )
        if operation == ("s3api", "abort-multipart-upload"):
            upload_id = argument_list[argument_list.index("--upload-id") + 1]
            self.upload_by_id.pop(upload_id)
        elif operation == ("s3api", "delete-objects"):
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


def test_cleanup_journal_resumes_each_phase_and_binds_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated hook resumes the same journal and never repeats completed phases."""

    manager = DevelopmentEnvironmentCleanupManager(
        account=_Account(),
        account_id="463564115167",
        aws=_Unused(),
        binding=_Binding(tmp_path),
        identity=_Identity(),
        region="us-east-1",
        stack=_Unused(),
    )
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
    monkeypatch.setattr(manager, "_inventory_get", lambda request: inventory)
    operation_list: list[str] = []
    monkeypatch.setattr(
        manager,
        "_compute_delete",
        lambda value: operation_list.append("compute"),
    )
    monkeypatch.setattr(
        manager._bucket_cleaner,
        "delete",
        lambda bucket: operation_list.append(f"bucket:{bucket}"),
    )
    monkeypatch.setattr(
        manager,
        "_stack_delete",
        lambda stack: operation_list.append(f"stack:{stack}"),
    )
    monkeypatch.setattr(
        manager,
        "_retained_delete",
        lambda value: operation_list.append("retained"),
    )
    monkeypatch.setattr(
        manager,
        "_kms_retire",
        lambda value: operation_list.append("kms"),
    )
    monkeypatch.setattr(
        manager,
        "_absence_validate",
        lambda value: operation_list.append("verify"),
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
        "bucket:bucket-a",
        "bucket:bucket-b",
        "bucket:bucket-c",
        "bucket:bucket-d",
        f"stack:{_Identity.data_plane_stack_name}",
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
