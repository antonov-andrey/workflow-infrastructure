"""Install verified host artifacts without network resolution."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from typing import Protocol
import zipfile

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.manifest import (
    HostBootstrapBundle,
)

_SYSTEM_PACKAGE_NAME_LIST = [
    "ca-certificates",
    "ec2-instance-connect",
    "iptables",
    "openssh-server",
    "rsync",
    "xfsprogs",
]


class CommandRunnerProtocol(Protocol):
    """Command boundary required by host-artifact installation."""

    def run(self, command_argument_list: list[str], *, check: bool = True):
        """Run one command.

        Args:
            command_argument_list: Ordered command argument values.
            check: Whether a nonzero command exit raises an error.
        """


class HostArtifactBootstrap:
    """Own installation of the verified non-k3s host artifact set."""

    def __init__(
        self,
        *,
        bundle: HostBootstrapBundle,
        python_runtime_path: Path,
        runner: CommandRunnerProtocol,
    ) -> None:
        """Bind installation to one verified bundle and Python runtime.

        Args:
            bundle: Verified bootstrap bundle.
            python_runtime_path: Exact Python runtime extracted by the launcher.
            runner: Checked process boundary.
        """

        self._bundle = bundle
        self._python_runtime_path = python_runtime_path.resolve(strict=True)
        self._runner = runner

    def install(self) -> None:
        """Install system packages, Docker, AWS CLI, uv, Python, and Helm."""

        self._runner.run(["apt-get", "update"])
        self._runner.run(
            [
                "apt-get",
                "install",
                "--no-install-recommends",
                "-y",
                *_SYSTEM_PACKAGE_NAME_LIST,
                str(self._bundle.artifact_path_get("containerd.io")),
                str(self._bundle.artifact_path_get("docker-buildx-plugin")),
                str(self._bundle.artifact_path_get("docker-ce")),
                str(self._bundle.artifact_path_get("docker-ce-cli")),
            ]
        )
        self._docker_validate()
        self._aws_cli_install()
        self._uv_install()
        self._python_install()
        self._helm_install()
        self._runner.run(["systemctl", "enable", "--now", "containerd", "docker", "ssh"])

    def _docker_validate(self) -> None:
        """Require every installed Docker package version from the bundle."""

        for package_name in (
            "containerd.io",
            "docker-buildx-plugin",
            "docker-ce",
            "docker-ce-cli",
        ):
            result = self._runner.run(["dpkg-query", "--show", "--showformat=${Version}", package_name])
            if result.stdout != self._bundle.artifact_version_get(package_name):
                raise DevelopmentEnvironmentError(f"Installed Docker package {package_name} differs from the bundle")

    def _aws_cli_install(self) -> None:
        """Install and verify the exact AWS CLI archive."""

        with tempfile.TemporaryDirectory(prefix="host-bootstrap-aws-") as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            with zipfile.ZipFile(self._bundle.artifact_path_get("aws-cli")) as archive:
                _zip_archive_extract(archive=archive, destination_path=temporary_root_path)
            self._runner.run([str(temporary_root_path / "aws/install"), "--update"])
        result = self._runner.run(["/usr/local/bin/aws", "--version"])
        output = result.stdout + result.stderr
        if f"aws-cli/{self._bundle.artifact_version_get('aws-cli')} " not in output:
            raise DevelopmentEnvironmentError("Installed AWS CLI version differs from the bundle")

    def _uv_install(self) -> None:
        """Install and verify the exact uv binary."""

        with tempfile.TemporaryDirectory(prefix="host-bootstrap-uv-") as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            with tarfile.open(self._bundle.artifact_path_get("uv"), mode="r:gz") as archive:
                archive.extractall(temporary_root_path, filter="data")
            candidate_path_list = list(temporary_root_path.rglob("uv"))
            if len(candidate_path_list) != 1 or not candidate_path_list[0].is_file():
                raise DevelopmentEnvironmentError("uv archive has an unexpected shape")
            destination_path = Path("/usr/local/bin/uv")
            shutil.copyfile(candidate_path_list[0], destination_path)
            os.chmod(destination_path, 0o755)
        result = self._runner.run([str(destination_path), "--version"])
        if _uv_version_get(result.stdout) != self._bundle.artifact_version_get("uv"):
            raise DevelopmentEnvironmentError("Installed uv version differs from the bundle")

    def _python_install(self) -> None:
        """Expose and verify the exact Python runtime used by bootstrap."""

        python_path = self._python_runtime_path / "bin/python3.14"
        result = self._runner.run(
            [
                str(python_path),
                "-c",
                "import platform; print(platform.python_version())",
            ]
        )
        self._bundle.python_version_validate(result.stdout.strip())
        link_path = Path("/usr/local/bin/python3.14")
        temporary_link_path = link_path.with_name(f".{link_path.name}.new")
        temporary_link_path.unlink(missing_ok=True)
        temporary_link_path.symlink_to(python_path)
        os.replace(temporary_link_path, link_path)

    def _helm_install(self) -> None:
        """Install and verify the exact Helm binary."""

        with tempfile.TemporaryDirectory(prefix="host-bootstrap-helm-") as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            with tarfile.open(self._bundle.artifact_path_get("helm"), mode="r:gz") as archive:
                archive.extractall(temporary_root_path, filter="data")
            candidate_path_list = list(temporary_root_path.rglob("helm"))
            if len(candidate_path_list) != 1 or not candidate_path_list[0].is_file():
                raise DevelopmentEnvironmentError("Helm archive has an unexpected shape")
            destination_path = Path("/usr/local/bin/helm")
            shutil.copyfile(candidate_path_list[0], destination_path)
            os.chmod(destination_path, 0o755)
        result = self._runner.run([str(destination_path), "version", "--template", "{{.Version}}"])
        if result.stdout.strip() != self._bundle.artifact_version_get("helm"):
            raise DevelopmentEnvironmentError("Installed Helm version differs from the bundle")


def _zip_archive_extract(*, archive: zipfile.ZipFile, destination_path: Path) -> None:
    """Extract ordinary files and directories without path or symlink escape.

    Args:
        archive: Archive.
        destination_path: Exact filesystem path for destination.
    """

    destination_path = destination_path.resolve(strict=True)
    for info in archive.infolist():
        relative_path = Path(info.filename)
        mode = info.external_attr >> 16
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or (mode & 0o170000) == 0o120000
        ):
            raise DevelopmentEnvironmentError("AWS CLI archive has an unsafe member")
        target_path = destination_path.joinpath(*relative_path.parts)
        if not target_path.resolve(strict=False).is_relative_to(destination_path):
            raise DevelopmentEnvironmentError("AWS CLI archive member escapes extraction root")
        if info.is_dir():
            target_path.mkdir(mode=0o755, parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with archive.open(info) as source, target_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        os.chmod(target_path, mode & 0o777 or 0o644)


def _uv_version_get(output: str) -> str:
    """Parse the stable uv version while permitting its target annotation.

    Args:
        output: Output.

    Returns:
        The stable uv version while permitting its target annotation.
    """

    match = re.fullmatch(
        r"uv (?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n()]+\))?\s*",
        output,
    )
    if match is None:
        raise DevelopmentEnvironmentError("Installed uv version output is malformed")
    return match.group("version")
