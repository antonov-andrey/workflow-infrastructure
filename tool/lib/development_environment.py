"""Own provisioning, access, delivery, lifecycle, and recovery of the development environment."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shlex
import subprocess
import tarfile
import tempfile
import time

AWS_ACCOUNT_ID = "463564115167"
AWS_PROFILE = "workflow-control-center-devel"
AWS_REGION = "us-east-1"
COMPUTE_STACK_NAME = "workflow-control-center-development-compute"
COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET = frozenset(
    {
        "DevelopmentInstance",
        "RetainedVolume",
        "RetainedVolumeAttachment",
    }
)
DATA_PLANE_STACK_NAME = "workflow-control-center-development"
HOST_CONTROL_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/control/current")
HOST_CONTROL_RELEASE_ROOT_PATH = Path("/opt/workflow-infrastructure/control/releases")
HOST_CURRENT_SOURCE_PATH = Path("/opt/workflow-infrastructure/current")
HOST_RETAINED_RELEASE_ROOT_PATH = Path("/srv/workflow-control-center/release")
HOST_RETAINED_CURRENT_RELEASE_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "current"
HOST_RELEASE_ROOT_PATH = HOST_RETAINED_RELEASE_ROOT_PATH / "releases"
HOST_STATE_ROOT_PATH = Path("/var/lib/workflow-infrastructure")
HELM_BINARY_PATH = Path("/usr/local/bin/helm")
HELM_VERSION = "v4.2.3"
HELM_RELEASE_BY_MACHINE_MAP = {
    "aarch64": (
        "arm64",
        "21abd9354d39b2cd79a8d76be6912cd137a983cbf997193503fb8a6a6e2f2785",
    ),
    "x86_64": (
        "amd64",
        "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c",
    ),
}
INSTANCE_NAME = "workflow-control-center-development"
LEASE_DURATION = timedelta(hours=2)
LEASE_GROUP_NAME = "workflow-control-center-development"
LEASE_NAME = "workflow-control-center-development-stop"
LEASE_RENEW_INTERVAL = timedelta(minutes=30)
PRODUCT_SOURCE_REPOSITORY_NAME_LIST = [
    "browser-runtime",
    "vpn-runtime",
    "workflow-container-contract",
    "workflow-container-runtime",
    "workflow-control-center",
]
REPOSITORY_URL_BY_NAME_MAP = {
    "browser-runtime": "git@github.com:antonov-andrey/browser-runtime.git",
    "vpn-runtime": "git@github.com:antonov-andrey/vpn-runtime.git",
    "workflow-container-contract": "git@github.com:antonov-andrey/workflow-container-contract.git",
    "workflow-container-runtime": "git@github.com:antonov-andrey/workflow-container-runtime.git",
    "workflow-control-center": "git@github.com:antonov-andrey/workflow-control-center.git",
    "workflow-infrastructure": "git@github.com:antonov-andrey/workflow-infrastructure.git",
}
SSM_DOCUMENT_PORT_FORWARD = "AWS-StartPortForwardingSession"
SSM_COMMAND_TIMEOUT_SECONDS = 3600
SSM_ONLINE_TIMEOUT_SECONDS = 1800
STACK_POLL_INTERVAL_SECONDS = 5
STACK_TIMEOUT_SECONDS = 3600


class CommandRunner:
    """Run external commands through one explicit process boundary."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command and return its completed process.

        Args:
            command_list: Complete argument vector.
            check: Whether a non-zero exit raises an exception.
            input_text: Optional standard-input text.
            should_capture: Whether to capture standard output and standard error.

        Returns:
            Completed process.
        """

        try:
            return subprocess.run(
                list(command_list),
                capture_output=should_capture,
                check=check,
                input=input_text,
                text=True,
            )
        except OSError as error:
            raise DevelopmentEnvironmentError(
                f"Unable to execute {command_list[0]}: {error}"
            ) from error
        except subprocess.CalledProcessError as error:
            error_text = (
                error.stderr or error.stdout or f"exit {error.returncode}"
            ).strip()
            raise DevelopmentEnvironmentError(
                f"{command_list[0]} failed: {error_text}"
            ) from error


class Clock:
    """Expose UTC time, monotonic time, and controlled waiting."""

    def monotonic(self) -> float:
        """Return the current monotonic clock value.

        Returns:
            Monotonic seconds.
        """

        return time.monotonic()

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant.

        Returns:
            Current UTC instant.
        """

        return datetime.now(UTC)

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a non-negative duration.

        Args:
            delay_seconds: Duration in seconds.
        """

        time.sleep(delay_seconds)


class DevelopmentEnvironment:
    """Own the complete bounded development-environment workflow."""

    def __init__(
        self, *, clock: Clock, project_root_path: Path, runner: CommandRunner
    ) -> None:
        """Initialize the environment workflow.

        Args:
            clock: UTC and monotonic time boundary.
            project_root_path: Root of the workflow-infrastructure checkout.
            runner: External process boundary.
        """

        self._clock = clock
        self._is_host = project_root_path.is_relative_to(
            HOST_CONTROL_RELEASE_ROOT_PATH
        ) or project_root_path.is_relative_to(HOST_RELEASE_ROOT_PATH)
        self._project_root_path = project_root_path
        self._runner = runner
        self._workspace_root_path = project_root_path.parent

    def apply(self) -> None:
        """Validate, plan, apply, and verify the data-plane and compute stacks."""

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        self._cost_review_record()
        self._stack_drift_validate(DATA_PLANE_STACK_NAME)
        data_resource_id_by_logical_name_map = (
            self._stack_resource_id_by_logical_name_map_get(DATA_PLANE_STACK_NAME)
        )
        self._template_validate(
            self._project_root_path
            / "cloudformation/workflow-control-center-development.yaml"
        )
        self._template_validate(
            self._project_root_path
            / "cloudformation/workflow-control-center-development-compute.yaml"
        )
        self._stack_apply(
            stack_name=DATA_PLANE_STACK_NAME,
            template_path=self._project_root_path
            / "cloudformation/workflow-control-center-development.yaml",
            parameter_by_name_map={"UiOrigin": "http://localhost:8080"},
            must_preserve_resource=True,
        )
        if data_resource_id_by_logical_name_map:
            current_resource_id_by_logical_name_map = (
                self._stack_resource_id_by_logical_name_map_get(DATA_PLANE_STACK_NAME)
            )
            if (
                current_resource_id_by_logical_name_map
                != data_resource_id_by_logical_name_map
            ):
                raise DevelopmentEnvironmentError(
                    "Stable data-plane physical resource identity changed"
                )
        compute_parameter_by_name_map: dict[str, str] = {}
        if not self._stack_payload_get(COMPUTE_STACK_NAME, is_required=False):
            compute_parameter_by_name_map.update(
                self._replacement_guard_parameter_by_name_map_get()
            )
        self._stack_apply(
            stack_name=COMPUTE_STACK_NAME,
            template_path=self._project_root_path
            / "cloudformation/workflow-control-center-development-compute.yaml",
            parameter_by_name_map=compute_parameter_by_name_map,
            must_preserve_resource=False,
            protected_identity_logical_id_set=COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET,
        )
        self._retained_volume_attachment_validate()
        self._instance_launch_template_version_validate()
        self.start()
        self._replacement_guard_disable()
        self._infrastructure_source_publish()
        self._stack_drift_validate(DATA_PLANE_STACK_NAME)
        self._stack_drift_validate(COMPUTE_STACK_NAME)
        print("OK: development data-plane and compute stacks are applied")

    def connect(self) -> int:
        """Open the Product HTTP tunnel through Session Manager.

        Returns:
            Foreground Session Manager process status.
        """

        self._local_operator_context_validate()
        instance_id = self._instance_id_get()
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "start-session",
                "--profile",
                AWS_PROFILE,
                "--region",
                AWS_REGION,
                "--target",
                instance_id,
                "--document-name",
                SSM_DOCUMENT_PORT_FORWARD,
                "--parameters",
                json.dumps(
                    {"localPortNumber": ["8080"], "portNumber": ["8080"]},
                    separators=(",", ":"),
                ),
            ],
            check=False,
            should_capture=False,
        )
        return result.returncode

    def console(self) -> int:
        """Open an ordinary Session Manager console.

        Returns:
            Foreground Session Manager process status.
        """

        self._local_operator_context_validate()
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "start-session",
                "--profile",
                AWS_PROFILE,
                "--region",
                AWS_REGION,
                "--target",
                self._instance_id_get(),
            ],
            check=False,
            should_capture=False,
        )
        return result.returncode

    def deploy(self) -> None:
        """Publish exact Product sources and invoke the Product-owned deployment."""

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        for repository_name in PRODUCT_SOURCE_REPOSITORY_NAME_LIST:
            self._source_repository_validate(
                self._workspace_root_path / repository_name, repository_name
            )
        self._instance_online_wait()
        release_name = self._clock.now().strftime("%Y%m%d%H%M%S%f")
        source_manifest_by_repository_name_map: dict[str, dict[str, object]] = {}
        with self._ssh_control_session() as ssh_control_path:
            repository_name_list = [
                "workflow-infrastructure",
                *PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
            ]
            for repository_name in repository_name_list:
                repository_path = (
                    self._project_root_path
                    if repository_name == "workflow-infrastructure"
                    else self._workspace_root_path / repository_name
                )
                source_manifest_by_repository_name_map[repository_name] = (
                    self._source_archive_publish(
                        repository_name=repository_name,
                        repository_path=repository_path,
                        release_name=release_name,
                        remote_release_root_path=HOST_RELEASE_ROOT_PATH,
                        ssh_control_path=ssh_control_path,
                    )
                )
            release_manifest_text = json.dumps(
                {
                    "release": release_name,
                    "repository_by_name_map": source_manifest_by_repository_name_map,
                    "t_deploy": self._clock.now().isoformat().replace("+00:00", "Z"),
                },
                indent=2,
                sort_keys=True,
            )
            self._remote_text_write(
                remote_path=HOST_RELEASE_ROOT_PATH
                / release_name
                / "source-manifest.json",
                text=release_manifest_text,
                ssh_control_path=ssh_control_path,
            )
            release_root_path = HOST_RELEASE_ROOT_PATH / release_name
            self._ssh_run(
                [
                    "sudo",
                    "python3.14",
                    str(
                        release_root_path
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-prepare",
                ],
                ssh_control_path=ssh_control_path,
            )
            platform = self._runtime_platform_get(ssh_control_path)
            product_command_list = [
                "sudo",
                "python3.14",
                str(
                    release_root_path
                    / "sources"
                    / "workflow-control-center"
                    / "tool"
                    / "development_kubernetes_manage.py"
                ),
                "deploy",
                "--release",
                release_name,
                "--source-root",
                str(release_root_path / "sources"),
                "--target-platform",
                platform,
            ]
            self._ssh_run(
                product_command_list,
                ssh_control_path=ssh_control_path,
                should_capture=False,
            )
            self._ssh_run(
                [
                    "sudo",
                    "python3.14",
                    str(
                        release_root_path
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-product-release-activate",
                    "--release",
                    release_name,
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "install",
                    "-d",
                    "-m",
                    "0755",
                    str(HOST_CONTROL_RELEASE_ROOT_PATH),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "ln",
                    "-sfn",
                    str(release_root_path),
                    str(HOST_CONTROL_CURRENT_SOURCE_PATH),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "python3.14",
                    str(
                        HOST_CURRENT_SOURCE_PATH
                        / "sources"
                        / "workflow-control-center"
                        / "tool"
                        / "development_kubernetes_manage.py"
                    ),
                    "host-install",
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "python3.14",
                    str(
                        HOST_CONTROL_CURRENT_SOURCE_PATH
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-install",
                ],
                ssh_control_path=ssh_control_path,
            )
        print(f"OK: exact Product release {release_name} is deployed for {platform}")

    def diagnose(self) -> None:
        """Print bounded infrastructure and Product diagnostics without secret values."""

        self._local_operator_context_validate()
        self.status()
        instance_id = self._instance_id_get()
        if self._instance_state_get(instance_id) != "running":
            print("OK: remote diagnostics skipped because the instance is not running")
            return
        self._ssm_shell_run(
            [
                "df -h / /srv/workflow-control-center",
                "sudo systemctl --no-pager --full status k3s workflow-control-center-host-controller || true",
                "sudo k3s kubectl get nodes,namespaces -o wide",
                "sudo k3s kubectl get pods --all-namespaces -o wide",
                "sudo k3s kubectl get events --all-namespaces --sort-by=.lastTimestamp | tail -200",
                (
                    f"sudo python3.14 {HOST_CURRENT_SOURCE_PATH}/sources/workflow-control-center/"
                    "tool/development_kubernetes_manage.py diagnose || true"
                ),
            ]
        )

    def host_controller(self) -> None:
        """Run the fail-safe host lifecycle controller until shutdown."""

        instance_id = self._instance_metadata_get("instance-id")
        self._runner.run(
            ["k3s", "kubectl", "uncordon", self._host_node_name_get()], check=False
        )
        idle_start_path = HOST_STATE_ROOT_PATH / "idle-start"
        HOST_STATE_ROOT_PATH.mkdir(mode=0o750, parents=True, exist_ok=True)
        idle_start_path.unlink(missing_ok=True)
        t_last_lease_renew = datetime.min.replace(tzinfo=UTC)
        while True:
            t_now = self._clock.now()
            have_session = self._host_active_session_count_get(instance_id) > 0
            product_activity = self._host_product_activity_get()
            is_busy = have_session or product_activity == "busy"
            if is_busy:
                idle_start_path.unlink(missing_ok=True)
                if t_now - t_last_lease_renew >= LEASE_RENEW_INTERVAL:
                    self._stop_lease_upsert()
                    t_last_lease_renew = t_now
            else:
                if not idle_start_path.exists():
                    idle_start_path.write_text(t_now.isoformat(), encoding="utf-8")
                    os.chmod(idle_start_path, 0o600)
                idle_start_text = idle_start_path.read_text(encoding="utf-8").strip()
                try:
                    t_idle_start = datetime.fromisoformat(idle_start_text)
                except ValueError:
                    idle_start_path.unlink(missing_ok=True)
                    self._clock.sleep(60)
                    continue
                if t_now - t_idle_start >= timedelta(minutes=30):
                    have_session = self._host_active_session_count_get(instance_id) > 0
                    product_activity = self._host_product_activity_get()
                    if not have_session and product_activity == "idle":
                        self.host_shutdown()
                        return
                    idle_start_path.unlink(missing_ok=True)
            self._clock.sleep(60)

    def host_prepare(self) -> None:
        """Install exact source-owned host dependencies required before Product deploy."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-prepare is supported only from an exact source release on the development host"
            )
        self._helm_ensure()
        print(f"OK: exact Helm {HELM_VERSION} is installed")

    @staticmethod
    def _atomic_symlink_replace(*, link_path: Path, target_path: Path) -> None:
        """Atomically replace one host symlink without exposing a missing-current gap."""

        link_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary_link_path = link_path.with_name(
            f".{link_path.name}.tmp-{os.getpid()}"
        )
        temporary_link_path.unlink(missing_ok=True)
        temporary_link_path.symlink_to(target_path)
        try:
            os.replace(temporary_link_path, link_path)
        finally:
            temporary_link_path.unlink(missing_ok=True)

    def _retained_product_release_validate(self, release_root_path: Path) -> str:
        """Validate every persisted identity and tracked source byte of one Product release."""

        try:
            resolved_release_root_path = release_root_path.resolve(strict=True)
            resolved_release_parent_path = HOST_RELEASE_ROOT_PATH.resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Retained Product release path is unavailable"
            ) from error
        release_name = resolved_release_root_path.name
        if (
            resolved_release_root_path.parent != resolved_release_parent_path
            or not release_name.isdigit()
            or len(release_name) not in {17, 20}
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release path has an invalid exact identity"
            )
        source_manifest_path = resolved_release_root_path / "source-manifest.json"
        product_manifest_path = resolved_release_root_path / "release-manifest.json"
        render_path = resolved_release_root_path / "render.yaml"
        try:
            source_manifest_bytes = source_manifest_path.read_bytes()
            source_manifest = json.loads(source_manifest_bytes)
            product_manifest = json.loads(
                product_manifest_path.read_text(encoding="utf-8")
            )
            render_bytes = render_path.read_bytes()
        except (OSError, json.JSONDecodeError) as error:
            raise DevelopmentEnvironmentError(
                "Retained Product release manifests are unavailable or malformed"
            ) from error
        if (
            not isinstance(source_manifest, Mapping)
            or not isinstance(product_manifest, Mapping)
            or source_manifest.get("release") != release_name
            or product_manifest.get("release") != release_name
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release manifests have inconsistent identities"
            )
        if (
            product_manifest.get("source_manifest_sha256")
            != hashlib.sha256(source_manifest_bytes).hexdigest()
            or product_manifest.get("render_sha256")
            != hashlib.sha256(render_bytes).hexdigest()
        ):
            raise DevelopmentEnvironmentError(
                "Retained Product release manifest digests are inconsistent"
            )
        target_platform = product_manifest.get("target_platform")
        if target_platform not in {"linux/amd64", "linux/arm64"}:
            raise DevelopmentEnvironmentError(
                "Retained Product release target platform is invalid"
            )

        repository_by_name_map = source_manifest.get("repository_by_name_map")
        required_repository_name_set = {
            "workflow-infrastructure",
            *PRODUCT_SOURCE_REPOSITORY_NAME_LIST,
        }
        if not isinstance(repository_by_name_map, Mapping) or set(
            repository_by_name_map
        ) != (required_repository_name_set):
            raise DevelopmentEnvironmentError(
                "Retained Product source graph is incomplete"
            )
        source_identity_by_name_map: dict[str, dict[str, str]] = {}
        source_root_path = resolved_release_root_path / "sources"
        for repository_name, repository_payload in repository_by_name_map.items():
            if not isinstance(repository_name, str) or not isinstance(
                repository_payload, Mapping
            ):
                raise DevelopmentEnvironmentError(
                    "Retained Product source entry is malformed"
                )
            source_identity: dict[str, str] = {}
            for field_name, expected_length in (
                ("archive_sha256", 64),
                ("commit_sha", 40),
                ("repository_url", 0),
            ):
                field_value = repository_payload.get(field_name)
                if not isinstance(field_value, str) or not field_value:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} {field_name} is invalid"
                    )
                if expected_length:
                    if (
                        len(field_value) != expected_length
                        or field_value != field_value.lower()
                    ):
                        raise DevelopmentEnvironmentError(
                            f"Retained Product source {repository_name} {field_name} is invalid"
                        )
                    try:
                        int(field_value, 16)
                    except ValueError as error:
                        raise DevelopmentEnvironmentError(
                            f"Retained Product source {repository_name} {field_name} is invalid"
                        ) from error
                source_identity[field_name] = field_value
            if (
                source_identity["repository_url"]
                != REPOSITORY_URL_BY_NAME_MAP[repository_name]
            ):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} repository URL is invalid"
                )
            file_sha256_by_path_map = repository_payload.get("file_sha256_by_path_map")
            if not isinstance(file_sha256_by_path_map, Mapping):
                raise DevelopmentEnvironmentError(
                    f"Retained Product source {repository_name} file graph is invalid"
                )
            repository_root_path = source_root_path / repository_name
            for relative_path_text, expected_sha256 in file_sha256_by_path_map.items():
                if (
                    not isinstance(relative_path_text, str)
                    or not relative_path_text
                    or not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                    or expected_sha256 != expected_sha256.lower()
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} file identity is invalid"
                    )
                try:
                    int(expected_sha256, 16)
                except ValueError as error:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} file identity is invalid"
                    ) from error
                relative_path = PurePosixPath(relative_path_text)
                if (
                    relative_path.is_absolute()
                    or not relative_path.parts
                    or relative_path.as_posix() != relative_path_text
                    or any(part in {"", ".", ".."} for part in relative_path.parts)
                ):
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source {repository_name} path is unsafe"
                    )
                source_path = repository_root_path.joinpath(*relative_path.parts)
                try:
                    source_payload = (
                        os.readlink(source_path).encode()
                        if source_path.is_symlink()
                        else source_path.read_bytes()
                    )
                except OSError as error:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source file is unavailable: "
                        f"{repository_name}/{relative_path_text}"
                    ) from error
                if hashlib.sha256(source_payload).hexdigest() != expected_sha256:
                    raise DevelopmentEnvironmentError(
                        f"Retained Product source file digest differs: "
                        f"{repository_name}/{relative_path_text}"
                    )
            source_identity_by_name_map[repository_name] = source_identity
        if product_manifest.get("source_by_name_map") != source_identity_by_name_map:
            raise DevelopmentEnvironmentError(
                "Retained Product and source manifests describe different source identities"
            )
        return release_name

    def host_product_release_activate(self, release_name: str) -> None:
        """Validate and atomically activate one accepted retained Product release."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-release-activate is supported only on the development host"
            )
        release_root_path = HOST_RELEASE_ROOT_PATH / release_name
        accepted_release_name = self._retained_product_release_validate(
            release_root_path
        )
        if accepted_release_name != release_name:
            raise DevelopmentEnvironmentError(
                "Retained Product release activation changed exact identity"
            )
        self._atomic_symlink_replace(
            link_path=HOST_RETAINED_CURRENT_RELEASE_PATH,
            target_path=release_root_path,
        )
        self._atomic_symlink_replace(
            link_path=HOST_CURRENT_SOURCE_PATH,
            target_path=HOST_RETAINED_CURRENT_RELEASE_PATH,
        )
        print(f"OK: retained Product release {release_name} is current")

    def host_product_release_restore(self) -> None:
        """Validate the snapshot-owned current release and restore its root-volume link."""

        if not self._is_host:
            raise DevelopmentEnvironmentError(
                "host-product-release-restore is supported only on the development host"
            )
        if not HOST_RETAINED_CURRENT_RELEASE_PATH.is_symlink():
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link is unavailable"
            )
        try:
            release_root_path = HOST_RETAINED_CURRENT_RELEASE_PATH.resolve(strict=True)
        except OSError as error:
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link is broken"
            ) from error
        if os.readlink(HOST_RETAINED_CURRENT_RELEASE_PATH) != str(release_root_path):
            raise DevelopmentEnvironmentError(
                "Retained Product current-release link is not an exact absolute target"
            )
        release_name = self._retained_product_release_validate(release_root_path)
        self._atomic_symlink_replace(
            link_path=HOST_CURRENT_SOURCE_PATH,
            target_path=HOST_RETAINED_CURRENT_RELEASE_PATH,
        )
        print(
            f"OK: retained Product release {release_name} root-volume link is restored"
        )

    def _helm_ensure(self) -> None:
        """Install the pinned Helm release atomically after archive verification."""

        if HELM_BINARY_PATH.is_file():
            current_result = self._runner.run(
                [
                    str(HELM_BINARY_PATH),
                    "version",
                    "--template",
                    "{{.Version}}",
                ],
                check=False,
            )
            if (
                current_result.returncode == 0
                and current_result.stdout.strip() == HELM_VERSION
            ):
                return
        machine = platform.machine()
        release = HELM_RELEASE_BY_MACHINE_MAP.get(machine)
        if release is None:
            raise DevelopmentEnvironmentError(
                f"Unsupported Helm host architecture {machine}"
            )
        archive_architecture, expected_sha256 = release
        archive_name = f"helm-{HELM_VERSION}-linux-{archive_architecture}.tar.gz"
        member_name = f"linux-{archive_architecture}/helm"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            archive_path = temporary_root_path / archive_name
            self._runner.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--location",
                    f"https://get.helm.sh/{archive_name}",
                    "-o",
                    str(archive_path),
                ]
            )
            actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if actual_sha256 != expected_sha256:
                raise DevelopmentEnvironmentError(
                    "Downloaded Helm archive digest does not match the pinned release"
                )
            with tarfile.open(archive_path, "r:gz") as archive:
                try:
                    member = archive.getmember(member_name)
                except KeyError as error:
                    raise DevelopmentEnvironmentError(
                        "Pinned Helm archive does not contain its expected binary"
                    ) from error
                if not member.isfile():
                    raise DevelopmentEnvironmentError(
                        "Pinned Helm archive member is not a regular file"
                    )
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise DevelopmentEnvironmentError(
                        "Pinned Helm archive binary is unreadable"
                    )
                binary_payload = source_file.read()
            if not binary_payload.startswith(b"\x7fELF"):
                raise DevelopmentEnvironmentError(
                    "Pinned Helm archive does not contain a Linux executable"
                )
            temporary_binary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=HELM_BINARY_PATH.parent,
                    delete=False,
                ) as temporary_binary:
                    temporary_binary_path = Path(temporary_binary.name)
                    temporary_binary.write(binary_payload)
                    temporary_binary.flush()
                    os.fsync(temporary_binary.fileno())
                os.chmod(temporary_binary_path, 0o755)
                os.replace(temporary_binary_path, HELM_BINARY_PATH)
            finally:
                if temporary_binary_path is not None:
                    temporary_binary_path.unlink(missing_ok=True)
        installed_result = self._runner.run(
            [
                str(HELM_BINARY_PATH),
                "version",
                "--template",
                "{{.Version}}",
            ]
        )
        if installed_result.stdout.strip() != HELM_VERSION:
            raise DevelopmentEnvironmentError(
                "Installed Helm version does not match the pinned release"
            )

    def host_install(self) -> None:
        """Install the source-owned host controller service from the current exact release."""

        self.host_prepare()
        infrastructure_source_path = (
            HOST_CONTROL_CURRENT_SOURCE_PATH / "sources" / "workflow-infrastructure"
        )
        self._runner.run(
            [
                "python3.14",
                str(infrastructure_source_path / "tool" / "venv_create.py"),
                "--runtime-only",
            ]
        )
        service_path = Path(
            "/etc/systemd/system/workflow-control-center-host-controller.service"
        )
        service_text = f"""[Unit]
