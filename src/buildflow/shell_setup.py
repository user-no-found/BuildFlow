"""Preserve and back up user files while adding one managed Bash include."""
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import uuid
from .contracts import BuildError


BEGIN = "# >>> BuildFlow tool environment >>>"
END = "# <<< BuildFlow tool environment <<<"


def current_bytes(path: Path) -> bytes | None:
    """Read regular configuration files without replacing user symlinks."""
    if path.is_symlink():
        raise BuildError(f"拒绝覆盖符号链接配置: {path}")
    if path.exists() and not path.is_file():
        raise BuildError(f"配置位置不是常规文件: {path}")
    return path.read_bytes() if path.exists() else None


def bashrc_content(path: Path, environment_file: Path) -> bytes:
    """Replace only a well-formed managed block, preserving all other bytes."""
    previous = current_bytes(path) or b""
    start_marker, end_marker = BEGIN.encode(), END.encode()
    block = (BEGIN + "\n" + f"if [ -f {shlex.quote(str(environment_file))} ]; then\n"
             + f"    . {shlex.quote(str(environment_file))}\nfi\n" + END).encode()
    starts, ends = previous.count(start_marker), previous.count(end_marker)
    if not starts and not ends:
        separator = b"" if not previous or previous.endswith(b"\n") else b"\n"
        return previous + separator + block + b"\n"
    if starts != 1 or ends != 1:
        raise BuildError(f"{path}: BuildFlow 配置区块不完整或重复，请先修正")
    start, end = previous.index(start_marker), previous.index(end_marker)
    if end < start or (start and previous[start - 1:start] != b"\n"):
        raise BuildError(f"{path}: BuildFlow 配置区块边界无效")
    end += len(end_marker)
    if end < len(previous) and previous[end:end + 1] not in (b"\n", b"\r"):
        raise BuildError(f"{path}: BuildFlow 配置区块结束标记无效")
    return previous[:start] + block + previous[end:]


def replace_with_backup(path: Path, content: bytes) -> Path | None:
    """Write atomically and back up changed originals beside the target."""
    previous = current_bytes(path)
    if previous == content:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if previous is not None:
        backup = path.with_name(path.name + ".buildflow-backup-" + uuid.uuid4().hex)
        shutil.copy2(path, backup)
    mode = path.stat().st_mode & 0o777 if previous is not None else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".buildflow-", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup
