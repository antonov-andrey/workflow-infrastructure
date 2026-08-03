"""Resolve, validate, package, and publish exact development source releases."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

import tomllib

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.product.release import (
    MOVING_SOURCE_SELECTOR,
    REPOSITORY_URL_BY_NAME_MAP,
)

MOVING_SOURCE_RESOLUTION_ATTEMPT_COUNT = 3
PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT = "PYTHONDONTWRITEBYTECODE=1"


class ClockProtocol(Protocol):
    """UTC time boundary required by release naming."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant.

        Returns:
            The current timezone-aware UTC instant.
        """


class EnvironmentIdentityProtocol(Protocol):
    """Stable environment identities required by source publication."""

    environment_name: str
    git_worktree: str
    host_control_entrypoint_path: Path
    host_control_current_source_path: Path
    host_control_release_root_path: Path
    instance_name: str


class CommandRunnerProtocol(Protocol):
    """External process boundary required by source resolution."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.
            should_capture: Whether stdout and stderr should be captured.

        Returns:
            Completed text-mode subprocess result.
        """


class SsmTransportProtocol(Protocol):
    """SSH-over-SSM surface required by source transfer."""

    def ssh_control_session(self) -> object:
        """Return one context manager yielding a control-socket path.

        Returns:
            One context manager yielding a control-socket path.
        """

    def ssh_run(
        self,
        remote_command_list: Sequence[str],
        *,
        ssh_control_path: Path,
        should_capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run one remote command.

        Args:
            remote_command_list: Ordered remote command values.
            ssh_control_path: Exact filesystem path for ssh control.
            should_capture: Whether stdout and stderr should be captured.

        Returns:
            Completed text-mode subprocess result.
        """


def _source_file_sha256_by_path_map_get(*, archive_path: Path) -> dict[str, str]:
    """Return every safe file and symlink digest from one source archive.

    Args:
        archive_path: Exact filesystem path for archive.

    Returns:
        Every safe file and symlink digest keyed by archive-relative path.
    """

    file_sha256_by_path_map: dict[str, str] = {}
    member_name_set: set[str] = set()
    try:
        with tarfile.open(archive_path, "r") as archive:
            for member in archive.getmembers():
                normalized_member_name = member.name.removesuffix("/")
                relative_path = PurePosixPath(normalized_member_name)
                if (
                    not normalized_member_name
                    or relative_path.is_absolute()
                    or relative_path.as_posix() != normalized_member_name
                    or any(path_part in {"", ".", ".."} for path_part in relative_path.parts)
                    or normalized_member_name in member_name_set
                ):
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract archive contains an unsafe or duplicate path"
                    )
                member_name_set.add(normalized_member_name)
                if member.isdir():
                    continue
                if member.issym():
                    payload = member.linkname.encode()
                elif member.isfile():
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        raise DevelopmentEnvironmentError("workflow-container-contract archive file cannot be read")
                    payload = source_file.read()
                else:
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract archive contains an unsupported entry"
                    )
                file_sha256_by_path_map[normalized_member_name] = hashlib.sha256(payload).hexdigest()
    except (OSError, tarfile.TarError) as error:
        raise DevelopmentEnvironmentError("workflow-container-contract archive is malformed") from error
    return file_sha256_by_path_map


