"""CloudFormation stack deletion boundary for task cleanup."""

from __future__ import annotations

from workflow_infrastructure.development_environment.cleanup.protocol import (
    AwsClientProtocol,
    StackManagerProtocol,
)
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class StackCleanup:
    """Delete and verify exact task CloudFormation stacks."""

    def __init__(self, *, aws: AwsClientProtocol, stack: StackManagerProtocol) -> None:
        """Initialize the stack cleanup dependencies.

        Args:
            aws: Aws.
            stack: Stack.
        """

        self._aws = aws
        self._stack = stack

    def delete(self, stack_name: str) -> None:
        """Idempotently delete one previously ownership-checked stack.

        Args:
            stack_name: Stack name.
        """

        if not self._stack.payload_get(stack_name, is_required=False):
            return
        self._aws.run(["cloudformation", "delete-stack", "--stack-name", stack_name])
        self._aws.run(
            [
                "cloudformation",
                "wait",
                "stack-delete-complete",
                "--stack-name",
                stack_name,
            ]
        )
        self.absence_validate(stack_name)

    def absence_validate(self, stack_name: str) -> None:
        """Require one exact stack to be absent.

        Args:
            stack_name: Stack name.
        """

        if self._stack.payload_get(stack_name, is_required=False):
            raise DevelopmentEnvironmentError(f"Task stack {stack_name} still exists after deletion")
