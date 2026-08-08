"""Versioned S3 bucket purge owned by task-environment cleanup."""

from __future__ import annotations

import json

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
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

    def delete(self, bucket_name: str, *, expected_owner: str) -> None:
        """Idempotently delete every retained object identity and the bucket.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
        """

        if not self._exists(bucket_name, expected_owner=expected_owner):
            return
        self._multipart_upload_list_delete(bucket_name, expected_owner=expected_owner)
        self._object_version_list_delete(bucket_name, expected_owner=expected_owner)
        result = self._aws.run(
            [
                "s3api",
                "delete-bucket",
                "--bucket",
                bucket_name,
                "--expected-bucket-owner",
                expected_owner,
            ],
            check=False,
        )
        if result.returncode != 0 and not aws_cli_error_matches(
            result,
            code_set=frozenset({"NoSuchBucket"}),
            operation="DeleteBucket",
        ):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} could not be deleted")
        if self._exists(bucket_name, expected_owner=expected_owner):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} still exists after deletion")

    def absence_validate(self, bucket_name: str, *, expected_owner: str) -> None:
        """Require one exact bucket to be absent.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
        """

        if not self.absent_get(bucket_name, expected_owner=expected_owner):
            raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} absence is not proven")

    def absent_get(self, bucket_name: str, *, expected_owner: str) -> bool:
        """Return exact current absence for one versioned bucket."""

        return not self._exists(bucket_name, expected_owner=expected_owner)

    def _exists(self, bucket_name: str, *, expected_owner: str) -> bool:
        """Report whether the exact versioned cleanup bucket still exists.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.

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
                expected_owner,
            ],
            check=False,
        )
        if result.returncode == 0:
            return True
        if aws_cli_error_matches(
            result,
            code_set=frozenset({"404", "NoSuchBucket"}),
            operation="HeadBucket",
        ):
            return False
        raise DevelopmentEnvironmentError(f"Task bucket {bucket_name} ownership cannot be observed")

    def _multipart_upload_list_delete(self, bucket_name: str, *, expected_owner: str) -> None:
        """Abort every incomplete multipart upload in one task bucket.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
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
                    expected_owner,
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
                        expected_owner,
                    ]
                )

    def _object_version_list_delete(self, bucket_name: str, *, expected_owner: str) -> None:
        """Remove every object version and delete marker from one task bucket.

        Args:
            bucket_name: Bucket name.
            expected_owner: Exact AWS account that owns the bucket.
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
                    expected_owner,
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
                        expected_owner,
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
