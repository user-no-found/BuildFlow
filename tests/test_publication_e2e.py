"""Real-process coverage of locked project versions and flat publication."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=evidence))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="buildflow-publication-")
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(self.root), "NO_COLOR": "1"}
        self.project = self.root / "project.json"
        self.project.write_text(json.dumps({"app_id": "demo", "display_name": "示例", "version": "1.2.3",
            "buildflow": {"schema": 1, "steps": [{"name": "生成", "argv": ["{python}", "-c",
                "import sys;from pathlib import Path;Path(sys.argv[1]).write_text(sys.argv[2])", "{work}/file", "{version}"]}],
            "artifacts": [{"path": "{work}/file", "name": "demo_{version}.bin"}]}}))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "buildflow", "--project", str(self.project), *args],
                              env=self.env, cwd=self.root, capture_output=True, text=True, timeout=10)

    def version(self):
        return json.loads(self.project.read_text())["version"]

    def test_explicit_versions_flat_output_and_collision(self):
        missing = self.run_cli()
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.version(), "1.2.3")
        for invalid in ("1.2", "01.2.3", "1.2.3-beta", "-1.2.3"):
            self.assertNotEqual(self.run_cli("--build-version=" + invalid).returncode, 0)
            self.assertEqual(self.version(), "1.2.3")
        first = self.run_cli("--next-version")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.version(), "1.2.4")
        artifact = self.root / "output/demo_1.2.4.bin"
        self.assertEqual(artifact.read_text(), "1.2.4")
        pointer = self.root / ".build/last-success.json"
        latest = json.loads(pointer.read_text())
        self.assertEqual(latest["schema"], 2)
        self.assertTrue((self.root / ".build" / latest["report"]).is_file())
        self.assertFalse((self.root / "output/runs").exists())
        self.assertFalse((self.root / "output/last-success.json").exists())
        repeated = self.run_cli("--build-version", "1.2.4")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        previous = pointer.read_bytes()
        if os.name != "nt":
            artifact.chmod(0o755)
            mode_conflict = self.run_cli("--build-version", "1.2.4")
            self.assertNotEqual(mode_conflict.returncode, 0)
            self.assertEqual(pointer.read_bytes(), previous)
            artifact.chmod(0o644)
        data = json.loads(self.project.read_text())
        data["buildflow"]["steps"][0]["argv"][-1] = "different contents"
        self.project.write_text(json.dumps(data))
        conflict = self.run_cli("--build-version", "1.2.4")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("同名不同内容", conflict.stderr)
        self.assertEqual(pointer.read_bytes(), previous)
        self.assertEqual(artifact.read_text(), "1.2.4")
        newer = self.run_cli("--build-version", "1.2.5")
        self.assertEqual(newer.returncode, 0, newer.stderr)
        self.assertTrue((self.root / "output/demo_1.2.5.bin").exists())
        self.assertTrue(artifact.exists())

    def test_channels_use_only_project_version_and_strict_suffix(self):
        for arguments, expected in [
            (("--next-version", "--channel", "test"), "1.2.3-01"),
            (("--next-version",), "1.2.3-02"),
            (("--next-version", "--channel", "stable"), "1.2.3"),
            (("--next-version",), "1.2.4"),
            (("--build-version", "1.2.4-99"), "1.2.4-99"),
            (("--next-version",), "1.2.4-100"),
        ]:
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.version(), expected)
        previous = self.project.read_bytes()
        for arguments in (("--build-version", "1.2.3-1"), ("--build-version", "1.2.3-00"),
                          ("--build-version", "1.2.3-001"), ("--channel", "stable", "--build-version", "1.2.3-01")):
            self.assertNotEqual(self.run_cli(*arguments).returncode, 0)
            self.assertEqual(self.project.read_bytes(), previous)

    def test_fresh_configure_and_failed_build_keeps_selected_version(self):
        adapter = self.root / "adapter.py"
        adapter.write_text('''from buildflow.contracts import Step,BuildError

def plan(project):
    def fail(ctx):
        assert project.data['version'] == ctx.project.data['version'] == '2.0.1'
        raise BuildError('deliberate failure', 27)
    return [Step('检查本次版本',fail)]
''')
        result = self.run_cli("--adapter", "adapter:plan", "--build-version", "2.0.1")
        self.assertEqual(result.returncode, 27, result.stderr)
        self.assertEqual(self.version(), "2.0.1")
        self.assertFalse((self.root / ".build/last-success.json").exists())
        script = self.root / "stale.py"
        script.write_text('''import json,sys
from pathlib import Path
from buildflow.project import load_project
from buildflow.api import build
p=Path(sys.argv[1]); stale=load_project(p); data=json.loads(p.read_text());data['version']='3.4.5';p.write_text(json.dumps(data))
build(stale,next_version=True)
''')
        result = subprocess.run([sys.executable, str(script), str(self.project)], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), "3.4.6")

    @unittest.skipIf(os.name == "nt", "POSIX PTY")
    def test_prompt_lock_default_manual_invalid_eof_and_cancel(self):
        import pty
        def launch():
            master, slave = pty.openpty()
            code = "from buildflow.api import build;import sys;build(sys.argv[1],installed_version='9.9.9')"
            process = subprocess.Popen([sys.executable, "-c", code, str(self.project)], cwd=self.root,
                                       env=self.env, stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = bytearray()
            while "构建类型".encode() not in output:
                self.assertTrue(select.select([master], [], [], 5)[0], output)
                output.extend(os.read(master, 8192))
            os.write(master, b"\n")
            while "（回车采用）:".encode() not in output:
                self.assertTrue(select.select([master], [], [], 5)[0], output)
                output.extend(os.read(master, 8192))
            return process, master, output
        def finish(process, master, output):
            while select.select([master], [], [], 5)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            process.wait(timeout=5)
            os.close(master)
            return output.decode()
        process, master, output = launch()
        locked = self.run_cli("--next-version")
        self.assertEqual(locked.returncode, 73)
        self.assertEqual(self.version(), "1.2.3")
        os.write(master, b"\n")
        text = finish(process, master, output)
        self.assertEqual(process.returncode, 0, text)
        self.assertIn("已安装版本: 9.9.9", text)
        self.assertIn("构建版本 [1.2.4]", text)
        self.assertEqual(self.version(), "1.2.4")
        for answer, expected in ((b"2.3.4\n", "2.3.4"), (b"2.3\n", "2.3.4"), (b"\x04", "2.3.4")):
            process, master, output = launch()
            os.write(master, answer)
            finish(process, master, output)
            self.assertEqual(self.version(), expected)
        process, master, output = launch()
        process.send_signal(signal.SIGINT)
        finish(process, master, output)
        self.assertEqual(self.version(), "2.3.4")
        process, master, output = launch()
        edited = json.loads(self.project.read_text())
        edited["display_name"] = "外部编辑保留"
        self.project.write_text(json.dumps(edited))
        os.write(master, b"\n")
        text = finish(process, master, output)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("已被修改", text)
        self.assertEqual(json.loads(self.project.read_text()), edited)

    @unittest.skipIf(os.name == "nt", "POSIX PTY")
    def test_prompt_channel_matrix_passes_selected_project_to_configure(self):
        import pty
        code = """from buildflow.api import build
