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
from workflow_infrastructure.development_environment.cleanup.inventory import (
    CleanupInventoryResolver,
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
        """Initialize the S3 AWS dependencies.

        Args:
            delete_error: Delete error.
        """

        self.bucket_exists = True
        self.delete_error = delete_error
        self.upload_by_id = {"upload-1": "partial/file"}
        self.object_set = {("data/file", "v1"), ("data/file", "delete-marker")}

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return the scripted cleanup-inventory AWS response.

        Args:
            argument_list: Exact command arguments.

        Returns:
            Decoded JSON object.
        """

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
    """AWS's post-deletion EC2 tombstone does not block idempotent cleanup."""

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

    def __init__(self, *, alias_target_key_id: str | None, key_state: str) -> None:
        """Initialize the KMS AWS dependencies.

        Args:
            alias_target_key_id: Exact alias target key identity.
            key_state: Key state.
        """

        self.alias_target_key_id = alias_target_key_id
        self.key_state = key_state

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        """Return the scripted KMS cleanup AWS response.

        Args:
            argument_list: Exact command arguments.

        Returns:
            Decoded JSON object.
        """

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
                            "KeyState": self.key_state,
                        }
                    }
                ),
                "",
            )
        raise AssertionError(argument_list)


def _cleanup_inventory_get() -> CleanupInventory:
    """Build one complete task-environment cleanup inventory fixture.

    Returns:
        The cleanup inventory.
    """

    return CleanupInventory(
        bucket_name_list=("bucket-a", "bucket-b", "bucket-c", "bucket-d"),
        common_prefix=COMMON_PREFIX,
        compute_stack_name=_Identity.compute_stack_name,
        data_stack_name=_Identity.data_plane_stack_name,
        environment_name=_Identity.environment_name,
        instance_id_list=("i-0123456789abcdef0",),
        kms_alias_name="alias/storage-w0123456789abcde",
        kms_key_arn_list=("arn:aws:kms:us-east-1:463564115167:key/test",),
        operation_identity=OPERATION_IDENTITY,
        retained_volume_id_list=("vol-0123456789abcdef0",),
    )


def test_cleanup_inventory_rejects_mutation_identities_outside_its_closed_shape() -> None:
    """A corrupted durable journal cannot broaden the AWS mutation scope."""

    payload = _cleanup_inventory_get().payload_get()
    payload["instance_id_list"] = ["primary-instance"]

    with pytest.raises(DevelopmentEnvironmentError, match="inventory is malformed"):
        CleanupInventory.from_payload(payload)


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
    """Accept the fixed test operator context without external AWS access."""

    def local_operator_context_validate(self) -> None:
        """Accept the fixed test account and region without external I/O."""

        return None


class _Binding:
    """Expose the prepared Git common directory for cleanup state."""

    def __init__(self, common_directory: Path) -> None:
        """Initialize the binding dependencies.

        Args:
            common_directory: Common directory.
        """

        self.common_directory = common_directory

    def common_directory_get(self) -> Path:
        """Expose the prepared shared Git directory for cleanup binding storage.

        Returns:
            Prepared shared Git directory.
        """

        return self.common_directory


class _Identity:
    """Declare every exact identity expected in the cleanup inventory."""

    compute_stack_name = "compute-w0123456789abcde"
    data_plane_stack_name = "data-w0123456789abcde"
    environment_name = "w0123456789abcde"
    git_worktree = COMMON_PREFIX
    is_primary = False


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
        instance_id_list: tuple[str, ...] = (),
        kms_key_arn_list: tuple[str, ...] = (),
        retained_volume_id_list: tuple[str, ...] = (),
    ) -> None:
        """Initialize the fake task resource inventory.

        Args:
            alias_key_arn: Key targeted by the deterministic alias.
            instance_id_list: Remaining instance identities.
            kms_key_arn_list: Remaining tagged KMS identities.
            retained_volume_id_list: Remaining retained-volume identities.
        """

        self.alias_key_arn = alias_key_arn
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
        if argument_list[:2] == ["resourcegroupstaggingapi", "get-resources"]:
            return {
                "ResourceTagMappingList": [
                    {"ResourceARN": key_arn, "Tags": _task_tag_list_get()} for key_arn in self.kms_key_arn_list
                ]
            }
        if argument_list[:2] == ["kms", "list-resource-tags"] and self.alias_key_arn:
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
        assert argument_list[:2] == ["kms", "describe-key"]
        if not self.alias_key_arn:
            return subprocess.CompletedProcess(
                argument_list,
                1,
                "",
                "An error occurred (NotFoundException) when calling the DescribeKey operation: Key not found",
            )
        return subprocess.CompletedProcess(
            argument_list,
            0,
            json.dumps({"KeyMetadata": {"Arn": self.alias_key_arn}}),
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
    ).resolve(CleanupRequest(common_prefix=COMMON_PREFIX, operation_identity=OPERATION_IDENTITY))

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
    inventory = CleanupInventoryResolver(
        account_id="463564115167",
        aws=_CleanupInventoryAws(
            alias_key_arn=stack_key_arn,
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
    ).resolve(CleanupRequest(common_prefix=COMMON_PREFIX, operation_identity=OPERATION_IDENTITY))

    assert inventory.instance_id_list == (stack_instance_id, tagged_instance_id)
    assert inventory.kms_key_arn_list == (stack_key_arn, tagged_key_arn)
    assert inventory.retained_volume_id_list == (stack_volume_id, tagged_volume_id)


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

    def resolve(self, request: CleanupRequest) -> CleanupInventory:
        """Resolve the inventory resolver result.

        Args:
            request: Validated operation request.

        Returns:
            The inventory resolver result.
        """

        assert request.common_prefix == self._inventory.common_prefix
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


class _StorageCleanup:
    """Record each task bucket deleted by the storage phase."""

    def __init__(self, operation_list: list[str]) -> None:
        """Initialize the storage cleanup dependencies.

        Args:
            operation_list: Ordered operation values.
        """

        self._operation_list = operation_list

    def delete(self, bucket_name: str) -> None:
        """Delete the storage cleanup target.

        Args:
            bucket_name: Bucket name.
        """

        self._operation_list.append(f"bucket:{bucket_name}")


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


class _Verifier:
    """Record final absence verification after all cleanup phases."""

    def __init__(self, operation_list: list[str]) -> None:
        """Initialize the verifier dependencies.

        Args:
            operation_list: Ordered operation values.
        """

        self._operation_list = operation_list

    def validate(self, inventory: CleanupInventory) -> None:
        """Validate the verifier contract.

        Args:
            inventory: Inventory.
        """

        assert inventory.common_prefix == COMMON_PREFIX
        self._operation_list.append("verify")


def test_cleanup_journal_resumes_each_phase_and_binds_operation(
    tmp_path: Path,
) -> None:
    """A repeated hook resumes the same journal and never repeats completed phases.

    Args:
        tmp_path: Temporary directory path.
    """

    inventory = CleanupInventory(
        bucket_name_list=("bucket-a", "bucket-b", "bucket-c", "bucket-d"),
        common_prefix=COMMON_PREFIX,
        compute_stack_name=_Identity.compute_stack_name,
        data_stack_name=_Identity.data_plane_stack_name,
        environment_name=_Identity.environment_name,
        instance_id_list=("i-0123456789abcdef0",),
        kms_alias_name="alias/storage-w0123456789abcde",
        kms_key_arn_list=("arn:aws:kms:us-east-1:463564115167:key/test",),
        operation_identity=OPERATION_IDENTITY,
        retained_volume_id_list=("vol-0123456789abcdef0",),
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
