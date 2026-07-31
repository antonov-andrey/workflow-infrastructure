#!/usr/bin/env python3
"""Manage the Workflow Control Center development environment."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True

for parent_path in Path(__file__).resolve().parents:
    if not (parent_path / "AGENTS.md").is_file():
        continue
    if str(parent_path) not in sys.path:
        sys.path.insert(0, str(parent_path))
    break

from workflow_infrastructure.development_environment.clock import Clock
from workflow_infrastructure.development_environment.command import CommandRunner
from workflow_infrastructure.development_environment.composition import (
    DevelopmentEnvironment,
)
from workflow_infrastructure.development_environment.error import (
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
        prog="development_environment_manage.py",
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
            "host-product-recovery-begin",
            "host-product-recovery-complete",
            "host-product-recovery-status",
            "host-product-release-activate",
            "host-product-release-reset",
            "host-product-release-restore",
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
        "--release",
        help="Exact retained Product release used by one host-local operation.",
    )
    parser.add_argument("--snapshot-id", help="Exact retained-volume snapshot used by restore.")
    parser.add_argument(
        "--retained-volume-id",
        help="Exact retained EBS volume expected by host-status.",
    )
    parser.add_argument(
        "--workflow-container-contract-commit",
        default="",
        help="Exact one-deploy workflow-container-contract commit override.",
    )
    parser.add_argument("--user-email", help="Preserved ZITADEL user to verify around Product reset.")
    parser.add_argument(
        "--expected-role-key",
        action="append",
        default=[],
        help="Exact Product role expected for the preserved user; repeat for multiple roles.",
    )
    parser.add_argument(
        "--reset-product-state",
        action="store_true",
        help="Run the approved destructive Product reset inside this exact deploy.",
    )
    args, remaining_argument_list = parser.parse_known_args(argv_list)
    args.ssh_argument_list = remaining_argument_list
    if args.command == "restore" and not args.snapshot_id:
        parser.error("--snapshot-id is required for restore")
    if args.snapshot_id and args.command != "restore":
        parser.error("--snapshot-id is supported only for restore")
    release_command_set = {
        "host-product-release-activate",
        "host-product-release-reset",
    }
    if args.command in release_command_set and not args.release:
        parser.error(f"--release is required for {args.command}")
    if args.release and args.command not in release_command_set:
        parser.error("--release is supported only for host Product release operations")
    if args.command == "host-status" and not args.retained_volume_id:
        parser.error("--retained-volume-id is required for host-status")
    if args.retained_volume_id and args.command != "host-status":
        parser.error("--retained-volume-id is supported only for host-status")
    if args.workflow_container_contract_commit and args.command != "deploy":
        parser.error("--workflow-container-contract-commit is supported only for deploy")
    if args.reset_product_state and args.command != "deploy":
        parser.error("--reset-product-state is supported only for deploy")
    if args.command == "deploy" and args.reset_product_state and not args.user_email:
        parser.error("--user-email is required with --reset-product-state")
    if (args.user_email or args.expected_role_key) and (args.command != "deploy" or not args.reset_product_state):
        parser.error("--user-email and --expected-role-key require deploy --reset-product-state")
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
    project_root_path = Path(__file__).resolve().parent
    environment = DevelopmentEnvironment(
        clock=Clock(),
        environment_name=args.environment_name,
        project_root_path=project_root_path,
        runner=CommandRunner(),
    )
    try:
        if args.command == "apply":
            environment.provisioning.apply()
        elif args.command == "connect":
            return environment.access.connect()
        elif args.command == "console":
            return environment.access.console()
        elif args.command == "deploy":
            environment.lifecycle.start(should_publish_infrastructure_source=True)
            environment.product_deployment.deploy(
                expected_role_key_list=args.expected_role_key,
                should_reset_product_state=args.reset_product_state,
                user_email=args.user_email or "",
                workflow_container_contract_commit=args.workflow_container_contract_commit,
            )
        elif args.command == "diagnose":
            environment.diagnostics.diagnose()
        elif args.command == "host-controller":
            environment.host.controller()
        elif args.command == "host-install":
            environment.host.install()
        elif args.command == "host-product-recovery-begin":
            environment.product_release.recovery_begin()
        elif args.command == "host-product-recovery-complete":
            environment.product_release.recovery_complete()
        elif args.command == "host-product-recovery-status":
            environment.product_release.recovery_status_print()
        elif args.command == "host-product-release-activate":
            environment.product_release.activate(args.release)
        elif args.command == "host-product-release-reset":
            environment.product_release.reset(args.release)
        elif args.command == "host-product-release-restore":
            environment.product_release.restore()
        elif args.command == "host-prepare":
            environment.host.prepare()
        elif args.command == "host-shutdown":
            environment.host.host_shutdown()
        elif args.command == "host-status":
            environment.host_status.print_local_status(args.retained_volume_id)
        elif args.command == "lifecycle-acceptance":
            environment.lifecycle.acceptance_run()
        elif args.command == "replace":
            environment.replacement.replace()
        elif args.command == "restore":
            environment.replacement.restore(args.snapshot_id)
        elif args.command == "ssh":
            return environment.access.ssh(args.ssh_argument_list)
        elif args.command == "start":
            environment.lifecycle.start()
        elif args.command == "status":
            environment.diagnostics.status()
        elif args.command == "stop":
            environment.lifecycle.stop()
    except DevelopmentEnvironmentError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
