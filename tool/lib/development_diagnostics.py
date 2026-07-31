"""Own safe operator status and bounded diagnostics for development."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Protocol

from tool.lib.development_environment_error import DevelopmentEnvironmentError


class AccountVerifierProtocol(Protocol):
    """Local AWS operator boundary required by diagnostics."""

    def local_operator_context_validate(self) -> None:
        """Validate the exact development account and region."""


class ComputeProtocol(Protocol):
    """Safe EC2 and Session Manager observations."""

    def active_session_count_get(self, instance_id: str) -> int:
        """Return active Session Manager session count."""

    def instance_id_get(self) -> str:
        """Return the exact current instance identity."""

    def ssm_ping_status_get(self, instance_id: str) -> str:
        """Return current SSM managed-instance status."""

    def state_get(self, instance_id: str) -> str:
        """Return current EC2 state."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment identities and paths required by diagnostics."""

    compute_stack_name: str
    data_plane_stack_name: str
    host_retained_root_path: Path


class HostStatusProtocol(Protocol):
    """Safe host-status collection boundary."""

    def payload_get(self, *, retained_volume_id: str) -> dict[str, object]:
        """Return complete current safe host status."""

    def unavailable_payload_get(self) -> dict[str, object]:
        """Return normalized unavailable host status."""


class ProductReleaseProtocol(Protocol):
    """Current Product diagnostic command boundary."""

    def current_product_tool_command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one exact Product tool command."""


class RetainedVolumeProtocol(Protocol):
    """Retained-volume status and backup boundary."""

    def latest_snapshot_id_get(self, volume_id: str) -> str | None:
        """Return latest snapshot for one retained volume."""

    def regular_backup_status_get(self) -> dict[str, object]:
        """Return safe primary-only backup policy status."""


class StackManagerProtocol(Protocol):
    """CloudFormation state required by status."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""

    def payload_get(
        self,
        stack_name: str,
        *,
        is_required: bool,
    ) -> dict[str, object]:
        """Return current stack payload or an empty mapping."""


class StopLeaseProtocol(Protocol):
    """Safe stop-lease status boundary."""

    def payload_get(self) -> dict[str, object]:
        """Return safe current lease state."""


class SsmTransportProtocol(Protocol):
    """Bounded remote diagnostics boundary."""

    def ssm_shell_run(self, command_list: list[str]) -> None:
        """Run remote shell commands."""


class DevelopmentDiagnostics:
    """Own safe status serialization and bounded remote diagnostics."""

    def __init__(
        self,
        *,
        account: AccountVerifierProtocol,
        account_id: str,
        compute: ComputeProtocol,
        host_status: HostStatusProtocol,
        identity: EnvironmentIdentityProtocol,
        product_release: ProductReleaseProtocol,
        region: str,
        retained_volume: RetainedVolumeProtocol,
        stack: StackManagerProtocol,
        stop_lease: StopLeaseProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Bind diagnostics to one exact environment and account."""

        self._account = account
        self._account_id = account_id
        self._compute = compute
        self._host_status = host_status
        self._identity = identity
        self._product_release = product_release
        self._region = region
        self._retained_volume = retained_volume
        self._stack = stack
        self._stop_lease = stop_lease
        self._transport = transport

    def status(self) -> None:
        """Print safe infrastructure, access, lease, storage, and release state."""

        self._account.local_operator_context_validate()
        data_stack = self._stack.payload_get(
            self._identity.data_plane_stack_name,
            is_required=False,
        )
        compute_stack = self._stack.payload_get(
            self._identity.compute_stack_name,
            is_required=False,
        )
        payload: dict[str, object] = {
            "account_id": self._account_id,
            "compute_stack_status": compute_stack.get("StackStatus", "absent"),
            "data_plane_stack_status": data_stack.get("StackStatus", "absent"),
            "region": self._region,
        }
        if compute_stack:
            output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
            instance_id = output_by_name_map["InstanceId"]
            instance_state = self._compute.state_get(instance_id)
            ssm_ping_status = self._compute.ssm_ping_status_get(instance_id)
            payload.update(
                {
                    "active_ssm_session_count": (self._compute.active_session_count_get(instance_id)),
                    "instance_id": instance_id,
                    "instance_state": instance_state,
                    "instance_type": output_by_name_map["InstanceType"],
                    "latest_retained_snapshot_id": (
                        self._retained_volume.latest_snapshot_id_get(output_by_name_map["RetainedVolumeId"])
                        or output_by_name_map.get("RetainedVolumeSourceSnapshotId", "")
                    ),
                    "retained_volume_id": output_by_name_map["RetainedVolumeId"],
                    "retained_volume_slot": output_by_name_map.get("RetainedVolumeSlot", "base"),
                    "retained_volume_source_snapshot_id": (
                        output_by_name_map.get("RetainedVolumeSourceSnapshotId", "")
                    ),
                    "retained_backup_policy": (self._retained_volume.regular_backup_status_get()),
                    "ssm_ping_status": ssm_ping_status,
                    "stop_lease": self._stop_lease.payload_get(),
                }
            )
            host_status_payload = self._host_status.unavailable_payload_get()
            if instance_state == "running" and ssm_ping_status == "Online":
                try:
                    host_status_payload = self._host_status.payload_get(
                        retained_volume_id=output_by_name_map["RetainedVolumeId"]
                    )
                except DevelopmentEnvironmentError:
                    pass
            payload.update(host_status_payload)
        print(json.dumps(payload, indent=2, sort_keys=True))

    def diagnose(self) -> None:
        """Print bounded infrastructure and Product diagnostics without secrets."""

        self._account.local_operator_context_validate()
        self.status()
        instance_id = self._compute.instance_id_get()
        if self._compute.state_get(instance_id) != "running":
            print("OK: remote diagnostics skipped because the instance is not running")
            return
        self._transport.ssm_shell_run(
            [
                f"df -h / {self._identity.host_retained_root_path}",
                ("sudo systemctl --no-pager --full status " "k3s workflow-control-center-host-controller || true"),
                "sudo k3s kubectl get nodes,namespaces -o wide",
                "sudo k3s kubectl get pods --all-namespaces -o wide",
                ("sudo k3s kubectl get events --all-namespaces " "--sort-by=.lastTimestamp | tail -200"),
                (
                    "sudo "
                    + shlex.join(self._product_release.current_product_tool_command_list_get("diagnose"))
                    + " || true"
                ),
            ]
        )
