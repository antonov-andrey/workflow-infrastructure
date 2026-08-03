"""Install the exact k3s binary and service."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Protocol

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.manifest import (
    HostBootstrapBundle,
)
from workflow_infrastructure.development_environment.host.bootstrap.services import (
    HostBootstrapServiceManager,
)

_K3S_UNIT = """[Unit]
Description=Lightweight Kubernetes
Documentation=https://k3s.io
Wants=network-online.target
After=network-online.target
Conflicts=shutdown.target

[Install]
WantedBy=multi-user.target

[Service]
Type=notify
Environment=KUBECONFIG=/etc/rancher/k3s/k3s.yaml
KillMode=process
Delegate=yes
User=root
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=0
Restart=always
RestartSec=5s
ExecStartPre=-/sbin/modprobe br_netfilter
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/local/bin/k3s server --cluster-cidr 10.42.0.0/16 --service-cidr 10.43.0.0/16 --disable traefik --secrets-encryption --write-kubeconfig-mode 0600 --node-label apwid.com/workflow-run-eligible=true
"""


class CommandRunnerProtocol(Protocol):
    """Command boundary required by k3s installation."""

    def run(self, command_argument_list: list[str], *, check: bool = True):
        """Run one command.

        Args:
            command_argument_list: Ordered command argument values.
            check: Whether a nonzero command exit raises an error.
        """


class HostK3sBootstrap:
    """Own k3s binary identity, links, and systemd lifecycle."""

    def __init__(
        self,
        *,
        bundle: HostBootstrapBundle,
        runner: CommandRunnerProtocol,
        service: HostBootstrapServiceManager,
    ) -> None:
        """Bind k3s installation to verified bundle inputs.

        Args:
            bundle: Verified bootstrap bundle.
            runner: Checked process boundary.
            service: Systemd publication boundary.
        """

        self._bundle = bundle
        self._runner = runner
        self._service = service

    def install(self) -> None:
        """Install and activate the exact k3s release."""

        binary_path = Path("/usr/local/bin/k3s")
        shutil.copyfile(self._bundle.artifact_path_get("k3s-binary"), binary_path)
        os.chmod(binary_path, 0o755)
        result = self._runner.run([str(binary_path), "--version"])
        expected_version = self._bundle.artifact_version_get("k3s-binary")
        actual_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
        if expected_version not in actual_line:
            raise DevelopmentEnvironmentError("Installed k3s version differs from the bundle")
        for link_name in ("kubectl", "crictl", "ctr"):
            link_path = Path("/usr/local/bin") / link_name
            link_path.unlink(missing_ok=True)
            link_path.symlink_to(binary_path)
        Path("/etc/rancher/k3s").mkdir(mode=0o755, parents=True, exist_ok=True)
        self._service.unit_write(name="k3s.service", text=_K3S_UNIT)
        self._service.activate("k3s.service")
