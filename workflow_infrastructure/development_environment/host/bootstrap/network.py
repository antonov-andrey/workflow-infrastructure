"""Install fail-closed host network policy."""

from __future__ import annotations

from workflow_infrastructure.development_environment.host.bootstrap.services import (
    HostBootstrapServiceManager,
)

_IMDS_DENY_UNIT = """[Unit]
Description=Deny Kubernetes pod access to EC2 instance metadata
After=k3s.service
Requires=k3s.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/iptables -D FORWARD -s 10.42.0.0/16 -d 169.254.169.254/32 -j REJECT
ExecStart=/usr/sbin/iptables -I FORWARD 1 -s 10.42.0.0/16 -d 169.254.169.254/32 -j REJECT
ExecStop=-/usr/sbin/iptables -D FORWARD -s 10.42.0.0/16 -d 169.254.169.254/32 -j REJECT

[Install]
WantedBy=multi-user.target
"""


class HostNetworkBootstrap:
    """Own the Kubernetes-to-IMDS host firewall service."""

    def __init__(self, *, service: HostBootstrapServiceManager) -> None:
        """Bind network policy to the shared service owner.

        Args:
            service: Systemd publication boundary.
        """

        self._service = service

    def install(self) -> None:
        """Install and activate the fail-closed IMDS policy."""

        self._service.unit_write(name="development-imds-deny.service", text=_IMDS_DENY_UNIT)
        self._service.activate("development-imds-deny.service")
