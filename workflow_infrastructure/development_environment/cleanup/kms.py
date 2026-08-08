"""KMS key retirement for one task environment."""

from __future__ import annotations

from typing import Mapping

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
from workflow_infrastructure.development_environment.cleanup.aws_response import (
    json_object_get,
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


class KmsCleanup:
    """Remove the exact task alias and schedule its key for service deletion."""

    def __init__(self, aws: AwsClientProtocol) -> None:
        """Initialize the KMS cleanup dependencies.

        Args:
            aws: Aws.
        """

        self._aws = aws

    def retire(self, inventory: CleanupInventory) -> None:
        """Idempotently move one ownership-proven key to PendingDeletion.

        Args:
            inventory: Inventory.
        """

        relevant_alias_by_name_map = self._relevant_alias_by_name_map_get(inventory)
        if not inventory.kms_key_arn_list:
            if relevant_alias_by_name_map:
                raise DevelopmentEnvironmentError("Task KMS alias exists without its owned key inventory")
            return
        key_id_set = {_key_id_get(key_arn) for key_arn in inventory.kms_key_arn_list}
        if relevant_alias_by_name_map and (
            set(relevant_alias_by_name_map) != {inventory.kms_alias_name}
            or relevant_alias_by_name_map[inventory.kms_alias_name] not in key_id_set
        ):
            raise DevelopmentEnvironmentError("Task KMS alias ownership is ambiguous")
        if relevant_alias_by_name_map:
            self._aws.run(["kms", "delete-alias", "--alias-name", inventory.kms_alias_name])
        for key_arn in inventory.kms_key_arn_list:
            key_state = self._key_state_get(key_arn)
            if key_state == "absent":
                continue
            if key_state == "Enabled":
                self._aws.run(["kms", "disable-key", "--key-id", key_arn])
                key_state = "Disabled"
            if key_state == "Disabled":
                self._aws.run(
                    [
                        "kms",
                        "schedule-key-deletion",
                        "--key-id",
                        key_arn,
                        "--pending-window-in-days",
                        "7",
                    ]
                )
                key_state = self._key_state_get(key_arn)
            if key_state != "PendingDeletion":
                raise DevelopmentEnvironmentError("Task KMS key did not enter PendingDeletion")

    def absence_validate(self, inventory: CleanupInventory) -> None:
        """Require no custom task alias and a key pending service deletion.

        Args:
            inventory: Inventory.
        """

        if not self.absent_get(inventory):
            raise DevelopmentEnvironmentError("Task KMS key or alias retirement proof is unavailable")

    def absent_get(self, inventory: CleanupInventory) -> bool:
        """Return exact accepted-retirement state for every key and alias."""

        key_state_list = [self._key_state_get(key_arn) for key_arn in inventory.kms_key_arn_list]
        key_absent = all(state in {"PendingDeletion", "absent"} for state in key_state_list)
        alias_absent = not self._relevant_alias_by_name_map_get(inventory)
        return key_absent and alias_absent

    def _relevant_alias_by_name_map_get(self, inventory: CleanupInventory) -> dict[str, str]:
        """Return the exact task alias and every other custom alias targeting its key.

        Args:
            inventory: Inventory.

        Returns:
            The exact task alias and every other custom alias targeting its key.
        """

        payload = self._aws.json_get(["kms", "list-aliases"])
        alias_list = payload.get("Aliases", [])
        if not isinstance(alias_list, list) or any(not isinstance(item, Mapping) for item in alias_list):
            raise DevelopmentEnvironmentError("Task KMS alias inventory is malformed")
        key_id_set = {_key_id_get(key_arn) for key_arn in inventory.kms_key_arn_list}
        seen_name_set: set[str] = set()
        result: dict[str, str] = {}
        for item in alias_list:
            name = item.get("AliasName")
            target_key_id = item.get("TargetKeyId")
            if not isinstance(name, str) or not name or name in seen_name_set:
                raise DevelopmentEnvironmentError("Task KMS alias inventory is malformed")
            seen_name_set.add(name)
            if name == inventory.kms_alias_name or target_key_id in key_id_set:
                if not isinstance(target_key_id, str) or not target_key_id:
                    raise DevelopmentEnvironmentError("Task KMS alias target is ambiguous")
                result[name] = target_key_id
        return result

    def _key_state_get(self, key_arn: str) -> str:
        """Read the exact lifecycle state of the task-owned KMS key.

        Args:
            key_arn: Exact KMS key ARN.

        Returns:
            Current AWS KMS key state.
        """

        result = self._aws.run(
            ["kms", "describe-key", "--key-id", key_arn],
            check=False,
        )
        if result.returncode != 0:
            if aws_cli_error_matches(
                result,
                code_set=frozenset({"NotFoundException"}),
                operation="DescribeKey",
            ):
                return "absent"
            raise DevelopmentEnvironmentError("Task KMS key state cannot be observed")
        payload = json_object_get(result.stdout, label="task KMS key")
        metadata = payload.get("KeyMetadata")
        if not isinstance(metadata, Mapping) or metadata.get("Arn") != key_arn:
            raise DevelopmentEnvironmentError("Task KMS key identity is malformed")
        state = metadata.get("KeyState")
        if not isinstance(state, str) or not state:
            raise DevelopmentEnvironmentError("Task KMS key state is malformed")
        return state


def _key_id_get(key_arn: str) -> str:
    """Return the exact key identifier embedded in the validated ARN.

    Args:
        key_arn: Exact KMS key ARN.

    Returns:
        The exact key identifier embedded in the validated ARN.
    """

    return key_arn.rsplit("/", maxsplit=1)[-1]
