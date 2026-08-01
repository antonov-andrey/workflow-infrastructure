"""Validate sealed goal cleanup ownership before task AWS mutation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

BOOTSTRAP_MANIFEST_NAME = "worktree-bootstrap.yaml"
CLEANUP_COMMAND_ARGUMENT_LIST = (
    "python",
    "development_environment_manage.py",
    "destroy",
    "--git-worktree",
    "{common_prefix}",
)
_RECEIPT_FIELD_NAME_SET = {
    "schema_version",
    "common_prefix",
    "cleanup_declaration_sha256",
    "manifest_sha256",
    "provider_state_generation",
    "sealed_specification_sha256",
}


class TaskCleanupBinding:
    """Require the goal-lifecycle receipt bound to this exact task manifest."""

    def __init__(self, *, project_root_path: Path) -> None:
        self._project_root_path = project_root_path.absolute()

    def validate(self, *, common_prefix: str) -> Mapping[str, object]:
        try:
            self._project_root_path = self._project_root_path.resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Task project root is unavailable"
            ) from error
        manifest_path = self._project_root_path / BOOTSTRAP_MANIFEST_NAME
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_nlink != 1
        ):
            raise DevelopmentEnvironmentError("Task bootstrap manifest is unavailable")
        common_directory = self.common_directory_get()
        receipt_path = (
            common_directory
            / "agent-workflows"
            / "cleanup-binding"
            / f"{common_prefix}.json"
        )
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Task cleanup binding receipt is unavailable"
            ) from error
        provider_state_generation = (
            receipt.get("provider_state_generation")
            if isinstance(receipt, dict)
            else None
        )
        expected_cleanup_sha256 = hashlib.sha256(
            b"\0".join(item.encode("utf-8") for item in CLEANUP_COMMAND_ARGUMENT_LIST)
        ).hexdigest()
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if (
            not isinstance(receipt, dict)
            or set(receipt) != _RECEIPT_FIELD_NAME_SET
            or receipt.get("schema_version") != 1
            or receipt.get("common_prefix") != common_prefix
            or receipt.get("cleanup_declaration_sha256") != expected_cleanup_sha256
            or receipt.get("manifest_sha256") != manifest_sha256
            or not isinstance(provider_state_generation, int)
            or isinstance(provider_state_generation, bool)
            or provider_state_generation < 1
            or not _sha256_is_valid(receipt.get("sealed_specification_sha256"))
        ):
            raise DevelopmentEnvironmentError(
                "Task cleanup binding receipt is stale or malformed"
            )
        return receipt

    def common_directory_get(self) -> Path:
        """Return the exact Git common directory that owns private receipts."""

        environment = os.environ.copy()
        for name in tuple(environment):
            if (
                name == "GIT_DIR"
                or name == "GIT_WORK_TREE"
                or name.startswith("GIT_CONFIG")
            ):
                environment.pop(name, None)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self._project_root_path),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        if result.returncode != 0:
            raise DevelopmentEnvironmentError(
                "Task Git common directory is unavailable"
            )
        try:
            return Path(result.stdout.strip()).resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Task Git common directory is unavailable"
            ) from error


def _sha256_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
