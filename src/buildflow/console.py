"""Terminal capability detection and column-aware styled text for native hosts."""
import os
import shutil
import sys
from .display_text import clipped, plain, columns


class Console:
    """Use ANSI only when the actual output stream supports terminal redraw."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty()
        self.restore_mode = None
        self.dynamic = self.tty and os.environ.get("TERM") != "dumb"
        if self.dynamic and os.name == "nt":
            self.dynamic = self._windows_vt()
        self.color = self.dynamic and "NO_COLOR" not in os.environ
        self.encoding = getattr(self.stream, "encoding", None) or "utf-8"
        try:
            "✓⠋·→".encode(self.encoding)
            self.unicode = True
        except (UnicodeError, LookupError):
            self.unicode = False

    def _windows_vt(self):
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = msvcrt.get_osfhandle(self.stream.fileno())
            mode = wintypes.DWORD()
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel.SetConsoleMode(handle, mode.value | 0x0004):
                return False
            self.restore_mode = (kernel, handle, mode.value)
            return True
        except (AttributeError, OSError, ValueError):
            return False

    def close(self):
        """Restore a Windows console mode changed by this presentation owner."""
        if self.restore_mode:
            kernel, handle, mode = self.restore_mode
            kernel.SetConsoleMode(handle, mode)
            self.restore_mode = None

    def width(self):
        try:
            width = os.get_terminal_size(self.stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            width = shutil.get_terminal_size((80, 24)).columns
        return max(1, width - 1)

    def write(self, text):
        self.stream.write(text.encode(self.encoding, errors="replace").decode(self.encoding))
        self.stream.flush()

    def styled(self, parts, *, clip=True):
        """Style individually sanitized segments after measuring visible cells."""
        result = []
        remaining = self.width() if clip and self.tty else None
        for text, style in parts:
            text = plain(str(text)).replace("\n", " ").expandtabs(4)
            text = text.encode(self.encoding, errors="replace").decode(self.encoding)
            if remaining is not None:
                text = clipped(text, remaining)
                remaining -= columns(text)
            if text:
                result.append(f"\x1b[{style}m{text}\x1b[0m" if self.color and style else text)
        return "".join(result)


def prompt_line(parts, *, end="\n"):
    """Share terminal styles with synchronous pre-build selection prompts."""
    console = Console()
    try:
        console.write(console.styled(parts, clip=False) + end)
    finally:
        console.close()
