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
