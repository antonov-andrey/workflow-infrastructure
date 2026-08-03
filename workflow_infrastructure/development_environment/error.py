"""Shared errors for development environment owner modules."""


class DevelopmentEnvironmentError(RuntimeError):
    """Report one safe development-environment operation failure."""


class DevelopmentCommandTimeoutError(DevelopmentEnvironmentError):
    """Report one external command that exceeded its explicit deadline."""
