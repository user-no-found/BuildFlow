"""Shared contracts for build plans, execution contexts and events."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


class BuildError(Exception):
    """Build failure with an exit status suitable for a CLI."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code if exit_code > 0 else 128 - exit_code


@dataclass(frozen=True)
class Project:
    """Validated project declaration and its owning directory."""

    path: Path
    root: Path
    data: dict[str, Any]
    config: dict[str, Any]


@dataclass(frozen=True)
class Completed:
    """Successful command execution result."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""


class RunnerProtocol(Protocol):
    """Commands use argv, explicit directories and incremental logging."""

    def run(self, argv, cwd: Path, env=None) -> Completed: ...
    def capture(self, argv, cwd: Path, env=None) -> str: ...


@dataclass
class Context:
    """Per-build state passed explicitly to each step."""

    project: Project
    work: Path
    output: Path
    runner: RunnerProtocol
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    """A named synchronous build action, executed in declaration order."""

    name: str
    action: Callable[[Context], None]


@dataclass(frozen=True)
class Artifact:
    """A verified file to publish under a relative output name."""

    path: Path | str
    name: str


@dataclass(frozen=True)
class BuildResult:
    """Successful build with its flat output directory and private report."""

    run_id: str
    output: Path
    report: Path
    metadata: dict[str, Any]


EventSink = Callable[[dict[str, Any]], None]