Description=Workflow Control Center development host lifecycle controller
After=k3s.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={infrastructure_source_path}/.venv/bin/python {infrastructure_source_path}/tool/development_environment_manage.py host-controller
Restart=always
RestartSec=30
User=root

[Install]
WantedBy=multi-user.target
"""
        service_path.write_text(service_text, encoding="utf-8")
        os.chmod(service_path, 0o644)
        self._runner.run(["systemctl", "daemon-reload"])
        self._runner.run(
            ["systemctl", "enable", "workflow-control-center-host-controller"]
        )
        self._runner.run(
            ["systemctl", "restart", "workflow-control-center-host-controller"]
        )
        print("OK: host lifecycle controller is installed")

    def host_shutdown(self) -> None:
        """Gracefully stop Product workloads and power off the development instance."""

        product_tool_path = (
            HOST_CURRENT_SOURCE_PATH
            / "sources"
            / "workflow-control-center"
            / "tool"
            / "development_kubernetes_manage.py"
        )
        if product_tool_path.is_file():
            result = self._runner.run(
                ["python3.14", str(product_tool_path), "shutdown"],
                check=False,
                should_capture=False,
            )
            if result.returncode != 0:
                self._runner.run(
                    ["k3s", "kubectl", "uncordon", self._host_node_name_get()],
                    check=False,
                )
                raise DevelopmentEnvironmentError(
                    "Product graceful shutdown failed; node was uncordoned"
                )
        else:
            self._runner.run(["systemctl", "stop", "k3s"], check=False)
        self._runner.run(["systemctl", "poweroff"], should_capture=False)

    def restore(self, snapshot_id: str) -> None:
        """Replace the retained volume from one exact snapshot and run recovery acceptance.

        Args:
            snapshot_id: Exact EBS snapshot identifier.
        """

        if not snapshot_id.startswith("snap-"):
            raise DevelopmentEnvironmentError("Snapshot ID must start with snap-")
        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        replacement_parameter_by_name_map = (
            self._replacement_parameter_by_name_map_get()
        )
        replacement_parameter_by_name_map["RetainedVolumeSnapshotId"] = snapshot_id
        self.stop()
        self._replacement_stack_apply(
            parameter_by_name_map=replacement_parameter_by_name_map
        )
        self.start()
        self._replacement_guard_disable()
        self._infrastructure_source_publish()
        self._retained_product_release_link_restore()
        self._product_recovery_apply_run()
        self._product_recovery_acceptance_run()
        print(f"OK: retained state restored and accepted from {snapshot_id}")

    def replace(self) -> None:
        """Replace the EC2 instance while preserving the exact retained volume."""

        self._local_operator_context_validate()
        self._source_repository_validate(
            self._project_root_path, "workflow-infrastructure"
        )
        replacement_parameter_by_name_map = (
            self._replacement_parameter_by_name_map_get()
        )
        replacement_slot = replacement_parameter_by_name_map["InstanceSlot"]
        self.stop()
        self._replacement_stack_apply(
            parameter_by_name_map=replacement_parameter_by_name_map
        )
        self.start()
        self._replacement_guard_disable()
        self._infrastructure_source_publish()
        self._retained_product_release_link_restore()
        self._product_recovery_apply_run()
        self._product_recovery_acceptance_run()
        print(
            f"OK: replacement instance in slot {replacement_slot} accepted the retained volume"
        )

    def _replacement_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return explicit slot and fail-safe schedule parameters for replacement.

        Returns:
            Parameter overrides that deliberately replace the current instance.
        """

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        try:
            current_slot = output_by_name_map["InstanceSlot"]
        except KeyError as error:
            raise DevelopmentEnvironmentError(
                "Compute stack replacement outputs are incomplete"
            ) from error
        if current_slot not in {"a", "b"}:
            raise DevelopmentEnvironmentError(
                "Compute stack replacement outputs are malformed"
            )
        parameter_by_name_map = self._replacement_guard_parameter_by_name_map_get()
        parameter_by_name_map["InstanceSlot"] = "b" if current_slot == "a" else "a"
        return parameter_by_name_map

    def _replacement_guard_parameter_by_name_map_get(self) -> dict[str, str]:
        """Return an enabled two-hour CloudFormation replacement guard."""

        t_stop = self._clock.now() + LEASE_DURATION
        return {
            "ReplacementGuardScheduleExpression": (
                f"at({t_stop.strftime('%Y-%m-%dT%H:%M:%S')})"
            ),
            "ReplacementGuardScheduleState": "ENABLED",
        }

    def _replacement_guard_disable(self) -> None:
        """Disable the CloudFormation guard after the renewable lease is proven."""

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        if "ReplacementGuardScheduleName" not in output_by_name_map:
            raise DevelopmentEnvironmentError(
                "Compute stack replacement guard output is missing"
            )
        self._stack_apply(
            stack_name=COMPUTE_STACK_NAME,
            template_path=self._project_root_path
            / "cloudformation/workflow-control-center-development-compute.yaml",
            parameter_by_name_map={"ReplacementGuardScheduleState": "DISABLED"},
            must_preserve_resource=False,
            protected_identity_logical_id_set=COMPUTE_STABLE_IDENTITY_LOGICAL_ID_SET,
        )

    def _replacement_stack_apply(
        self, *, parameter_by_name_map: dict[str, str]
    ) -> None:
        """Apply one explicit replacement after proving the retained volume detached.

        Args:
            parameter_by_name_map: Exact replacement and optional restore parameters.
        """

        if (
            parameter_by_name_map.get("ReplacementGuardScheduleState") != "ENABLED"
            or "ReplacementGuardScheduleExpression" not in parameter_by_name_map
        ):
            raise DevelopmentEnvironmentError(
                "Explicit replacement requires an enabled CloudFormation guard"
            )
        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        renewable_lease_created = "StopLeaseTargetArn" in output_by_name_map
        if renewable_lease_created:
            self._stop_lease_upsert()
        try:
            self._retained_volume_detach_for_replacement()
        except Exception:
            if renewable_lease_created:
                self._stop_lease_delete()
            raise
        try:
            self._stack_apply(
                stack_name=COMPUTE_STACK_NAME,
                template_path=self._project_root_path
                / "cloudformation/workflow-control-center-development-compute.yaml",
                parameter_by_name_map=parameter_by_name_map,
                must_preserve_resource=False,
            )
        except Exception as error:
            try:
                self._retained_volume_attachment_ensure()
            except Exception as recovery_error:
                raise DevelopmentEnvironmentError(
                    "Compute replacement failed and retained-volume attachment recovery failed: "
                    f"{recovery_error}"
                ) from error
            if renewable_lease_created:
                self._stop_lease_delete()
            raise
        self._retained_volume_attachment_validate()
        self._instance_launch_template_version_validate()

    def ssh(self, ssh_argument_list: list[str]) -> int:
        """Run one SSH client command through an ephemeral SSH-over-SSM session.

        Args:
            ssh_argument_list: Additional OpenSSH arguments.

        Returns:
            SSH process status.
        """

        self._local_operator_context_validate()
        self._instance_online_wait()
        with self._ssh_control_session() as ssh_control_path:
            command_list = ["ssh", "-S", str(ssh_control_path), INSTANCE_NAME]
            command_list.extend(ssh_argument_list)
            result = self._runner.run(command_list, check=False, should_capture=False)
            return result.returncode

    def start(self) -> None:
        """Create the external stop lease before starting and verify host readiness."""

        self._local_operator_context_validate()
        instance_id = self._instance_id_get()
        self._stop_lease_upsert()
        state = self._instance_state_get(instance_id)
        if state == "stopped":
            self._aws_run(["ec2", "start-instances", "--instance-ids", instance_id])
        elif state not in {"pending", "running"}:
            raise DevelopmentEnvironmentError(
                f"Instance cannot start from state {state}"
            )
        self._instance_online_wait()
        print(f"OK: development instance {instance_id} is ready")

    def status(self) -> None:
        """Print safe infrastructure, access, lease, storage, and release state."""

        self._local_operator_context_validate()
        data_stack = self._stack_payload_get(DATA_PLANE_STACK_NAME, is_required=False)
        compute_stack = self._stack_payload_get(COMPUTE_STACK_NAME, is_required=False)
        payload: dict[str, object] = {
            "account_id": AWS_ACCOUNT_ID,
            "compute_stack_status": compute_stack.get("StackStatus", "absent"),
            "data_plane_stack_status": data_stack.get("StackStatus", "absent"),
            "region": AWS_REGION,
        }
        if compute_stack:
            output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
            instance_id = output_by_name_map["InstanceId"]
            payload.update(
                {
                    "active_ssm_session_count": self._active_session_count_get(
                        instance_id
                    ),
                    "instance_id": instance_id,
                    "instance_state": self._instance_state_get(instance_id),
                    "instance_type": output_by_name_map["InstanceType"],
                    "latest_retained_snapshot_id": self._latest_snapshot_id_get(
                        output_by_name_map["RetainedVolumeId"]
                    ),
                    "retained_volume_id": output_by_name_map["RetainedVolumeId"],
                    "stop_lease": self._stop_lease_payload_get(),
                }
            )
        print(json.dumps(payload, indent=2, sort_keys=True))

    def stop(self) -> None:
        """Run graceful remote shutdown, prove EC2 stop, and remove the pending lease."""

        self._local_operator_context_validate()
        instance_id = self._instance_id_get()
        state = self._instance_state_get(instance_id)
        if state == "stopped":
            self._stop_lease_delete()
            print(f"OK: development instance {instance_id} is already stopped")
            return
        if state != "running":
            raise DevelopmentEnvironmentError(
                f"Instance cannot stop gracefully from state {state}"
            )
        command_id = self._ssm_command_start(
            [
                (
                    f"if [ -f {HOST_CONTROL_CURRENT_SOURCE_PATH}/sources/workflow-infrastructure/"
                    "tool/development_environment_manage.py ]; then "
                    f"sudo python3.14 {HOST_CONTROL_CURRENT_SOURCE_PATH}/sources/workflow-infrastructure/"
                    "tool/development_environment_manage.py host-shutdown; "
                    "else sudo systemctl stop k3s || true; sudo systemctl poweroff; fi"
                )
            ]
        )
        print(f"OK: graceful shutdown command {command_id} started")
        self._aws_run(
            ["ec2", "wait", "instance-stopped", "--instance-ids", instance_id]
        )
        self._stop_lease_delete()
        print(f"OK: development instance {instance_id} stopped")

    def _active_session_count_get(self, instance_id: str) -> int:
        payload = self._aws_json_get(
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
            raise DevelopmentEnvironmentError(
                "Session Manager returned malformed Sessions"
            )
        return len(session_list)

    def _aws_json_get(self, aws_argument_list: Sequence[str]) -> dict[str, object]:
        result = self._aws_run([*aws_argument_list, "--output", "json"])
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                f"AWS {aws_argument_list[0]} returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(
                f"AWS {aws_argument_list[0]} returned unexpected JSON"
            )
        return payload

    def _aws_run(
        self,
        aws_argument_list: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_list = ["aws", *aws_argument_list, "--region", AWS_REGION]
        if not self._is_host:
            command_list.extend(["--profile", AWS_PROFILE])
        return self._runner.run(command_list, check=check)

    def _host_active_session_count_get(self, instance_id: str) -> int:
        result = self._runner.run(
            [
                "aws",
                "ssm",
                "describe-sessions",
                "--region",
                AWS_REGION,
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
            raise DevelopmentEnvironmentError(
                "Host Session Manager response is invalid"
            ) from error
        session_list = payload.get("Sessions", []) if isinstance(payload, dict) else []
        if not isinstance(session_list, list):
            raise DevelopmentEnvironmentError(
                "Host Session Manager response is malformed"
            )
        return len(session_list)

    def _host_node_name_get(self) -> str:
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

    def _host_product_activity_get(self) -> str:
        product_tool_path = (
            HOST_CURRENT_SOURCE_PATH
            / "sources"
            / "workflow-control-center"
            / "tool"
            / "development_kubernetes_manage.py"
        )
        if not product_tool_path.is_file():
            return "busy"
        result = self._runner.run(
            ["python3.14", str(product_tool_path), "activity"],
            check=False,
        )
        activity = result.stdout.strip()
        if result.returncode != 0 or activity not in {"busy", "idle"}:
            return "busy"
        return activity

    def _cost_review_record(self) -> None:
        instance_hour_price = self._price_usd_get(
            {
                "capacitystatus": "Used",
                "instanceType": "m7g.xlarge",
                "operatingSystem": "Linux",
                "preInstalledSw": "NA",
                "regionCode": AWS_REGION,
                "tenancy": "Shared",
            },
            unit="Hrs",
            usage_type="",
        )
        gp3_gib_month_price = self._price_usd_get(
            {
                "regionCode": AWS_REGION,
                "volumeApiName": "gp3",
            },
            unit="GB-Mo",
            usage_type="EBS:VolumeUsage.gp3",
        )
        snapshot_gib_month_price = self._price_usd_get(
            {
                "productFamily": "Storage Snapshot",
                "regionCode": AWS_REGION,
            },
            unit="GB-Mo",
            usage_type="EBS:SnapshotUsage",
        )
        active_hour_count_monthly = Decimal(80)
        gp3_gib_count = Decimal(180)
        snapshot_gib_count_max = Decimal(80)
        estimated_compute_monthly = instance_hour_price * active_hour_count_monthly
        estimated_gp3_monthly = gp3_gib_month_price * gp3_gib_count
        estimated_snapshot_monthly_max = (
            snapshot_gib_month_price * snapshot_gib_count_max
        )
        review_payload = {
            "architecture_checkpoint": "approved-2026-07-28",
            "assumption": {
                "active_hour_count_monthly": int(active_hour_count_monthly),
                "gp3_gib_count": int(gp3_gib_count),
                "snapshot_gib_count_max": int(snapshot_gib_count_max),
            },
            "estimated_monthly_usd": {
                "compute": str(estimated_compute_monthly.quantize(Decimal("0.01"))),
                "gp3": str(estimated_gp3_monthly.quantize(Decimal("0.01"))),
                "snapshot_max": str(
                    estimated_snapshot_monthly_max.quantize(Decimal("0.01"))
                ),
                "total_fixed_max": str(
                    (
                        estimated_compute_monthly
                        + estimated_gp3_monthly
                        + estimated_snapshot_monthly_max
                    ).quantize(Decimal("0.01"))
                ),
            },
            "price_usd": {
                "gp3_gib_month": str(gp3_gib_month_price),
                "m7g_xlarge_hour": str(instance_hour_price),
                "snapshot_gib_month": str(snapshot_gib_month_price),
            },
            "t_calculate": self._clock.now().isoformat().replace("+00:00", "Z"),
            "variable_cost_note": (
                "Existing S3, KMS, Glue, Athena, API Gateway, data transfer, and request costs remain usage-based."
            ),
        }
        review_path = self._project_root_path / ".local" / "cost-review.json"
        review_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        review_path.write_text(
            json.dumps(review_payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.chmod(review_path, 0o600)
        print(json.dumps(review_payload, indent=2, sort_keys=True))

    def _infrastructure_source_publish(self) -> None:
        release_name = self._clock.now().strftime("%Y%m%d%H%M%S%f")
        with self._ssh_control_session() as ssh_control_path:
            self._source_archive_publish(
                repository_name="workflow-infrastructure",
                repository_path=self._project_root_path,
                release_name=release_name,
                remote_release_root_path=HOST_CONTROL_RELEASE_ROOT_PATH,
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "install",
                    "-d",
                    "-m",
                    "0755",
                    str(HOST_CONTROL_RELEASE_ROOT_PATH / release_name / "sources"),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "ln",
                    "-sfn",
                    str(HOST_CONTROL_RELEASE_ROOT_PATH / release_name),
                    str(HOST_CONTROL_CURRENT_SOURCE_PATH),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._ssh_run(
                [
                    "sudo",
                    "python3.14",
                    str(
                        HOST_CONTROL_CURRENT_SOURCE_PATH
                        / "sources"
                        / "workflow-infrastructure"
                        / "tool"
                        / "development_environment_manage.py"
                    ),
                    "host-install",
                ],
                ssh_control_path=ssh_control_path,
            )

    def _instance_id_get(self) -> str:
        return self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)["InstanceId"]

    def _instance_metadata_get(self, path: str) -> str:
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

    def _instance_online_wait(self) -> None:
        instance_id = self._instance_id_get()
        self._aws_run(
            ["ec2", "wait", "instance-status-ok", "--instance-ids", instance_id]
        )
        t_deadline = self._clock.monotonic() + SSM_ONLINE_TIMEOUT_SECONDS
        while self._clock.monotonic() < t_deadline:
            payload = self._aws_json_get(
                [
                    "ssm",
                    "describe-instance-information",
                    "--filters",
                    f"Key=InstanceIds,Values={instance_id}",
                ]
            )
            information_list = payload.get("InstanceInformationList", [])
            if (
                isinstance(information_list, list)
                and information_list
                and isinstance(information_list[0], dict)
                and information_list[0].get("PingStatus") == "Online"
            ):
                return
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(
            f"Instance {instance_id} did not become SSM Online"
        )

    def _instance_state_get(self, instance_id: str) -> str:
        payload = self._aws_json_get(
            ["ec2", "describe-instances", "--instance-ids", instance_id]
        )
        try:
            state = payload["Reservations"][0]["Instances"][0]["State"]["Name"]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "EC2 instance state response is malformed"
            ) from error
        if not isinstance(state, str):
            raise DevelopmentEnvironmentError("EC2 instance state is not text")
        return state

    def _instance_launch_template_version_validate(self) -> None:
        """Prove the instance records the exact latest immutable template version."""

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        instance_id = output_by_name_map["InstanceId"]
        expected_version = output_by_name_map["LatestLaunchTemplateVersion"]
        payload = self._aws_json_get(
            ["ec2", "describe-instances", "--instance-ids", instance_id]
        )
        try:
            tag_list = payload["Reservations"][0]["Instances"][0]["Tags"]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "EC2 instance launch-template response is malformed"
            ) from error
        if not isinstance(tag_list, list) or any(
            not isinstance(tag, dict) for tag in tag_list
        ):
            raise DevelopmentEnvironmentError(
                "EC2 instance launch-template tags are malformed"
            )
        tag_by_name_map = {
            tag.get("Key"): tag.get("Value")
            for tag in tag_list
            if isinstance(tag.get("Key"), str)
        }
        if (
            not isinstance(expected_version, str)
            or not expected_version.isdigit()
            or tag_by_name_map.get("aws:ec2launchtemplate:version") != expected_version
        ):
            raise DevelopmentEnvironmentError(
                "EC2 instance does not use the exact latest launch-template version"
            )

    def _retained_volume_state_get(
        self, *, volume_id: str
    ) -> tuple[str, list[dict[str, object]]]:
        """Return exact EBS state and validated attachment records.

        Args:
            volume_id: Retained EBS volume identity.

        Returns:
            Volume state and attachment payload list.
        """

        payload = self._aws_json_get(
            ["ec2", "describe-volumes", "--volume-ids", volume_id]
        )
        volume_list = payload.get("Volumes", [])
        if (
            not isinstance(volume_list, list)
            or len(volume_list) != 1
            or not isinstance(volume_list[0], dict)
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume response is malformed"
            )
        volume = volume_list[0]
        state = volume.get("State")
        attachment_list = volume.get("Attachments", [])
        if (
            not isinstance(state, str)
            or not isinstance(attachment_list, list)
            or any(not isinstance(attachment, dict) for attachment in attachment_list)
        ):
            raise DevelopmentEnvironmentError("Retained EBS volume state is malformed")
        return state, list(attachment_list)

    def _retained_volume_attachment_validate(self) -> None:
        """Prove the current retained volume is attached only to the stack instance."""

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if (
            state != "in-use"
            or len(attachment_list) != 1
            or attachment_list[0].get("DeleteOnTermination") is not False
            or attachment_list[0].get("Device") != "/dev/sdf"
            or attachment_list[0].get("InstanceId") != instance_id
            or attachment_list[0].get("State") != "attached"
            or attachment_list[0].get("VolumeId") != volume_id
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume is not exactly attached to the current stack instance"
            )

    def _retained_volume_detach_for_replacement(self) -> None:
        """Detach the retained volume only after the old instance is proven stopped."""

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        if self._instance_state_get(instance_id) != "stopped":
            raise DevelopmentEnvironmentError(
                "Retained EBS volume can be detached only from a stopped instance"
            )
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if not attachment_list and state == "available":
            return
        if (
            state != "in-use"
            or len(attachment_list) != 1
            or attachment_list[0].get("InstanceId") != instance_id
            or attachment_list[0].get("Device") != "/dev/sdf"
            or attachment_list[0].get("State") != "attached"
            or attachment_list[0].get("DeleteOnTermination") is not False
        ):
            raise DevelopmentEnvironmentError(
                "Retained EBS volume has an unexpected attachment boundary"
            )
        self._aws_run(
            [
                "ec2",
                "detach-volume",
                "--device",
                "/dev/sdf",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
            ]
        )
        self._aws_run(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if state != "available" or attachment_list:
            raise DevelopmentEnvironmentError(
                "Retained EBS volume detachment was not proven"
            )

    def _retained_volume_attachment_ensure(self) -> None:
        """Recover the stack-declared attachment after a failed replacement."""

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        instance_id = output_by_name_map["InstanceId"]
        volume_id = output_by_name_map["RetainedVolumeId"]
        state, attachment_list = self._retained_volume_state_get(volume_id=volume_id)
        if attachment_list:
            self._retained_volume_attachment_validate()
            return
        if state != "available":
            raise DevelopmentEnvironmentError(
                "Retained EBS volume cannot be reattached from its current state"
            )
        self._aws_run(
            [
                "ec2",
                "attach-volume",
                "--device",
                "/dev/sdf",
                "--instance-id",
                instance_id,
                "--volume-id",
                volume_id,
            ]
        )
        self._aws_run(["ec2", "wait", "volume-in-use", "--volume-ids", volume_id])
        self._retained_volume_attachment_validate()

    def _latest_snapshot_id_get(self, volume_id: str) -> str:
        payload = self._aws_json_get(
            [
                "ec2",
                "describe-snapshots",
                "--owner-ids",
                "self",
                "--filters",
                f"Name=volume-id,Values={volume_id}",
            ]
        )
        snapshot_list = payload.get("Snapshots", [])
        if not isinstance(snapshot_list, list) or not snapshot_list:
            return ""
        snapshot_payload = max(
            (item for item in snapshot_list if isinstance(item, dict)),
            key=lambda item: str(item.get("StartTime", "")),
            default={},
        )
        snapshot_id = snapshot_payload.get("SnapshotId", "")
        return snapshot_id if isinstance(snapshot_id, str) else ""

    def _local_operator_context_validate(self) -> None:
        payload = self._aws_json_get(["sts", "get-caller-identity"])
        if payload.get("Account") != AWS_ACCOUNT_ID:
            raise DevelopmentEnvironmentError(
                f"AWS profile {AWS_PROFILE} targets {payload.get('Account')}, expected {AWS_ACCOUNT_ID}"
            )
        region_result = self._runner.run(
            ["aws", "configure", "get", "region", "--profile", AWS_PROFILE]
        )
        if region_result.stdout.strip() != AWS_REGION:
            raise DevelopmentEnvironmentError(
                f"AWS profile {AWS_PROFILE} region is {region_result.stdout.strip()}, expected {AWS_REGION}"
            )
        self._service_readiness_validate()

    def _service_readiness_validate(self) -> None:
        """Prove the development account can reach every required AWS control plane."""

        readiness_command_list = [
            ["s3api", "list-buckets"],
            ["kms", "list-keys", "--limit", "1"],
            ["athena", "list-work-groups", "--max-results", "1"],
            [
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                DATA_PLANE_STACK_NAME,
            ],
        ]
        for aws_argument_list in readiness_command_list:
            self._aws_run(aws_argument_list)

    def _product_recovery_acceptance_run(self) -> None:
        self._ssm_shell_run(
            [
                (
                    f"sudo python3.14 {HOST_CURRENT_SOURCE_PATH}/sources/workflow-control-center/"
                    "tool/development_kubernetes_manage.py recovery-acceptance"
                )
            ]
        )

    def _retained_product_release_link_restore(self) -> None:
        """Restore `/opt` access only after trusted infrastructure validates retained source."""

        self._ssm_shell_run(
            [
                (
                    f"sudo python3.14 {HOST_CONTROL_CURRENT_SOURCE_PATH}/sources/workflow-infrastructure/"
                    "tool/development_environment_manage.py host-product-release-restore"
                )
            ]
        )

    def _product_recovery_apply_run(self) -> None:
        """Reapply the exact retained Product release and reinstall its host service."""

        self._ssm_shell_run(
            [
                (
                    f"sudo python3.14 {HOST_CURRENT_SOURCE_PATH}/sources/workflow-control-center/"
                    "tool/development_kubernetes_manage.py recover"
                )
            ]
        )
        self._ssm_shell_run(
            [
                (
                    f"sudo python3.14 {HOST_CURRENT_SOURCE_PATH}/sources/workflow-control-center/"
                    "tool/development_kubernetes_manage.py host-install"
                )
            ]
        )

    def _price_usd_get(
        self,
        filter_by_field_map: dict[str, str],
        *,
        unit: str,
        usage_type: str,
    ) -> Decimal:
        aws_argument_list = [
            "pricing",
            "get-products",
            "--service-code",
            "AmazonEC2",
            "--max-results",
            "100",
        ]
        if filter_by_field_map:
            aws_argument_list.append("--filters")
            for field, value in sorted(filter_by_field_map.items()):
                aws_argument_list.append(f"Type=TERM_MATCH,Field={field},Value={value}")
        payload = self._aws_json_get(aws_argument_list)
        price_list = payload.get("PriceList", [])
        if not isinstance(price_list, list):
            raise DevelopmentEnvironmentError("AWS Pricing response is malformed")
        price_set: set[Decimal] = set()
        for product_text in price_list:
            if not isinstance(product_text, str):
                raise DevelopmentEnvironmentError("AWS Pricing product is malformed")
            try:
                product_payload = json.loads(product_text)
            except json.JSONDecodeError as error:
                raise DevelopmentEnvironmentError(
                    "AWS Pricing product is invalid"
                ) from error
            if not isinstance(product_payload, dict):
                raise DevelopmentEnvironmentError("AWS Pricing product is malformed")
            product = product_payload.get("product", {})
            attribute_by_name_map = (
                product.get("attributes", {}) if isinstance(product, dict) else {}
            )
            if not isinstance(attribute_by_name_map, dict):
                continue
            if usage_type and attribute_by_name_map.get("usagetype") != usage_type:
                continue
            term_by_code_map = product_payload.get("terms", {}).get("OnDemand", {})
            if not isinstance(term_by_code_map, dict):
                continue
            for term_payload in term_by_code_map.values():
                if not isinstance(term_payload, dict):
                    continue
                dimension_by_code_map = term_payload.get("priceDimensions", {})
                if not isinstance(dimension_by_code_map, dict):
                    continue
                for dimension_payload in dimension_by_code_map.values():
                    if (
                        not isinstance(dimension_payload, dict)
                        or dimension_payload.get("unit") != unit
                    ):
                        continue
                    price_per_unit = dimension_payload.get("pricePerUnit", {})
                    price_text = (
                        price_per_unit.get("USD")
                        if isinstance(price_per_unit, dict)
                        else None
                    )
                    if isinstance(price_text, str):
                        price_set.add(Decimal(price_text))
        if len(price_set) != 1:
            raise DevelopmentEnvironmentError(
                f"AWS Pricing returned {len(price_set)} distinct {unit} prices for usage type {usage_type or 'instance'}"
            )
        return next(iter(price_set))

    def _remote_text_write(
        self, *, remote_path: Path, text: str, ssh_control_path: Path
    ) -> None:
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
                    f"{INSTANCE_NAME}:/tmp/{remote_path.name}",
                ]
            )
            self._ssh_run(
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
            self._ssh_run(
                ["rm", "-f", f"/tmp/{remote_path.name}"],
                ssh_control_path=ssh_control_path,
            )
        finally:
            local_path.unlink(missing_ok=True)

    def _runtime_platform_get(self, ssh_control_path: Path) -> str:
        result = self._ssh_run(
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
            raise DevelopmentEnvironmentError(
                "Kubernetes node platform response is invalid"
            ) from error
        item_list = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(item_list, list) or not item_list:
            raise DevelopmentEnvironmentError(
                "No WorkflowRun-eligible Kubernetes nodes exist"
            )
        platform_set: set[str] = set()
        for item in item_list:
            if not isinstance(item, dict):
                raise DevelopmentEnvironmentError(
                    "Kubernetes node platform response is malformed"
                )
            node_info = item.get("status", {}).get("nodeInfo", {})
            operating_system = node_info.get("operatingSystem")
            architecture = node_info.get("architecture")
            if operating_system != "linux" or architecture not in {"amd64", "arm64"}:
                raise DevelopmentEnvironmentError(
                    f"Unsupported WorkflowRun node platform {operating_system}/{architecture}"
                )
            platform_set.add(f"{operating_system}/{architecture}")
        if len(platform_set) != 1:
            raise DevelopmentEnvironmentError(
                f"WorkflowRun node platforms are mixed: {sorted(platform_set)}"
            )
        return next(iter(platform_set))

    def _source_archive_publish(
        self,
        *,
        repository_name: str,
        repository_path: Path,
        release_name: str,
        remote_release_root_path: Path,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            archive_path = temporary_root_path / f"{repository_name}.tar"
            manifest_path = temporary_root_path / f"{repository_name}.json"
            manifest = self._source_archive_create(
                archive_path=archive_path,
                manifest_path=manifest_path,
                repository_name=repository_name,
                repository_path=repository_path,
            )
            remote_staging_path = (
                f"/tmp/workflow-source-{release_name}-{repository_name}"
            )
            self._runner.run(
                [
                    "rsync",
                    "--archive",
                    "--checksum",
                    "--rsh",
                    f"ssh -o ControlPath={ssh_control_path}",
                    f"{archive_path}",
                    f"{manifest_path}",
                    f"{INSTANCE_NAME}:{remote_staging_path}/",
                ]
            )
            remote_release_path = (
                remote_release_root_path / release_name / "sources" / repository_name
            )
            verification_code = f"""\
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

root_path = Path({remote_staging_path!r})
manifest = json.loads((root_path / {manifest_path.name!r}).read_text(encoding="utf-8"))
archive_path = root_path / {archive_path.name!r}
archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
if archive_sha256 != manifest["archive_sha256"]:
    raise RuntimeError("source archive digest mismatch")
target_path = Path({str(remote_release_path)!r})
shutil.rmtree(target_path, ignore_errors=True)
target_path.mkdir(parents=True)
with tarfile.open(archive_path) as source_archive:
    source_archive.extractall(target_path, filter="data")
actual_file_sha256_by_path_map = {{}}
for relative_path in manifest["file_sha256_by_path_map"]:
    source_path = target_path / relative_path
    if source_path.is_symlink():
        payload = os.readlink(source_path).encode()
    elif source_path.is_file():
        payload = source_path.read_bytes()
    else:
        raise RuntimeError(f"source path is missing: {{relative_path}}")
    actual_file_sha256_by_path_map[relative_path] = hashlib.sha256(payload).hexdigest()
if actual_file_sha256_by_path_map != manifest["file_sha256_by_path_map"]:
    raise RuntimeError("extracted source manifest mismatch")
shutil.rmtree(root_path)
"""
            self._ssh_run(
                [
                    "sudo",
                    "python3",
                    "-c",
                    verification_code,
                ],
                ssh_control_path=ssh_control_path,
            )
            return manifest

    def _source_archive_create(
        self,
        *,
        archive_path: Path,
        manifest_path: Path,
        repository_name: str,
        repository_path: Path,
    ) -> dict[str, object]:
        tracked_path_list = self._tracked_path_list_get(repository_path)
        file_sha256_by_path_map: dict[str, str] = {}
        with tarfile.open(archive_path, "w") as archive:
            for relative_path in tracked_path_list:
                source_path = repository_path / relative_path
                if source_path.is_dir():
                    continue
                archive_info = archive.gettarinfo(
                    str(source_path), arcname=relative_path.as_posix()
                )
                archive_info.gid = 0
                archive_info.gname = ""
                archive_info.mtime = 0
                archive_info.uid = 0
                archive_info.uname = ""
                if source_path.is_symlink():
                    archive.addfile(archive_info)
                    file_sha256_by_path_map[relative_path.as_posix()] = hashlib.sha256(
                        os.readlink(source_path).encode()
                    ).hexdigest()
                else:
                    with source_path.open("rb") as source_file:
                        payload = source_file.read()
                    archive.addfile(archive_info, fileobj=io.BytesIO(payload))
                    file_sha256_by_path_map[relative_path.as_posix()] = hashlib.sha256(
                        payload
                    ).hexdigest()
        manifest: dict[str, object] = {
            "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "commit_sha": self._git_stdout_get(repository_path, ["rev-parse", "HEAD"]),
            "file_sha256_by_path_map": file_sha256_by_path_map,
            "repository_url": REPOSITORY_URL_BY_NAME_MAP[repository_name],
            "submodule_by_path_map": self._submodule_by_path_map_get(repository_path),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        return manifest

    def _source_repository_validate(
        self, repository_path: Path, repository_name: str
    ) -> None:
        expected_url = REPOSITORY_URL_BY_NAME_MAP[repository_name]
        actual_url = self._git_stdout_get(
            repository_path, ["remote", "get-url", "origin"]
        )
        if actual_url != expected_url:
            raise DevelopmentEnvironmentError(
                f"{repository_name} origin is {actual_url}, expected {expected_url}"
            )
        status = self._git_stdout_get(
            repository_path,
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
        )
        if status:
            raise DevelopmentEnvironmentError(
                f"{repository_name} worktree is not clean"
            )
        head_sha = self._git_stdout_get(repository_path, ["rev-parse", "HEAD"])
        branch_name = self._git_stdout_get(
            repository_path, ["branch", "--show-current"]
        )
        if not branch_name:
            raise DevelopmentEnvironmentError(f"{repository_name} is detached")
        remote_result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{branch_name}",
            ]
        )
        remote_field_list = remote_result.stdout.strip().split()
        if len(remote_field_list) != 2 or remote_field_list[0] != head_sha:
            raise DevelopmentEnvironmentError(
                f"{repository_name} HEAD is not exact origin/{branch_name}"
            )
        submodule_result = self._runner.run(
            ["git", "-C", str(repository_path), "submodule", "status", "--recursive"],
            check=False,
        )
        if submodule_result.returncode != 0:
            raise DevelopmentEnvironmentError(
                f"{repository_name} submodule status failed"
            )
        for status_line in submodule_result.stdout.splitlines():
            if status_line[:1] != " ":
                raise DevelopmentEnvironmentError(
                    f"{repository_name} has a non-exact submodule: {status_line}"
                )
        for submodule_path_text, submodule_payload in self._submodule_by_path_map_get(
            repository_path
        ).items():
            submodule_path = repository_path / submodule_path_text
            expected_submodule_url = submodule_payload["repository_url"]
            actual_submodule_url = self._git_stdout_get(
                submodule_path, ["remote", "get-url", "origin"]
            )
            if actual_submodule_url != expected_submodule_url:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} origin is {actual_submodule_url}, "
                    f"expected {expected_submodule_url}"
                )
            remote_result = self._runner.run(
                [
                    "git",
                    "-C",
                    str(submodule_path),
                    "ls-remote",
                    "--exit-code",
                    "origin",
                    "refs/heads/main",
                ]
            )
            remote_field_list = remote_result.stdout.strip().split()
            if len(remote_field_list) != 2:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} origin/main lookup is malformed"
                )
            ancestor_result = self._runner.run(
                [
                    "git",
                    "-C",
                    str(submodule_path),
                    "merge-base",
                    "--is-ancestor",
                    str(submodule_payload["commit_sha"]),
                    remote_field_list[0],
                ],
                check=False,
            )
            if ancestor_result.returncode != 0:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} commit is not published on origin/main"
                )

    def _ssh_control_session(self) -> "SshControlSession":
        return SshControlSession(environment=self)

    def _ssh_run(
        self,
        remote_command_list: Sequence[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        remote_command_text = shlex.join(remote_command_list)
        return self._runner.run(
            [
                "ssh",
                "-o",
                f"ControlPath={ssh_control_path}",
                INSTANCE_NAME,
                remote_command_text,
            ],
            should_capture=should_capture,
        )

    def _ssm_command_start(self, shell_command_list: list[str]) -> str:
        payload = self._aws_json_get(
            [
                "ssm",
                "send-command",
                "--instance-ids",
                self._instance_id_get(),
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                json.dumps({"commands": shell_command_list}, separators=(",", ":")),
            ]
        )
        try:
            command_id = payload["Command"]["CommandId"]
        except (KeyError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "SSM send-command response is malformed"
            ) from error
        if not isinstance(command_id, str):
            raise DevelopmentEnvironmentError("SSM command ID is not text")
        return command_id

    def _ssm_shell_run(self, shell_command_list: list[str]) -> None:
        command_id = self._ssm_command_start(shell_command_list)
        instance_id = self._instance_id_get()
        t_deadline = self._clock.monotonic() + SSM_COMMAND_TIMEOUT_SECONDS
        payload: dict[str, object] | None = None
        while self._clock.monotonic() < t_deadline:
            payload = self._ssm_command_invocation_payload_get(
                command_id=command_id,
                instance_id=instance_id,
            )
            if payload is None or payload.get("Status") in {
                "Delayed",
                "InProgress",
                "Pending",
            }:
                self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
                continue
            break
        if payload is None or payload.get("Status") in {
            "Delayed",
            "InProgress",
            "Pending",
        }:
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} did not finish within "
                f"{SSM_COMMAND_TIMEOUT_SECONDS} seconds; the remote command was not cancelled"
            )
        print(payload.get("StandardOutputContent", ""), end="")
        error_text = payload.get("StandardErrorContent", "")
        if error_text:
            print(error_text, end="", file=os.sys.stderr)
        if payload.get("Status") != "Success":
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} failed with {payload.get('Status')}"
            )

    def _ssm_command_invocation_payload_get(
        self, *, command_id: str, instance_id: str
    ) -> dict[str, object] | None:
        """Return one SSM invocation, tolerating its short registration delay.

        Args:
            command_id: Exact Run Command identifier.
            instance_id: Exact target instance identifier.

        Returns:
            Invocation payload, or ``None`` while the invocation is not registered.
        """

        result = self._aws_run(
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout).strip()
            if "InvocationDoesNotExist" in error_text:
                return None
            raise DevelopmentEnvironmentError(
                f"Unable to inspect SSM command {command_id}: "
                f"{error_text or f'exit {result.returncode}'}"
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError(
                f"SSM command {command_id} returned unexpected JSON"
            )
        return payload

    def _stack_apply(
        self,
        *,
        stack_name: str,
        template_path: Path,
        parameter_by_name_map: dict[str, str],
        must_preserve_resource: bool,
        protected_identity_logical_id_set: Collection[str] = (),
    ) -> None:
        stack_payload = self._stack_payload_get(stack_name, is_required=False)
        change_set_type = "UPDATE" if stack_payload else "CREATE"
        change_set_name = f"codex-{self._clock.now().strftime('%Y%m%d%H%M%S%f')}"
        command_list = [
            "cloudformation",
            "create-change-set",
            "--stack-name",
            stack_name,
            "--change-set-name",
            change_set_name,
            "--change-set-type",
            change_set_type,
            "--template-body",
            f"file://{template_path}",
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
        ]
        if stack_payload:
            current_parameter_by_name_map = self._stack_parameter_by_name_map_get(
                stack_name
            )
            current_parameter_by_name_map.update(parameter_by_name_map)
            parameter_by_name_map = current_parameter_by_name_map
        if parameter_by_name_map:
            command_list.append("--parameters")
            for parameter_name, parameter_value in sorted(
                parameter_by_name_map.items()
            ):
                command_list.append(
                    f"ParameterKey={parameter_name},ParameterValue={parameter_value}"
                )
        self._aws_run(command_list)
        wait_result = self._aws_run(
            [
                "cloudformation",
                "wait",
                "change-set-create-complete",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ],
            check=False,
        )
        change_set_payload = self._aws_json_get(
            [
                "cloudformation",
                "describe-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )
        if wait_result.returncode != 0:
            reason = str(change_set_payload.get("StatusReason", ""))
            if "didn't contain changes" in reason:
                self._aws_run(
                    [
                        "cloudformation",
                        "delete-change-set",
                        "--stack-name",
                        stack_name,
                        "--change-set-name",
                        change_set_name,
                    ]
                )
                print(f"OK: stack {stack_name} has no changes")
                return
            raise DevelopmentEnvironmentError(
                f"Change set {stack_name}/{change_set_name} failed: {reason}"
            )
        change_list = change_set_payload.get("Changes", [])
        if not isinstance(change_list, list):
            raise DevelopmentEnvironmentError(
                f"Change set {stack_name}/{change_set_name} is malformed"
            )
        change_summary_list: list[dict[str, object]] = []
        for change_payload in change_list:
            if not isinstance(change_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Change set {stack_name}/{change_set_name} is malformed"
                )
            resource_change = change_payload.get("ResourceChange", {})
            if not isinstance(resource_change, dict):
                raise DevelopmentEnvironmentError(
                    f"Change set {stack_name}/{change_set_name} is malformed"
                )
            summary = {
                "action": resource_change.get("Action"),
                "logical_resource_id": resource_change.get("LogicalResourceId"),
                "replacement": resource_change.get("Replacement", "False"),
                "resource_type": resource_change.get("ResourceType"),
                "detail_list": resource_change.get("Details", []),
            }
            change_summary_list.append(summary)
        print(
            json.dumps(
                {"change_set": change_set_name, "changes": change_summary_list},
                indent=2,
                sort_keys=True,
            )
        )
        if must_preserve_resource:
            violation_logical_id_list = self._stable_data_change_violation_list_get(
                change_summary_list
            )
            if violation_logical_id_list:
                self._aws_run(
                    [
                        "cloudformation",
                        "delete-change-set",
                        "--stack-name",
                        stack_name,
                        "--change-set-name",
                        change_set_name,
                    ]
                )
                raise DevelopmentEnvironmentError(
                    "Stable data-plane change would remove or replace "
                    + ", ".join(violation_logical_id_list)
                )
        protected_identity_violation_list = (
            self._protected_identity_change_violation_list_get(
                change_summary_list=change_summary_list,
                protected_identity_logical_id_set=protected_identity_logical_id_set,
            )
        )
        if protected_identity_violation_list:
            self._aws_run(
                [
                    "cloudformation",
                    "delete-change-set",
                    "--stack-name",
                    stack_name,
                    "--change-set-name",
                    change_set_name,
                ]
            )
            raise DevelopmentEnvironmentError(
                "Ordinary compute apply would replace a protected identity: "
                + ", ".join(protected_identity_violation_list)
            )
        self._aws_run(
            [
                "cloudformation",
                "execute-change-set",
                "--stack-name",
                stack_name,
                "--change-set-name",
                change_set_name,
            ]
        )
        wait_name = (
            "stack-update-complete"
            if change_set_type == "UPDATE"
            else "stack-create-complete"
        )
        self._aws_run(["cloudformation", "wait", wait_name, "--stack-name", stack_name])
        if self._stack_payload_get(stack_name, is_required=True).get(
            "StackStatus"
        ) not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }:
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} did not reach a complete state"
            )

    @staticmethod
    def _protected_identity_change_violation_list_get(
        *,
        change_summary_list: Sequence[Mapping[str, object]],
        protected_identity_logical_id_set: Collection[str],
    ) -> list[str]:
        """Return protected identities whose planned action can change physical ID.

        Args:
            change_summary_list: Complete CloudFormation change summaries.
            protected_identity_logical_id_set: Identities forbidden to replace in this path.

        Returns:
            Sorted violating logical resource identities.
        """

        return sorted(
            str(summary.get("logical_resource_id"))
            for summary in change_summary_list
            if summary.get("logical_resource_id") in protected_identity_logical_id_set
            and (
                summary.get("action") == "Remove"
                or summary.get("replacement") != "False"
            )
        )

    def _stable_data_change_violation_list_get(
        self,
        change_summary_list: list[dict[str, object]],
    ) -> list[str]:
        """Return data-plane changes that are not proven identity-preserving.

        CloudFormation reports ``Conditional`` for a dependent resource when
        its property references another modified resource, even if that
        resource's physical identity cannot change. Such a change is safe only
        when every replacement-relevant detail is a dynamic resource-attribute
        reference to a change explicitly reported with ``Replacement=False``.

        Args:
            change_summary_list: Complete resource-change summaries.

        Returns:
            Sorted violating logical resource IDs.
        """

        summary_by_logical_id_map = {
            str(summary.get("logical_resource_id")): summary
            for summary in change_summary_list
        }
        violation_logical_id_list: list[str] = []
        for summary in change_summary_list:
            logical_resource_id = str(summary.get("logical_resource_id"))
            action = summary.get("action")
            replacement = summary.get("replacement")
            if action == "Remove" or replacement == "True":
                violation_logical_id_list.append(logical_resource_id)
                continue
            if replacement != "Conditional":
                continue
            detail_list = summary.get("detail_list")
            if not isinstance(detail_list, list) or not detail_list:
                violation_logical_id_list.append(logical_resource_id)
                continue
            replacement_detail_list = []
            for detail in detail_list:
                if not isinstance(detail, dict):
                    replacement_detail_list.append(detail)
                    continue
                target = detail.get("Target")
                if isinstance(target, dict) and target.get("RequiresRecreation") in {
                    "Always",
                    "Conditionally",
                }:
                    replacement_detail_list.append(detail)
            if not replacement_detail_list:
                violation_logical_id_list.append(logical_resource_id)
                continue
            for detail in replacement_detail_list:
                if not isinstance(detail, dict):
                    violation_logical_id_list.append(logical_resource_id)
                    break
                causing_entity = detail.get("CausingEntity")
                causing_logical_id = str(causing_entity).split(".", maxsplit=1)[0]
                causing_summary = summary_by_logical_id_map.get(causing_logical_id)
                if (
                    detail.get("Evaluation") != "Dynamic"
                    or detail.get("ChangeSource") != "ResourceAttribute"
                    or causing_summary is None
                    or causing_summary.get("action") == "Remove"
                    or causing_summary.get("replacement") != "False"
                ):
                    violation_logical_id_list.append(logical_resource_id)
                    break
        return sorted(set(violation_logical_id_list))

    def _submodule_by_path_map_get(
        self, repository_path: Path
    ) -> dict[str, dict[str, str]]:
        gitmodules_path = repository_path / ".gitmodules"
        if not gitmodules_path.is_file():
            return {}
        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "config",
                "--file",
                ".gitmodules",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise DevelopmentEnvironmentError(
                f"{repository_path.name} .gitmodules lookup failed"
            )
        submodule_by_path_map: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            field_list = line.split(maxsplit=1)
            if len(field_list) != 2:
                raise DevelopmentEnvironmentError(
                    f"{repository_path.name} .gitmodules path is malformed"
                )
            path_key, submodule_path = field_list
            name = path_key.removeprefix("submodule.").removesuffix(".path")
            repository_url = self._git_stdout_get(
                repository_path,
                ["config", "--file", ".gitmodules", "--get", f"submodule.{name}.url"],
            )
            submodule_by_path_map[submodule_path] = {
                "commit_sha": self._git_stdout_get(
                    repository_path / submodule_path, ["rev-parse", "HEAD"]
                ),
                "repository_url": repository_url,
            }
        return submodule_by_path_map

    def _stack_drift_validate(self, stack_name: str) -> None:
        payload = self._aws_json_get(
            ["cloudformation", "detect-stack-drift", "--stack-name", stack_name]
        )
        drift_detection_id = payload.get("StackDriftDetectionId")
        if not isinstance(drift_detection_id, str):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} drift detection ID is missing"
            )
        t_deadline = self._clock.monotonic() + STACK_TIMEOUT_SECONDS
        while self._clock.monotonic() < t_deadline:
            status_payload = self._aws_json_get(
                [
                    "cloudformation",
                    "describe-stack-drift-detection-status",
                    "--stack-drift-detection-id",
                    drift_detection_id,
                ]
            )
            detection_status = status_payload.get("DetectionStatus")
            if detection_status == "DETECTION_COMPLETE":
                if status_payload.get("StackDriftStatus") != "IN_SYNC":
                    raise DevelopmentEnvironmentError(
                        f"Stack {stack_name} is not IN_SYNC"
                    )
                print(f"OK: stack {stack_name} drift is IN_SYNC")
                return
            if detection_status == "DETECTION_FAILED":
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} drift detection failed"
                )
            self._clock.sleep(STACK_POLL_INTERVAL_SECONDS)
        raise DevelopmentEnvironmentError(
            f"Stack {stack_name} drift detection timed out"
        )

    def _stack_output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        stack_payload = self._stack_payload_get(stack_name, is_required=True)
        output_list = stack_payload.get("Outputs", [])
        if not isinstance(output_list, list):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} Outputs are malformed"
            )
        output_by_name_map: dict[str, str] = {}
        for output_payload in output_list:
            if not isinstance(output_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} Outputs are malformed"
                )
            output_name = output_payload.get("OutputKey")
            output_value = output_payload.get("OutputValue")
            if not isinstance(output_name, str) or not isinstance(output_value, str):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} output is malformed"
                )
            output_by_name_map[output_name] = output_value
        return output_by_name_map

    def _stack_parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        stack_payload = self._stack_payload_get(stack_name, is_required=True)
        parameter_list = stack_payload.get("Parameters", [])
        if not isinstance(parameter_list, list):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} Parameters are malformed"
            )
        parameter_by_name_map: dict[str, str] = {}
        for parameter_payload in parameter_list:
            if not isinstance(parameter_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} Parameters are malformed"
                )
            parameter_name = parameter_payload.get("ParameterKey")
            parameter_value = parameter_payload.get("ParameterValue")
            if not isinstance(parameter_name, str) or not isinstance(
                parameter_value, str
            ):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} parameter is malformed"
                )
            parameter_by_name_map[parameter_name] = parameter_value
        return parameter_by_name_map

    def _stack_payload_get(
        self, stack_name: str, *, is_required: bool
    ) -> dict[str, object]:
        result = self._aws_run(
            [
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack_name,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            if not is_required and "does not exist" in result.stderr:
                return {}
            raise DevelopmentEnvironmentError(
                f"Unable to describe stack {stack_name}: {(result.stderr or result.stdout).strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} response is invalid"
            ) from error
        stack_list = payload.get("Stacks", []) if isinstance(payload, dict) else []
        if (
            not isinstance(stack_list, list)
            or len(stack_list) != 1
            or not isinstance(stack_list[0], dict)
        ):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} response is malformed"
            )
        return stack_list[0]

    def _stack_resource_id_by_logical_name_map_get(
        self, stack_name: str
    ) -> dict[str, str]:
        if not self._stack_payload_get(stack_name, is_required=False):
            return {}
        payload = self._aws_json_get(
            ["cloudformation", "list-stack-resources", "--stack-name", stack_name]
        )
        resource_list = payload.get("StackResourceSummaries", [])
        if not isinstance(resource_list, list):
            raise DevelopmentEnvironmentError(
                f"Stack {stack_name} resources are malformed"
            )
        resource_id_by_logical_name_map: dict[str, str] = {}
        for resource_payload in resource_list:
            if not isinstance(resource_payload, dict):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} resource is malformed"
                )
            logical_name = resource_payload.get("LogicalResourceId")
            resource_id = resource_payload.get("PhysicalResourceId")
            if not isinstance(logical_name, str) or not isinstance(resource_id, str):
                raise DevelopmentEnvironmentError(
                    f"Stack {stack_name} resource identity is malformed"
                )
            resource_id_by_logical_name_map[logical_name] = resource_id
        return resource_id_by_logical_name_map

    def _stop_lease_delete(self) -> None:
        result = self._aws_run(
            [
                "scheduler",
                "delete-schedule",
                "--group-name",
                LEASE_GROUP_NAME,
                "--name",
                LEASE_NAME,
            ],
            check=False,
        )
        if result.returncode != 0 and "ResourceNotFoundException" not in result.stderr:
            raise DevelopmentEnvironmentError(
                f"Stop lease deletion failed: {result.stderr.strip()}"
            )

    def _stop_lease_payload_get(self) -> dict[str, object]:
        result = self._aws_run(
            [
                "scheduler",
                "get-schedule",
                "--group-name",
                LEASE_GROUP_NAME,
                "--name",
                LEASE_NAME,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            if "ResourceNotFoundException" in result.stderr:
                return {"state": "absent"}
            raise DevelopmentEnvironmentError(
                f"Stop lease lookup failed: {result.stderr.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError(
                "Stop lease response is invalid"
            ) from error
        if not isinstance(payload, dict):
            raise DevelopmentEnvironmentError("Stop lease response is malformed")
        target_payload = payload.get("Target")
        if not isinstance(target_payload, dict):
            raise DevelopmentEnvironmentError("Stop lease target is malformed")
        return {
            "action_after_completion": payload.get("ActionAfterCompletion"),
            "schedule_expression": payload.get("ScheduleExpression"),
            "state": payload.get("State"),
            "target_arn": target_payload.get("Arn"),
        }

    def _stop_lease_upsert(self) -> None:
        """Create or renew a lease that resolves the current instance at expiry."""

        output_by_name_map = self._stack_output_by_name_map_get(COMPUTE_STACK_NAME)
        t_stop = self._clock.now() + LEASE_DURATION
        schedule_expression = f"at({t_stop.strftime('%Y-%m-%dT%H:%M:%S')})"
        target_arn = output_by_name_map["StopLeaseTargetArn"]
        target_payload = {
            "Arn": target_arn,
            "Input": "{}",
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": 3600,
                "MaximumRetryAttempts": 3,
            },
            "RoleArn": output_by_name_map["SchedulerExecutionRoleArn"],
        }
        common_argument_list = [
            "--action-after-completion",
            "DELETE",
            "--flexible-time-window",
            json.dumps({"Mode": "OFF"}, separators=(",", ":")),
            "--group-name",
            LEASE_GROUP_NAME,
            "--name",
            LEASE_NAME,
            "--schedule-expression",
            schedule_expression,
            "--schedule-expression-timezone",
            "UTC",
            "--state",
            "ENABLED",
            "--target",
            json.dumps(target_payload, separators=(",", ":")),
        ]
        result = self._aws_run(
            [
                "scheduler",
                "get-schedule",
                "--group-name",
                LEASE_GROUP_NAME,
                "--name",
                LEASE_NAME,
            ],
            check=False,
        )
        operation = "update-schedule" if result.returncode == 0 else "create-schedule"
        create_argument_list = ["scheduler", operation, *common_argument_list]
        self._aws_run(create_argument_list)
        lease_payload = self._stop_lease_payload_get()
        if (
            lease_payload.get("action_after_completion") != "DELETE"
            or lease_payload.get("schedule_expression") != schedule_expression
            or lease_payload.get("state") != "ENABLED"
            or lease_payload.get("target_arn") != target_arn
        ):
            raise DevelopmentEnvironmentError("Stop lease was not proven enabled")

    def _template_validate(self, template_path: Path) -> None:
        self._runner.run(
            [
                str(self._project_root_path / ".venv" / "bin" / "cfn-lint"),
                str(template_path),
            ]
        )
        self._aws_run(
            [
                "cloudformation",
                "validate-template",
                "--template-body",
                f"file://{template_path}",
            ]
        )

    def _tracked_path_list_get(self, repository_path: Path) -> list[Path]:
        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-files",
                "--cached",
                "--recurse-submodules",
                "-z",
            ]
        )
        path_list = [Path(value) for value in result.stdout.split("\0") if value]
        return sorted(path_list, key=lambda path: path.as_posix())

    def _git_stdout_get(
        self, repository_path: Path, git_argument_list: Sequence[str]
    ) -> str:
        result = self._runner.run(
            ["git", "-C", str(repository_path), *git_argument_list]
        )
        return result.stdout.strip()


