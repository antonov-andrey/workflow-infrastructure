"""Verify native host-bootstrap artifact consumption."""

from __future__ import annotations

import pytest

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.artifacts import (
    _uv_version_get,
)


@pytest.mark.parametrize(
    ("output", "expected_version"),
    [
        ("uv 0.12.1\n", "0.12.1"),
        ("uv 0.12.1 (aarch64-unknown-linux-gnu)\n", "0.12.1"),
    ],
)
def test_uv_version_accepts_current_machine_annotation(
    output: str,
    expected_version: str,
) -> None:
    """A verified uv binary may append its build target to the stable version.

    Args:
        output: Output.
        expected_version: Expected version.
    """

    assert _uv_version_get(output) == expected_version


@pytest.mark.parametrize(
    "output",
    [
        "uv 0.12.1 unexpected\n",
        "uv 0.12.1 (target) trailing\n",
        "uv latest\n",
    ],
)
def test_uv_version_rejects_ambiguous_output(output: str) -> None:
    """Version verification remains strict when uv output cannot be parsed exactly.

    Args:
        output: Output.
    """

    with pytest.raises(DevelopmentEnvironmentError, match="output is malformed"):
        _uv_version_get(output)
