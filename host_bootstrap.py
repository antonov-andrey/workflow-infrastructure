#!/usr/bin/env python3
"""Bootstrap one development host from verified local artifacts."""

from __future__ import annotations

import argparse
import fcntl
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.command import (
    HostBootstrapCommandRunner,
)
from workflow_infrastructure.development_environment.host.bootstrap.manager import (
    DevelopmentHostBootstrapManager,
)


def _args_parse(argv_list: list[str]) -> argparse.Namespace:
    """Parse exact bootstrap identities.

    Args:
        argv_list: Command arguments without the executable path.

    Returns:
        Validated argument namespace.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--bundle-manifest-sha256", required=True)
    parser.add_argument("--environment-name", required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--retained-root", type=Path, required=True)
    parser.add_argument("--retained-volume-id", required=True)
    parser.add_argument(
        "--retained-volume-initialization-allowed",
        choices=("false", "true"),
        required=True,
    )
    return parser.parse_args(argv_list)


def main(argv_list: list[str]) -> int:
    """Run one bounded host bootstrap.

    Args:
        argv_list: Command arguments without the executable path.

    Returns:
        Process exit status.
    """

    args = _args_parse(argv_list)
    try:
        lock_path = Path("/run/lock/workflow-infrastructure-host-bootstrap.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            DevelopmentHostBootstrapManager(
                architecture=args.architecture,
                bundle_root_path=args.bundle_root,
                bundle_manifest_sha256=args.bundle_manifest_sha256,
                environment_name=args.environment_name,
                python_runtime_path=args.python_runtime,
                retained_root_path=args.retained_root,
                retained_volume_id=args.retained_volume_id,
                retained_volume_initialization_allowed=(
                    args.retained_volume_initialization_allowed == "true"
                ),
                runner=HostBootstrapCommandRunner(),
            ).run()
    except DevelopmentEnvironmentError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
