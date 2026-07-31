"""Own development EC2 instance identity, readiness, and remote runtime facts."""

from __future__ import annotations

import json
import shlex
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from tool.lib.development_environment_error import DevelopmentEnvironmentError

HOST_READY_TIMEOUT_SECONDS = 1800
HOST_STATUS_COMMAND_TIMEOUT_SECONDS = 120
SSM_ONLINE_TIMEOUT_SECONDS = 1800
STACK_POLL_INTERVAL_SECONDS = 5


class AwsClientProtocol(Protocol):
    """AWS operations consumed by compute runtime."""

    def json_get(self, argument_list: Sequence[str]) -> dict[str, object]:
        """Run one AWS CLI operation and decode JSON."""

    def run(
        self,
        argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> object:
        """Run one AWS CLI operation."""


class ClockProtocol(Protocol):
    """Clock surface consumed by bounded readiness waits."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a bounded duration."""


class CommandRunnerProtocol(Protocol):
    """Local command boundary consumed by remote file transfer."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> object:
        """Run one local command."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment identity consumed by compute runtime."""

    compute_stack_name: str
    host_retained_root_path: Path
    instance_name: str


class HostStatusProtocol(Protocol):
    """Safe host-status surface consumed by readiness."""

    def payload_get(self, *, retained_volume_id: str) -> dict[str, object]:
        """Return current safe host status."""

    def unavailable_payload_get(self) -> dict[str, object]:
        """Return safe unavailable host status."""


class StackManagerProtocol(Protocol):
    """CloudFormation state consumed by compute runtime."""

    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        """Return exact stack outputs."""


class SsmTransportProtocol(Protocol):
    """SSM and SSH transport consumed by compute runtime."""

    def ssh_run(
        self,
        command_list: Sequence[str],
        *,
        ssh_control_path: Path,
    ) -> object:
        """Run one command through SSH-over-SSM."""

    def ssm_shell_result_get(
        self,
        command_list: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        """Run remote shell commands and return structured SSM output."""


class DevelopmentComputeManager:
    """Own the current development EC2 instance and its runtime observations."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        clock: ClockProtocol,
        host_status: HostStatusProtocol,
        identity: EnvironmentIdentityProtocol,
        runner: CommandRunnerProtocol,
        stack: StackManagerProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Initialize compute runtime from explicit provider boundaries."""

        self._aws = aws
        self._clock = clock
        self._host_status = host_status
        self._identity = identity
        self._runner = runner
        self._stack = stack
        self._transport = transport

    def instance_id_get(self) -> str:
        """Return the exact instance ID owned by the compute stack."""

        return self._stack.output_by_name_map_get(self._identity.compute_stack_name)["InstanceId"]

    def active_session_count_get(self, instance_id: str) -> int:
        """Return active Session Manager sessions for idle-stop decisions."""

        payload = self._aws.json_get(
            [
                "ssm",
                "describe-sessions",
                "--state",
                "Active",
                "--filters",
                f"key=Target,value={instance_id}",
            ]
        )
        session_list = payload.get("Sessions", [])
        if not isinstance(session_list, list):
            raise DevelopmentEnvironmentError("Session Manager returned malformed Sessions")
        return len(session_list)

    def failed_bootstrap_replacement_is_proven(self) -> bool:
        """Return whether an unmounted disposable replacement failed bootstrap."""

        diagnostic_code = "\n".join(
            [
                "import json",
                "import subprocess",
                "cloud_init = subprocess.run(",
                "    ['cloud-init', 'status', '--long'],",
                "    capture_output=True,",
                "    check=False,",
                "    text=True,",
                ")",
                "retained_mount = subprocess.run(",
                "    [",
                "        'findmnt',",
                "        '--noheadings',",
                "        '--output',",
                "        'TARGET',",
                "        '--target',",
                f"        {str(self._identity.host_retained_root_path)!r},",
                "    ],",
                "    capture_output=True,",
                "    check=False,",
                "    text=True,",
                ")",
                "k3s = subprocess.run(",
                "    ['systemctl', 'is-active', 'k3s'],",
                "    capture_output=True,",
                "    check=False,",
                "    text=True,",
                ")",
                "print(",
                "    json.dumps(",
                "        {",
                "            'cloud_init_returncode': cloud_init.returncode,",
                "            'cloud_init_status': cloud_init.stdout,",
                "            'k3s_status': k3s.stdout.strip(),",
                "            'retained_mount_target': retained_mount.stdout.strip(),",
                "        },",
                "        sort_keys=True,",
                "    )",
                ")",
            ]
        )
        payload = self._transport.ssm_shell_result_get(
            [shlex.join(["python3", "-c", diagnostic_code])],
            timeout_seconds=HOST_STATUS_COMMAND_TIMEOUT_SECONDS,
        )
        output_text = payload.get("StandardOutputContent")
        if not isinstance(output_text, str):
            raise DevelopmentEnvironmentError("Replacement bootstrap diagnostic output is malformed")
        try:
            diagnostic = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError("Replacement bootstrap diagnostic output is not JSON") from error
        if not isinstance(diagnostic, dict):
            raise DevelopmentEnvironmentError("Replacement bootstrap diagnostic payload is malformed")
        cloud_init_returncode = diagnostic.get("cloud_init_returncode")
        cloud_init_status = diagnostic.get("cloud_init_status")
        k3s_status = diagnostic.get("k3s_status")
        retained_mount_target = diagnostic.get("retained_mount_target")
        if (
            not isinstance(cloud_init_returncode, int)
            or not isinstance(cloud_init_status, str)
            or not isinstance(k3s_status, str)
            or not isinstance(retained_mount_target, str)
        ):
            raise DevelopmentEnvironmentError("Replacement bootstrap diagnostic fields are malformed")
        if cloud_init_returncode == 0:
            return False
        if (
            cloud_init_returncode != 1
            or "status: error" not in cloud_init_status
            or "extended_status: error - done" not in cloud_init_status
        ):
            raise DevelopmentEnvironmentError(
                "Replacement host cloud-init state is neither success nor a " "proven terminal bootstrap failure"
            )
        if retained_mount_target == str(self._identity.host_retained_root_path) or k3s_status == "active":
            raise DevelopmentEnvironmentError(
                "Failed replacement bootstrap reached retained state or active k3s; "
                "automatic host replacement is unsafe"
            )
        print("OK: replacement host bootstrap failure is terminal and retained state " "is unmounted")
        return True

    def online_wait(self) -> None:
        """Wait for EC2 and Session Manager readiness."""

        instance_id = self.instance_id_get()
        self._aws.run(["ec2", "wait", "instance-status-ok", "--instance-ids", instance_id])
        t_deadline = self._clock.monotonic() + SSM_ONLINE_TIMEOUT_SECONDS
        while self._clock.monotonic() < t_deadline:
            if self.ssm_ping_status_get(instance_id) == "Online":
                return
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(f"Instance {instance_id} did not become SSM Online")

    def ssm_ping_status_get(self, instance_id: str) -> str:
        """Return current SSM managed-node ping status."""

        payload = self._aws.json_get(
            [
                "ssm",
                "describe-instance-information",
                "--filters",
                f"Key=InstanceIds,Values={instance_id}",
            ]
        )
        information_list = payload.get("InstanceInformationList", [])
        if not isinstance(information_list, list):
            raise DevelopmentEnvironmentError("SSM instance information response is malformed")
        if not information_list:
            return "Unavailable"
        if len(information_list) != 1 or not isinstance(information_list[0], dict):
            raise DevelopmentEnvironmentError("SSM instance information response is malformed")
        ping_status = information_list[0].get("PingStatus")
        if ping_status not in {"ConnectionLost", "Inactive", "Online"}:
            raise DevelopmentEnvironmentError("SSM instance ping status is malformed")
        return ping_status

    def readiness_wait(self) -> None:
        """Prove retained storage, k3s, node, and host-controller readiness."""

        retained_volume_id = self._stack.output_by_name_map_get(self._identity.compute_stack_name)["RetainedVolumeId"]
        t_deadline = self._clock.monotonic() + HOST_READY_TIMEOUT_SECONDS
        host_status_payload = self._host_status.unavailable_payload_get()
        while self._clock.monotonic() < t_deadline:
            try:
                host_status_payload = self._host_status.payload_get(retained_volume_id=retained_volume_id)
            except DevelopmentEnvironmentError:
                self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
                continue
            foundation_is_ready = (
                host_status_payload["retained_mount_status"] == "ready"
                and host_status_payload["k3s_service_status"] == "active"
                and host_status_payload["kubernetes_node_status"] == "ready"
            )
            controller_is_ready = (
                host_status_payload["host_controller_unit_status"] == "loaded"
                and host_status_payload["host_controller_service_status"] == "active"
            )
            if foundation_is_ready and controller_is_ready:
                return
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        safe_status_text = json.dumps(host_status_payload, sort_keys=True)
        raise DevelopmentEnvironmentError(
            "Development host did not become ready within "
            f"{HOST_READY_TIMEOUT_SECONDS} seconds; last safe status: "
            f"{safe_status_text}"
        )

    def state_get(self, instance_id: str) -> str:
        """Return the current EC2 instance state."""

        payload = self._aws.json_get(["ec2", "describe-instances", "--instance-ids", instance_id])
        try:
            state = payload["Reservations"][0]["Instances"][0]["State"]["Name"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError("EC2 instance state response is malformed") from error
        if not isinstance(state, str):
            raise DevelopmentEnvironmentError("EC2 instance state is not text")
        return state

    def launch_template_version_get(self) -> str:
        """Return exact launch-template version recorded by the EC2 instance."""

        instance_id = self.instance_id_get()
        payload = self._aws.json_get(["ec2", "describe-instances", "--instance-ids", instance_id])
        try:
            tag_list = payload["Reservations"][0]["Instances"][0]["Tags"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError("EC2 instance launch-template response is malformed") from error
        if not isinstance(tag_list, list) or any(not isinstance(tag, dict) for tag in tag_list):
            raise DevelopmentEnvironmentError("EC2 instance launch-template tags are malformed")
        tag_by_name_map = {tag.get("Key"): tag.get("Value") for tag in tag_list if isinstance(tag.get("Key"), str)}
        actual_version = tag_by_name_map.get("aws:ec2launchtemplate:version")
        if not isinstance(actual_version, str) or not actual_version.isdigit():
            raise DevelopmentEnvironmentError("EC2 instance has no exact launch-template version")
        return actual_version

    def launch_template_update_is_pending(self) -> bool:
        """Return whether new launch input requires controlled replacement."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        active_version = output_by_name_map.get("InstanceLaunchTemplateVersion")
        latest_version = output_by_name_map.get("LatestLaunchTemplateVersion")
        if (
            not isinstance(active_version, str)
            or not active_version.isdigit()
            or not isinstance(latest_version, str)
            or not latest_version.isdigit()
        ):
            raise DevelopmentEnvironmentError("Compute stack launch-template outputs are malformed")
        return active_version != latest_version

    def launch_template_version_validate(
        self,
        *,
        require_latest: bool = True,
    ) -> None:
        """Prove instance uses declared active and optionally latest version."""

        output_by_name_map = self._stack.output_by_name_map_get(self._identity.compute_stack_name)
        active_version = output_by_name_map.get("InstanceLaunchTemplateVersion")
        latest_version = output_by_name_map.get("LatestLaunchTemplateVersion")
        actual_version = self.launch_template_version_get()
        if not isinstance(active_version, str) or not active_version.isdigit() or actual_version != active_version:
            raise DevelopmentEnvironmentError("EC2 instance does not use the declared launch-template version")
        if require_latest and (
            not isinstance(latest_version, str) or not latest_version.isdigit() or active_version != latest_version
        ):
            raise DevelopmentEnvironmentError("EC2 instance does not use the exact latest launch-template version")

    def remote_text_write(
        self,
        *,
        remote_path: Path,
        text: str,
        ssh_control_path: Path,
    ) -> None:
        """Install one generated non-secret text file through SSH-over-SSM."""

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as file:
            file.write(text)
            local_path = Path(file.name)
        try:
            self._runner.run(
                [
                    "scp",
                    "-o",
                    f"ControlPath={ssh_control_path}",
                    str(local_path),
                    f"{self._identity.instance_name}:/tmp/{remote_path.name}",
                ]
            )
            self._transport.ssh_run(
                [
                    "sudo",
                    "install",
                    "-D",
                    "-m",
                    "0644",
                    f"/tmp/{remote_path.name}",
                    str(remote_path),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._transport.ssh_run(
                ["rm", "-f", f"/tmp/{remote_path.name}"],
                ssh_control_path=ssh_control_path,
            )
        finally:
            local_path.unlink(missing_ok=True)

    def runtime_platform_get(self, ssh_control_path: Path) -> str:
        """Return the one exact WorkflowRun-eligible node platform."""

        result = self._transport.ssh_run(
            [
                "sudo",
                "k3s",
                "kubectl",
                "get",
                "nodes",
                "-l",
                "apwid.com/workflow-run-eligible=true",
                "-o",
                "json",
            ],
            ssh_control_path=ssh_control_path,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError("Kubernetes node platform response is invalid") from error
        item_list = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(item_list, list) or not item_list:
            raise DevelopmentEnvironmentError("No WorkflowRun-eligible Kubernetes nodes exist")
        platform_set: set[str] = set()
        for item in item_list:
            if not isinstance(item, dict):
                raise DevelopmentEnvironmentError("Kubernetes node platform response is malformed")
            node_info = item.get("status", {}).get("nodeInfo", {})
            operating_system = node_info.get("operatingSystem")
            architecture = node_info.get("architecture")
            if operating_system != "linux" or architecture not in {
                "amd64",
                "arm64",
            }:
                raise DevelopmentEnvironmentError(
                    "Unsupported WorkflowRun node platform " f"{operating_system}/{architecture}"
                )
            platform_set.add(f"{operating_system}/{architecture}")
        if len(platform_set) != 1:
            raise DevelopmentEnvironmentError(f"WorkflowRun node platforms are mixed: {sorted(platform_set)}")
        return next(iter(platform_set))
