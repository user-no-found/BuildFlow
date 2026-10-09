"""Render build events as a compact terminal status or stable plain text."""
import os
import shutil
import sys
import time
from .display_text import clipped, plain, wrapped


class Terminal:
    """Independent callable event sink; it never starts build processes."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty()
        self.color = self.tty and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        self.dynamic = self.tty and os.environ.get("TERM") != "dumb"
        self.started = time.monotonic()
        self.build_started = self.started
        self.stage = ""
        self.last_draw = 0.0
        self.active = False

    def _write(self, text):
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        self.stream.write(text.encode(encoding, errors="replace").decode(encoding))
        self.stream.flush()

    def _width(self):
        return max(1, shutil.get_terminal_size((80, 24)).columns - 1)

    def _line(self, text, color=None):
        if self.active:
            self._write("\r\x1b[2K")
            self.active = False
        text = plain(text).rstrip()
        if self.tty:
            text = clipped(text, self._width())
        if self.color and color:
            text = f"\x1b[{color}m{text}\x1b[0m"
        self._write(text + "\n")

    def __call__(self, event):
        kind = event["type"]
        if kind == "build_start":
            self.build_started = time.monotonic()
            self._line("BuildFlow | 原生构建", "36")
            self._line(event["name"])
            self._line("-" * min(48, self._width()))
        elif kind == "step_start":
            self.stage = f"[{event['index']}/{event['total']}] {event['name']}"
            self.started = time.monotonic()
            if not self.dynamic:
                self._line(self.stage)
        elif kind == "output" and not self.dynamic and not event.get("captured"):
            self._write(plain(event["text"]))
        elif kind == "step_end":
            self._line(f"[完成] {event['elapsed']:.1f}s | {self.stage}", "32")
        elif kind == "build_end":
            elapsed = event.get("elapsed", time.monotonic() - self.build_started)
            self._line("-" * min(48, self._width()))
            self._line(f"[成功] 总耗时 {elapsed:.1f}s", "32")
            self._line(f"产物: {event['output']}")
            if event.get("log"):
                self._line(f"日志: {event['log']}")
        elif kind == "warning":
            self._line(f"[警告] {event['message']}", "33")
        elif kind == "error":
            self._line(f"[失败] 总耗时 {time.monotonic() - self.build_started:.1f}s", "31")
            for line in wrapped(event["message"], self._width() if self.tty else 10000):
                self._line(line, "31")
        if self.dynamic and kind in {"tick", "step_start", "output"}:
            now = time.monotonic()
            if now - self.last_draw < 0.1:
                return
            self.last_draw = now
            spinner = "|/-\\"[int(now * 8) % 4]
            status = clipped(f"{spinner} {now - self.started:.1f}s | {self.stage}", self._width())
            if self.color:
                status = f"\x1b[36m{status}\x1b[0m"
            self._write("\r\x1b[2K" + status)
            self.active = True
