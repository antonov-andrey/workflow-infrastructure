"""Closed request and live inventory models for task cleanup."""

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
    """Exact task resource identities freshly attested from live AWS state."""

    account_id: str
    bucket_name_list: tuple[str, ...]
    common_prefix: str
    compute_stack_name: str
    data_stack_name: str
    environment_name: str
    instance_id_list: tuple[str, ...]
    kms_alias_name: str
    kms_key_arn_list: tuple[str, ...]
    region: str
    retained_volume_id_list: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject any runtime inventory outside the exact cleanup identity."""

        identity_tuple_list = (
            self.bucket_name_list,
            self.instance_id_list,
            self.kms_key_arn_list,
            self.retained_volume_id_list,
        )
        if (
            not isinstance(self.account_id, str)
            or re.fullmatch(r"[0-9]{12}", self.account_id) is None
            or not isinstance(self.region, str)
            or re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]", self.region) is None
            or not isinstance(self.bucket_name_list, tuple)
            or len(self.bucket_name_list) != 4
            or any(
                not isinstance(identity_tuple, tuple)
                or any(not isinstance(item, str) or not item for item in identity_tuple)
                or tuple(sorted(identity_tuple)) != identity_tuple
                or len(identity_tuple) != len(set(identity_tuple))
                for identity_tuple in identity_tuple_list
            )
            or any(
                not isinstance(value, str) or not value
                for value in (
                    self.common_prefix,
                    self.compute_stack_name,
                    self.data_stack_name,
                    self.environment_name,
                    self.kms_alias_name,
                )
            )
            or _COMMON_PREFIX_PATTERN.fullmatch(self.common_prefix) is None
            or _ENVIRONMENT_NAME_PATTERN.fullmatch(self.environment_name) is None
            or self.compute_stack_name != f"compute-{self.environment_name}"
            or self.data_stack_name != f"data-{self.environment_name}"
            or self.kms_alias_name != f"alias/storage-{self.environment_name}"
            or any(_EC2_INSTANCE_ID_PATTERN.fullmatch(item) is None for item in self.instance_id_list)
            or any(
                _KMS_KEY_ARN_PATTERN.fullmatch(item) is None
                or not item.startswith(f"arn:aws:kms:{self.region}:{self.account_id}:key/")
                for item in self.kms_key_arn_list
            )
            or any(_EBS_VOLUME_ID_PATTERN.fullmatch(item) is None for item in self.retained_volume_id_list)
        ):
            raise DevelopmentEnvironmentError("Task cleanup inventory is malformed")
