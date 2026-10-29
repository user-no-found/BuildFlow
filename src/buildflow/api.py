"""Compose project configuration, one pipeline, logs and artifact publication."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import signal
import sys
import threading
import time
import uuid
from . import artifacts as publication
from .activity import Activities
from .contracts import Artifact, BuildError, BuildResult, Context, Project, Step
from .event_stream import EventStream
from .display_text import plain
from .locking import Lock
from .metadata import identity, version
from .pipeline import execute
from .project import load_project
from .runner import Runner
from .terminal import Terminal
from .versioning import choose_version, persist_version
from .version_history import VersionHistory


def _expand(value: str, context: Context) -> str:
    for key, replacement in {"project": context.project.root, "work": context.work,
                             "output": context.output, "python": sys.executable,
                             "version": context.project.data.get("version", "")}.items():
        value = value.replace("{" + key + "}", str(replacement))
    return value


def declared_steps(project: Project) -> list[Step]:
    """Convert validated JSON command declarations into reusable steps."""
    steps = []
    for definition in project.config.get("steps", []):
        def action(context, definition=definition):
            cwd = Path(_expand(definition.get("cwd", "{project}"), context))
            if not cwd.is_absolute():
                cwd = project.root / cwd
            context.runner.run([_expand(arg, context) for arg in definition["argv"]], cwd=cwd,
                               env={key: _expand(value, context) for key, value in definition.get("env", {}).items()})
        steps.append(Step(definition["name"], action))
    return steps


def _configured_artifacts(context: Context) -> list[Artifact]:
    result = []
    for entry in context.project.config.get("artifacts", []):
        path = Path(_expand(entry["path"], context))
        if not path.is_absolute():
            path = context.project.root / path
        result.append(Artifact(path, _expand(entry["name"], context)))
    return result


def _owned_directory(root: Path, name: str) -> Path:
    directory = root / name
    if directory.is_symlink():
        raise BuildError(f"项目目录不能是符号链接: {directory}")
    directory.mkdir(exist_ok=True)
    return directory


def build(project_path: Path | str | Project, *, steps=None, artifacts=None,
          metadata=None, ui=None, build_version=None, next_version=False,
          installed_version=None, configure=None, channel=None) -> BuildResult:
    """Select a version under lock, execute actions and publish verified files.

    ``steps`` overrides declarative commands. ``artifacts`` may be a list or a
    callable evaluated after all steps. Both receive the same project context.
    """
    tool_version = version()
    project = load_project(project_path.path if isinstance(project_path, Project) else project_path, tool_version)
    state = _owned_directory(project.root, ".build")
    output = _owned_directory(project.root, "output")
    with Lock(state / "build.lock"):
        project = load_project(project.path, tool_version)
        history = VersionHistory(project)
        selected_version = choose_version(project, build_version=build_version,
                                          next_version=next_version, installed_version=installed_version, channel=channel, history=history)
        if selected_version is not None:
            history.begin(selected_version)
            persist_version(project, selected_version)
            project = load_project(project.path, tool_version)
        if configure is not None:
            configuration = configure(project)
            if not isinstance(configuration, dict) or set(configuration) - {"steps", "artifacts", "metadata"}:
                raise BuildError("configure 必须返回 steps/artifacts/metadata 配置对象")
            steps = configuration.get("steps", steps)
            artifacts = configuration.get("artifacts", artifacts)
            metadata = {**(metadata or {}), **configuration.get("metadata", {})}
        plan = list(steps) if steps is not None else declared_steps(project)
        if not all(isinstance(step, Step) and step.name and callable(step.action) for step in plan):
            raise BuildError("步骤必须是有名称和 action 的 Step")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
        work = state / run_id
        work.mkdir()
        view = Terminal() if ui is None else ui
        with (work / "events.jsonl").open("w", encoding="utf-8", buffering=1) as events, EventStream(events, view) as sink:
            activities = Activities(sink)
            context = Context(project, work, output, Runner(work / "build.log", sink, activities=activities),
                              dict(metadata or {}), activities)
            previous = {}
            if threading.current_thread() is threading.main_thread():
                def cancel(signum, frame):
                    raise KeyboardInterrupt
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, cancel)
            try:
                started = time.monotonic()
                sink({"type": "build_start", "name": project.data["display_name"], "run_id": run_id})
                execute(plan, context, sink)
                selected = artifacts(context) if callable(artifacts) else artifacts
                if selected is None:
                    selected = _configured_artifacts(context)
                report = {"schema": 1, "run_id": run_id, "app_id": project.data["app_id"],
                          "project": str(project.path), "version": project.data.get("version"),
                          "buildflow_version": tool_version,
                          "host": {"os": platform.system().lower(), "arch": platform.machine()},
                          "targets": project.config.get("targets", []),
                          "target_os": project.config.get("target_os"), "target_arch": project.config.get("target_arch"),
                          "tools": {"buildflow": identity()}, "metadata": context.metadata,
                          "log": str(work / "build.log"), "events": str(work / "events.jsonl")}
                if selected_version is not None:
                    report["version_state"] = history.committed_state(selected_version)
                with context.activity("发布产物"):
                    sink.flush()
                    if sink.failure is not None:
                        raise BuildError(f"构建显示或事件记录失败，未发布产物: {plain(str(sink.failure))}")
                    final = publication.publish(output, work, list(selected), report)
                sink({"type": "build_end", "output": str(final),
                      "elapsed": time.monotonic() - started, "log": str(work / "build.log")})
                sink.finish()
                if sink.failure is not None:
                    # Publication is committed; a late display fault cannot undo it.
                    try:
                        print(f"[警告] 产物已发布至 {final}，显示或事件记录失败: {plain(str(sink.failure))}", file=sys.stderr)
                    except OSError:
                        pass
                return BuildResult(run_id, final, work / "report.json", context.metadata)
            except KeyboardInterrupt as exc:
                sink({"type": "error", "message": f"构建已取消；日志: {work}"})
                sink.flush()
                raise BuildError(f"构建已取消；日志: {work}", 130, reported=sink.error_delivered) from exc
            except Exception as exc:
                sink({"type": "error", "message": f"{exc}；日志: {work}"})
                sink.flush()
                if isinstance(exc, BuildError):
                    exc.reported = sink.error_delivered
                    raise
                raise BuildError(f"{exc}；日志: {work}", reported=sink.error_delivered) from exc
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
