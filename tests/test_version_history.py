"""Publication-level checks for consumed versions and retryable candidates."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_publication_e2e as publication


class VersionHistoryTests(unittest.TestCase):
    setUp = publication.PublicationTests.setUp
    run_cli = publication.PublicationTests.run_cli
    version = publication.PublicationTests.version

    def latest(self):
        pointer = json.loads((self.root / '.build/last-success.json').read_text())
        return json.loads((self.root / '.build' / pointer['report']).read_text())

    def steps_fail(self, enabled=True):
        data = json.loads(self.project.read_text())
        if enabled:
            self.original = data['buildflow']['steps']
            data['buildflow']['steps'] = [{'name': 'deliberate failure',
                'argv': ['{python}', '-c', 'import sys;sys.exit(21)']}]
        else:
            data['buildflow']['steps'] = self.original
        self.project.write_text(json.dumps(data))

    def test_formal_success_resets_internal_serial_then_test_starts_at_one(self):
        for version in ('0.0.0-02', '0.0.1'):
            result = self.run_cli('--build-version', version)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.latest()['version_state'],
                         {'schema': 1, 'stable': '0.0.1', 'test_base': '0.0.1', 'test_serial': 0})
        result = self.run_cli('--next-version', '--channel', 'test')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), '0.0.1-01')
        self.assertEqual(self.latest()['version_state']['test_serial'], 1)
        self.assertEqual(self.run_cli('--next-version').returncode, 0)
        self.assertEqual(self.version(), '0.0.1-02')

    def test_failed_formal_and_manual_test_candidates_are_retried_exactly(self):
        self.assertEqual(self.run_cli('--build-version', '0.0.1').returncode, 0)
        before = (self.root / '.build/last-success.json').read_bytes()
        for candidate in ('0.0.2', '0.0.2-05'):
            self.steps_fail()
            failed = self.run_cli('--build-version', candidate)
            self.assertEqual(failed.returncode, 21, failed.stderr)
            retry = self.run_cli('--next-version')
            self.assertEqual(retry.returncode, 21, retry.stderr)
            self.assertEqual(self.version(), candidate)
            self.assertEqual((self.root / '.build/last-success.json').read_bytes(), before)
            self.steps_fail(False)
            succeeded = self.run_cli('--next-version')
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertEqual(self.version(), candidate)
            before = (self.root / '.build/last-success.json').read_bytes()
        self.assertEqual(self.run_cli('--next-version').returncode, 0)
        self.assertEqual(self.version(), '0.0.2-06')

    def test_configure_failure_is_also_retryable(self):
        code = "from buildflow.api import build;import sys;build(sys.argv[1],build_version='5.6.7-05',configure=lambda project:1/0)"
        failed = subprocess.run([sys.executable, '-c', code, str(self.project)],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        succeeded = self.run_cli('--next-version')
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertEqual(self.version(), '5.6.7-05')

    def test_cancellation_keeps_candidate_and_success_state(self):
        adapter = self.root / 'cancel_adapter.py'
        adapter.write_text("from buildflow.contracts import Step\ndef plan(project):\n def cancel(ctx): raise KeyboardInterrupt\n return [Step('cancel',cancel)]\n")
        failed = self.run_cli('--adapter', 'cancel_adapter:plan', '--build-version', '0.0.2-05')
        self.assertEqual(failed.returncode, 130, failed.stderr)
        self.assertFalse((self.root / '.build/last-success.json').exists())
        self.assertEqual(self.run_cli('--next-version').returncode, 0)
        self.assertEqual(self.version(), '0.0.2-05')

    def test_crash_between_journal_and_product_write_retains_candidate_channel(self):
        code = "from buildflow.project import load_project;from buildflow.version_history import VersionHistory;import sys;from pathlib import Path;p=load_project(sys.argv[1]);(p.root/'.build').mkdir();VersionHistory(p).begin('9.8.7-05')"
        prepared = subprocess.run([sys.executable, '-c', code, str(self.project)],
                                  cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(self.version(), '1.2.3')
        succeeded = self.run_cli('--next-version')
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertEqual(self.version(), '9.8.7-05')

    def test_publication_failure_does_not_consume_version(self):
        self.assertEqual(self.run_cli('--build-version', '1.2.4').returncode, 0)
        (self.root / 'output/demo_1.2.5.bin').write_text('conflicting artifact')
        failed = self.run_cli('--next-version')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.latest()['version'], '1.2.4')
        (self.root / 'output/demo_1.2.5.bin').unlink()
        self.assertEqual(self.run_cli('--next-version').returncode, 0)
        self.assertEqual(self.version(), '1.2.5')

    @unittest.skipIf(os.name == 'nt', 'POSIX hard termination')
    def test_abrupt_parent_death_keeps_candidate(self):
        adapter = self.root / 'sleep_adapter.py'
        adapter.write_text("from buildflow.contracts import Step\nimport time\ndef plan(project):\n def sleep(ctx):\n  (ctx.project.root/'ready').touch()\n  time.sleep(60)\n return [Step('sleep',sleep)]\n")
        process = subprocess.Popen([sys.executable, '-m', 'buildflow', '--project', str(self.project),
                                   '--adapter', 'sleep_adapter:plan', '--build-version', '8.9.0-07'],
                                  cwd=self.root, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'ready').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / 'ready').exists())
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            process.wait(timeout=5)
        succeeded = self.run_cli('--next-version')
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertEqual(self.version(), '8.9.0-07')


if __name__ == '__main__':
    unittest.main(verbosity=2)
