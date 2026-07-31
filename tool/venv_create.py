#!/usr/bin/env python3
"""Create the repository Python 3.14 virtual environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

PYTHON_BINARY_DEFAULT = "python3.14"
PYTHON_VERSION_EXPECTED = "3.14"


def main() -> int:
    """Recreate the root virtual environment from runtime and development requirements.

    Returns:
        Process exit status.
    """

    python_binary = os.environ.get("PYTHON_BINARY", PYTHON_BINARY_DEFAULT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.epilog = (
        f"PYTHON_BINARY selects the interpreter and defaults to {PYTHON_BINARY_DEFAULT}; "
        f"the effective value is {python_binary!r}."
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Install requirements.txt without development and verification dependencies.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    venv_path = project_root / ".venv"
    venv_python_path = venv_path / "bin" / "python"
    requirement_path = project_root / ("requirements.txt" if args.runtime_only else "requirements-dev.txt")
    command_list = [
        [
            python_binary,
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        [python_binary, "-m", "venv", "--clear", str(venv_path)],
        [
            str(venv_python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirement_path),
        ],
    ]
    try:
        version_result = subprocess.run(command_list[0], capture_output=True, check=False, text=True)
        if version_result.returncode != 0:
            print("PYTHON_BINARY could not report its version.", file=sys.stderr)
            return 1
        python_version = version_result.stdout.strip()
        if python_version != PYTHON_VERSION_EXPECTED:
            print(
                f"Expected Python {PYTHON_VERSION_EXPECTED}, got {python_version}.",
                file=sys.stderr,
            )
            return 1
        for argument_list in command_list[1:]:
            result = subprocess.run(argument_list, check=False, cwd=project_root)
            if result.returncode != 0:
                return result.returncode
    except OSError as error:
        print(f"Unable to execute environment command: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
