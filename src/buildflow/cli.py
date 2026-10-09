"""Command-line project selection and composition of explicit build adapters."""
import argparse
import importlib
import json
from pathlib import Path
import sys
from .api import build
from .contracts import BuildError
from .discovery import discover
from .metadata import version
from .project import load_project


def select_project(project=None, workspaces=(), *, target=None, max_depth=4, max_directories=1000, validator=None):
    """Select a declared path; interactive discovery always requires a TTY."""
    validate = validator or (lambda path: load_project(path, version()))
    if project:
        return validate(project)
    if not workspaces and (Path.cwd() / "project.json").is_file():
        return validate(Path.cwd() / "project.json")
    if not workspaces:
        raise BuildError("请传入 --project /路径/project.json 或明确的 --workspace /路径")
    result = discover(workspaces, max_depth=max_depth, max_directories=max_directories,
                      validator=validate)
    for warning in result.warnings:
        print(f"警告: {warning}", file=sys.stderr)
    candidates = [candidate for candidate in result.candidates if not target or target in candidate.targets or (candidate.error and not candidate.targets)]
    for index, candidate in enumerate(candidates, 1):
        status = f"不可构建: {candidate.error}" if candidate.error else candidate.app_id
        print(f"{index}. {candidate.display_name or '(无名称)'} [{status}] {candidate.path}", file=sys.stderr)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise BuildError("非交互环境不能选择项目；请使用 --project")
    if not candidates:
        raise BuildError("搜索范围内没有声明接入的项目")
    try:
        choice = int(input("项目编号: "))
        if choice < 1 or choice > len(candidates):
            raise ValueError
    except (ValueError, EOFError) as exc:
        raise BuildError("无效的项目编号") from exc
    candidate = candidates[choice - 1]
    if candidate.error:
        raise BuildError(candidate.error)
    return validate(candidate.path)


def _command_project(path, has_adapter):
    project = load_project(path, version())
    if not has_adapter and not project.config.get("steps"):
        raise BuildError("未声明通用构建步骤；请使用项目入口或对应格式工具")
    return project


def main(argv=None) -> int:
    """Run a declared project or list candidates without executing adapters."""
    parser = argparse.ArgumentParser(description="BuildFlow 原生项目构建")
    parser.add_argument("--version", action="version", version=version())
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--project", type=Path)
    selection.add_argument("--workspace", type=Path, action="append", default=[])
    parser.add_argument("--discover", action="store_true", help="仅输出 JSON 发现结果")
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--max-directories", type=int, default=1000)
    parser.add_argument("--adapter", help="明确构建入口 module:function；发现时不加载")
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument("--build-version", help="本次严格 x.x.x 版本并写回项目")
    versions.add_argument("--next-version", action="store_true", help="项目 patch 版本加一并写回")
    parser.add_argument("--channel", choices=("stable", "test"), help="正式或测试构建类型")
    args = parser.parse_args(argv)
    try:
        if args.discover:
            if not args.workspace:
                raise BuildError("--discover 必须指定 --workspace")
            result = discover(args.workspace, max_depth=args.max_depth, max_directories=args.max_directories,
                              validator=lambda path: _command_project(path, bool(args.adapter)))
            print(json.dumps({"candidates": [{**vars(candidate), "path": str(candidate.path)} for candidate in result.candidates],
                              "warnings": result.warnings}, ensure_ascii=False, indent=2))
            return 0 if not result.warnings else 2
        project = select_project(args.project, args.workspace, max_depth=args.max_depth,
                                 max_directories=args.max_directories,
                                 validator=lambda path: _command_project(path, bool(args.adapter)))
        def configure(current):
            if not args.adapter:
                if not current.config.get("steps"):
                    raise BuildError("未声明通用构建步骤；请使用项目入口或对应格式工具")
                return {}
            module, separator, function = args.adapter.partition(":")
            if not separator or not module or not function:
                raise BuildError("--adapter 格式为 module:function")
            try:
                steps = list(getattr(importlib.import_module(module), function)(current))
            except TypeError as exc:
                raise BuildError("adapter 必须返回非空步骤列表") from exc
            if not steps:
                raise BuildError("adapter 返回空步骤；请使用项目入口或对应格式工具")
            return {"steps": steps}
        result = build(project, configure=configure, build_version=args.build_version,
                       next_version=args.next_version, channel=args.channel)
        print(result.output)
        return 0
    except BuildError as exc:
        print(f"BuildFlow: {exc}", file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        print(f"BuildFlow: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
