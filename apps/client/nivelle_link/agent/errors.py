from __future__ import annotations


class AgentError(Exception):
    """A safe, structured failure raised by the local Agent boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


class PathValidationError(AgentError):
    pass


class ApprovalError(AgentError):
    pass


class IdempotencyError(AgentError):
    pass
