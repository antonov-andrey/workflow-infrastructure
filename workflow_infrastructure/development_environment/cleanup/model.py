"""Closed request and durable inventory models for task cleanup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Self

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

_COMMON_PREFIX_PATTERN = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]*")
_EC2_INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]{8,17}")
_EBS_VOLUME_ID_PATTERN = re.compile(r"vol-[0-9a-f]{8,17}")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]{0,15}")
_KMS_KEY_ARN_PATTERN = re.compile(r"arn:aws(?:-[a-z]+)?:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9/_-]+")


@dataclass(frozen=True, slots=True)
class CleanupRequest:
    """One exact request issued by the registered cleanup provider."""

    common_prefix: str
    schema_version: int = 1

    @classmethod
    def from_json(cls, text: str, *, expected_common_prefix: str) -> Self:
        """Decode one closed request and bind it to the CLI selector.

        Args:
            text: Text.
            expected_common_prefix: Expected common prefix.

        Returns:
            Validated current object.
        """

        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError("Task cleanup requires one exact JSON request on stdin") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "common_prefix"}
            or payload.get("schema_version") != 1
            or isinstance(payload.get("schema_version"), bool)
            or payload.get("common_prefix") != expected_common_prefix
            or _COMMON_PREFIX_PATTERN.fullmatch(expected_common_prefix) is None
        ):
            raise DevelopmentEnvironmentError("Task cleanup request is malformed or has another task identity")
        return cls(common_prefix=expected_common_prefix)

    def payload_get(self) -> dict[str, object]:
        """Return the canonical protocol payload.

        Returns:
            The canonical protocol payload.
        """

        return asdict(self)


@dataclass(frozen=True, slots=True)
class CleanupInventory:
    """Exact task resource identities retained across cleanup interruption."""

    bucket_name_list: tuple[str, ...]
    common_prefix: str
    compute_stack_name: str
    data_stack_name: str
    environment_name: str
    instance_id_list: tuple[str, ...]
    kms_alias_name: str
    kms_key_arn_list: tuple[str, ...]
    retained_volume_id_list: tuple[str, ...]
    schema_version: int = 3

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        """Decode one exact durable cleanup inventory.

        Args:
            payload: Structured operation payload.

        Returns:
            Validated current object.
        """

        field_name_set = {
            "bucket_name_list",
            "common_prefix",
            "compute_stack_name",
            "data_stack_name",
            "environment_name",
            "instance_id_list",
            "kms_alias_name",
            "kms_key_arn_list",
            "retained_volume_id_list",
            "schema_version",
        }
        if not isinstance(payload, dict) or set(payload) != field_name_set:
            raise DevelopmentEnvironmentError("Task cleanup inventory has another shape")
        list_field_name_set = {
            "bucket_name_list",
            "instance_id_list",
            "kms_key_arn_list",
            "retained_volume_id_list",
        }
        bucket_name_list = payload["bucket_name_list"]
        instance_id_list = payload["instance_id_list"]
        kms_key_arn_list = payload["kms_key_arn_list"]
        retained_volume_id_list = payload["retained_volume_id_list"]
        environment_name = payload["environment_name"]
        if (
            payload["schema_version"] != 3
            or isinstance(payload["schema_version"], bool)
            or not isinstance(bucket_name_list, list)
            or len(bucket_name_list) != 4
            or any(not isinstance(item, str) or not item for item in bucket_name_list)
            or len(set(bucket_name_list)) != 4
            or any(
                not isinstance(payload[name], list)
                or any(not isinstance(item, str) or not item for item in payload[name])
                or len(payload[name]) != len(set(payload[name]))
                for name in list_field_name_set - {"bucket_name_list"}
            )
            or any(
                not isinstance(payload[name], str) or not payload[name]
                for name in field_name_set - list_field_name_set - {"schema_version"}
            )
            or _COMMON_PREFIX_PATTERN.fullmatch(payload["common_prefix"]) is None
            or _ENVIRONMENT_NAME_PATTERN.fullmatch(environment_name) is None
            or payload["compute_stack_name"] != f"compute-{environment_name}"
            or payload["data_stack_name"] != f"data-{environment_name}"
            or payload["kms_alias_name"] != f"alias/storage-{environment_name}"
            or any(_EC2_INSTANCE_ID_PATTERN.fullmatch(item) is None for item in instance_id_list)
            or any(_KMS_KEY_ARN_PATTERN.fullmatch(item) is None for item in kms_key_arn_list)
            or any(_EBS_VOLUME_ID_PATTERN.fullmatch(item) is None for item in retained_volume_id_list)
        ):
            raise DevelopmentEnvironmentError("Task cleanup inventory is malformed")
        return cls(
            bucket_name_list=tuple(sorted(bucket_name_list)),
            common_prefix=payload["common_prefix"],
            compute_stack_name=payload["compute_stack_name"],
            data_stack_name=payload["data_stack_name"],
            environment_name=payload["environment_name"],
            instance_id_list=tuple(sorted(payload["instance_id_list"])),
            kms_alias_name=payload["kms_alias_name"],
            kms_key_arn_list=tuple(sorted(payload["kms_key_arn_list"])),
            retained_volume_id_list=tuple(sorted(payload["retained_volume_id_list"])),
            schema_version=3,
        )

    def payload_get(self) -> dict[str, object]:
        """Return the canonical JSON representation.

        Returns:
            The canonical JSON representation.
        """

        payload = asdict(self)
        payload["bucket_name_list"] = list(self.bucket_name_list)
        payload["instance_id_list"] = list(self.instance_id_list)
        payload["kms_key_arn_list"] = list(self.kms_key_arn_list)
        payload["retained_volume_id_list"] = list(self.retained_volume_id_list)
        return payload
