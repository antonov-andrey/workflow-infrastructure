"""Versioned S3 bucket purge owned by task-environment cleanup."""

from __future__ import annotations

import json

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
from workflow_infrastructure.development_environment.cleanup.aws_response import (
    tag_map_get,
    task_ownership_tag_validate,
)
from workflow_infrastructure.development_environment.cleanup.model import (
    CleanupInventory,
)
from workflow_infrastructure.development_environment.cleanup.protocol import (
    AwsClientProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class VersionedBucketCleaner:
    """Delete all versions, delete markers, uploads, and one exact bucket."""

    def __init__(self, aws: AwsClientProtocol) -> None:
        """Initialize the versioned bucket cleaner dependencies.

        Args:
            aws: Aws.
        """

        self._aws = aws

    def delete(self, inventory: CleanupInventory, bucket_name: str) -> None:
        """Idempotently delete every retained object identity and the bucket.

        Args:
            inventory: Fresh task identity and account/region fence.
            bucket_name: Bucket name.
        """

        self._bucket_name_validate(inventory, bucket_name)
        if not self._owned_exists(inventory, bucket_name):
            return
        self._multipart_upload_list_delete(inventory, bucket_name)
        self._object_version_list_delete(inventory, bucket_name)
        self._ownership_require(inventory, bucket_name)
        result = self._aws.run(
            [
                "s3api",
                "delete-bucket",
                "--bucket",
                bucket_name,
                "--expected-bucket-owner",
                inventory.account_id,
            ],
            check=False,
        )
        if result.returncode != 0 and not aws_cli_error_matches(
            result,
            code_set=frozenset({"NoSuchBucket"}),
            operation="DeleteBucket",
        ):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} could not be deleted")
        if self._owned_exists(inventory, bucket_name):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} still exists after deletion")

    def absence_validate(self, inventory: CleanupInventory, bucket_name: str) -> None:
        """Require one exact bucket to be absent.

        Args:
            inventory: Fresh task identity and account/region fence.
            bucket_name: Bucket name.
        """

        if not self.absent_get(inventory, bucket_name):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} absence is not proven")

    def absent_get(self, inventory: CleanupInventory, bucket_name: str) -> bool:
        """Return exact current absence for one versioned bucket."""

        self._bucket_name_validate(inventory, bucket_name)
        return not self._owned_exists(inventory, bucket_name)

    def _owned_exists(self, inventory: CleanupInventory, bucket_name: str) -> bool:
        """Report current existence only after exact owner, region, and tag proof.

        Args:
            bucket_name: Bucket name.
            inventory: Fresh task identity and account/region fence.

        Returns:
            Whether the exact bucket still exists.
        """

        result = self._aws.run(
            [
                "s3api",
                "head-bucket",
                "--bucket",
                bucket_name,
                "--expected-bucket-owner",
                inventory.account_id,
            ],
            check=False,
        )
        if result.returncode == 0:
            location_payload = self._aws.json_get(
                [
                    "s3api",
                    "get-bucket-location",
                    "--bucket",
                    bucket_name,
                    "--expected-bucket-owner",
                    inventory.account_id,
                ]
            )
            location = location_payload.get("LocationConstraint")
            current_region = "us-east-1" if location is None else "eu-west-1" if location == "EU" else location
            if current_region != inventory.region:
                raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} belongs to another region")
            tag_payload = self._aws.json_get(
                [
                    "s3api",
                    "get-bucket-tagging",
                    "--bucket",
                    bucket_name,
                    "--expected-bucket-owner",
                    inventory.account_id,
                ]
            )
            task_ownership_tag_validate(
                tag_map_get(tag_payload.get("TagSet")),
                common_prefix=inventory.common_prefix,
                environment_name=inventory.environment_name,
                label=f"bucket {bucket_name}",
            )
            return True
        if aws_cli_error_matches(
            result,
            code_set=frozenset({"404", "NoSuchBucket"}),
            operation="HeadBucket",
        ):
            return False
        raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} ownership cannot be observed")

    def _multipart_upload_list_delete(self, inventory: CleanupInventory, bucket_name: str) -> None:
        """Abort every incomplete multipart upload in one task bucket.

        Args:
            bucket_name: Bucket name.
            inventory: Fresh task identity and account/region fence.
        """

        while True:
            payload = self._aws.json_get(
                [
                    "s3api",
                    "list-multipart-uploads",
                    "--bucket",
                    bucket_name,
                    "--max-uploads",
                    "1000",
                    "--expected-bucket-owner",
                    inventory.account_id,
                ]
            )
            upload_list = payload.get("Uploads", [])
            if not isinstance(upload_list, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("Key"), str)
                or not isinstance(item.get("UploadId"), str)
                for item in upload_list
            ):
                raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} multipart inventory is malformed")
            if not upload_list:
                return
            for item in upload_list:
                self._ownership_require(inventory, bucket_name)
                self._aws.run(
                    [
                        "s3api",
                        "abort-multipart-upload",
                        "--bucket",
                        bucket_name,
                        "--key",
                        item["Key"],
                        "--upload-id",
                        item["UploadId"],
                        "--expected-bucket-owner",
                        inventory.account_id,
                    ]
                )

    def _object_version_list_delete(self, inventory: CleanupInventory, bucket_name: str) -> None:
        """Remove every object version and delete marker from one task bucket.

        Args:
            bucket_name: Bucket name.
            inventory: Fresh task identity and account/region fence.
        """

        while True:
            payload = self._aws.json_get(
                [
                    "s3api",
                    "list-object-versions",
                    "--bucket",
                    bucket_name,
                    "--max-keys",
                    "1000",
                    "--expected-bucket-owner",
                    inventory.account_id,
                ]
            )
            object_list: list[dict[str, str]] = []
            for field_name in ("Versions", "DeleteMarkers"):
                item_list = payload.get(field_name, [])
                if not isinstance(item_list, list) or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("Key"), str)
                    or not isinstance(item.get("VersionId"), str)
                    for item in item_list
                ):
                    raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} version inventory is malformed")
                object_list.extend({"Key": item["Key"], "VersionId": item["VersionId"]} for item in item_list)
            if not object_list:
                return
            for offset in range(0, len(object_list), 100):
                self._ownership_require(inventory, bucket_name)
                delete_payload = json.dumps(
                    {"Objects": object_list[offset : offset + 100], "Quiet": True},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                result = self._aws.run(
                    [
                        "s3api",
                        "delete-objects",
                        "--bucket",
                        bucket_name,
                        "--delete",
                        delete_payload,
                        "--expected-bucket-owner",
                        inventory.account_id,
                    ],
                    check=False,
                )
                if result.returncode != 0:
                    raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} version batch could not be deleted")
                try:
                    response = json.loads(result.stdout or "{}")
                except json.JSONDecodeError as error:
                    raise DevelopmentEnvironmentError(
                        f"Task bucket {bucket_name} deletion response is malformed"
                    ) from error
                if not isinstance(response, dict) or response.get("Errors"):
                    raise DevelopmentEnvironmentError(
                        f"Task bucket {bucket_name} version batch was only partially deleted"
                    )

    @staticmethod
    def _bucket_name_validate(inventory: CleanupInventory, bucket_name: str) -> None:
        """Require one deterministic bucket identity from the fresh inventory."""

        if bucket_name not in inventory.bucket_name_list:
            raise DevelopmentEnvironmentError("Task bucket name is outside the cleanup inventory")

    def _ownership_require(self, inventory: CleanupInventory, bucket_name: str) -> None:
        """Re-attest exact current bucket ownership immediately before mutation."""

        if not self._owned_exists(inventory, bucket_name):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} disappeared before mutation")
