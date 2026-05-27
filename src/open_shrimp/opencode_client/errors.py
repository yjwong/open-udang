from __future__ import annotations


class CLIConnectionError(Exception):
    pass


class ProcessError(Exception):
    pass


class OpenCodeAuthError(CLIConnectionError):
    pass


class OpenCodeNotFoundError(CLIConnectionError):
    pass
