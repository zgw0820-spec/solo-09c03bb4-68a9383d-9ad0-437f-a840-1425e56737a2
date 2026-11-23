"""Typed, user-facing conversion failures.

The web layer only exposes :class:`ConversionError` details.  Any other
exception is treated as an internal error so that tracebacks and server
paths never reach the browser.
"""

from __future__ import annotations

from typing import Any, Mapping


class ConversionError(Exception):
    """A rejected input, with a stable machine code and Chinese explanation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        status_code: int = 422,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class VerificationError(Exception):
    """The produced image failed its mandatory post-conversion checks."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("output verification failed")
        self.reasons = reasons
