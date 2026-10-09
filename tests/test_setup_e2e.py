"""Isolated-home subprocess checks for shared tool initialization."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "Bash integration requires POSIX; Windows registry needs native validation")
class SetupTests(unittest.TestCase):
    """Exercise actual setup scripts without touching the current user's home."""

    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            parent = Path(evidence)
            parent.mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=parent))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="buildflow-setup-")
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.home = self.root / "home with ' quote"
        self.home.mkdir()
        self.xdg = self.home / "config with ' quote"
        self.tool = self.root / "tool with ' quote $(touch injection)"
        self.tool.mkdir()
        shutil.copytree(ROOT / "src/buildflow", self.tool / "src/buildflow", ignore=shutil.ignore_patterns("__pycache__"))
        for filename in ("setup.sh", "pyproject.toml", "build.sh"):
            shutil.copy2(ROOT / filename, self.tool / filename)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("BUILDFLOW_PATH", "FNPACKKIT_PATH", "PYTHONPATH", "BASH_ENV", "ENV")}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.xdg), PYTHON=sys.executable)
        self.rc = self.home / ".bashrc"
        self.original = b"# my private Bash configuration\nexport KEPT_VALUE='keep me'\n"
        self.rc.write_bytes(self.original)
        self.config = self.xdg / "buildflow"

    def run_command(self, argv, env=None):
        return subprocess.run(argv, cwd=self.root, env=env or self.env,
                              capture_output=True, text=True, timeout=10)

    def setup_tool(self):
        return self.run_command(["sh", str(self.tool / "setup.sh")])

    def test_register_idempotence_old_shell_and_new_bash(self):
        first = self.setup_tool()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads((self.config / "environment.json").read_text())["tools"],
                         {"BUILDFLOW_PATH": str(self.tool)})
        self.assertTrue(self.rc.read_bytes().startswith(self.original))
        backups = list(self.home.glob(".bashrc.buildflow-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        before = {path: path.read_bytes() for path in (self.rc, self.config / "environment.json", self.config / "environment.sh")}
        second = self.setup_tool()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(list(self.home.glob(".bashrc.buildflow-backup-*"))), 1)
        self.assertEqual(self.rc.read_text().count("# >>> BuildFlow tool environment >>>"), 1)
        old = self.run_command(["sh", "-c", '. "$XDG_CONFIG_HOME/buildflow/environment.sh"; exec "$BUILDFLOW_PATH/build.sh" --version'])
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(old.stdout.strip(), "0.1.0")
        new = self.run_command(["bash", "--noprofile", "-ic", 'printf "%s\\n%s\\n" "$BUILDFLOW_PATH" "$KEPT_VALUE"'])
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(new.stdout.splitlines(), [str(self.tool), "keep me"])
        self.assertFalse((self.root / "injection").exists())
        self.assertNotIn("PYTHONPATH", (self.config / "environment.sh").read_text())

    def test_explicit_override_and_empty_value_are_not_replaced(self):
        self.assertEqual(self.setup_tool().returncode, 0)
        override = self.run_command(["sh", "-c", '. "$XDG_CONFIG_HOME/buildflow/environment.sh"; printf "%s" "$BUILDFLOW_PATH"'],
                                    {**self.env, "BUILDFLOW_PATH": str(ROOT)})
        self.assertEqual(override.stdout, str(ROOT))
        code = "from buildflow.tool_environment import resolve_tool; print(resolve_tool('BUILDFLOW_PATH'))"
        explicit = self.run_command([sys.executable, "-c", code], {**self.env, "PYTHONPATH": str(ROOT / "src"), "BUILDFLOW_PATH": ""})
        self.assertNotEqual(explicit.returncode, 0)
        self.assertIn("不能是空值", explicit.stderr)
        read = self.run_command([sys.executable, "-c", code], {**self.env, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(read.stdout.strip(), str(self.tool))

    def test_multi_tool_registration_and_configuration_rejection(self):
        other = self.root / "pack kit"
        (other / "src/fnpackkit").mkdir(parents=True)
        (other / "src/fnpackkit/__init__.py").write_text('"""Fixture package."""\n')
        (other / "pyproject.toml").write_text('[project]\nname="fnpackkit"\n')
        registration = self.run_command(["sh", str(self.tool / "setup.sh"), "--register", "FNPACKKIT_PATH", str(other)])
        self.assertEqual(registration.returncode, 0, registration.stderr)
        data = json.loads((self.config / "environment.json").read_text())
        self.assertEqual(data["tools"]["FNPACKKIT_PATH"], str(other))
        previous = self.rc.read_bytes()
        (self.config / "environment.json").write_text('{"schema": 100, "tools": {}}')
        invalid = self.setup_tool()
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("schema", invalid.stderr)
        self.assertEqual(previous, self.rc.read_bytes())

    def test_invalid_marker_and_symlink_do_not_overwrite_user_config(self):
        broken = self.original + b"# >>> BuildFlow tool environment >>>\n"
        self.rc.write_bytes(broken)
        refused = self.setup_tool()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.rc.read_bytes(), broken)
        self.assertFalse((self.config / "environment.json").exists())
        self.rc.unlink()
        target = self.home / "external.rc"
        target.write_bytes(self.original)
        self.rc.symlink_to(target)
        linked = self.setup_tool()
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn("符号链接", linked.stderr)
        self.assertEqual(target.read_bytes(), self.original)
        self.rc.unlink()
        self.rc.write_bytes(self.original)
        recovered = self.setup_tool()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        backup = next(self.home.glob(".bashrc.buildflow-backup-*"))
        shutil.copy2(backup, self.rc)
        self.assertEqual(self.rc.read_bytes(), self.original)
        repeated = self.setup_tool()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(self.rc.read_text().count("# >>> BuildFlow tool environment >>>"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
