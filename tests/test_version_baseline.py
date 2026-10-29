"""Exercise installation seeds and consumed counters through actual publication."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from buildflow.api import build
from buildflow.contracts import BuildError, Step
from buildflow.project import load_project
from buildflow.version_history import VersionHistory


class VersionBaselineTests(unittest.TestCase):
    def project(self, version=None):
        temporary = tempfile.TemporaryDirectory(prefix="buildflow-version-seed-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "project.json"
        data = {"app_id": "seedfixture", "display_name": "版本基线夹具",
                "buildflow": {"schema": 1, "steps": [{"name": "生成版本产物", "argv": [
                    "{python}", "-c", "import sys;from pathlib import Path;Path(sys.argv[1]).write_text(sys.argv[2])",
                    "{work}/version.txt", "{version}"]}],
                    "artifacts": [{"path": "{work}/version.txt", "name": "seed_{version}.txt"}]}}
        if version is not None:
            data["version"] = version
        path.write_text(json.dumps(data))
        return path

    def run_build(self, project, installed=None, **options):
        if "build_version" not in options:
            options.setdefault("next_version", True)
        with contextlib.redirect_stderr(io.StringIO()):
            return build(project, installed_version=lambda _: installed,
                         ui=lambda event: None, **options)

    def report(self, project):
        directory = project.parent / ".build"
        pointer = json.loads((directory / "last-success.json").read_text())
        return json.loads((directory / pointer["report"]).read_text())

    def check(self, project, expected):
        self.assertEqual(json.loads(project.read_text())["version"], expected)
        self.assertEqual(self.report(project)["version"], expected)
        self.assertEqual((project.parent / "output" / f"seed_{expected}.txt").read_text(), expected)

    def test_first_selection_uses_installation_or_zero_instead_of_project_value(self):
        for installed, channel, expected in (
            (None, "test", "0.0.0-01"), (None, "stable", "0.0.1"),
            ("0.0.5", "test", "0.0.5-01"), ("0.0.5", "stable", "0.0.6"),
            ("0.0.10-15", "test", "0.0.10-16"), ("0.0.10-15", "stable", "0.0.11"),
        ):
            for declared in (None, "99.99.99"):
                with self.subTest(installed=installed, channel=channel, declared=declared):
                    project = self.project(declared)
                    self.run_build(project, installed, channel=channel)
                    self.check(project, expected)

    def test_success_advances_without_installing_new_packages_and_resets_counter(self):
        project = self.project()
        for channel, expected, serial in (
            ("test", "0.0.5-01", 1), ("test", "0.0.5-02", 2),
            ("stable", "0.0.6", 0), ("test", "0.0.6-01", 1), ("stable", "0.0.7", 0),
        ):
            self.run_build(project, "0.0.5", channel=channel)
            self.check(project, expected)
            self.assertEqual(self.report(project)["version_state"]["test_serial"], serial)
        data = json.loads(project.read_text())
        data["version"] = "99.99.99-99"
        project.write_text(json.dumps(data))
        self.run_build(project, "0.0.5", channel="test")
        self.check(project, "0.0.7-01")

    def test_three_digit_test_counter_and_formal_reset(self):
        project = self.project()
        self.run_build(project, "0.0.10-99", channel="test")
        self.check(project, "0.0.10-100")
        self.run_build(project, "0.0.10-99", channel="stable")
        self.check(project, "0.0.11")
        self.assertEqual(self.report(project)["version_state"],
                         {"schema": 1, "stable": "0.0.11", "test_base": "0.0.11", "test_serial": 0})

    def test_first_failed_selection_keeps_missing_previous_and_retries_exactly(self):
        project = self.project()

        def reject(project):
            raise BuildError("拒绝配置夹具")

        with self.assertRaisesRegex(BuildError, "拒绝配置夹具"):
            self.run_build(project, build_version="8.7.6-09", configure=reject)
        attempt = json.loads((project.parent / ".build/version-attempt.json").read_text())
        self.assertIsNone(attempt["previous"])
        self.assertFalse((project.parent / ".build/last-success.json").exists())
        self.run_build(project)
        self.check(project, "8.7.6-09")

    def test_crash_before_first_project_write_is_retryable(self):
        project = self.project()
        (project.parent / ".build").mkdir()
        VersionHistory(load_project(project)).begin("5.6.7-05")
        self.assertNotIn("version", json.loads(project.read_text()))
        self.run_build(project)
        self.check(project, "5.6.7-05")

    def test_switching_from_failed_candidate_uses_unconsumed_installation_seed(self):
        project = self.project()

        def fail(context):
            raise BuildError("构建拒绝", 21)

        with self.assertRaises(BuildError):
            self.run_build(project, "0.0.5", build_version="9.8.7-15", steps=[Step("失败", fail)])
        self.run_build(project, "0.0.5", channel="stable")
        self.check(project, "0.0.6")

    def test_cancellation_and_publication_conflict_do_not_consume_counter(self):
        project = self.project()
        self.run_build(project, channel="stable")
        pointer = project.parent / ".build/last-success.json"
        before = pointer.read_bytes()

        def cancel(context):
            raise KeyboardInterrupt

        with self.assertRaises(BuildError) as caught:
            self.run_build(project, channel="test", steps=[Step("取消", cancel)])
        self.assertEqual(caught.exception.exit_code, 130)
        self.assertEqual(pointer.read_bytes(), before)
        self.assertEqual(self.report(project)["version_state"]["test_serial"], 0)
        self.run_build(project)
        self.check(project, "0.0.1-01")
        before = pointer.read_bytes()
        conflict = project.parent / "output/seed_0.0.1-02.txt"
        conflict.write_text("与候选产物冲突")
        with self.assertRaisesRegex(BuildError, "同名不同内容"):
            self.run_build(project)
        self.assertEqual(pointer.read_bytes(), before)
        self.assertEqual(self.report(project)["version_state"]["test_serial"], 1)
        conflict.unlink()
        self.run_build(project)
        self.check(project, "0.0.1-02")

    def test_installation_error_or_invalid_version_never_becomes_first_use(self):
        for installed in ("not-a-version", "0.0.1-00", ""):
            project = self.project()
            before = project.read_bytes()
            with self.assertRaises(BuildError):
                self.run_build(project, installed)
            self.assertEqual(project.read_bytes(), before)
            self.assertFalse((project.parent / ".build/version-attempt.json").exists())
        project = self.project()

        def unreadable(project):
            raise BuildError("无法读取安装版本夹具")

        with self.assertRaisesRegex(BuildError, "无法读取安装版本夹具"):
            build(project, installed_version=unreadable, next_version=True, ui=lambda _: None)
        self.assertNotIn("version", json.loads(project.read_text()))

    def test_selected_version_reaches_configure_and_missing_prompt_does_not_write(self):
        project = self.project()
        before = project.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(BuildError, "非交互构建"):
            build(project, installed_version=lambda _: None, ui=lambda _: None)
        self.assertEqual(project.read_bytes(), before)
        self.assertFalse((project.parent / ".build/version-attempt.json").exists())
        seen = []

        def configure(selected):
            seen.append(selected.data["version"])
            self.assertEqual(json.loads(project.read_text())["version"], "0.0.1")
            return {}

        self.run_build(project, channel="stable", configure=configure)
        self.assertEqual(seen, ["0.0.1"])
        self.check(project, "0.0.1")

    def test_generic_unversioned_task_remains_unversioned(self):
        project = self.project()
        before = project.read_bytes()
        build(project, steps=[], artifacts=[], ui=lambda _: None)
        self.assertEqual(project.read_bytes(), before)
        self.assertIsNone(self.report(project)["version"])
        self.assertNotIn("version_state", self.report(project))
        self.assertFalse((project.parent / ".build/version-attempt.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
