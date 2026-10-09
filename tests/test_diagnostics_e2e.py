"""Native subprocess and PTY regression coverage for failure diagnostics."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FailureDiagnosticsTests(unittest.TestCase):
    """Run wrapped commands and inspect the real build output and logs."""

    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            parent = Path(evidence)
            parent.mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=parent))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="buildflow-diagnostic-")
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(self.root),
                    "NO_COLOR": "1", "TERM": "xterm"}
        self.project = self.root / "project.json"
        self.project.write_text(json.dumps({"app_id": "sample", "display_name": "诊断示例", "buildflow": {
            "schema": 1, "steps": [{"name": "准备", "argv": ["{python}", "-c", "pass"]}], "artifacts": []}}))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "buildflow", "--project", str(self.project), *args],
                              env=self.env, cwd=self.root, capture_output=True, text=True, timeout=10)

    def test_capture_failure_and_success_diagnostics(self):
        adapter = self.root / "adapter.py"
        adapter.write_text('''from buildflow.contracts import Step
import sys

def plan(project):
    def fail(ctx):
        ctx.runner.capture([sys.executable, "-c", "import sys; print('stdout failure detail'); sys.exit(29)", "--token", "PRIVATE_ARGUMENT"], cwd=ctx.work)
    return [Step("第一阶段", lambda ctx: None), Step("第二阶段", lambda ctx: None), Step("源码验证", fail)]
''')
        failure = self.run_cli("--adapter", "adapter:plan")
        self.assertEqual(failure.returncode, 29, failure.stderr)
        self.assertIn("失败阶段 [3/3] 源码验证", failure.stderr)
        self.assertIn("stdout failure detail", failure.stderr)
        self.assertNotIn("PRIVATE_ARGUMENT", failure.stderr)
        self.assertFalse((self.root / ".build/last-success.json").exists())
        raw = next(self.root.glob(".build/*/build.log")).read_bytes()
        self.assertIn(b"PRIVATE_ARGUMENT", raw)
        self.assertIn(b"stdout failure detail", raw)
        adapter.write_text('''from buildflow.contracts import Step
import sys

def plan(project):
    def capture(ctx):
        result = ctx.runner.capture([sys.executable, "-c", "print('BIG_CAPTURE_PAYLOAD'*1000)"], cwd=ctx.work)
        assert result.startswith("BIG_CAPTURE_PAYLOAD")
    return [Step("读取数据", capture)]
''')
        success = self.run_cli("--adapter", "adapter:plan")
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertNotIn("BIG_CAPTURE_PAYLOAD", success.stderr)
        self.assertTrue(any(b"BIG_CAPTURE_PAYLOAD" in path.read_bytes() for path in self.root.glob(".build/*/build.log")))
        recorded = [json.loads(line) for path in self.root.glob(".build/*/events.jsonl")
                    for line in path.read_text().splitlines()]
        self.assertTrue(any(event.get("captured") and "BIG_CAPTURE_PAYLOAD" in event.get("text", "")
                            for event in recorded))

    @unittest.skipIf(os.name == "nt", "POSIX wrappers and PTY")
    def test_wrapped_failure_is_visible_in_tty_without_control_injection(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        self.assertIsNotNone(shutil.which("nice"))
        self.assertIsNotNone(shutil.which("taskset"))
        child = self.root / "go"
        child.write_text("#!" + sys.executable + "\nimport os,sys\nos.write(2,b'EARLY_DETAIL\\n'+b'x'*20000+b'\\n\\x1b]0;INJECT_TITLE\\x07\\x1b[31mpermission denied\\x1b[0m\\n')\nsys.exit(37)\n")
        child.chmod(0o755)
        argv = ["nice", "-n", "10", "taskset", "-c", str(min(os.sched_getaffinity(0))),
                str(child), "run", "./source", "--password", "PRIVATE_ARGUMENT"]
        data = json.loads(self.project.read_text())
        data["buildflow"]["steps"] += [{"name": "编译准备", "argv": ["{python}", "-c", "pass"]},
                                          {"name": "上游源码验证", "argv": argv}]
        self.project.write_text(json.dumps(data))
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 48, 0, 0))
        process = subprocess.Popen([sys.executable, "-m", "buildflow", "--project", str(self.project)],
                                   cwd=self.root, env=self.env, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        while True:
            if not select.select([master], [], [], 10)[0]:
                process.kill()
                self.fail("PTY output timed out")
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        process.wait(timeout=5)
        text = output.decode("utf-8")
        (self.root / "terminal.txt").write_text(text)
        self.assertEqual(process.returncode, 37)
        self.assertIn("失败阶段 [3/3] 上游源码验证", text)
        self.assertIn("nice → taskset → go run", text)
        self.assertIn("permission denied", text)
        self.assertNotIn("PRIVATE_ARGUMENT", text)
        self.assertNotIn("EARLY_DETAIL", text)
        self.assertNotIn("\x1b]0;", text)
        self.assertNotIn("\x1b[31m", text)
        self.assertLess(len(output), 30000)
        raw = next(self.root.glob(".build/*/build.log")).read_bytes()
        self.assertIn(b"EARLY_DETAIL", raw)
        self.assertIn(b"\x1b]0;INJECT_TITLE\x07", raw)
        self.assertIn(b"permission denied", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
