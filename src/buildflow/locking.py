"""Cross-platform advisory locks released automatically when a process exits."""
from pathlib import Path
from .contracts import BuildError


class Lock:
    """Prevent concurrent writers to one declared project or source cache."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise BuildError(f"锁路径不能是符号链接: {self.path}")
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise BuildError(f"已有构建持有锁: {self.path}", 73) from exc
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.close()
            self.handle = None
