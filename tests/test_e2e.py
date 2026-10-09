"""Native subprocess regressions for public build and discovery contracts."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NativeBuildTests(unittest.TestCase):
    """Exercise real CLI processes, files, Git objects and process trees."""

    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=directory))
            self.addCleanup(self.record)
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix="buildflow-e2e-")
            self.root = Path(self.temporary.name)
            self.addCleanup(self.temporary.cleanup)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "NO_COLOR": "1"}

    def record(self):
        (self.root / "evidence.json").write_text(json.dumps({"test": self._testMethodName,
            "python": sys.version, "platform": sys.platform, "directory": str(self.root)}, indent=2))

    def project(self, name="sample", code=None):
        project = self.root / name
        project.mkdir()
        if code is None:
            code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('原生构建',encoding='utf-8'); print('成功 中文')"
        data = {"app_id": name, "display_name": "示例应用", "buildflow": {
            "schema": 1, "requires": ">=0.1,<0.2", "targets": ["linux-amd64"],
            "steps": [{"name": "生成产物", "argv": ["{python}", "-c", code, "{work}/hello.txt"]}],
            "artifacts": [{"path": "{work}/hello.txt", "name": "hello.txt"}]}}
        path = project / "project.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *args, timeout=15):
        return subprocess.run([sys.executable, "-m", "buildflow", *map(str, args)],
            env=self.env, cwd=self.root, capture_output=True, text=True, timeout=timeout)

    def wait_file(self, path, process, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"file not created: {path}")

    def test_success_failure_preserves_publication(self):
        project = self.project()
        result = self.cli("--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stderr)
        self.assertIn("成功 中文", result.stderr)
        pointer = project.parent / ".build/last-success.json"
        previous = pointer.read_bytes()
        published = project.parent / "output"
        self.assertEqual((published / "hello.txt").read_text(), "原生构建")
        report = json.loads((project.parent / ".build" / json.loads(previous)["report"]).read_text())
        self.assertEqual(report["artifacts"][0]["size"], len("原生构建".encode()))
        data = json.loads(project.read_text())
        data["buildflow"]["steps"][0]["argv"] = ["{python}", "-c", "import sys; print('error');sys.exit(23)"]
        project.write_text(json.dumps(data))
        failed = self.cli("--project", project)
        self.assertEqual(failed.returncode, 23, failed.stderr)
        self.assertEqual(pointer.read_bytes(), previous)
        logs = list(project.parent.glob(".build/*/build.log"))
        self.assertTrue(any(b"error" in log.read_bytes() for log in logs))

    def test_generic_cli_refuses_empty_plan_without_replacing_success(self):
        project = self.project()
        built = self.cli("--project", project)
        self.assertEqual(built.returncode, 0, built.stderr)
        pointer = project.parent / ".build/last-success.json"
        previous = pointer.read_bytes()
        data = json.loads(project.read_text())
        data["buildflow"]["steps"] = []
        data["buildflow"]["artifacts"] = []
        project.write_text(json.dumps(data))
        refused = self.cli("--project", project)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("项目入口", refused.stderr)
        self.assertEqual(pointer.read_bytes(), previous)
        discovery = self.cli("--workspace", project.parent, "--discover")
        candidates = json.loads(discovery.stdout)["candidates"]
        self.assertIn("项目入口", candidates[0]["error"])
        adapter = self.root / "empty_adapter.py"
        adapter.write_text("def plan(project):\n    return []\n")
        self.env["PYTHONPATH"] += os.pathsep + str(self.root)
        explicit = self.cli("--project", project, "--adapter", "empty_adapter:plan")
        self.assertNotEqual(explicit.returncode, 0)
        self.assertEqual(pointer.read_bytes(), previous)

    def test_discovery_is_read_only_bounded_and_noninteractive(self):
        project = self.project()
        other = self.project("other")
        data = json.loads(other.read_text())
        data["buildflow"]["schema"] = 900
        data["buildflow"]["targets"] = None
        other.write_text(json.dumps(data))
        excluded = self.root / "output"
        excluded.mkdir()
        (excluded / "project.json").write_text(project.read_text())
        if os.name != "nt":
            (self.root / "linked").symlink_to(project.parent, target_is_directory=True)
        result = self.cli("--workspace", self.root, "--discover", "--adapter", "missing:bad")
        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = json.loads(result.stdout)["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(any(row["error"] for row in candidates))
        self.assertFalse((project.parent / ".build").exists())
        selection = self.cli("--workspace", self.root)
        self.assertNotEqual(selection.returncode, 0)
        self.assertIn("--project", selection.stderr)
        limited = self.cli("--workspace", self.root, "--discover", "--max-directories", 1)
        self.assertEqual(limited.returncode, 2)
        self.assertTrue(json.loads(limited.stdout)["warnings"])

    @unittest.skipIf(os.name == "nt", "POSIX signals; Windows process jobs require a native Windows host")
    def test_lock_cancellation_and_descendant_cleanup(self):
        project = self.project(code="import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); Path('child.pid').write_text(str(child.pid));print('ready',flush=True);time.sleep(60)")
        first = subprocess.Popen([sys.executable, "-m", "buildflow", "--project", str(project)],
            env=self.env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        self.wait_file(project.parent / "child.pid", first)
        duplicate = self.cli("--project", project)
        self.assertEqual(duplicate.returncode, 73, duplicate.stderr)
        first.send_signal(signal.SIGINT)
        stdout, stderr = first.communicate(timeout=8)
        self.assertEqual(first.returncode, 130, stderr)
        pid = int((project.parent / "child.pid").read_text())
        state = Path(f"/proc/{pid}/stat")
        self.assertTrue(not state.exists() or state.read_text().split()[2] == "Z")
        self.assertFalse((project.parent / ".build/last-success.json").exists())
        self.assertTrue(any(b"ready" in path.read_bytes() for path in project.parent.glob(".build/*/build.log")))
        data = json.loads(project.read_text())
        data["buildflow"]["steps"][0]["argv"] = ["{python}", "-c", "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);sys.exit(19)"]
        project.write_text(json.dumps(data))
        started = time.monotonic()
        orphan = self.cli("--project", project, timeout=8)
        self.assertEqual(orphan.returncode, 19, orphan.stderr)
        self.assertLess(time.monotonic() - started, 6)

    def test_git_head_resolved_once_then_exact_revision(self):
        upstream = self.root / "upstream"
        upstream.mkdir()
        git_env = {**self.env, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
                   "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test"}
        def git(*args):
            return subprocess.run(["git", *args], cwd=upstream, env=git_env, capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-b", "main")
        (upstream / "value.txt").write_text("first")
        git("add", ".")
        git("commit", "-m", "first")
        first_sha = git("rev-parse", "HEAD")
        script = self.root / "fetch.py"
        script.write_text("""from pathlib import Path
