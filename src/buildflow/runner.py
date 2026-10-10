"""Execute argv commands while streaming output and preserving exit status."""
from __future__ import annotations

import codecs
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from .activity import Activities
from .command_diagnostics import OutputTail, command_label
from .contracts import BuildError, Completed, EventSink
from .platform_process import ProcessTree


_WINDOWS_BOOTSTRAP = (
    "import json,subprocess,sys; "
    "spec=json.loads(sys.stdin.readline()); "
    "p=subprocess.Popen(spec,stdin=subprocess.DEVNULL); "
    "sys.exit(p.wait())"
)


class Runner:
    """One build's subprocess runner with a continuously flushed raw log."""

    def __init__(self, log_path: Path, sink: EventSink | None = None, *, activities=None):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self.sink = sink or (lambda event: None)
        self.activities = activities or Activities(self.sink)

    def run(self, argv, cwd: Path, env=None) -> Completed:
        """Run a command, failing with its real nonzero exit status."""
        return self._execute(argv, cwd, env, capture=False)

    def capture(self, argv, cwd: Path, env=None) -> str:
        """Capture stdout while stderr and stdout still stream to the log."""
        return self._execute(argv, cwd, env, capture=True).stdout

    def _execute(self, argv, cwd, env, capture):
        import json
        args = tuple(os.fspath(arg) for arg in argv)
        if not args or not all(isinstance(arg, str) for arg in args):
            raise BuildError("命令 argv 不能为空")
        environment = os.environ.copy()
        if env:
            environment.update({str(key): str(value) for key, value in env.items()})
        identity = uuid.uuid4().hex
        started = time.monotonic()
        activity_id = self.activities.current.get()
        self.sink({"type": "command", "id": identity, "activity_id": activity_id,
                   "label": command_label(args), "argv": list(args), "cwd": str(cwd)})
        tree = ProcessTree()
        process = None
        readers = []
        chunks = []
        events = queue.Queue()
        output_tail = OutputTail()
        try:
            command = [sys.executable, "-c", _WINDOWS_BOOTSTRAP] if os.name == "nt" else args
            process = subprocess.Popen(command, cwd=cwd, env=environment,
                                       stdin=subprocess.PIPE if os.name == "nt" else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, **tree.options())
            tree.attach(process)
            if os.name == "nt":
                process.stdin.write((json.dumps(args) + "\n").encode())
                process.stdin.close()
            def read_stream(stream, label):
                try:
                    while chunk := stream.read1(8192):
                        events.put((label, chunk))
                finally:
                    stream.close()
                    events.put((label, None))
            for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = threading.Thread(target=read_stream, args=(stream, label), daemon=True)
                thread.start()
                readers.append(thread)
            with self.log_path.open("ab", buffering=0) as log:
                log.write(("\n$ " + json.dumps(args, ensure_ascii=False) + "\n").encode("utf-8"))
                ended = 0
                cleaned = False
                decoders = {label: codecs.getincrementaldecoder("utf-8")("replace") for label in ("stdout", "stderr")}
                while ended < 2:
                    if not cleaned and process.poll() is not None:
                        tree.cancel(process)
                        cleaned = True
                    try:
                        label, chunk = events.get(timeout=0.1)
                    except queue.Empty:
                        self.sink({"type": "tick"})
                        continue
                    if chunk is None:
                        ended += 1
                        tail = decoders[label].decode(b"", final=True)
                        if tail:
                            output_tail.append(label, tail)
                            self.sink({"type": "output", "id": identity, "stream": label, "text": tail,
                                       "captured": capture and label == "stdout"})
                        continue
                    log.write(chunk)
                    if capture and label == "stdout":
                        chunks.append(chunk)
                    text = decoders[label].decode(chunk)
                    output_tail.append(label, text)
                    self.sink({"type": "output", "id": identity, "stream": label, "text": text,
                               "captured": capture and label == "stdout"})
                code = process.wait()
            if code:
                message = f"命令失败，退出码 {code}: {command_label(args)}"
                if diagnostic := output_tail.summary():
                    message += "\n" + diagnostic
                raise BuildError(message, code)
            return Completed(args, code, b"".join(chunks).decode("utf-8", errors="replace"))
        except BaseException:
            if process:
                tree.cancel(process)
                process.wait()
                for reader in readers:
                    reader.join(timeout=2)
                with self.log_path.open("ab", buffering=0) as log:
                    while not events.empty():
                        label, chunk = events.get_nowait()
                        if chunk is not None:
                            log.write(chunk)
            raise
        finally:
            if process and process.poll() is not None and os.name != "nt":
                tree.cancel(process)
            tree.close()
            for reader in readers:
                reader.join(timeout=2)
            self.sink({"type": "command_end", "id": identity, "activity_id": activity_id,
                       "label": command_label(args), "returncode": process.returncode if process else None,
                       "elapsed": time.monotonic() - started})
