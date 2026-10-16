"""Real controlling PTYs verify shared prompts and foreground authorization."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = '''from buildflow.contracts import Step
from buildflow.privilege import require_sudo
import time

def plan(project):
    def ordinary(ctx):
        ctx.notice("普通步骤使用共享提示", level="info")
        with ctx.activity("普通构建"):
            time.sleep(.2)
    def privileged(ctx):
        require_sudo(ctx, "隔离测试")
        with ctx.activity("完成隔离验证"):
            time.sleep(.25)
        (ctx.work / "result.txt").write_text("synthetic artifact")
    return [Step("普通步骤", ordinary), Step("需要管理员的步骤", privileged)]
'''
SUDO = '''#!/usr/bin/python3
import os, sys, termios
from pathlib import Path
state = Path(os.environ["SYNTHETIC_AUTH_STATE"])
if "-n" in sys.argv:
    sys.exit(0 if state.exists() else 1)
assert sys.argv[1:] == ["-v"]
stream = sys.stdin
original = termios.tcgetattr(stream.fileno())
hidden = list(original)
hidden[3] &= ~termios.ECHO
termios.tcsetattr(stream.fileno(), termios.TCSANOW, hidden)
try:
    print("SYNTHETIC_PASSWORD_PROMPT: ", end="", file=sys.stderr, flush=True)
    answer = stream.readline().strip()
except KeyboardInterrupt:
    sys.exit(130)
finally:
    termios.tcsetattr(stream.fileno(), termios.TCSANOW, original)
    print(file=sys.stderr, flush=True)
if answer != "SYNTHETIC_PRIVATE_INPUT":
    sys.exit(17)
state.write_text("authorized")
print("SYNTHETIC_AUTH_ACCEPTED", flush=True)
'''

@unittest.skipUnless(os.name == "posix", "POSIX controlling terminal and sudo")
class InteractionBuildTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            parent = Path(evidence)
            parent.mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName+"-", dir=parent))
        else:
            directory = tempfile.TemporaryDirectory(prefix="buildflow-interaction-")
            self.addCleanup(directory.cleanup)
            self.root = Path(directory.name)
        self.state = self.root / "authorized"
        (self.root / "sudo").write_text(SUDO)
        (self.root / "sudo").chmod(0o700)
        (self.root / "adapter.py").write_text(ADAPTER)
        self.project = self.root / "project.json"
        self.project.write_text(json.dumps({
            "app_id": "synthetic", "display_name": "共享交互测试",
            "buildflow": {"schema": 1, "artifacts": [
                {"path": "{work}/result.txt", "name": "result.txt"}]}}))
        self.env = {**os.environ, "TERM": "xterm-256color",
                    "PYTHONPATH": str(ROOT / "src")+os.pathsep+str(self.root),
                    "PATH": str(self.root), "SYNTHETIC_AUTH_STATE": str(self.state)}
        self.env.pop("NO_COLOR", None)
        self.command = [sys.executable, "-m", "buildflow", "--project",
                        str(self.project), "--adapter", "adapter:plan"]

    def events(self):
        return [json.loads(line) for p in self.root.glob(".build/*/events.jsonl")
                for line in p.read_text().splitlines()]

    def assert_private(self):
        for p in self.root.glob(".build/*/*"):
            if p.is_file() and p.name in {"build.log", "events.jsonl"}:
                self.assertNotIn("SYNTHETIC_PRIVATE_INPUT", p.read_text())
        self.assertNotIn("SYNTHETIC_PRIVATE_INPUT",
                         (self.root / "terminal.txt").read_text())

    def pty_run(self, env=None, answer=b"SYNTHETIC_PRIVATE_INPUT\n",
                expected=0, cancel=False, columns=100):
        import fcntl
        import pty
        import select
        import struct
        import termios
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execvpe(self.command[0], self.command, env or self.env)
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 30, columns, 0, 0))
        output = bytearray()
        frames = []
        prompt_at = None
        quiet_at = None
        answered = False
        status = None
        started = time.monotonic()
        try:
            while time.monotonic()-started < 10:
                now = time.monotonic()
                if prompt_at is not None and not answered and now-prompt_at >= .4:
                    self.assertEqual(len(output), quiet_at,
                                     "progress or other output overwrote the waiting password prompt")
                    os.write(master, b"\x03" if cancel else answer)
                    answered = True
                if select.select([master], [], [], .05)[0]:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    frames.append([round(now-started, 6), "o",
                                   chunk.decode("utf-8", "replace")])
                    if b"SYNTHETIC_PASSWORD_PROMPT: " in output and prompt_at is None:
                        prompt_at = time.monotonic()
                        quiet_at = len(output)
            else:
                self.fail("interactive build timed out")
            _, status = os.waitpid(pid, 0)
        finally:
            if status is None:
                os.killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)
        text = output.decode("utf-8", "replace")
        (self.root / "terminal.txt").write_text(text)
        header = {"version": 2, "width": columns, "height": 30,
                  "title": "BuildFlow foreground interaction"}
        (self.root / "terminal.cast").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in [header, *frames])+"\n")
        self.assertTrue(answered, text)
        self.assertEqual(os.waitstatus_to_exitcode(status), expected, text)
        return text

    def test_dynamic_prompt_is_separate_colored_and_waits_then_resumes(self):
        text = self.pty_run()
        self.assertIn("\x1b[33m[提示]", text)
        self.assertLess(text.index("[2/2]"), text.index("需要管理员权限"))
        self.assertLess(text.index("普通构建"), text.index("需要管理员权限"))
        self.assertNotIn("普通构建", text.split("[提示]")[1].split("\n")[0])
        self.assertIn("\x1b[36m[提示]", text)
        prompt_line = text.split("SYNTHETIC_PASSWORD_PROMPT: ")[0].rsplit("\n", 1)[-1]
        self.assertNotIn("需要管理员权限", prompt_line)
        self.assertIn("\r\x1b[2K", text.split("SYNTHETIC_AUTH_ACCEPTED")[1])
        self.assertIn("[成功]", text)
        self.assertEqual((self.root / "output/result.txt").read_text(), "synthetic artifact")
        events = self.events()
        self.assertEqual(sum(e["type"] == "interaction_start" for e in events), 1)
        self.assertEqual(sum(e["type"] == "interaction_end" for e in events), 1)
        self.assert_private()

    def test_no_color_narrow_prompt_does_not_lose_message(self):
        text = self.pty_run({**self.env, "NO_COLOR": "1"}, columns=40)
        self.assertNotIn("\x1b[33m", text)
        self.assertIn("[提示]", text)
        self.assertIn("密码。", text)
        self.assertIn("[成功]", text)
        self.assert_private()

    def test_dumb_terminal_keeps_plain_prompt_and_privacy(self):
        text = self.pty_run({**self.env, "TERM": "dumb"})
        self.assertNotIn("\x1b", text)
        self.assertIn("[提示]", text)
        self.assertIn("[成功]", text)
        self.assert_private()

    def test_declined_authorization_fails_and_restores_display(self):
        text = self.pty_run(answer=b"DECLINED\n", expected=17)
        self.assertEqual(text.count("[失败]"), 1)
        self.assertNotIn("[成功]", text)
        self.assertFalse((self.root / ".build/last-success.json").exists())
        self.assertEqual(sum(e["type"] == "interaction_end" for e in self.events()), 1)
        self.assert_private()

    def test_cancel_during_password_wait_restores_display(self):
        text = self.pty_run(expected=130, cancel=True)
        self.assertIn("构建已取消", text)
        self.assertNotIn("[成功]", text)
        self.assertEqual(sum(e["type"] == "interaction_end" for e in self.events()), 1)
        self.assert_private()

    def run_plain(self, expected):
        result = subprocess.run(self.command, cwd=self.root, env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=8)
        (self.root / "terminal.txt").write_text(result.stderr)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stderr

    def test_noninteractive_cached_authorization_has_no_prompt(self):
        self.state.write_text("authorized")
        text = self.run_plain(0)
        self.assertNotIn("需要管理员权限", text)
        self.assertNotIn("interaction_start", str(self.events()))
        self.assert_private()

    def test_noninteractive_missing_authorization_fails_at_needed_step(self):
        text = self.run_plain(1)
        self.assertIn("管理员授权", text)
        self.assertIn("[完成] [1/2]", text)
        self.assertIn("失败阶段 [2/2]", text)
        self.assertFalse((self.root / ".build/last-success.json").exists())
        self.assert_private()

    def test_missing_sudo_reports_actionable_error(self):
        (self.root / "sudo").unlink()
        text = self.run_plain(1)
        self.assertIn("sudo", text)
        self.assertIn("不可用", text)
        self.assert_private()

class InteractionStreamTests(unittest.TestCase):
    def test_nested_interaction_defers_other_output_and_restores_one_writer(self):
        import io
        import threading
        from buildflow.activity import Activities
        from buildflow.event_stream import EventStream
        seen = []
        with EventStream(io.StringIO(), lambda e: seen.append((e["type"], threading.get_ident()))) as sink:
            activities = Activities(sink)
            sink({"type": "build_start"})
            time.sleep(.15)
            with self.assertRaisesRegex(ValueError, "interrupted"):
                with activities.interaction():
                    before = len(seen)
                    time.sleep(.25)
                    self.assertEqual(len(seen), before)
                    sink({"type": "notice", "message": "deferred", "level": "info"})
                    with activities.interaction():
                        self.assertNotIn("notice", [kind for kind, _ in seen])
                    self.assertNotIn("notice", [kind for kind, _ in seen])
                    raise ValueError("interrupted")
            sink({"type": "build_end"})
        kinds = [kind for kind, _ in seen]
        self.assertEqual(kinds.count("interaction_start"), 2)
        self.assertEqual(kinds.count("interaction_end"), 2)
        self.assertGreater(kinds.index("notice"), max(i for i, k in enumerate(kinds) if k == "interaction_end"))
        self.assertEqual(len({writer for _, writer in seen}), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX sudo authorization")
    def test_display_failure_prevents_starting_terminal_authorization(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from buildflow.activity import Activities
        from buildflow.event_stream import EventStream
        from buildflow.privilege import require_sudo
        def fail(event):
            raise RuntimeError("display unavailable")
        with EventStream(io.StringIO(), fail) as sink:
            activities = Activities(sink)
            ctx = SimpleNamespace(notice=activities.notice, interaction=activities.interaction)
            with patch("buildflow.privilege.os.geteuid", return_value=1000), \
                 patch("buildflow.privilege.sys.stdin", Mock(isatty=lambda: True)), \
                 patch("buildflow.privilege.sys.stderr", Mock(isatty=lambda: True)), \
                 patch("buildflow.privilege.open", return_value=io.BytesIO(), create=True), \
                 patch("buildflow.privilege.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaisesRegex(RuntimeError, "交互操作未启动"):
                    require_sudo(ctx, "test")
                self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(os.name == "posix", "POSIX sudo authorization")
    def test_sudo_cache_timeout_stops_before_interaction(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from buildflow.contracts import BuildError
        from buildflow.privilege import require_sudo
        ctx = Mock()
        with patch("buildflow.privilege.os.geteuid", return_value=1000), \
             patch("buildflow.privilege.subprocess.run", side_effect=subprocess.TimeoutExpired("sudo", 10)):
            with self.assertRaisesRegex(BuildError, "检查超时"):
                require_sudo(ctx, "test")
        ctx.interaction.assert_not_called()
        ctx.notice.assert_not_called()



if __name__ == "__main__":
    unittest.main(verbosity=2)
