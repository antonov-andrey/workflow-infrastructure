"""Select exact canonical or task-worktree Product source checkouts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class CommandResultProtocol(Protocol):
    """Command fields required by checkout selection."""

    returncode: int
    stdout: str


class CommandRunnerProtocol(Protocol):
    """External Git command boundary required by checkout selection."""

    def run(
        self,
        command_list: Sequence[str],
        *,
        check: bool = True,
        should_capture: bool = True,
    ) -> CommandResultProtocol:
        """Run one Git command.

        Args:
            command_list: Ordered command values.
            check: Whether a nonzero command exit raises an error.
            should_capture: Whether stdout and stderr should be captured.

        Returns:
            Resulting command result protocol.
        """


class DevelopmentSourceCheckoutResolver:
    """Resolve each source by exact task identity instead of path proximity."""

    def __init__(
        self,
        *,
        git_worktree: str,
        project_root_path: Path,
        runner: CommandRunnerProtocol,
    ) -> None:
        """Bind the infrastructure checkout and optional task common prefix.

        Args:
            git_worktree: Git worktree.
            project_root_path: Exact filesystem path for project root.
            runner: Explicit command execution boundary.
        """

        self._git_worktree = git_worktree
        self._project_root_path = project_root_path.resolve()
        self._runner = runner
        self._workspace_root_path: Path | None = None

    def repository_path_get(self, repository_name: str) -> Path:
        """Return one exact published-source checkout for a repository.

        The infrastructure repository is always the invoking checkout. Other
        repositories use the exact same-prefix worktree when its remote task
        branch exists; otherwise they must use the canonical ``main`` checkout.

        Args:
            repository_name: Repository name.

        Returns:
            One exact published-source checkout for a repository.
        """

        if repository_name == "workflow-infrastructure":
            return self._project_root_path
        canonical_path = self._workspace_root_path_get() / repository_name
        if not canonical_path.is_dir():
            raise DevelopmentEnvironmentError(f"Canonical source repository is missing: {canonical_path}")
        if not self._git_worktree:
            self._branch_require(
                repository_name=repository_name,
                repository_path=canonical_path,
                required_branch="main",
            )
            return canonical_path

        task_path = canonical_path / ".worktree" / self._git_worktree
        remote_task_branch_exists = self._remote_branch_exists(
            repository_path=canonical_path,
            branch_name=self._git_worktree,
        )
        if task_path.exists():
            if not task_path.is_dir():
                raise DevelopmentEnvironmentError(f"Task source path is not a directory: {task_path}")
            self._branch_require(
                repository_name=repository_name,
                repository_path=task_path,
                required_branch=self._git_worktree,
            )
            if not remote_task_branch_exists:
                raise DevelopmentEnvironmentError(
                    f"{repository_name} task worktree has no exact origin/{self._git_worktree}"
                )
            return task_path
        if remote_task_branch_exists:
            raise DevelopmentEnvironmentError(
                f"{repository_name} origin/{self._git_worktree} exists without its exact task worktree"
            )
        self._branch_require(
            repository_name=repository_name,
            repository_path=canonical_path,
            required_branch="main",
        )
        return canonical_path

    def _workspace_root_path_get(self) -> Path:
        """Derive the canonical workspace from Git's common directory.

        Returns:
            Resolved filesystem path.
        """

        if self._workspace_root_path is not None:
            return self._workspace_root_path
        result = self._runner.run(
            [
                "git",
                "-C",
                str(self._project_root_path),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ]
        )
        common_directory = Path(result.stdout.strip()).resolve()
        canonical_repository_path = common_directory.parent
        if common_directory.name != ".git" or canonical_repository_path.name != "workflow-infrastructure":
            raise DevelopmentEnvironmentError(
                "Infrastructure Git common directory does not identify the canonical workspace"
            )
        self._workspace_root_path = canonical_repository_path.parent
        return self._workspace_root_path

    def _branch_require(
        self,
        *,
        repository_name: str,
        repository_path: Path,
        required_branch: str,
    ) -> None:
        """Require one checkout to be attached to the exact expected branch.

        Args:
            repository_name: Repository name.
            repository_path: Exact filesystem path for repository.
            required_branch: Required branch.
        """

        result = self._runner.run(["git", "-C", str(repository_path), "branch", "--show-current"])
        if result.stdout.strip() != required_branch:
            raise DevelopmentEnvironmentError(f"{repository_name} source must use branch {required_branch}")

    def _remote_branch_exists(self, *, repository_path: Path, branch_name: str) -> bool:
        """Return whether origin advertises one exact task branch.

        Args:
            repository_path: Exact filesystem path for repository.
            branch_name: Branch name.

        Returns:
            Whether origin advertises one exact task branch.
        """

        result = self._runner.run(
            [
                "git",
                "-C",
                str(repository_path),
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{branch_name}",
            ],
            check=False,
        )
        if result.returncode == 0:
            field_list = result.stdout.strip().split()
            if len(field_list) != 2 or field_list[1] != f"refs/heads/{branch_name}":
                raise DevelopmentEnvironmentError(f"{repository_path.name} task branch lookup is malformed")
            return True
        if result.returncode == 2:
            return False
        raise DevelopmentEnvironmentError(f"{repository_path.name} task branch lookup failed")
