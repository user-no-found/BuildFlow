"""Select and persist an application's strict three-part project version."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from .contracts import BuildError, Project
from .console import prompt_line
from .display_text import plain


_BASE = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_VERSION = re.compile(_BASE + r"(?:-(0[1-9]|[1-9][0-9]+))?", re.ASCII)


def validate_version(value: str) -> str:
    """Accept canonical stable x.x.x or test x.x.x-xx versions."""
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise BuildError(f"版本必须为正式 x.x.x 或测试 x.x.x-xx（例如 1.2.3-01）: {plain(str(value))}")
    return value


def version_channel(value: str) -> str:
    """Return the channel encoded by the validated version."""
    return "test" if "-" in validate_version(value) else "stable"


def next_patch(value: str) -> str:
    """Increment the patch component of a stable version."""
    if version_channel(value) != "stable":
        raise BuildError("next_patch 只接受正式版本")
    major, minor, patch = value.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def suggested_version(value: str, channel: str) -> str:
    """Advance within a channel or retain the base when switching channels."""
    current_channel = version_channel(value)
    if channel not in ("stable", "test"):
        raise BuildError("channel 必须为 stable 或 test")
    base, _, suffix = value.partition("-")
    if channel == "stable":
        return base if current_channel == "test" else next_patch(base)
    if current_channel == "stable":
        return base + "-01"
    return f"{base}-{int(suffix) + 1:02d}"


def _answer(prompt: str) -> str:
    prompt_line([(prompt, "1;36")], end="")
    try:
        answer = sys.stdin.readline()
        if not answer:
            raise BuildError("版本输入已取消（EOF），项目版本未修改", 130)
        return answer.strip()
    except KeyboardInterrupt as exc:
        raise BuildError("版本输入已取消，项目版本未修改", 130) from exc


def choose_version(project: Project, *, build_version=None, next_version=False,
                   installed_version=None, channel=None) -> str | None:
    """Choose type and version under the caller's project lock, without writes."""
    if build_version is not None and next_version:
        raise BuildError("--build-version 与 --next-version 不能同时使用")
    if channel not in (None, "stable", "test"):
        raise BuildError("--channel 必须为 stable 或 test")
    if "version" not in project.data:
        if build_version is not None or next_version or channel is not None:
            raise BuildError("请先在 project.json 声明 version")
        return None
    current = validate_version(project.data["version"])
    has_provider = callable(installed_version) or installed_version is not None
    installed = installed_version(project) if callable(installed_version) else installed_version
    installed_label = plain(str(installed)) if installed is not None else ("未安装" if has_provider else "未检测")
    prompt_line([("已安装版本: ", "2"), (installed_label, "1")])
    prompt_line([("项目版本: ", "2"), (current, "1;36")])
    if build_version is not None:
        selected_channel = version_channel(build_version)
        if channel is not None and channel != selected_channel:
            raise BuildError("--channel 与 --build-version 格式矛盾")
        return build_version
    if next_version:
        return suggested_version(current, channel or version_channel(current))
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise BuildError("非交互构建必须指定 --build-version X.Y.Z[-XX] 或 --next-version，可配 --channel")
    if channel is None:
        default = version_channel(current)
        prompt_line([("1. ", "36"), ("正式版", "1"), ("（x.x.x）", "2")])
        prompt_line([("2. ", "36"), ("测试版", "1"), ("（x.x.x-xx）", "2")])
        default_number = "1" if default == "stable" else "2"
        selected = _answer(f"构建类型 [{default_number}]: ") or default
        choices = {"stable": "stable", "test": "test", "正式": "stable", "测试": "test", "1": "stable", "2": "test"}
        if selected not in choices:
            raise BuildError("构建类型必须为 stable 或 test，项目版本未修改")
        channel = choices[selected]
    suggested = suggested_version(current, channel)
    selected = _answer(f"构建版本 [{suggested}]（回车采用）: ") or suggested
    if version_channel(selected) != channel:
        raise BuildError("输入版本格式与构建类型矛盾，项目版本未修改")
    return selected


def persist_version(project: Project, selected: str) -> None:
    """Atomically update the owning JSON file while preserving its mode."""
    validate_version(selected)
    try:
        current = json.loads(project.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"写回版本前无法重新读取项目配置: {exc}") from exc
    if current != project.data:
        raise BuildError("版本选择期间 project.json 已被修改；保留外部改动，请重新构建")
    if selected == project.data["version"]:
        return
    content = {**project.data, "version": selected}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=project.root, prefix=".project-version-",
                                         encoding="utf-8", delete=False) as file:
            temporary = Path(file.name)
            json.dump(content, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(project.path.stat().st_mode & 0o777)
        os.replace(temporary, project.path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
