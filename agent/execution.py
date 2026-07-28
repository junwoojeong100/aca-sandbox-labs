"""Backend-neutral Python execution contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ExecutionError(RuntimeError):
    """A backend execution or cleanup failure."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ExecutionResult:
    status: str
    stdout: str
    stderr: str
    result: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "Succeeded"

    @property
    def warnings(self) -> str:
        return self.stderr.strip() if self.succeeded else ""


class PythonExecution(Protocol):
    identifier: str

    def execute(self, code: str, *, timeout: int = 300) -> ExecutionResult: ...

    def upload(self, name: str, content: bytes) -> dict[str, Any]: ...

    def list_files(self) -> dict[str, Any]: ...

    def download(self, name: str) -> bytes: ...

    def info(self) -> dict[str, Any]: ...

    def delete(self) -> int: ...
