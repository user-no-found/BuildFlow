"""Real non-FPK builds exercise activity events and recorded terminal behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = '''from buildflow.contracts import BuildError, Step
import sys
import time

def plan(project):
    def work(ctx):
        with ctx.activity("扫描文案", target="linux/amd64"):
            ctx.status("核对本地目录")
            time.sleep(.65)
            with ctx.activity("验证预期拒绝"):
                try:
                    ctx.runner.capture([sys.executable, "-c", "import sys;print('EXPECTED_CAPTURE');sys.exit(23)", "--token", "PRIVATE_ARGUMENT"], cwd=ctx.work)
                except BuildError as exc:
                    assert exc.exit_code == 23
            ctx.runner.capture([sys.executable, "-c", "print('SECRET_CAPTURE')"], cwd=ctx.work)
            ctx.status("目录核对完成")
        with ctx.activity("生成原生产物", target="linux/amd64"):
            (ctx.work / "result.txt").write_text("native artifact")
    return [Step("原生应用构建", work)]
'''


class ActivityBuildTests(unittest.TestCase):
    """Keep portable CLI cases and native POSIX PTY recordings together."""

    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            parent = Path(evidence)
            parent.mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=parent))
        else:
            directory = tempfile.TemporaryDirectory(prefix="buildflow-activity-")
            self.addCleanup(directory.cleanup)
            self.root = Path(directory.name)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(self.root), "TERM": "xterm"}
        self.env.pop("NO_COLOR", None)
        self.project = self.root / "project.json"
        self.project.write_text(json.dumps({"app_id": "native", "display_name": "通用原生构建", "buildflow": {
            "schema": 1, "artifacts": [{"path": "{work}/result.txt", "name": "result.txt"}]}}))
        (self.root / "adapter.py").write_text(ADAPTER, encoding="utf-8")
        self.command = [sys.executable, "-m", "buildflow", "--project", str(self.project), "--adapter", "adapter:plan"]

    def run_cli(self, env=None):
        result = subprocess.run(self.command, cwd=self.root, env=env or self.env,
                                capture_output=True, text=True, timeout=10)
        (self.root / "terminal.txt").write_text(result.stderr, encoding="utf-8")
        return result

    def events(self):
        return [json.loads(line) for path in self.root.glob(".build/*/events.jsonl") for line in path.read_text().splitlines()]

    def test_native_activity_lifecycle_and_expected_failure(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stderr)
        self.assertIn("扫描文案", result.stderr)
        self.assertIn("linux/amd64", result.stderr)
        self.assertNotIn("PRIVATE_ARGUMENT", result.stderr)
        self.assertNotIn("SECRET_CAPTURE", result.stderr)
        self.assertNotIn("EXPECTED_CAPTURE", result.stderr)
        self.assertNotIn("[失败]", result.stderr)
        self.assertEqual((self.root / "output/result.txt").read_text(), "native artifact")
        events = self.events()
        starts = [e for e in events if e["type"] == "activity_start"]
        self.assertEqual([e["name"] for e in starts], ["扫描文案", "验证预期拒绝", "生成原生产物", "发布产物"])
        self.assertEqual(starts[1]["parent_id"], starts[0]["id"])
        commands = [e for e in events if e["type"] == "command"]
        ends = [e for e in events if e["type"] == "command_end"]
        self.assertEqual([e["id"] for e in commands], [e["id"] for e in ends])
        self.assertEqual([e["returncode"] for e in ends], [23, 0])
        self.assertEqual(commands[0]["activity_id"], starts[1]["id"])
        self.assertTrue(all(e["elapsed"] >= 0 for e in ends))
        self.assertEqual(events[-1]["type"], "build_end")

    def test_failed_build_has_one_summary_and_preserves_exit_code(self):
        (self.root / "adapter.py").write_text('''from buildflow.contracts import Step
import sys
def plan(project):
    def fail(ctx):
        with ctx.activity("验证程序"):
            ctx.runner.capture([sys.executable, "-c", "import sys;print('UNIQUE_FAILURE');sys.exit(37)"], cwd=ctx.work)
    return [Step("程序检查", fail)]
''')
        result = self.run_cli()
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(result.stderr.count("UNIQUE_FAILURE"), 1, result.stderr)
        self.assertEqual(result.stderr.count("[失败]"), 1, result.stderr)
        self.assertNotIn("[成功]", result.stderr)
        self.assertFalse((self.root / ".build/last-success.json").exists())

    def test_custom_sink_has_one_writer_and_local_heartbeats(self):
        script = '''import json, threading
from pathlib import Path
from buildflow.api import build
from adapter import plan
seen=[]
def view(event):
    seen.append((event["type"], threading.get_ident()))
build("project.json", configure=lambda p: {"steps": plan(p)}, ui=view)
Path("writer.json").write_text(json.dumps(seen))
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads((self.root / "writer.json").read_text())
        self.assertEqual(len({item[1] for item in seen}), 1)
        self.assertGreaterEqual(sum(item[0] == "tick" for item in seen), 3)
        self.assertEqual(seen[-1][0], "build_end")

    def test_writer_failure_respects_publication_and_command_error(self):
        script = '''import json, sys
from pathlib import Path
from buildflow.api import build
from buildflow.contracts import BuildError, Step
def write(ctx):
    (ctx.work / "result.txt").write_text("first")
first=build("project.json", steps=[Step("first", write)], ui=lambda e: None)
before=Path(".build/last-success.json").read_bytes()
def bad_view(event):
    raise RuntimeError("display unavailable")
def other(ctx):
    (ctx.work / "result.txt").write_text("second")
try:
    build("project.json", steps=[Step("second", other)], ui=bad_view)
    raise AssertionError("writer failure must prevent publication")
except BuildError as exc:
    assert not exc.reported
assert Path(".build/last-success.json").read_bytes()==before
assert Path("output/result.txt").read_text()=="first"
def fail(ctx):
    ctx.runner.run([sys.executable,"-c","import sys;sys.exit(37)"],cwd=ctx.work)
try:
    build("project.json", steps=[Step("failure", fail)], ui=bad_view)
    raise AssertionError("command must fail")
except BuildError as exc:
    assert exc.exit_code==37 and not exc.reported
def late_view(event):
    if event["type"]=="build_end": raise RuntimeError("late display failure")
last=build("project.json", steps=[Step("last", write)], ui=late_view)
assert last.output.is_dir()
assert json.loads(Path(".build/last-success.json").read_text())["run_id"]==last.run_id
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("产物已发布", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX PTY; Windows console needs a native host")
    def test_pty_live_local_activity_and_color(self):
        output, frames = self.pty_run(self.env)
        self.assertIn("扫描文案", output)
        self.assertIn("核对本地目录", output)
        self.assertIn("\x1b[36m", output)
        self.assertIn("\x1b[2m", output)
        self.assertNotIn("PRIVATE_ARGUMENT", output)
        self.assertNotIn("CAPTURE", output)
        self.assertGreaterEqual(output.count("核对本地目录"), 3)
        self.assertNotIn("%", output)
        self.assertNotIn("\r\x1b[2K", output[output.index("[成功]") + 4:])
        self.assertGreater(len(frames), 3)

    @unittest.skipIf(os.name == "nt", "POSIX PTY")
    def test_pty_failure_summary_is_single_and_safe(self):
        (self.root / "adapter.py").write_text('''from buildflow.contracts import Step
import sys
def plan(project):
    def fail(ctx):
        with ctx.activity("验证编译结果", target="linux/amd64"):
            ctx.runner.capture([sys.executable, "-c", "import sys;print('SAFE_FAILURE_REASON');sys.exit(37)", "--token", "PRIVATE_ARGUMENT"], cwd=ctx.work)
    return [Step("原生产物验证", fail)]
''')
        output, _ = self.pty_run(self.env, expected_code=37)
        self.assertEqual(output.count("SAFE_FAILURE_REASON"), 1)
        self.assertEqual(output.count("[失败]"), 1)
        self.assertNotIn("PRIVATE_ARGUMENT", output)
        self.assertNotIn("[成功]", output)
        self.assertIn("\x1b[31m", output)

    @unittest.skipIf(os.name == "nt", "POSIX PTY")
    def test_pty_no_color_dumb_and_ascii(self):
        output, _ = self.pty_run({**self.env, "NO_COLOR": "1"}, columns=48)
        self.assertNotIn("\x1b[36m", output)
        self.assertIn("\r\x1b[2K", output)
        self.assertIn("扫描文案", output)
        output, _ = self.pty_run({**self.env, "TERM": "dumb", "PYTHONIOENCODING": "ascii"}, columns=48)
        self.assertNotIn("\x1b", output)
        self.assertIn("linux/amd64", output)

    def pty_run(self, env, columns=80, expected_code=0):
        import fcntl
        import pty
        import select
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, columns, 0, 0))
        process = subprocess.Popen(self.command, cwd=self.root, env=env, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        frames = []
        start = time.monotonic()
        try:
            while True:
                if not select.select([master], [], [], 10)[0]:
                    self.fail("PTY output timed out")
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                frames.append([round(time.monotonic() - start, 6), "o", chunk.decode("utf-8", errors="replace")])
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        text = output.decode("utf-8", errors="replace")
        name = "terminal-" + env.get("TERM", "") + "-" + env.get("NO_COLOR", "color")
        (self.root / (name + ".txt")).write_text(text, encoding="utf-8")
        header = {"version": 2, "width": columns, "height": 30, "title": "BuildFlow native activity E2E"}
        (self.root / (name + ".cast")).write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [header, *frames]) + "\n", encoding="utf-8")
        self.assertEqual(process.returncode, expected_code, text)
        return text, frames


if __name__ == "__main__":
    unittest.main(verbosity=2)
