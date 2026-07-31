"""Safe development-host and remote status collection."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)

HOST_EBS_DEVICE_BY_ID_ROOT_PATH = Path("/dev/disk/by-id")
HOST_STATUS_COMMAND_TIMEOUT_SECONDS = 120
PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT = "PYTHONDONTWRITEBYTECODE=1"


class CommandResultProtocol(Protocol):
    """Command result fields consumed by status collection."""

    returncode: int
    stdout: str


class CommandRunnerProtocol(Protocol):
    """Host process boundary consumed by status collection."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> CommandResultProtocol:
        """Run one command."""


class IdentityProtocol(Protocol):
    """Environment paths and identity used by safe status collection."""

    environment_name: str
    host_control_current_source_path: Path
    host_release_root_path: Path
    host_retained_current_release_path: Path
    host_retained_root_path: Path


class RetainedVolumeProtocol(Protocol):
    """Retained-volume identity validator."""

    def volume_id_validate(self, volume_id: str) -> str:
        """Validate one EBS volume identity."""


class TransportProtocol(Protocol):
    """Remote shell boundary used by operator status."""

    def ssm_shell_result_get(self, command_list: Sequence[str], *, timeout_seconds: int) -> dict[str, object]:
        """Run one remote command and return its invocation payload."""


class DevelopmentHostStatus:
    """Own safe status collection on-host and through Session Manager."""

    def __init__(
        self,
        *,
        identity: IdentityProtocol,
        is_host: bool,
        product_activity_get: Callable[[], str],
        retained_volume: RetainedVolumeProtocol,
        runner: CommandRunnerProtocol,
        transport: TransportProtocol,
    ) -> None:
        """Bind status collection to one exact environment."""

        self._identity = identity
        self._is_host = is_host
        self._product_activity_get = product_activity_get
        self._retained_volume = retained_volume
        self._runner = runner
        self._transport = transport

    def print_local_status(self, retained_volume_id: str) -> None:
        """Print safe host state from one exact infrastructure release.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the Product root.
        """

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-status is supported only from an exact source release " "on the development host"
            )
        payload = self.payload_validate(self._local_payload_get(retained_volume_id=retained_volume_id))
        print(json.dumps(payload, sort_keys=True))

    def payload_get(self, *, retained_volume_id: str) -> dict[str, str]:
        """Inspect safe host state through one bounded SSM Run Command.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the Product root.

        Returns:
            Safe normalized host status fields.
        """

        self._retained_volume.volume_id_validate(retained_volume_id)
        result_payload = self._transport.ssm_shell_result_get(
            [
                shlex.join(
                    [
                        "env",
                        PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                        "python3.14",
                        "-B",
                        str(
                            self._identity.host_control_current_source_path
                            / "sources"
                            / "workflow-infrastructure"
                            / "tool"
                            / "development_environment_manage.py"
                        ),
                        "host-status",
                        "--environment-name",
                        self._identity.environment_name,
                        "--retained-volume-id",
                        retained_volume_id,
                    ]
                )
            ],
            timeout_seconds=HOST_STATUS_COMMAND_TIMEOUT_SECONDS,
        )
        output_text = result_payload.get("StandardOutputContent")
        if not isinstance(output_text, str):
            raise DevelopmentEnvironmentError("Development host status output is malformed")
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError("Development host status output is invalid") from error
        return self.payload_validate(payload)

    def _local_payload_get(self, *, retained_volume_id: str) -> dict[str, str]:
        """Collect safe state directly on the development host.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the Product root.

        Returns:
            Safe host status fields.
        """

        self._retained_volume.volume_id_validate(retained_volume_id)
        k3s_service_status = self._host_service_status_get("k3s")
        host_controller_service_status = self._host_service_status_get("workflow-control-center-host-controller")
        host_controller_unit_result = self._runner.run(
            [
                "systemctl",
                "show",
                "--property=LoadState",
                "--value",
                "workflow-control-center-host-controller",
            ],
            check=False,
        )
        host_controller_unit_status = host_controller_unit_result.stdout.strip()
        if host_controller_unit_status not in {"loaded", "masked", "not-found"}:
            host_controller_unit_status = "unknown"

        return {
            "current_release": self._host_current_release_get(),
            "host_controller_service_status": host_controller_service_status,
            "host_controller_unit_status": host_controller_unit_status,
            "host_status_probe": "ok",
            "k3s_service_status": k3s_service_status,
            "kubernetes_node_status": self._host_kubernetes_node_status_get(k3s_service_status=k3s_service_status),
            "retained_mount_status": self._host_retained_mount_status_get(retained_volume_id=retained_volume_id),
            "wcc_activity": self._product_activity_get(),
        }

    def _host_current_release_get(self) -> str:
        """Return the safe exact retained release name or its invalid state."""

        if not self._identity.host_retained_current_release_path.is_symlink():
            return ""
        try:
            current_release_path = self._identity.host_retained_current_release_path.resolve(strict=True)
        except OSError:
            return "invalid"
        release_name = current_release_path.name
        if (
            current_release_path.parent == self._identity.host_release_root_path
            and len(release_name) == 20
            and release_name.isdigit()
        ):
            return release_name
        return "invalid"

    def _host_kubernetes_node_status_get(self, *, k3s_service_status: str) -> str:
        """Return normalized readiness across every node in the local k3s cluster.

        Args:
            k3s_service_status: Already normalized k3s systemd state.

        Returns:
            ``ready``, ``not-ready``, or ``unavailable``.
        """

        if k3s_service_status != "active":
            return "unavailable"
        node_result = self._runner.run(
            [
                "k3s",
                "kubectl",
                "get",
                "nodes",
                "--output",
                "json",
                "--request-timeout=10s",
            ],
            check=False,
        )
        if node_result.returncode != 0:
            return "unavailable"
        try:
            node_payload = json.loads(node_result.stdout)
            node_list = node_payload["items"]
            readiness_list = [
                next(condition["status"] for condition in node["status"]["conditions"] if condition["type"] == "Ready")
                for node in node_list
            ]
        except (
            KeyError,
            StopIteration,
            TypeError,
            json.JSONDecodeError,
        ):
            return "unavailable"
        if readiness_list and all(status == "True" for status in readiness_list):
            return "ready"
        return "not-ready"

    def _host_retained_mount_status_get(self, *, retained_volume_id: str) -> str:
        """Prove the exact retained EBS device is the Product XFS mount.

        Args:
            retained_volume_id: Exact retained EBS volume expected at the mount.

        Returns:
            ``ready``, ``unmounted``, or ``wrong-device``.
        """

        retained_device_path = HOST_EBS_DEVICE_BY_ID_ROOT_PATH / (
            "nvme-Amazon_Elastic_Block_Store_" + retained_volume_id.replace("-", "")
        )
        findmnt_result = self._runner.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "SOURCE,FSTYPE,TARGET",
                "--target",
                str(self._identity.host_retained_root_path),
            ],
            check=False,
        )
        if findmnt_result.returncode != 0:
            return "unmounted"
        findmnt_field_list = findmnt_result.stdout.strip().split()
        if len(findmnt_field_list) != 3:
            return "wrong-device"
        source_text, filesystem_type, target_text = findmnt_field_list
        try:
            actual_device_path = Path(source_text).resolve(strict=True)
            expected_device_path = retained_device_path.resolve(strict=True)
        except OSError:
            return "wrong-device"
        if (
            actual_device_path == expected_device_path
            and filesystem_type == "xfs"
            and target_text == str(self._identity.host_retained_root_path)
        ):
            return "ready"
        return "wrong-device"

    def _host_service_status_get(self, unit_name: str) -> str:
        """Return one normalized systemd service state.

        Args:
            unit_name: Exact systemd unit name.

        Returns:
            Safe normalized active state.
        """

        result = self._runner.run(
            ["systemctl", "is-active", unit_name],
            check=False,
        )
        status = result.stdout.strip()
        if status not in {
            "active",
            "activating",
            "deactivating",
            "failed",
            "inactive",
            "maintenance",
            "reloading",
        }:
            return "unknown"
        return status

    @staticmethod
    def payload_validate(payload: object) -> dict[str, str]:
        """Validate the fixed safe host-status response contract.

        Args:
            payload: Decoded host-status response.

        Returns:
            Normalized safe status fields.
        """

        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError("Development host status output is malformed")
        expected_field_set = {
            "current_release",
            "host_controller_service_status",
            "host_controller_unit_status",
            "host_status_probe",
            "k3s_service_status",
            "kubernetes_node_status",
            "retained_mount_status",
            "wcc_activity",
        }
        if set(payload) != expected_field_set or any(not isinstance(value, str) for value in payload.values()):
            raise DevelopmentEnvironmentError("Development host status output is malformed")
        current_release = payload["current_release"]
        if current_release not in {"", "invalid"} and (len(current_release) != 20 or not current_release.isdigit()):
            raise DevelopmentEnvironmentError("Development host current release is malformed")
        allowed_value_by_field_map = {
            "host_controller_service_status": {
                "active",
                "activating",
                "deactivating",
                "failed",
                "inactive",
                "maintenance",
                "reloading",
                "unknown",
            },
            "host_controller_unit_status": {
                "loaded",
                "masked",
                "not-found",
                "unknown",
            },
            "host_status_probe": {"ok"},
            "k3s_service_status": {
                "active",
                "activating",
                "deactivating",
                "failed",
                "inactive",
                "maintenance",
                "reloading",
                "unknown",
            },
            "kubernetes_node_status": {
                "not-ready",
                "ready",
                "unavailable",
            },
            "retained_mount_status": {
                "ready",
                "unmounted",
                "wrong-device",
            },
            "wcc_activity": {"busy", "idle"},
        }
        for field, allowed_value_set in allowed_value_by_field_map.items():
            if payload[field] not in allowed_value_set:
                raise DevelopmentEnvironmentError(f"Development host status field {field} is malformed")
        return {field: str(value) for field, value in payload.items()}

    @staticmethod
    def unavailable_payload_get() -> dict[str, str]:
        """Return stable status fields when the remote host cannot be inspected."""

        return {
            "current_release": "",
            "host_controller_service_status": "unavailable",
            "host_controller_unit_status": "unavailable",
            "host_status_probe": "unavailable",
            "k3s_service_status": "unavailable",
            "kubernetes_node_status": "unavailable",
            "retained_mount_status": "unavailable",
            "wcc_activity": "unavailable",
        }
