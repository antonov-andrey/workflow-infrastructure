"""Resolve and revalidate immutable Git release-ref identities."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Sequence
from typing import Protocol

from workflow_infrastructure.development_environment.host.artifact.model import (
    HostArtifactResolutionError,
)


class CommandResultProtocol(Protocol):
    returncode: int
    stdout: str


class CommandRunnerProtocol(Protocol):
    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
    ) -> CommandResultProtocol:
        """Run one local command."""


def _tag_commit_sha_by_ref_map_get(output: str) -> dict[str, str]:
    """Map each tag to its peeled commit identity when present."""

    sha_by_ref: dict[str, str] = {}
    for line in output.splitlines():
        part_list = line.split()
        if len(part_list) != 2:
            continue
        sha, ref = part_list
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None or not ref.startswith("refs/tags/"):
            continue
        previous_sha = sha_by_ref.setdefault(ref, sha)
        if previous_sha != sha:
            raise HostArtifactResolutionError(f"Git returned conflicting identities for {ref}")
    return {ref: sha_by_ref.get(f"{ref}^{{}}", sha) for ref, sha in sha_by_ref.items() if not ref.endswith("^{}")}


class GitRefResolver:
    """Own tag selection, commit-object proof, and moving-ref recheck."""

    def __init__(self, *, runner: CommandRunnerProtocol) -> None:
        self._runner = runner

    def latest_tag_resolve(
        self,
        *,
        repository_url: str,
        selector: str,
        tag_pattern: re.Pattern[str],
    ) -> tuple[str, str, str]:
        """Resolve the numerically latest stable tag accepted by one selector."""

        result = self._runner.run(["git", "ls-remote", "--tags", repository_url])
        commit_sha_by_ref = _tag_commit_sha_by_ref_map_get(result.stdout)
        candidate_list: list[tuple[tuple[int, ...], str, str, str]] = []
        for resolved_ref, commit_sha in commit_sha_by_ref.items():
            match = tag_pattern.fullmatch(resolved_ref)
            if match is not None:
                candidate_list.append(
                    (
                        tuple(int(group) for group in match.groups()[1:]),
                        match.group(1),
                        resolved_ref,
                        commit_sha,
                    )
                )
        if not candidate_list:
            raise HostArtifactResolutionError(f"no stable tag satisfies selector {selector} in {repository_url}")
        _, version, resolved_ref, commit_sha = max(candidate_list)
        return version, resolved_ref, commit_sha

    def commit_validate(self, *, repository_url: str, resolved_ref: str, expected_commit_sha: str) -> None:
        """Prove an exact shallow fetch peels to the recorded commit object."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            self._runner.run(["git", "init", "--quiet", temporary_directory])
            self._runner.run(
                [
                    "git",
                    "-C",
                    temporary_directory,
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--no-tags",
                    repository_url,
                    resolved_ref,
                ]
            )
            result = self._runner.run(
                [
                    "git",
                    "-C",
                    temporary_directory,
                    "rev-parse",
                    "--verify",
                    "FETCH_HEAD^{commit}",
                ],
                check=False,
            )
        if result.returncode != 0 or result.stdout.strip().lower() != expected_commit_sha:
            raise HostArtifactResolutionError(f"artifact ref does not resolve to expected commit: {resolved_ref}")

    def unchanged_validate(self, *, repository_url: str, resolved_ref: str, expected_commit_sha: str) -> None:
        """Fail if the selected moving ref changed during resolution."""

        result = self._runner.run(
            [
                "git",
                "ls-remote",
                "--tags",
                repository_url,
                resolved_ref,
                f"{resolved_ref}^{{}}",
            ]
        )
        if _tag_commit_sha_by_ref_map_get(result.stdout).get(resolved_ref) != expected_commit_sha:
            raise HostArtifactResolutionError(f"moving artifact ref changed during resolution: {resolved_ref}")
