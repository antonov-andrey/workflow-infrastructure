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
_OPERATION_IDENTITY_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class CleanupRequest:
    """One exact request issued by the goal-delete transaction."""

    common_prefix: str
    operation_identity: str
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
            or set(payload) != {"schema_version", "common_prefix", "operation_identity"}
            or payload.get("schema_version") != 1
            or isinstance(payload.get("schema_version"), bool)
            or payload.get("common_prefix") != expected_common_prefix
            or _COMMON_PREFIX_PATTERN.fullmatch(expected_common_prefix) is None
            or not isinstance(payload.get("operation_identity"), str)
            or _OPERATION_IDENTITY_PATTERN.fullmatch(payload["operation_identity"]) is None
        ):
            raise DevelopmentEnvironmentError("Task cleanup request is malformed or has another task identity")
        return cls(
            common_prefix=expected_common_prefix,
            operation_identity=payload["operation_identity"],
        )

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
    instance_id: str
    kms_alias_name: str
    kms_key_arn: str
    operation_identity: str
    retained_volume_id: str
    schema_version: int = 1

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
            "instance_id",
            "kms_alias_name",
            "kms_key_arn",
            "operation_identity",
            "retained_volume_id",
            "schema_version",
        }
        if not isinstance(payload, dict) or set(payload) != field_name_set:
            raise DevelopmentEnvironmentError("Task cleanup inventory has another shape")
        bucket_name_list = payload["bucket_name_list"]
        if (
            payload["schema_version"] != 1
            or isinstance(payload["schema_version"], bool)
            or not isinstance(bucket_name_list, list)
            or len(bucket_name_list) != 4
            or len(set(bucket_name_list)) != 4
            or any(not isinstance(item, str) or not item for item in bucket_name_list)
            or any(
                not isinstance(payload[name], str) or not payload[name]
                for name in field_name_set - {"bucket_name_list", "schema_version"}
            )
        ):
            raise DevelopmentEnvironmentError("Task cleanup inventory is malformed")
        return cls(
            bucket_name_list=tuple(sorted(bucket_name_list)),
            common_prefix=payload["common_prefix"],
            compute_stack_name=payload["compute_stack_name"],
            data_stack_name=payload["data_stack_name"],
            environment_name=payload["environment_name"],
            instance_id=payload["instance_id"],
            kms_alias_name=payload["kms_alias_name"],
            kms_key_arn=payload["kms_key_arn"],
            operation_identity=payload["operation_identity"],
            retained_volume_id=payload["retained_volume_id"],
        )

    def payload_get(self) -> dict[str, object]:
        """Return the canonical JSON representation.

        Returns:
            The canonical JSON representation.
        """

        payload = asdict(self)
        payload["bucket_name_list"] = list(self.bucket_name_list)
        return payload
