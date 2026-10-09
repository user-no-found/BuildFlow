"""Load and validate the BuildFlow portion of explicit project declarations."""
import json
import re
from pathlib import Path
from .contracts import BuildError, Project
from .versioning import validate_version


def check_requirement(requirement: str, actual: str) -> None:
    """Check a comma-separated numeric comparison constraint."""
    current = tuple(int(part) for part in actual.split("."))
    for term in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})\s*", term)
        if not match:
            raise BuildError(f"不支持的版本约束: {requirement}")
        operator, value = match.groups()
        expected = tuple(int(part) for part in value.split("."))
        size = max(len(current), len(expected))
        left = current + (0,) * (size - len(current))
        right = expected + (0,) * (size - len(expected))
        if not {">=": left >= right, "<=": left <= right, "==": left == right,
                ">": left > right, "<": left < right}[operator]:
            raise BuildError(f"BuildFlow {actual} 不满足 {requirement}")


def load_project(path: Path | str, version: str = "") -> Project:
    """Read JSON without importing adapters or executing project commands."""
    path = Path(path).expanduser().absolute()
    if path.is_dir():
        path = path / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BuildError(f"{path}: 顶层必须是对象")
    if "version" in data:
        validate_version(data["version"])
    config = data.get("buildflow")
    if not isinstance(config, dict) or type(config.get("schema")) is not int or config["schema"] != 1:
        raise BuildError(f"{path}: buildflow.schema 必须为 1")
    for key in ("app_id", "display_name"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise BuildError(f"{path}: {key} 必须是非空字符串")
    for key in ("steps", "artifacts", "targets"):
        if not isinstance(config.get(key, []), list):
            raise BuildError(f"{path}: buildflow.{key} 必须是列表")
    if not all(isinstance(target, str) and target for target in config.get("targets", [])):
        raise BuildError(f"{path}: targets 必须是非空字符串列表")
    for key in ("target_os", "target_arch"):
        if key in config and (not isinstance(config[key], str) or not config[key]):
            raise BuildError(f"{path}: buildflow.{key} 必须是非空字符串")
    requirement = config.get("requires", ">=0.1,<0.2")
    if not isinstance(requirement, str):
        raise BuildError(f"{path}: requires 必须是版本约束字符串")
    if version:
        check_requirement(requirement, version)
    for step in config.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("name"), str):
            raise BuildError(f"{path}: 每一步必须有 name")
        if not isinstance(step.get("argv"), list) or not step["argv"] or not all(
            isinstance(arg, str) for arg in step["argv"]
        ):
            raise BuildError(f"{path}: 步骤 argv 必须是非空字符串列表")
        if not isinstance(step.get("cwd", "{project}"), str):
            raise BuildError(f"{path}: 步骤 cwd 必须是字符串")
        env = step.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise BuildError(f"{path}: 步骤 env 必须是字符串映射")
    for artifact in config.get("artifacts", []):
        if not isinstance(artifact, dict) or not all(isinstance(artifact.get(k), str) and artifact[k] for k in ("path", "name")):
            raise BuildError(f"{path}: artifact 必须包含 path/name")
    return Project(path.resolve(), path.parent.resolve(), data, config)
