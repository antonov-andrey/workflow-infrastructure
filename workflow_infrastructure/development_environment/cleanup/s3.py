"""Versioned S3 bucket purge owned by task-environment cleanup."""

from __future__ import annotations

import json
from typing import Protocol, Sequence
import subprocess

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class AwsClientProtocol(Protocol):
    def json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI command and decode its object response."""

    def run(
        self, aws_argument_list: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run one AWS CLI command."""


class VersionedBucketCleaner:
    """Delete all versions, delete markers, uploads, and one exact bucket."""

    def __init__(self, aws: AwsClientProtocol) -> None:
        self._aws = aws

    def delete(self, bucket_name: str) -> None:
        """Idempotently delete every retained object identity and the bucket."""

        if not self._exists(bucket_name):
            return
        self._multipart_upload_list_delete(bucket_name)
        self._object_version_list_delete(bucket_name)
        result = self._aws.run(
            ["s3api", "delete-bucket", "--bucket", bucket_name], check=False
        )
        if result.returncode != 0 and not _is_absent_error(result):
            raise DevelopmentEnvironmentError(
                f"Task bucket {bucket_name} could not be deleted"
            )
        if self._exists(bucket_name):
            raise DevelopmentEnvironmentError(
                f"Task bucket {bucket_name} still exists after deletion"
            )

    def _exists(self, bucket_name: str) -> bool:
        result = self._aws.run(
            ["s3api", "head-bucket", "--bucket", bucket_name], check=False
        )
        if result.returncode == 0:
            return True
        if _is_absent_error(result):
            return False
        raise DevelopmentEnvironmentError(
            f"Task bucket {bucket_name} ownership cannot be observed"
        )

    def _multipart_upload_list_delete(self, bucket_name: str) -> None:
        while True:
            payload = self._aws.json_get(
                [
                    "s3api",
                    "list-multipart-uploads",
                    "--bucket",
                    bucket_name,
                    "--max-uploads",
                    "1000",
                ]
            )
            upload_list = payload.get("Uploads", [])
            if not isinstance(upload_list, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("Key"), str)
                or not isinstance(item.get("UploadId"), str)
                for item in upload_list
            ):
                raise DevelopmentEnvironmentError(
                    f"Task bucket {bucket_name} multipart inventory is malformed"
                )
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
                    ]
                )

    def _object_version_list_delete(self, bucket_name: str) -> None:
        while True:
            payload = self._aws.json_get(
                [
                    "s3api",
                    "list-object-versions",
                    "--bucket",
                    bucket_name,
                    "--max-keys",
                    "1000",
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
                    raise DevelopmentEnvironmentError(
                        f"Task bucket {bucket_name} version inventory is malformed"
                    )
                object_list.extend(
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for item in item_list
                )
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
                    ],
                    check=False,
                )
                if result.returncode != 0:
                    raise DevelopmentEnvironmentError(
                        f"Task bucket {bucket_name} version batch could not be deleted"
                    )


def _is_absent_error(result: subprocess.CompletedProcess[str]) -> bool:
    diagnostic = f"{result.stdout}\n{result.stderr}"
    return any(
        marker in diagnostic
        for marker in ("NoSuchBucket", "Not Found", "404", "does not exist")
    )
