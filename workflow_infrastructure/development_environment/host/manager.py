"""Own host-local development lifecycle, activity, and installation behavior."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.storage import (
    DevelopmentHostLifecycle,
)
from workflow_infrastructure.development_environment.host.manifest import (
    HostArtifactResolutionError,
    host_artifact_manifest_json_decode,
)

HELM_BINARY_PATH = Path("/usr/local/bin/helm")
HOST_ARTIFACT_MANIFEST_PATH = Path("/etc/workflow-control-center/host-artifact-manifest.json")
HOST_ARTIFACT_MANIFEST_SHA256_PATH = Path("/etc/workflow-control-center/host-artifact-manifest.sha256")
HOST_PYTHON_PATH = Path("/usr/local/bin/python3.14")
PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT = "PYTHONDONTWRITEBYTECODE=1"


class ClockProtocol(Protocol):
    """Clock surface consumed by host lifecycle."""

    def now(self) -> datetime:
        """Return the current UTC instant."""

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a bounded duration."""


class CommandResultProtocol(Protocol):
    """Command result surface consumed by host lifecycle."""

    returncode: int
    stderr: str
    stdout: str


class CommandRunnerProtocol(Protocol):
    """Command boundary consumed by host lifecycle."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> CommandResultProtocol:
        """Run one host-local command."""


class EnvironmentIdentityProtocol(Protocol):
    """Environment paths and names consumed by host lifecycle."""

    environment_name: str
    host_control_infrastructure_source_path: Path
    host_release_root_path: Path
    host_retained_root_path: Path
    host_state_root_path: Path


class ProductReleaseProtocol(Protocol):
    """Retained Product release boundary consumed by host lifecycle."""

    def current_product_tool_command_list_get(
        self,
        command: str,
        *argument_list: str,
    ) -> list[str]:
        """Return one exact Product tool command."""

    def current_product_tool_path_get(self) -> Path:
        """Return the exact current Product tool path."""

    def recovery_status_get(self) -> str:
        """Return retained Product recovery state."""


class StopLeaseProtocol(Protocol):
    """Renewable stop-lease boundary consumed by host lifecycle."""

    def upsert(self, *, duration: timedelta | None = None) -> None:
        """Create or renew the stop lease."""


class DevelopmentHostManager:
    """Own behavior that executes inside one development EC2 host."""

    def __init__(
        self,
        *,
        aws_region: str,
        clock: ClockProtocol,
        identity: EnvironmentIdentityProtocol,
        is_host: bool,
        product_release: ProductReleaseProtocol,
        project_root_path: Path,
        runner: CommandRunnerProtocol,
        stop_lease: StopLeaseProtocol,
    ) -> None:
        """Initialize host-local lifecycle from explicit collaborators."""

        self._aws_region = aws_region
        self._clock = clock
        self._identity = identity
        self._is_host = is_host
        self._product_release = product_release
        self._project_root_path = project_root_path
        self._runner = runner
        self._stop_lease = stop_lease

    def controller(self) -> None:
        """Run the fail-safe host lifecycle controller until shutdown."""

        DevelopmentHostLifecycle(
            clock=self._clock,
            host_port=self,
            retained_root_path=self._identity.host_retained_root_path,
            state_root_path=self._identity.host_state_root_path,
        ).run(instance_id=self.instance_metadata_get("instance-id"))

    def host_node_uncordon(self) -> None:
        """Allow workloads to schedule after host-controller startup."""

        self._runner.run(
            ["k3s", "kubectl", "uncordon", self._node_name_get()],
            check=False,
        )

    def host_product_activity_get(self) -> str:
        """Return fail-closed Product activity for the lifecycle controller."""

        try:
            if self._product_release.recovery_status_get() == "pending":
                return "busy"
        except DevelopmentEnvironmentError:
            return "busy"
        product_tool_path = self._product_release.current_product_tool_path_get()
        if not product_tool_path.is_file():
            return "busy"
        result = self._runner.run(
            self._product_release.current_product_tool_command_list_get("activity"),
            check=False,
        )
        if result.returncode != 0:
            return "busy"
        try:
            observation = json.loads(result.stdout)
            status = observation["status"]
            reason_key_list = observation["reason_key_list"]
            t_observed = datetime.fromisoformat(observation["t_observed"])
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            return "busy"
        if (
            status not in {"busy", "idle"}
            or not isinstance(reason_key_list, list)
            or any(
                not isinstance(reason_key, str)
                or reason_key
                not in {
                    "controller_unavailable",
                    "persisted_work",
                    "probe_unavailable",
                    "recovery",
                }
                for reason_key in reason_key_list
            )
            or t_observed.utcoffset() != timedelta(0)
            or (status == "idle" and reason_key_list)
            or (status == "busy" and not reason_key_list)
        ):
            return "busy"
        return status

    def host_product_maintenance_run(self) -> bool:
        """Run Product retention only after the lifecycle owner proves idle."""

        result = self._runner.run(
            self._product_release.current_product_tool_command_list_get("maintenance"),
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            print(f"WARNING: Product maintenance failed: {message}")
            return False
        print("OK: idle Product retention maintenance completed")
        return True

    def host_session_is_busy(self, instance_id: str) -> bool:
        """Fail closed unless Session Manager proves that no session exists."""

        try:
            return self._active_session_count_get(instance_id) > 0
        except DevelopmentEnvironmentError:
            return True

    def stop_lease_renew(self) -> None:
        """Renew the ordinary environment stop lease."""

        self._stop_lease.upsert()

    def prepare(self) -> None:
        """Validate exact source-owned host dependencies before Product deploy."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-prepare is supported only from an exact source release " "on the development host"
            )
        host_artifact_manifest = self.host_artifact_manifest_get()
        if self._project_root_path.is_relative_to(self._identity.host_release_root_path):
            source_manifest_path = self._project_root_path.parent.parent / "source-manifest.json"
            try:
                source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DevelopmentEnvironmentError(
                    "Product source manifest is unavailable during host preparation"
                ) from error
            if (
                not isinstance(source_manifest, Mapping)
                or source_manifest.get("environment_name") != self._identity.environment_name
                or source_manifest.get("host_artifact_manifest") != host_artifact_manifest
            ):
                raise DevelopmentEnvironmentError("Product release and active host artifact identities differ")
        helm_version = self._helm_validate(host_artifact_manifest)
        print(f"OK: exact Helm {helm_version} is installed")

    def host_artifact_manifest_get(self) -> dict[str, object]:
        """Return the immutable launch manifest installed on this exact host."""

        try:
            manifest_json = HOST_ARTIFACT_MANIFEST_PATH.read_text(encoding="utf-8")
            expected_sha256 = HOST_ARTIFACT_MANIFEST_SHA256_PATH.read_text(encoding="utf-8").strip()
            manifest = host_artifact_manifest_json_decode(
                manifest_json=manifest_json,
                expected_sha256=expected_sha256,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError("Host artifact manifest is unavailable") from error
        except HostArtifactResolutionError as error:
            raise DevelopmentEnvironmentError(f"Host artifact manifest is invalid: {error}") from error
        return manifest

    def install(self) -> None:
        """Install the source-owned host controller from the exact release."""

        self.prepare()
        infrastructure_source_path = self._identity.host_control_infrastructure_source_path
        runtime_home_path = self._identity.host_state_root_path / "home"
        for path in (self._identity.host_state_root_path, runtime_home_path):
            if path.is_symlink():
                raise DevelopmentEnvironmentError("Host controller state path must not be a symbolic link: " f"{path}")
        self._identity.host_state_root_path.mkdir(mode=0o750, parents=True, exist_ok=True)
        runtime_home_path.mkdir(mode=0o700, exist_ok=True)
        for path in (self._identity.host_state_root_path, runtime_home_path):
            if path.is_symlink():
                raise DevelopmentEnvironmentError("Host controller state path must not be a symbolic link: " f"{path}")
        os.chmod(self._identity.host_state_root_path, 0o750)
        os.chmod(runtime_home_path, 0o700)
        service_path = Path("/etc/systemd/system/workflow-control-center-host-controller.service")
        service_text = f"""[Unit]
Description=Workflow Control Center development host lifecycle controller
After=k3s.service network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment={PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT}
Environment=HOME={runtime_home_path}
WorkingDirectory={self._identity.host_state_root_path}
ExecStart={HOST_PYTHON_PATH} -B {infrastructure_source_path}/development_environment_manage.py host-controller --environment-name {self._identity.environment_name}
Restart=always
RestartSec=30
User=root

[Install]
WantedBy=multi-user.target
"""
        service_path.write_text(service_text, encoding="utf-8")
        os.chmod(service_path, 0o644)
        self._runner.run(["systemctl", "daemon-reload"])
        self._runner.run(["systemctl", "enable", "workflow-control-center-host-controller"])
        self._runner.run(["systemctl", "restart", "workflow-control-center-host-controller"])
        print("OK: host lifecycle controller is installed")

    def host_shutdown(self) -> None:
        """Gracefully stop Product workloads and power off the host."""

        product_tool_path = self._product_release.current_product_tool_path_get()
        if product_tool_path.is_file():
            result = self._runner.run(
                self._product_release.current_product_tool_command_list_get("shutdown"),
                check=False,
                should_capture=False,
            )
            if result.returncode != 0:
                self._runner.run(
                    ["k3s", "kubectl", "uncordon", self._node_name_get()],
                    check=False,
                )
                raise DevelopmentEnvironmentError("Product graceful shutdown failed; node was uncordoned")
        else:
            self._runner.run(["systemctl", "stop", "k3s"], check=False)
        self._runner.run(["systemctl", "poweroff"], should_capture=False)

    def instance_metadata_get(self, path: str) -> str:
        """Read one nonempty value through IMDSv2."""

        token_result = self._runner.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--request",
                "PUT",
                "--header",
                "X-aws-ec2-metadata-token-ttl-seconds: 21600",
                "http://169.254.169.254/latest/api/token",
            ]
        )
        result = self._runner.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--header",
                f"X-aws-ec2-metadata-token: {token_result.stdout.strip()}",
                f"http://169.254.169.254/latest/meta-data/{path}",
            ]
        )
        value = result.stdout.strip()
        if not value:
            raise DevelopmentEnvironmentError(f"Instance metadata {path} is empty")
        return value

    def _active_session_count_get(self, instance_id: str) -> int:
        """Return active Session Manager session count through the instance role."""

        result = self._runner.run(
            [
                "aws",
                "ssm",
                "describe-sessions",
                "--region",
                self._aws_region,
                "--state",
                "Active",
                "--filters",
                f"key=Target,value={instance_id}",
                "--output",
                "json",
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError("Host Session Manager response is invalid") from error
        session_list = payload.get("Sessions", []) if isinstance(payload, dict) else []
        if not isinstance(session_list, list):
            raise DevelopmentEnvironmentError("Host Session Manager response is malformed")
        return len(session_list)

    def _node_name_get(self) -> str:
        """Return the single local k3s node name."""

        result = self._runner.run(
            [
                "k3s",
                "kubectl",
                "get",
                "node",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ]
        )
        node_name = result.stdout.strip()
        if not node_name:
            raise DevelopmentEnvironmentError("Kubernetes node name is empty")
        return node_name

    def _helm_validate(
        self,
        host_artifact_manifest: Mapping[str, object],
    ) -> str:
        """Validate preinstalled Helm against immutable launch input."""

        artifact_by_name_map = host_artifact_manifest.get("artifact_by_name_map")
        helm_artifact = artifact_by_name_map.get("helm") if isinstance(artifact_by_name_map, dict) else None
        helm_version = helm_artifact.get("version") if isinstance(helm_artifact, dict) else None
        if not isinstance(helm_version, str) or re.fullmatch(r"v4\.[0-9]+\.[0-9]+", helm_version) is None:
            raise DevelopmentEnvironmentError("Host artifact manifest has no exact Helm identity")
        if not HELM_BINARY_PATH.is_file():
            raise DevelopmentEnvironmentError("Exact Helm binary was not installed by host bootstrap")
        installed_result = self._runner.run(
            [
                str(HELM_BINARY_PATH),
                "version",
                "--template",
                "{{.Version}}",
            ],
            check=False,
        )
        if installed_result.returncode != 0 or installed_result.stdout.strip() != helm_version:
            raise DevelopmentEnvironmentError("Installed Helm version differs from immutable launch input")
        return helm_version
