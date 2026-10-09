"""Bounded read-only discovery of explicitly declared BuildFlow projects."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import os
from typing import Callable


EXCLUDED = frozenset({".git", ".cache", ".build", "output", "dist", "build", "vendor",
                      "node_modules", "venv", ".venv", "__pycache__", "target"})


@dataclass(frozen=True)
class Candidate:
    """A declaration visible in discovery, including invalid declarations."""

    path: Path
    app_id: str = ""
    display_name: str = ""
    error: str = ""
    targets: tuple[str, ...] = ()


@dataclass
class Discovery:
    """Candidates and explicit reasons a scan may be incomplete."""

    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def discover(roots, *, max_depth: int = 4, max_directories: int = 1000,
             validator: Callable | None = None) -> Discovery:
    """Inspect JSON only; never follow symlinks, import adapters or run code."""
    if max_depth < 0 or max_directories < 1:
        raise ValueError("发现预算必须为有效正数（depth 可为 0）")
    result = Discovery()
    seen = set()
    stack = [(Path(root).expanduser().absolute(), 0) for root in reversed(list(roots))]
    scanned = 0
    while stack:
        directory, depth = stack.pop()
        if directory.is_symlink():
            result.warnings.append(f"跳过符号链接: {directory}")
            continue
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if scanned >= max_directories:
            result.warnings.append(f"目录预算 {max_directories} 已耗尽；扫描不完整")
            break
        scanned += 1
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            result.warnings.append(f"无法扫描 {directory}: {exc}")
            continue
        declaration = directory / "project.json"
        if declaration.is_file() and not declaration.is_symlink():
            try:
                data = json.loads(declaration.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "buildflow" in data:
                    error = ""
                    if validator:
                        try:
                            validator(declaration)
                        except Exception as exc:
                            error = str(exc)
                    config = data.get("buildflow")
                    targets = config.get("targets", []) if isinstance(config, dict) else []
                    if not isinstance(targets, list):
                        targets = []
                    result.candidates.append(Candidate(declaration.resolve(), str(data.get("app_id", "")),
                                                       str(data.get("display_name", "")), error,
                                                       tuple(t for t in targets if isinstance(t, str))))
            except (ValueError, OSError) as exc:
                result.candidates.append(Candidate(declaration.resolve(), error=str(exc)))
        subdirs = []
        for entry in entries:
            if entry.name in EXCLUDED or entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
            except OSError as exc:
                result.warnings.append(f"无法读取 {entry.path}: {exc}")
        if depth >= max_depth and subdirs:
            result.warnings.append(f"达到深度预算 {max_depth}: {directory}；扫描不完整")
        elif depth < max_depth:
            stack.extend((path, depth + 1) for path in reversed(subdirs))
    result.candidates.sort(key=lambda candidate: str(candidate.path))
    ids = {}
    for candidate in result.candidates:
        if candidate.app_id:
            ids.setdefault(candidate.app_id, []).append(candidate.path)
    for app_id, paths in ids.items():
        if len(paths) > 1:
            result.warnings.append(f"重复 app_id {app_id}: " + ", ".join(map(str, paths)))
    return result
