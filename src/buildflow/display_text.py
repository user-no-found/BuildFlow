"""Sanitize subprocess text and measure it for safe terminal presentation."""
import re
import unicodedata


_ESCAPE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")


def plain(text: str) -> str:
    """Remove terminal controls while retaining readable Unicode text."""
    return "".join(char for char in _ESCAPE.sub("", text)
                   if char in "\n\t" or not unicodedata.category(char).startswith("C"))


def clipped(text: str, width: int) -> str:
    """Clip by terminal columns, including wide CJK and combining marks."""
    output = []
    used = 0
    for char in text:
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + size > width:
            break
        output.append(char)
        used += size
    return "".join(output)


def wrapped(text: str, width: int):
    """Yield readable lines without truncating the diagnostic's ending."""
    for line in plain(text).expandtabs(4).splitlines():
        if not line:
            yield ""
        while line:
            part = clipped(line, width)
            # A one-column terminal cannot fit a wide glyph; make progress.
            if not part:
                part, line = "?", line[1:]
            else:
                line = line[len(part):]
            yield part


def columns(text: str) -> int:
    """Measure the same terminal cells used by clipped text."""
    return sum(0 if unicodedata.combining(char) else
               (2 if unicodedata.east_asian_width(char) in "WF" else 1) for char in text)


class PlainStream:
    """Strip controls across subprocess chunk boundaries without buffering payloads."""

    def __init__(self):
        self.state = "text"

    def feed(self, text):
        output = []
        for char in text:
            if self.state == "text":
                if char == "\x1b":
                    self.state = "escape"
                elif char in "\n\t" or not unicodedata.category(char).startswith("C"):
                    output.append(char)
            elif self.state == "escape":
                self.state = "csi" if char == "[" else ("string" if char in "]P^_X" else "text")
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state == "string":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if char == "\\" else "string"
        return "".join(output)
