"""Track retry candidates and consumed versions at the publication commit boundary."""
import json
import os
from pathlib import Path
import tempfile

from .contracts import BuildError
from .versioning import suggested_version, validate_version, version_channel


def _read(path):
    if path.is_symlink():
        raise BuildError(f"版本记录不能是符号链接: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BuildError(f"无法读取版本记录 {path}: {error}") from error
    if not isinstance(value, dict):
        raise BuildError(f"版本记录必须是对象: {path}")
    return value


def _state(version, previous=None):
    base, _, serial = version.partition("-")
    if serial:
        return {"schema": 1, "stable": (previous or {}).get("stable"),
                "test_base": base, "test_serial": int(serial)}
    return {"schema": 1, "stable": version, "test_base": version, "test_serial": 0}


def _checked_state(value):
    if not isinstance(value, dict) or set(value) != {"schema", "stable", "test_base", "test_serial"}:
        raise BuildError("成功构建中的 version_state 字段无效")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise BuildError("不支持的成功版本记录 schema")
    for key in ("stable", "test_base"):
        item = value[key]
        if item is None and key == "stable":
            continue
        if version_channel(item) != "stable":
            raise BuildError(f"version_state.{key} 必须是正式版本")
    if type(value["test_serial"]) is not int or value["test_serial"] < 0:
        raise BuildError("version_state.test_serial 必须是非负整数")
    return value


class VersionHistory:
    """Default from committed builds; keep failed choices separate and retryable."""

    def __init__(self, project):
        self.project = project
        self.directory = project.root / ".build"
        self.attempt_path = self.directory / "version-attempt.json"
        self.run_id = None
        self.success_version = None
        self.state = None
        self.attempt = None
        pointer = self.directory / "last-success.json"
        if pointer.exists() or pointer.is_symlink():
            latest = _read(pointer)
            report_name = latest.get("report")
            if latest.get("schema") != 2 or not isinstance(report_name, str):
                raise BuildError("成功构建记录缺少 schema 2 报告引用")
            report_path = self.directory / report_name
            if not report_path.resolve().is_relative_to(self.directory.resolve()):
                raise BuildError("成功构建报告越出 .build")
            report = _read(report_path)
            if report.get("app_id") != project.data["app_id"] or report.get("project") != str(project.path):
                raise BuildError("成功构建记录与当前项目身份不一致")
            if report.get("run_id") != latest.get("run_id") or report.get("artifacts") != latest.get("artifacts"):
                raise BuildError("成功构建报告与发布指针不一致")
            self.run_id = latest["run_id"]
            if report.get("version") is not None:
                self.success_version = validate_version(report["version"])
                raw = report.get("version_state")
                self.state = _checked_state(raw) if raw is not None else _state(self.success_version)
        if self.attempt_path.exists() or self.attempt_path.is_symlink():
            attempt = _read(self.attempt_path)
            if type(attempt.get("schema")) is not int or attempt.get("schema") != 1:
                raise BuildError("不支持的候选版本记录 schema")
            if attempt.get("project") != str(project.path) or attempt.get("app_id") != project.data["app_id"]:
                raise BuildError("候选版本记录与当前项目身份不一致")
            validate_version(attempt.get("version"))
            if "previous" not in attempt:
                raise BuildError("候选版本记录缺少 previous")
            if attempt["previous"] is not None:
                validate_version(attempt["previous"])
            if attempt.get("baseline_run_id") == self.run_id:
                self.attempt = attempt

    def baseline(self, current, initial=None):
        """Prefer committed history, then the caller's installation seed."""
        if self.success_version is not None:
            return self.success_version
        if initial is not None:
            return initial
        if self.attempt and current in (self.attempt["version"], self.attempt["previous"]):
            return self.attempt["previous"]
        return current

    def default_channel(self, current, initial=None):
        if self.attempt and current in (self.attempt['version'], self.attempt['previous']):
            return version_channel(self.attempt['version'])
        return version_channel(self.baseline(current, initial))

    def suggest(self, current, channel, initial=None):
        """Retry an uncommitted choice, including a manually selected future version."""
        source = self.baseline(current, initial)
        if self.attempt and current in (self.attempt["version"], self.attempt["previous"]):
            candidate = self.attempt["version"]
            if version_channel(candidate) == channel:
                return candidate
        if self.state and source == self.success_version and channel == "test":
            return f"{self.state['test_base']}-{self.state['test_serial'] + 1:02d}"
        return suggested_version(source, channel)

    def begin(self, selected):
        """Journal before the product write, so configure errors and crashes can retry."""
        validate_version(selected)
        value = {"schema": 1, "project": str(self.project.path),
                 "app_id": self.project.data["app_id"], "version": selected,
                 "previous": self.project.data.get("version"), "baseline_run_id": self.run_id}
        if self.attempt_path.is_symlink():
            raise BuildError("候选版本记录不能是符号链接")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.directory, prefix=".version-attempt-",
                                             encoding="utf-8", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.attempt_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def committed_state(self, selected):
        """This value becomes consumed only when the success pointer is replaced."""
        validate_version(selected)
        return _state(selected, self.state)
