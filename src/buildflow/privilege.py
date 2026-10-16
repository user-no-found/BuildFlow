"""POSIX sudo authorization for bounded build operations."""
import os
import subprocess
import sys
from .contracts import BuildError


def require_sudo(ctx, reason: str):
    """Authorize at the point of use without capturing terminal credentials."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("authorization reason must be nonempty text")
    if os.name != "posix":
        raise BuildError("当前平台不支持 sudo 授权")
    if os.geteuid() == 0:
        return
    try:
        cached = subprocess.run(["sudo", "-n", "-v"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10)
    except FileNotFoundError as exc:
        raise BuildError("sudo 不可用；请先确认管理员授权工具") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError("管理员授权检查超时，操作已停止") from exc
    if cached.returncode == 0:
        return
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise BuildError("当前操作需要管理员授权；请在交互终端执行，"
                         "或在同一终端先运行 sudo -v")
    try:
        terminal = open("/dev/tty", "r+b", buffering=0)
    except OSError as exc:
        raise BuildError("管理员授权需要可用的控制终端") from exc
    with terminal:
        ctx.notice(reason + "需要管理员权限，请在系统提示中输入密码。",
                   level="warning")
        with ctx.interaction():
            result = subprocess.run(["sudo", "-v"], stdin=terminal,
                                    stdout=terminal, stderr=terminal)
    if result.returncode:
        raise BuildError("管理员授权未完成，操作已停止", result.returncode)
