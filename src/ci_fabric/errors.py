"""Typed exceptions and process exit codes.

Exit codes are part of the local CI contract:

* 0   success
* 1   a blocking gate failed
* 2   configuration or usage error
* 3   a required prerequisite (tool, runtime, archive) is unavailable
* 130 interrupted by the user
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_CONFIG = 2
EXIT_PREREQUISITE = 3
EXIT_INTERRUPTED = 130


class CiError(Exception):
    """An expected, explainable failure."""

    exit_code = EXIT_GATE_FAILED

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def __str__(self) -> str:
        return self.message


class ConfigError(CiError):
    """Invalid or missing repository CI configuration."""

    exit_code = EXIT_CONFIG


class PrerequisiteError(CiError):
    """A required tool or runtime is missing."""

    exit_code = EXIT_PREREQUISITE


class UsageError(CiError):
    """Invalid command-line usage not caught by argparse."""

    exit_code = EXIT_CONFIG
