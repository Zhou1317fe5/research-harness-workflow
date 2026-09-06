"""Stable errors shared by the CLI and remote worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RRCError(Exception):
    code: str
    message: str
    phase: str = "runtime"
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "details": self.details,
        }


def require(condition: bool, code: str, message: str, *, phase: str, **details: Any) -> None:
    if not condition:
        raise RRCError(code=code, message=message, phase=phase, details=details)