class DevelopmentSourcePublisher:
    """Own exact source identity, archive construction, and remote publication."""

    def __init__(
        self,
        *,
        clock: ClockProtocol,
        identity: EnvironmentIdentityProtocol,
        project_root_path: Path,
        runner: CommandRunnerProtocol,
        transport: SsmTransportProtocol,
    ) -> None:
        """Bind source publication to one explicit workspace and environment.

        Args:
            clock: Clock.
            identity: Identity.
            project_root_path: Exact filesystem path for project root.
            runner: Explicit command execution boundary.
            transport: Transport.
        """

        self._clock = clock
        self._identity = identity
        self._project_root_path = project_root_path
        self._runner = runner
        self._transport = transport

    def infrastructure_publish(
        self,
        *,
        should_install_host_controller: bool = True,
    ) -> None:
        """Publish exact infrastructure control source and install its controller.

        Args:
            should_install_host_controller: Should install host controller.
        """

        release_name = self._clock.now().strftime("%Y%m%d%H%M%S%f")
        with self._transport.ssh_control_session() as ssh_control_path:  # type: ignore[attr-defined]
            self.archive_publish(
                repository_name="workflow-infrastructure",
                repository_path=self._project_root_path,
                release_name=release_name,
                remote_release_root_path=(self._identity.host_control_release_root_path),
                ssh_control_path=ssh_control_path,
            )
            self._transport.ssh_run(
                [
                    "sudo",
                    "install",
                    "-d",
                    "-m",
                    "0755",
                    str(self._identity.host_control_release_root_path / release_name / "sources"),
                ],
                ssh_control_path=ssh_control_path,
            )
            self._transport.ssh_run(
                [
                    "sudo",
                    "ln",
                    "-sfn",
                    str(self._identity.host_control_release_root_path / release_name),
                    str(self._identity.host_control_current_source_path),
                ],
                ssh_control_path=ssh_control_path,
            )
            if should_install_host_controller:
                self._transport.ssh_run(
                    [
                        "sudo",
                        "env",
                        PYTHON_BYTECODE_ENVIRONMENT_ASSIGNMENT,
                        "python3.14",
                        "-B",
                        str(self._identity.host_control_entrypoint_path),
                        "host-install",
                        "--environment-name",
                        self._identity.environment_name,
                    ],
                    ssh_control_path=ssh_control_path,
                )

    def moving_archive_publish(
        self,
        *,
        exact_override_commit: str,
        release_name: str,
        remote_release_root_path: Path,
        repository_name: str,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Resolve, transfer, and verify one moving source exactly once.

        Args:
            exact_override_commit: Exact override commit.
            release_name: Release name.
            remote_release_root_path: Exact filesystem path for remote release root.
            repository_name: Repository name.
            ssh_control_path: Exact filesystem path for ssh control.

        Returns:
            Verified manifest for the resolved moving source archive.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            archive_path = temporary_root_path / f"{repository_name}.tar"
            manifest_path = temporary_root_path / f"{repository_name}.json"
            manifest = self.moving_archive_create(
                archive_path=archive_path,
                exact_override_commit=exact_override_commit,
                manifest_path=manifest_path,
                repository_name=repository_name,
            )
            return self._archive_transfer(
                archive_path=archive_path,
                manifest=manifest,
                manifest_path=manifest_path,
                release_name=release_name,
                remote_release_root_path=remote_release_root_path,
                repository_name=repository_name,
                ssh_control_path=ssh_control_path,
            )

    def moving_archive_create(
        self,
        *,
        archive_path: Path,
        exact_override_commit: str,
        manifest_path: Path,
        repository_name: str,
    ) -> dict[str, object]:
        """Resolve one moving source and export its immutable archive.

        Args:
            archive_path: Exact filesystem path for archive.
            exact_override_commit: Exact override commit.
            manifest_path: Exact filesystem path for manifest.
            repository_name: Repository name.

        Returns:
            One moving source and export its immutable archive.
        """

        repository_url = REPOSITORY_URL_BY_NAME_MAP[repository_name]
        if exact_override_commit and re.fullmatch(r"[0-9a-f]{40}", exact_override_commit) is None:
            raise DevelopmentEnvironmentError(
                "workflow-container-contract override must be one lowercase " "40-character commit SHA"
            )
        for _attempt_index in range(MOVING_SOURCE_RESOLUTION_ATTEMPT_COUNT):
            remote_head_by_field_map = self._moving_source_head_by_field_map_get(repository_url=repository_url)
            resolved_ref = remote_head_by_field_map["resolved_ref"]
            commit_sha = exact_override_commit or remote_head_by_field_map["commit_sha"]
            with tempfile.TemporaryDirectory() as temporary_directory:
                repository_path = Path(temporary_directory) / repository_name
                self._runner.run(["git", "init", "--quiet", str(repository_path)])
                self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "fetch",
                        "--depth=1",
                        "--no-tags",
                        repository_url,
                        (commit_sha if exact_override_commit else resolved_ref),
                    ]
                )
                fetched_commit_sha = self._git_stdout_get(
                    repository_path,
                    ["rev-parse", "FETCH_HEAD"],
                )
                if fetched_commit_sha != commit_sha:
                    if exact_override_commit:
                        raise DevelopmentEnvironmentError(
                            "workflow-container-contract override resolved to " "another commit"
                        )
                    continue
                if (
                    self._git_stdout_get(
                        repository_path,
                        ["cat-file", "-t", commit_sha],
                    )
                    != "commit"
                ):
                    raise DevelopmentEnvironmentError("workflow-container-contract source identity is not a commit")
                remote_head_after_by_field_map = self._moving_source_head_by_field_map_get(
                    repository_url=repository_url
                )
                if remote_head_after_by_field_map["resolved_ref"] != resolved_ref or (
                    not exact_override_commit and remote_head_after_by_field_map["commit_sha"] != commit_sha
                ):
                    continue
                tree_result = self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "ls-tree",
                        "-r",
                        "--full-tree",
                        commit_sha,
                    ]
                )
                if any(line.startswith("160000 ") for line in tree_result.stdout.splitlines()):
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract moving source must not " "contain submodules"
                    )
                pyproject_result = self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "show",
                        f"{commit_sha}:pyproject.toml",
                    ]
                )
                try:
                    pyproject = tomllib.loads(pyproject_result.stdout)
                except tomllib.TOMLDecodeError as error:
                    raise DevelopmentEnvironmentError(
                        "workflow-container-contract pyproject.toml is malformed"
                    ) from error
                project = pyproject.get("project")
                package_version = project.get("version") if isinstance(project, dict) else None
                if (
                    not isinstance(project, dict)
                    or project.get("name") != "workflow-container-contract"
                    or not isinstance(package_version, str)
                    or not package_version
                ):
                    raise DevelopmentEnvironmentError("workflow-container-contract package identity is malformed")
                self._runner.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "archive",
                        "--format=tar",
                        f"--output={archive_path}",
                        commit_sha,
                    ]
                )
            manifest: dict[str, object] = {
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "commit_sha": commit_sha,
                "file_sha256_by_path_map": (_source_file_sha256_by_path_map_get(archive_path=archive_path)),
                "package_version": package_version,
                "repository_url": repository_url,
                "requested_selector": MOVING_SOURCE_SELECTOR,
                "resolved_ref": resolved_ref,
                "source_kind": "resolved_moving_source",
                "submodule_by_path_map": {},
            }
            if exact_override_commit:
                manifest.update(
                    {
                        "override_identity": exact_override_commit,
                        "override_reason": "explicit operator deploy argument",
                    }
                )
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return manifest
        raise DevelopmentEnvironmentError(
            "workflow-container-contract default branch changed during every " "bounded resolution attempt"
        )

    def archive_publish(
        self,
        *,
        repository_name: str,
        repository_path: Path,
        release_name: str,
        remote_release_root_path: Path,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Create, transfer, and verify one exact-checkout source archive.

        Args:
            repository_name: Repository name.
            repository_path: Exact filesystem path for repository.
            release_name: Release name.
            remote_release_root_path: Exact filesystem path for remote release root.
            ssh_control_path: Exact filesystem path for ssh control.

        Returns:
            Verified manifest for the exact-checkout source archive.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root_path = Path(temporary_directory)
            archive_path = temporary_root_path / f"{repository_name}.tar"
            manifest_path = temporary_root_path / f"{repository_name}.json"
            manifest = self.archive_create(
                archive_path=archive_path,
                manifest_path=manifest_path,
                repository_name=repository_name,
                repository_path=repository_path,
            )
            return self._archive_transfer(
                archive_path=archive_path,
                manifest=manifest,
                manifest_path=manifest_path,
                release_name=release_name,
                remote_release_root_path=remote_release_root_path,
                repository_name=repository_name,
                ssh_control_path=ssh_control_path,
            )

    def archive_create(
        self,
        *,
        archive_path: Path,
        manifest_path: Path,
        repository_name: str,
        repository_path: Path,
    ) -> dict[str, object]:
        """Build one deterministic archive from the exact tracked source tree.

        Args:
            archive_path: Exact filesystem path for archive.
            manifest_path: Exact filesystem path for manifest.
            repository_name: Repository name.
            repository_path: Exact filesystem path for repository.

        Returns:
            One deterministic archive from the exact tracked source tree.
        """

        tracked_path_list = self._tracked_path_list_get(repository_path)
        file_sha256_by_path_map: dict[str, str] = {}
        with tarfile.open(archive_path, "w") as archive:
            for relative_path in tracked_path_list:
                source_path = repository_path / relative_path
                if source_path.is_dir():
                    continue
                archive_info = archive.gettarinfo(
                    str(source_path),
                    arcname=relative_path.as_posix(),
                )
                archive_info.gid = 0
                archive_info.gname = ""
                archive_info.mtime = 0
                archive_info.uid = 0
                archive_info.uname = ""
                if source_path.is_symlink():
                    archive.addfile(archive_info)
                    payload = os.readlink(source_path).encode()
                else:
                    payload = source_path.read_bytes()
                    archive.addfile(
                        archive_info,
                        fileobj=io.BytesIO(payload),
                    )
                file_sha256_by_path_map[relative_path.as_posix()] = hashlib.sha256(payload).hexdigest()
        manifest: dict[str, object] = {
            "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "commit_sha": self._git_stdout_get(
                repository_path,
                ["rev-parse", "HEAD"],
            ),
            "file_sha256_by_path_map": file_sha256_by_path_map,
            "repository_url": REPOSITORY_URL_BY_NAME_MAP[repository_name],
            "source_kind": "exact_checkout",
            "submodule_by_path_map": self._submodule_by_path_map_get(repository_path),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return manifest

    def validate_repository(
        self,
        repository_path: Path,
        repository_name: str,
    ) -> None:
        """Prove one checkout and every submodule are clean and published.

        Args:
            repository_path: Exact filesystem path for repository.
            repository_name: Repository name.
        """

        expected_url = REPOSITORY_URL_BY_NAME_MAP[repository_name]
        actual_url = self._git_stdout_get(
            repository_path,
            ["remote", "get-url", "origin"],
        )
        if actual_url != expected_url:
            raise DevelopmentEnvironmentError(f"{repository_name} origin is {actual_url}, expected {expected_url}")
        status = self._git_stdout_get(
            repository_path,
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
        )
        if status:
            raise DevelopmentEnvironmentError(f"{repository_name} worktree is not clean")
        head_sha = self._git_stdout_get(
            repository_path,
            ["rev-parse", "HEAD"],
        )
        branch_name = self._git_stdout_get(
            repository_path,
            ["branch", "--show-current"],
        )
        if not branch_name:
            raise DevelopmentEnvironmentError(f"{repository_name} is detached")
        remote_result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{branch_name}",
            ]
        )
        remote_field_list = remote_result.stdout.strip().split()
        if len(remote_field_list) != 2 or remote_field_list[0] != head_sha:
            raise DevelopmentEnvironmentError(f"{repository_name} HEAD is not exact origin/{branch_name}")
        submodule_result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "submodule",
                "status",
                "--recursive",
            ],
            check=False,
        )
        if submodule_result.returncode != 0:
            raise DevelopmentEnvironmentError(f"{repository_name} submodule status failed")
        for status_line in submodule_result.stdout.splitlines():
            if status_line[:1] != " ":
                raise DevelopmentEnvironmentError(f"{repository_name} has a non-exact submodule: {status_line}")
        for (
            submodule_path_text,
            submodule_payload,
        ) in self._submodule_by_path_map_get(repository_path).items():
            submodule_path = repository_path / submodule_path_text
            expected_submodule_url = submodule_payload["repository_url"]
            actual_submodule_url = self._git_stdout_get(
                submodule_path,
                ["remote", "get-url", "origin"],
            )
            if actual_submodule_url != expected_submodule_url:
                raise DevelopmentEnvironmentError(
                    f"{repository_name}/{submodule_path_text} origin is "
                    f"{actual_submodule_url}, expected {expected_submodule_url}"
                )
            self._submodule_commit_publication_validate(
                commit_sha=submodule_payload["commit_sha"],
                repository_name=repository_name,
                submodule_path=submodule_path,
                submodule_path_text=submodule_path_text,
            )

    def _submodule_commit_publication_validate(
        self,
        *,
        commit_sha: str,
        repository_name: str,
        submodule_path: Path,
        submodule_path_text: str,
    ) -> None:
        """Prove one gitlink on canonical main or the exact task branch.

        Args:
            commit_sha: Commit sha.
            repository_name: Repository name.
            submodule_path: Exact filesystem path for submodule.
            submodule_path_text: Submodule path text.
        """

        self._runner.run(
            [
                "git",
                "-C",
                str(submodule_path),
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ]
        )
        main_ancestor_result = self._runner.run(
            [
                "git",
                "-C",
                str(submodule_path),
                "merge-base",
                "--is-ancestor",
                commit_sha,
                "refs/remotes/origin/main",
            ],
            check=False,
        )
        if main_ancestor_result.returncode == 0:
            return
        if self._identity.git_worktree:
            task_result = self._runner.run(
                [
                    "git",
                    "-C",
                    str(submodule_path),
                    "ls-remote",
                    "--exit-code",
                    "origin",
                    f"refs/heads/{self._identity.git_worktree}",
                ],
                check=False,
            )
            task_field_list = task_result.stdout.strip().split()
            if task_result.returncode == 0 and len(task_field_list) == 2 and task_field_list[0] == commit_sha:
                return
        raise DevelopmentEnvironmentError(
            f"{repository_name}/{submodule_path_text} commit is not published on "
            "origin/main or the exact environment task branch"
        )

    def _archive_transfer(
        self,
        *,
        archive_path: Path,
        manifest: dict[str, object],
        manifest_path: Path,
        release_name: str,
        remote_release_root_path: Path,
        repository_name: str,
        ssh_control_path: Path,
    ) -> dict[str, object]:
        """Transfer and verify one already prepared immutable source archive.

        Args:
            archive_path: Exact filesystem path for archive.
            manifest: Manifest.
            manifest_path: Exact filesystem path for manifest.
            release_name: Release name.
            remote_release_root_path: Exact filesystem path for remote release root.
            repository_name: Repository name.
            ssh_control_path: Exact filesystem path for ssh control.

        Returns:
            Verified manifest for the remotely installed source archive.
        """

        remote_staging_path = f"/tmp/workflow-source-{release_name}-{repository_name}"
        self._runner.run(
            [
                "rsync",
                "--archive",
                "--checksum",
                "--rsh",
                f"ssh -o ControlPath={ssh_control_path}",
                str(archive_path),
                str(manifest_path),
                f"{self._identity.instance_name}:{remote_staging_path}/",
            ]
        )
        remote_release_path = remote_release_root_path / release_name / "sources" / repository_name
        verification_code = f"""\
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

root_path = Path({remote_staging_path!r})
manifest = json.loads((root_path / {manifest_path.name!r}).read_text(encoding="utf-8"))
archive_path = root_path / {archive_path.name!r}
archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
if archive_sha256 != manifest["archive_sha256"]:
    raise RuntimeError("source archive digest mismatch")
target_path = Path({str(remote_release_path)!r})
target_path.parent.mkdir(parents=True, exist_ok=True)
target_path.mkdir()
with tarfile.open(archive_path) as source_archive:
    source_archive.extractall(target_path, filter="data")
actual_file_sha256_by_path_map = {{}}
for relative_path in manifest["file_sha256_by_path_map"]:
    source_path = target_path / relative_path
    if source_path.is_symlink():
        payload = os.readlink(source_path).encode()
    elif source_path.is_file():
        payload = source_path.read_bytes()
    else:
        raise RuntimeError(f"source path is missing: {{relative_path}}")
    actual_file_sha256_by_path_map[relative_path] = hashlib.sha256(payload).hexdigest()
if actual_file_sha256_by_path_map != manifest["file_sha256_by_path_map"]:
    raise RuntimeError("extracted source manifest mismatch")
shutil.rmtree(root_path)
"""
        self._transport.ssh_run(
            ["sudo", "python3", "-c", verification_code],
            ssh_control_path=ssh_control_path,
        )
        return manifest

    def _moving_source_head_by_field_map_get(
        self,
        *,
        repository_url: str,
    ) -> dict[str, str]:
        """Return the advertised symbolic remote HEAD and exact commit.

        Args:
            repository_url: Repository URL.

        Returns:
            The advertised symbolic remote HEAD and exact commit.
        """

        result = self._runner.run(
            [
                "git",
                "ls-remote",
                "--symref",
                repository_url,
                MOVING_SOURCE_SELECTOR,
            ]
        )
        resolved_ref = ""
        commit_sha = ""
        for line in result.stdout.splitlines():
            field_list = line.split()
            if (
                len(field_list) == 3
                and field_list[0] == "ref:"
                and field_list[1].startswith("refs/heads/")
                and field_list[2] == MOVING_SOURCE_SELECTOR
            ):
                resolved_ref = field_list[1]
            elif (
                len(field_list) == 2
                and field_list[1] == MOVING_SOURCE_SELECTOR
                and re.fullmatch(r"[0-9a-f]{40}", field_list[0])
            ):
                commit_sha = field_list[0]
        if not resolved_ref or not commit_sha:
            raise DevelopmentEnvironmentError(
                "workflow-container-contract remote HEAD has no advertised " "symbolic branch and exact commit"
            )
        return {"commit_sha": commit_sha, "resolved_ref": resolved_ref}

    def _submodule_by_path_map_get(
        self,
        repository_path: Path,
    ) -> dict[str, dict[str, str]]:
        """Return exact submodule commits and configured remote URLs.

        Args:
            repository_path: Exact filesystem path for repository.

        Returns:
            The exact submodule commits and configured remote URLs.
        """

        gitmodules_path = repository_path / ".gitmodules"
        if not gitmodules_path.is_file():
            return {}
        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "config",
                "--file",
                ".gitmodules",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise DevelopmentEnvironmentError(f"{repository_path.name} .gitmodules lookup failed")
        submodule_by_path_map: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            field_list = line.split(maxsplit=1)
            if len(field_list) != 2:
                raise DevelopmentEnvironmentError(f"{repository_path.name} .gitmodules path is malformed")
            path_key, submodule_path = field_list
            name = path_key.removeprefix("submodule.").removesuffix(".path")
            repository_url = self._git_stdout_get(
                repository_path,
                [
                    "config",
                    "--file",
                    ".gitmodules",
                    "--get",
                    f"submodule.{name}.url",
                ],
            )
            submodule_by_path_map[submodule_path] = {
                "commit_sha": self._git_stdout_get(
                    repository_path / submodule_path,
                    ["rev-parse", "HEAD"],
                ),
                "repository_url": repository_url,
            }
        return submodule_by_path_map

    def _tracked_path_list_get(self, repository_path: Path) -> list[Path]:
        """Return the complete tracked path set including submodule bytes.

        Args:
            repository_path: Exact filesystem path for repository.

        Returns:
            The complete tracked path set including submodule bytes.
        """

        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-files",
                "--cached",
                "--recurse-submodules",
                "-z",
            ]
        )
        path_list = [Path(value) for value in result.stdout.split("\0") if value]
        return sorted(path_list, key=lambda path: path.as_posix())

    def _git_stdout_get(
        self,
        repository_path: Path,
        git_argument_list: Sequence[str],
    ) -> str:
        """Run Git in one repository and return stripped output.

        Args:
            repository_path: Exact filesystem path for repository.
            git_argument_list: Ordered Git argument values.

        Returns:
            Resulting text value.
        """

        result = self._runner.run(["git", "-C", str(repository_path), *git_argument_list])
        return result.stdout.strip()
