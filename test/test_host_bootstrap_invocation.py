"""Verify exact numeric/hash-bound SSM host bootstrap invocation."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from test_development_environment import ClockFixed, _template_get
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.bootstrap.invocation import (
    DevelopmentHostBootstrapInvocation,
)
from workflow_infrastructure.development_environment.identity import (
    DevelopmentEnvironmentIdentity,
)

ACCOUNT_ID = "463564115167"
DOCUMENT_HASH = "d" * 64
DOCUMENT_NAME = "bootstrap-primary"
DOCUMENT_VERSION = "7"
INSTANCE_ID = "i-0123456789abcdef0"
PARAMETER_BY_NAME = {
    "HostBootstrapBundleSha256": "b" * 64,
    "HostBootstrapBundleSourceInfo": '{"path":"bucket/bootstrap.tar.gz"}',
    "HostBootstrapManifestSha256": "c" * 64,
    "HostPythonArtifactSha256": "a" * 64,
    "HostPythonArtifactSourceInfo": '{"path":"bucket/python.tar.gz"}',
}
OUTPUT_BY_NAME = {
    "ComputeArchitecture": "arm64",
    "HostBootstrapDocumentName": DOCUMENT_NAME,
    "InstanceId": INSTANCE_ID,
    "RetainedVolumeId": "vol-0123456789abcdef0",
}


class _Stack:
    def output_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        assert stack_name == "compute-primary"
        return OUTPUT_BY_NAME

    def parameter_by_name_map_get(self, stack_name: str) -> dict[str, str]:
        assert stack_name == "compute-primary"
        return PARAMETER_BY_NAME


class _Aws:
    def __init__(self, content: dict[str, object]) -> None:
        self.content = content
        self.send_argument_list: list[str] = []

    def json_get(self, argument_list: list[str]) -> dict[str, object]:
        if argument_list[:2] == ["ssm", "describe-document"]:
            return {
                "Document": {
                    "DefaultVersion": DOCUMENT_VERSION,
                    "DocumentFormat": "YAML",
                    "DocumentType": "Command",
                    "DocumentVersion": DOCUMENT_VERSION,
                    "Hash": DOCUMENT_HASH,
                    "HashType": "Sha256",
                    "LatestVersion": DOCUMENT_VERSION,
                    "Name": DOCUMENT_NAME,
                    "Owner": ACCOUNT_ID,
                    "Status": "Active",
                    "VersionName": "b-"
                    + PARAMETER_BY_NAME["HostBootstrapBundleSha256"],
                }
            }
        if argument_list[:2] == ["ssm", "get-document"]:
            return {
                "Content": json.dumps(self.content),
                "DocumentFormat": "JSON",
                "DocumentType": "Command",
                "DocumentVersion": DOCUMENT_VERSION,
                "Name": DOCUMENT_NAME,
                "Status": "Active",
                "VersionName": "b-" + PARAMETER_BY_NAME["HostBootstrapBundleSha256"],
            }
        if argument_list[:2] == ["ssm", "send-command"]:
            self.send_argument_list = argument_list
            return {"Command": {"CommandId": "command-identity"}}
        raise AssertionError(argument_list)

    def run(
        self, argument_list: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del check
        assert argument_list[:2] == ["ssm", "get-command-invocation"]
        return subprocess.CompletedProcess(
            argument_list, 0, json.dumps({"Status": "Success"}), ""
        )


def _content_get() -> dict[str, object]:
    project_root = Path(__file__).resolve().parents[1]
    template = _template_get(project_root, "development-compute.yaml")
    content = deepcopy(
        template["Resources"]["HostBootstrapDocument"]["Properties"]["Content"]
    )

    def resolve(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"Ref"}:
                return PARAMETER_BY_NAME[value["Ref"]]
            if set(value) == {"Fn::Sub"}:
                text = value["Fn::Sub"]
                for name, replacement in PARAMETER_BY_NAME.items():
                    text = text.replace("${" + name + "}", replacement)
                return text
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    resolved = resolve(content)
    assert isinstance(resolved, dict)
    return resolved


def _invocation_get(aws: _Aws) -> DevelopmentHostBootstrapInvocation:
    return DevelopmentHostBootstrapInvocation(
        account_id=ACCOUNT_ID,
        aws=aws,
        clock=ClockFixed(),
        compute_stack_name="compute-primary",
        identity=DevelopmentEnvironmentIdentity(),
        stack=_Stack(),
    )


def test_bootstrap_uses_exact_numeric_version_and_system_hash() -> None:
    """SendCommand is bound to the content-validated latest/default document."""

    aws = _Aws(_content_get())
    _invocation_get(aws).run()
    assert aws.send_argument_list[:2] == ["ssm", "send-command"]
    assert (
        aws.send_argument_list[aws.send_argument_list.index("--document-version") + 1]
        == DOCUMENT_VERSION
    )
    assert (
        aws.send_argument_list[aws.send_argument_list.index("--document-hash") + 1]
        == DOCUMENT_HASH
    )
    assert (
        aws.send_argument_list[aws.send_argument_list.index("--document-hash-type") + 1]
        == "Sha256"
    )
    parameter_payload = json.loads(
        aws.send_argument_list[aws.send_argument_list.index("--parameters") + 1]
    )
    assert parameter_payload == {
        "Architecture": ["arm64"],
        "EnvironmentName": ["primary"],
        "RetainedRootPath": ["/srv/workflow-control-center"],
        "RetainedVolumeId": ["vol-0123456789abcdef0"],
    }


def test_bootstrap_rejects_any_launcher_drift_before_send_command() -> None:
    """Required substrings cannot hide an extra command or changed launcher."""

    content = _content_get()
    content["mainSteps"][2]["inputs"]["runCommand"][0] += "echo unexpected\n"
    aws = _Aws(content)
    with pytest.raises(
        DevelopmentEnvironmentError,
        match="launcher differs",
    ):
        _invocation_get(aws).run()
    assert aws.send_argument_list == []