from buildflow.contracts import Step
import sys
expected=sys.argv[2]
def configure(project):
    assert project.data['version']==expected
    def action(ctx):
        assert ctx.project is project
        assert ctx.project.data['version']==expected
    return {'steps':[Step('已选版本检查',action)],'artifacts':[]}
build(sys.argv[1],configure=configure,installed_version='99.99.99')
"""
        for current, answer, expected in (("1.2.3", b"\n\n", "1.2.4"),
                                          ("1.2.3-02", b"\n\n", "1.2.3-03"),
                                          ("1.2.3", b"2\n\n", "1.2.3-01"),
                                          ("1.2.3-02", b"1\n\n", "1.2.3")):
            with self.subTest(current=current, expected=expected):
                data = json.loads(self.project.read_text())
                data['version'] = current
                self.project.write_text(json.dumps(data))
                master, slave = pty.openpty()
                process = subprocess.Popen([sys.executable, '-c', code, str(self.project), expected],
                                           env=self.env, cwd=self.root, stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                output = bytearray()
                sent = False
                while select.select([master], [], [], 5)[0]:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    if not sent and '构建类型'.encode() in output:
                        os.write(master, answer)
                        sent = True
                process.wait(timeout=5)
                os.close(master)
                self.assertEqual(process.returncode, 0, output.decode())
                self.assertEqual(self.version(), expected)
                (self.root / ('prompt-' + expected + '.txt')).write_text(output.decode())

    def test_publish_error_rolls_back_only_added_files(self):
        script = self.root / "rollback.py"
        script.write_text('''from pathlib import Path
from buildflow.api import build
from buildflow.contracts import Artifact,Step,BuildError
import sys
root=Path(sys.argv[1]); (root/'output').mkdir();(root/'output/existing.bin').write_text('old')
def prepare(ctx):
    (ctx.work/'new').write_text('new');(ctx.work/'old').write_text('old');(ctx.work/'report.json').mkdir()
try:
    build(root/'project.json',next_version=True,steps=[Step('制造发布报告失败',prepare)],artifacts=lambda ctx:[Artifact(ctx.work/'old','existing.bin'),Artifact(ctx.work/'new','new.bin')])
except BuildError:
    pass
else:
    raise AssertionError('publication unexpectedly succeeded')
assert (root/'output/existing.bin').read_text()=='old'
assert not (root/'output/new.bin').exists()
assert not (root/'.build/last-success.json').exists()
''')
        result = subprocess.run([sys.executable, str(script), str(self.root)], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