class DevelopmentEnvironmentError(RuntimeError):
    """Report one safe development-environment operation failure."""


class SshControlSession:
    """Own one ephemeral SSH key and multiplexed SSH-over-SSM control connection."""

    def __init__(self, *, environment: DevelopmentEnvironment) -> None:
        """Initialize the SSH session owner.

        Args:
            environment: Development environment owning AWS and process boundaries.
        """

        self._environment = environment
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        """Create an ephemeral key, publish it through EC2 Instance Connect, and open SSH.

        Returns:
            OpenSSH control-socket path.
        """

        self._temporary_directory = tempfile.TemporaryDirectory()
        temporary_root_path = Path(self._temporary_directory.name)
        private_key_path = temporary_root_path / "id_ed25519"
        control_path = temporary_root_path / "control"
        self._environment._runner.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(private_key_path),
            ]
        )
        instance_id = self._environment._instance_id_get()
        instance_payload = self._environment._aws_json_get(
            ["ec2", "describe-instances", "--instance-ids", instance_id]
        )
        try:
            availability_zone = instance_payload["Reservations"][0]["Instances"][0][
                "Placement"
            ]["AvailabilityZone"]
        except (KeyError, IndexError, TypeError) as error:
            raise DevelopmentEnvironmentError(
                "EC2 availability zone response is malformed"
            ) from error
        if not isinstance(availability_zone, str):
            raise DevelopmentEnvironmentError("EC2 availability zone is not text")
        self._environment._aws_run(
            [
                "ec2-instance-connect",
                "send-ssh-public-key",
                "--instance-id",
                instance_id,
                "--instance-os-user",
                "ubuntu",
                "--ssh-public-key",
                f"file://{private_key_path}.pub",
                "--availability-zone",
                availability_zone,
            ]
        )
        proxy_command = (
            f"aws ssm start-session --profile {shlex.quote(AWS_PROFILE)} --region {shlex.quote(AWS_REGION)} "
            f"--target {shlex.quote(instance_id)} --document-name AWS-StartSSHSession "
            "--parameters 'portNumber=%p'"
        )
        config_path = temporary_root_path / "config"
        config_path.write_text(
            "\n".join(
                [
                    f"Host {INSTANCE_NAME}",
                    f"  HostName {instance_id}",
                    "  User ubuntu",
                    f"  IdentityFile {private_key_path}",
                    "  IdentitiesOnly yes",
                    "  StrictHostKeyChecking accept-new",
                    f"  UserKnownHostsFile {temporary_root_path / 'known_hosts'}",
                    f"  ProxyCommand {proxy_command}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._environment._runner.run(
            [
                "ssh",
                "-F",
                str(config_path),
                "-M",
                "-N",
                "-f",
                "-o",
                f"ControlPath={control_path}",
                "-o",
                "ControlPersist=600",
                INSTANCE_NAME,
            ]
        )
        return control_path

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Close the multiplexed connection and delete ephemeral key material.

        Args:
            exc_type: Active exception type.
            exc_value: Active exception value.
            traceback: Active traceback.
        """

        if self._temporary_directory is None:
            return
        control_path = Path(self._temporary_directory.name) / "control"
        self._environment._runner.run(
            ["ssh", "-S", str(control_path), "-O", "exit", INSTANCE_NAME],
            check=False,
        )
        self._temporary_directory.cleanup()
        self._temporary_directory = None