import sys
from buildflow.runner import Runner
from buildflow.source import resolve_git,fetch_git
root=Path(sys.argv[1]); runner=Runner(root/'source.log')
source=resolve_git(str(root/'upstream'),runner=runner)
(root/'upstream/value.txt').write_text('second')
runner.run(['git','add','.'],cwd=root/'upstream')
runner.run(['git','commit','-m','second'],cwd=root/'upstream')
fetch_git(source,root/'checkout',root/'cache',runner=runner)
(root/'resolved.txt').write_text(source.revision)
""")
        result = subprocess.run([sys.executable, str(script), str(self.root)], env=git_env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "resolved.txt").read_text(), first_sha)
        self.assertEqual((self.root / "checkout/value.txt").read_text(), "first")
        self.assertEqual((upstream / "value.txt").read_text(), "second")
        self.assertEqual((self.root / "source.log").read_text().count('"ls-remote"'), 1)
        shutil.rmtree(upstream)
        fail = subprocess.run([sys.executable, "-c", "from pathlib import Path;from buildflow.runner import Runner;from buildflow.source import resolve_git;import sys;resolve_git(sys.argv[1],runner=Runner(Path(sys.argv[2])))", str(upstream), str(self.root / "offline.log")], env=self.env, capture_output=True)
        self.assertNotEqual(fail.returncode, 0)

    def test_split_utf8_and_path_safety(self):
        project = self.project(code="import os,time,sys;from pathlib import Path;os.write(1,b'\\xe4');time.sleep(.2);os.write(1,b'\\xb8\\xad\\xe6\\x96\\x87\\n');Path(sys.argv[1]).write_text('ok')")
        result = self.cli("--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("中文", result.stderr)
        self.assertNotIn("�", result.stderr)
        previous = (project.parent / ".build/last-success.json").read_bytes()
        data = json.loads(project.read_text())
        data["buildflow"]["artifacts"][0]["name"] = "../escape.txt"
        project.write_text(json.dumps(data))
        invalid = self.cli("--project", project)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual((project.parent / ".build/last-success.json").read_bytes(), previous)
        self.assertFalse((project.parent / "output/escape.txt").exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable file permission")
    def test_published_program_remains_executable(self):
        project = self.project(code="from pathlib import Path;import sys;p=Path(sys.argv[1]);p.write_text('#!/bin/sh\\necho native-output\\n');p.chmod(0o755)")
        result = self.cli("--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        pointer = json.loads((project.parent / ".build/last-success.json").read_text())
        artifact = project.parent / "output" / "hello.txt"
        executed = subprocess.run([str(artifact)], capture_output=True, text=True, check=True)
        self.assertEqual(executed.stdout.strip(), "native-output")

    @unittest.skipIf(os.name == "nt", "POSIX pseudo-terminal fixture")
    def test_standalone_terminal_in_narrow_tty(self):
        import fcntl
        import pty
        import struct
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 24, 0, 0))
        code = "from buildflow.terminal import Terminal;t=Terminal();t({'type':'step_start','name':'中文状态显示','index':1,'total':1});t({'type':'step_end','name':'中文状态显示','elapsed':.5})"
        process = subprocess.Popen([sys.executable, "-c", code], env={**self.env, "TERM": "xterm"}, stderr=slave)
        os.close(slave)
        process.wait(timeout=5)
        chunks = []
        while True:
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        output = b"".join(chunks).decode()
        self.assertEqual(process.returncode, 0)
        self.assertIn("中文", output)
        self.assertNotIn("%", output)
        self.assertNotIn("\x1b[36m", output)
        self.assertIn("0.5s", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
