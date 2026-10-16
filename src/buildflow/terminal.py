"""Render semantic build events with truthful activity and command lifetimes."""
import time
from .console import Console
from .display_text import PlainStream, clipped, columns, wrapped


class Terminal:
    """Callable event sink; one event writer owns its mutable presentation state."""

    def __init__(self, stream=None):
        self.console = Console(stream)
        self.stream = self.console.stream
        self.tty = self.console.tty
        self.color = self.console.color
        self.dynamic = self.console.dynamic
        self.started = time.monotonic()
        self.build_started = self.started
        self.stage = ""
        self.last_draw = 0.0
        self.active = False
        self.running = False
        self.activities = {}
        self.commands = {}
        self.detail = ""
        self.output_streams = {}
        self.interactions = 0

    def close(self):
        """Release console capabilities after the final event has been rendered."""
        try:
            self._clear()
        finally:
            self.console.close()

    def _clear(self):
        if self.active:
            self.console.write("\r\x1b[2K")
            self.active = False

    def _line(self, *parts):
        self._clear()
        self.console.write(self.console.styled(parts) + "\n")

    def _symbol(self, unicode, ascii):
        return unicode if self.console.unicode else ascii

    def _name(self, event):
        return event["name"] + (" · " + event["target"] if event.get("target") else "")

    def _result(self, name, elapsed, *, completed=True):
        icon = self._symbol("✓", "+") if completed else "-"
        tail = f"  {elapsed:.1f}s"
        if self.tty:
            name = clipped(name, max(0, self.console.width() - columns(f"  {icon} " + tail)))
        self._line((f"  {icon} ", "32" if completed else "2"),
                   (name, None), (tail, "2"))

    def __call__(self, event):
        kind = event["type"]
        now = time.monotonic()
        if kind == "build_start":
            self.build_started = now
            self.running = True
            self._line(("BuildFlow", "1;36"), (" · 原生构建", "2"))
            self._line((event["name"], "1"))
            self._separator()
        elif kind == "step_start":
            self.stage = f"[{event['index']}/{event['total']}] {event['name']}"
            self.started = now
            self.detail = ""
            self.running = True
            self._line((self.stage, "1;36"))
        elif kind == "activity_start":
            self.activities[event["id"]] = {**event, "started": now, "detail": ""}
            if not self.dynamic:
                self._line(("  > ", "36"), (self._name(event), None))
        elif kind == "activity_status":
            activity = self.activities.get(event.get("activity_id"))
            if activity is not None:
                activity["detail"] = event["message"]
            else:
                self.detail = event["message"]
            if not self.dynamic:
                self._line(("    " + event["message"], "2"))
        elif kind == "activity_end":
            self.activities.pop(event["id"], None)
            # A caught negative control is not a failed build. Only error is final.
            completed = event["outcome"] == "completed"
            name = self._name(event) + ("" if completed else " · 操作已结束")
            self._result(name, event["elapsed"], completed=completed)
        elif kind == "command":
            self.commands[event.get("id", "")] = {**event, "started": now}
            if not self.dynamic and not event.get("activity_id"):
                self._line(("  > ", "36"), (event.get("label", "执行命令"), None))
        elif kind == "command_end":
            command = self.commands.pop(event["id"], None)
            if command and not command.get("activity_id"):
                code = event["returncode"]
                name = event["label"] + (f" · 退出码 {code}" if code else "")
                self._result(name, event["elapsed"], completed=code == 0)
            for key in list(self.output_streams):
                if key[0] == event["id"]:
                    del self.output_streams[key]
        elif kind == "output" and not self.dynamic and not event.get("captured"):
            key = (event.get("id", ""), event["stream"])
            sanitizer = self.output_streams.setdefault(key, PlainStream())
            self.console.write(sanitizer.feed(event["text"]))
        elif kind == "step_end":
            tail = f"  {event['elapsed']:.1f}s"
            name = clipped(self.stage, max(0, self.console.width() - columns("[完成] " + tail))) if self.tty else self.stage
            self._line(("[完成] ", "32"), (name, "1"), (tail, "2"))
            self.stage = ""
            self.detail = ""
        elif kind == "build_end":
            self.running = False
            self._separator()
            self._line(("[成功] ", "32"), (f"总耗时 {event.get('elapsed', now - self.build_started):.1f}s", "1"))
            self._path("产物", event["output"])
            if event.get("log"):
                self._path("日志", event["log"])
        elif kind == "notice":
            style = {"info": "36", "warning": "33", "error": "31"}[event["level"]]
            for line in wrapped("[提示] " + event["message"],
                                self.console.width() if self.tty else 10000):
                self._line((line, style))
        elif kind == "interaction_start":
            self.interactions += 1
            self._clear()
        elif kind == "interaction_end":
            self.interactions -= 1
            if self.interactions == 0 and self.tty:
                self.console.write("\n")
        elif kind == "warning":
            self._line(("[警告] ", "33"), (event["message"], None))
        elif kind == "error":
            self.running = False
            self._line(("[失败] ", "31"), (f"总耗时 {now - self.build_started:.1f}s", "1"))
            for line in wrapped(event["message"], self.console.width() if self.tty else 10000):
                self._line((line, "31"))
        if self.dynamic and self.running and not self.interactions and kind not in {"output", "step_end", "build_start"}:
            self._draw(now, force=kind != "tick")

    def _separator(self):
        self._line((("─" if self.console.unicode else "-") * min(56, self.console.width()), "2"))

    def _path(self, label, value):
        for line in wrapped(f"{label}: {value}", self.console.width() if self.tty else 10000):
            self._line((line, "2" if label == "日志" else None))

    def _draw(self, now, *, force=False):
        if not force and now - self.last_draw < .09:
            return
        self.last_draw = now
        activity = next(reversed(self.activities.values()), None)
        command = next(reversed(self.commands.values()), None)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if self.console.unicode else "|/-\\"
        parts = [("  " + spinner[int(now * 10) % len(spinner)] + " ", "36")]
        if activity:
            parts.append((self._name(activity), "1"))
            parts.append((f"  {now - activity['started']:.1f}s", "2"))
            if activity["detail"]:
                parts.append((" · " + activity["detail"], "2"))
        elif command:
            parts.append((command.get("label", "执行命令"), None))
        else:
            parts.append((self.stage or "整理构建结果", None))
        if command:
            if activity:
                parts.append((" · " + command.get("label", "执行命令"), None))
            parts.append((f"  {now - command['started']:.1f}s", "2"))
        elif not activity:
            parts.append((f"  {now - self.started:.1f}s", "2"))
            if self.detail:
                parts.append((" · " + self.detail, "2"))
        self.console.write("\r\x1b[2K" + self.console.styled(parts))
        self.active = True
