"""Register shared checkout locations for tool entry scripts and new shells."""
import argparse
import json
import os
from pathlib import Path
import sys
from .contracts import BuildError
from .locking import Lock
from .shell_setup import bashrc_content, current_bytes, replace_with_backup
from .tool_environment import checked_tool, config_directory, read_tools, shell_environment
from .windows_setup import register_environment


def register_tools(registrations: dict[str, str | Path]) -> list[Path]:
    """Merge explicit tool paths, back up changes and persist host integration."""
    if not registrations:
        raise BuildError("至少需要登记一个工具路径")
    selected = {name: str(checked_tool(name, value)) for name, value in registrations.items()}
    directory = config_directory()
    directory.mkdir(parents=True, exist_ok=True)
    backups = []
    with Lock(directory / "setup.lock"):
        tools = {**read_tools(directory), **selected}
        declaration = directory / "environment.json"
        environment = directory / "environment.sh"
        document = (json.dumps({"schema": 1, "tools": tools}, ensure_ascii=False, indent=2) + "\n").encode()
        plan = [(declaration, document), (environment, shell_environment(tools).encode())]
        if os.name != "nt":
            bashrc = Path.home() / ".bashrc"
            plan.append((bashrc, bashrc_content(bashrc, environment)))
        # Validate every destination before making any persistent content change.
        for path, _ in plan:
            current_bytes(path)
        if os.name == "nt":
            backup = register_environment(selected, directory)
            if backup:
                backups.append(backup)
        for path, content in plan:
            backup = replace_with_backup(path, content)
            if backup:
                backups.append(backup)
    return backups


def register_tool(name: str, path: str | Path) -> list[Path]:
    """Register one checkout through the same multi-tool merge operation."""
    return register_tools({name: path})


def main(argv=None) -> int:
    """Provide a shared setup command for BuildFlow and its dependent tools."""
    parser = argparse.ArgumentParser(description="登记当前用户的构建工具路径")
    parser.add_argument("--register", nargs=2, action="append", required=True,
                        metavar=("VARIABLE", "CHECKOUT"))
    args = parser.parse_args(argv)
    try:
        registrations = dict(args.register)
        if len(registrations) != len(args.register):
            raise BuildError("同一工具变量不能重复登记")
        backups = register_tools(registrations)
        print(f"工具环境已配置: {config_directory() / 'environment.json'}")
        for name, path in registrations.items():
            print(f"{name}={Path(path).expanduser().resolve()}")
        for backup in backups:
            print(f"原配置备份: {backup}")
        if os.name == "nt":
            print("新终端将使用用户环境变量；现有终端需重新打开，或由接入 resolve_tool 的入口读取用户配置。")
        else:
            print("新 Bash 终端自动加载；现有终端的应用入口可直接读取共同工具环境。")
        return 0
    except (BuildError, OSError, ValueError) as exc:
        print(f"BuildFlow setup: {exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, BuildError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
