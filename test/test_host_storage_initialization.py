"""Verify monotonic retained-volume filesystem authorization in AWS state."""

from __future__ import annotations

from pathlib import Path

import pytest

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.storage import (
    DevelopmentHostStorageInitialization,
)


class _Identity:
    compute_stack_name = "compute-w729c92ceba194ac"
    environment_name = "w729c92ceba194ac"
    git_worktree = "2026-08-01-workflow-platform-hardening"


class _Aws:
    def __init__(self, stack: "_Stack") -> None:
        self._stack = stack

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        assert argument_list == [
            "ec2",
            "describe-volumes",
            "--volume-ids",
            "vol-0123456789abcdef0",
        ]
        return {
            "Volumes": [
                {
                    "Attachments": [
                        {
                            "AttachTime": "2026-08-01T00:00:00Z",
                            "DeleteOnTermination": False,
                            "Device": "/dev/sdf",
                            "InstanceId": "i-0123456789abcdef0",
                            "State": "attached",
                            "VolumeId": "vol-0123456789abcdef0",
                        }
                    ],
                    "Encrypted": True,
                    "SnapshotId": self._stack.snapshot_id,
                    "State": "in-use",
                    "Tags": [
                        {"Key": key, "Value": value}
                        for key, value in {
                            "EnvironmentClass": "development",
                            "EnvironmentName": _Identity.environment_name,
                            "FilesystemState": self._stack.state,
                            "ManagedBy": "CloudFormation",
                            "Name": f"retained-{_Identity.environment_name}",
                            "aws:cloudformation:stack-name": (
                                _Identity.compute_stack_name
                            ),
                            "git-worktree": _Identity.git_worktree,
                        }.items()
                    ],
                    "VolumeId": "vol-0123456789abcdef0",
                }
            ]
        }


class _Stack:
    def __init__(self, *, snapshot_id: str = "", state: str = "pending") -> None:
        self.apply_argument_list: list[dict[str, object]] = []
        self.snapshot_id = snapshot_id
        self.state = state

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        assert stack_name == _Identity.compute_stack_name
        return {"RetainedVolumeFilesystemState": self.state}

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        assert stack_name == _Identity.compute_stack_name
        return {
            "InstanceId": "i-0123456789abcdef0",
            "RetainedVolumeFilesystemState": self.state,
            "RetainedVolumeId": "vol-0123456789abcdef0",
            "RetainedVolumeSlot": "base" if not self.snapshot_id else "a",
            "RetainedVolumeSourceSnapshotId": self.snapshot_id,
        }

    def apply(self, **kwargs: object) -> None:
        self.apply_argument_list.append(kwargs)
        parameter_by_name_map = kwargs["parameter_by_name_map"]
        assert isinstance(parameter_by_name_map, dict)
        self.state = str(parameter_by_name_map["RetainedVolumeFilesystemState"])

    def drift_validate(self, stack_name: str) -> None:
        assert stack_name == _Identity.compute_stack_name


def _owner_get(stack: _Stack) -> DevelopmentHostStorageInitialization:
    return DevelopmentHostStorageInitialization(
        aws=_Aws(stack),
        compute_stable_identity_logical_id_set={
            "DevelopmentInstance",
            "RetainedVolume",
        },
        compute_template_path=Path("/project/cloudformation/development-compute.yaml"),
        identity=_Identity(),
        stack=stack,
    )


def test_pending_base_volume_advances_once_to_complete() -> None:
    """The exact new base volume receives one monotonic authorization transition."""

    stack = _Stack()
    owner = _owner_get(stack)

    assert owner.initialization_allowed_get() is True
    owner.complete()

    assert owner.initialization_allowed_get() is False
    assert len(stack.apply_argument_list) == 1
    assert stack.apply_argument_list[0]["parameter_by_name_map"] == {
        "RetainedVolumeFilesystemState": "complete"
    }
    assert stack.apply_argument_list[0]["protected_identity_logical_id_set"] == {
        "DevelopmentInstance",
        "RetainedVolume",
    }
    owner.complete()
    assert len(stack.apply_argument_list) == 1


def test_snapshot_volume_can_never_be_pending() -> None:
    """A restore must arrive with an already complete filesystem contract."""

    owner = _owner_get(_Stack(snapshot_id="snap-0123456789abcdef0"))

    with pytest.raises(DevelopmentEnvironmentError, match="Only a new base"):
        owner.initialization_allowed_get()


def test_complete_base_volume_never_regains_initialization_authorization() -> None:
    """A completed base volume remains read-only with respect to mkfs authority."""

    stack = _Stack(state="complete")
    owner = _owner_get(stack)

    assert owner.initialization_allowed_get() is False
    owner.complete()
    assert stack.apply_argument_list == []
