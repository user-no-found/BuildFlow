"""Bound command failure summaries without exposing arbitrary arguments."""
from pathlib import Path
import re
from .display_text import plain


class OutputTail:
    """Keep at most 8,192 characters per stream, display six short lines each."""

    def __init__(self):
        self.streams = {"stdout": "", "stderr": ""}

    def append(self, stream: str, text: str) -> None:
        self.streams[stream] = (self.streams[stream] + text)[-8192:]

    def summary(self) -> str:
        sections = []
        for stream in ("stderr", "stdout"):
            lines = plain(self.streams[stream]).strip().splitlines()[-6:]
            if lines:
                # Keep line endings: tools often append the actual cause there.
                lines = [("…" + line[-400:]) if len(line) > 400 else line for line in lines]
                sections.append(f"{stream} 尾部:\n" + "\n".join(lines))
        return "\n".join(sections)


def command_label(argv) -> str:
    """Skip recognized nice/taskset wrappers and label only the safe leaf command."""
    index = 0
    labels = []
    while index < len(argv):
        program = Path(argv[index]).name
        if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", program):
            labels.append("程序")
            break
        labels.append(program)
        index += 1
        if program == "nice":
            if index < len(argv) and argv[index] in ("-n", "--adjustment"):
                if index + 1 >= len(argv) or not re.fullmatch(r"[+-]?\d+", argv[index + 1]):
                    break
                index += 2
            elif index < len(argv) and re.fullmatch(r"(?:--adjustment=|-)[+-]?\d+", argv[index]):
                index += 1
        elif program == "taskset":
            if index < len(argv) and argv[index] in ("-c", "--cpu-list"):
                index += 1
            if index >= len(argv) or not re.fullmatch(r"[0-9a-fA-Fx,:\-]+", argv[index]):
                break
            index += 1
        else:
            if program in ("go", "go.exe") and index < len(argv) and argv[index] in ("run", "build", "test", "vet", "version", "list", "generate"):
                labels[-1] += " " + argv[index]
            break
        if index < len(argv) and argv[index] == "--":
            index += 1
        if index < len(argv) and argv[index].startswith("-"):
            break
        if len(labels) >= 4:
            break
    return labels[-1] if labels else "程序"
