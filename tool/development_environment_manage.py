#!/usr/bin/env python3
"""Manage the Workflow Control Center development environment."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

for parent_path in Path(__file__).resolve().parents:
    if not (parent_path / "AGENTS.md").is_file():
        continue
    if str(parent_path) not in sys.path:
        sys.path.insert(0, str(parent_path))
    break

from tool.lib.development_environment import (
    Clock,
    CommandRunner,
    DevelopmentEnvironment,
    DevelopmentEnvironmentError,
)


def _args_parse(argv_list: list[str]) -> argparse.Namespace:
    """Parse development-environment management arguments.

    Args:
        argv_list: Raw command-line arguments without the executable path.

    Returns:
        Parsed CLI namespace.
    """

    parser = argparse.ArgumentParser(
        prog="tool/development_environment_manage.py",
        description=__doc__,
    )
    parser.add_argument(
        "command",
        choices=[
            "apply",
            "connect",
            "console",
            "deploy",
            "diagnose",
            "host-controller",
            "host-install",
            "host-product-release-activate",
            "host-product-release-restore",
            "host-product-runtime-retain",
            "host-prepare",
            "host-shutdown",
            "host-status",
            "lifecycle-acceptance",
            "replace",
            "restore",
            "ssh",
            "start",
            "status",
            "stop",
        ],
    )
    parser.add_argument(
        "--environment-name",
        default="primary",
        help="Stable development environment selector (default: primary).",
    )
    parser.add_argument(
        "--release", help="Exact retained Product release to activate on the host."
    )
    parser.add_argument(
        "--snapshot-id", help="Exact retained-volume snapshot used by restore."
    )
    parser.add_argument(
        "--retained-volume-id",
        help="Exact retained EBS volume expected by host-status.",
    )
    parser.add_argument(
        "--workflow-container-contract-commit",
        default="",
        help="Exact one-deploy workflow-container-contract commit override.",
    )
    args, remaining_argument_list = parser.parse_known_args(argv_list)
    args.ssh_argument_list = remaining_argument_list
    if args.command == "restore" and not args.snapshot_id:
        parser.error("--snapshot-id is required for restore")
    if args.snapshot_id and args.command != "restore":
        parser.error("--snapshot-id is supported only for restore")
    if args.command == "host-product-release-activate" and not args.release:
        parser.error("--release is required for host-product-release-activate")
    if args.release and args.command != "host-product-release-activate":
        parser.error("--release is supported only for host-product-release-activate")
    if args.command == "host-status" and not args.retained_volume_id:
        parser.error("--retained-volume-id is required for host-status")
    if args.retained_volume_id and args.command != "host-status":
        parser.error("--retained-volume-id is supported only for host-status")
    if args.workflow_container_contract_commit and args.command != "deploy":
        parser.error(
            "--workflow-container-contract-commit is supported only for deploy"
        )
    if args.ssh_argument_list and args.command != "ssh":
        parser.error("arguments after the command are supported only for ssh")
    if args.ssh_argument_list[:1] == ["--"]:
        args.ssh_argument_list = args.ssh_argument_list[1:]
    return args


def main(argv_list: list[str]) -> int:
    """Run one development-environment operation.

    Args:
        argv_list: Raw command-line arguments without the executable path.

    Returns:
        Process exit status.
    """

    args = _args_parse(argv_list)
    project_root_path = Path(__file__).resolve().parents[1]
    environment = DevelopmentEnvironment(
        clock=Clock(),
        environment_name=args.environment_name,
        project_root_path=project_root_path,
        runner=CommandRunner(),
    )
    try:
        if args.command == "apply":
            environment.apply()
        elif args.command == "connect":
            return environment.connect()
        elif args.command == "console":
            return environment.console()
        elif args.command == "deploy":
            environment.deploy(
                workflow_container_contract_commit=args.workflow_container_contract_commit
            )
        elif args.command == "diagnose":
            environment.diagnose()
        elif args.command == "host-controller":
            environment.host_controller()
        elif args.command == "host-install":
            environment.host_install()
        elif args.command == "host-product-release-activate":
            environment.host_product_release_activate(args.release)
        elif args.command == "host-product-release-restore":
            environment.host_product_release_restore()
        elif args.command == "host-product-runtime-retain":
            environment.host_product_runtime_retain()
        elif args.command == "host-prepare":
            environment.host_prepare()
        elif args.command == "host-shutdown":
            environment.host_shutdown()
        elif args.command == "host-status":
            environment.host_status(args.retained_volume_id)
        elif args.command == "lifecycle-acceptance":
            environment.lifecycle_acceptance()
        elif args.command == "replace":
            environment.replace()
        elif args.command == "restore":
            environment.restore(args.snapshot_id)
        elif args.command == "ssh":
            return environment.ssh(args.ssh_argument_list)
        elif args.command == "start":
            environment.start()
        elif args.command == "status":
            environment.status()
        elif args.command == "stop":
            environment.stop()
    except DevelopmentEnvironmentError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
