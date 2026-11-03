"""Public release metadata stays aligned when preparing a future version."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class ReleaseConfigurationTests(unittest.TestCase):
    def setUp(self):
        output = ROOT/'test-output'
        output.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='release-config-', dir=output)
        self.root = Path(self.temp.name)
        for folder in ('scripts', 'build-support', 'docs'):
            (self.root/folder).mkdir()
        for relative in ('scripts/ConfigureRelease.py', 'build-support/write_version_info.py'):
            shutil.copy2(ROOT/relative, self.root/relative)
        (self.root/'package.json').write_text(json.dumps({'version': '1.0.0'}), encoding='utf-8')
        (self.root/'package-lock.json').write_text(json.dumps({'version': '1.0.0', 'packages': {'': {'version': '1.0.0'}}}), encoding='utf-8')
        for name in ('README.md', 'README.txt', 'QUICK START.txt', 'GUIDE.html', 'Transport Options.html', 'docs/PUBLISHING.md'):
            (self.root/name).write_text('EyeCopy v1.0.0 / Setup-EyeCopy-1.0.0.exe', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def run_config(self, *args):
        return subprocess.run([sys.executable, str(self.root/'scripts/ConfigureRelease.py'), *args],
                              cwd=self.root, capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_version_repository_and_windows_metadata_stay_aligned(self):
        result = self.run_config('--version', 'v1.2.3', '--repository', 'example-owner/example-project')
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((self.root/'package.json').read_text(encoding='utf-8'))
        lock = json.loads((self.root/'package-lock.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['version'], '1.2.3')
        self.assertEqual(metadata['repository']['url'], 'https://github.com/example-owner/example-project')
        self.assertEqual(lock['version'], lock['packages']['']['version'])
        self.assertEqual(lock['version'], metadata['version'])
        self.assertIn('filevers=(1, 2, 3, 0)', (self.root/'build-support/version-info.txt').read_text(encoding='utf-8'))
        for name in ('README.md', 'GUIDE.html', 'docs/PUBLISHING.md'):
            text = (self.root/name).read_text(encoding='utf-8')
            self.assertIn('Setup-EyeCopy-1.2.3.exe', text)
            self.assertNotIn('1.0.0', text)

    def test_invalid_inputs_never_change_configuration(self):
        before = (self.root/'package.json').read_bytes()
        for args in [('--version', '1.01.0'), ('--version', '999999.1.0'),
                     ('--repository', 'https://example.org/owner/repo'),
                     ('--repository', 'https://github.com/owner/repo/releases'),
                     ('--repository', 'https://github.com/owner/repo?token=secret')]:
            with self.subTest(args=args):
                self.assertNotEqual(self.run_config(*args).returncode, 0)
                self.assertEqual((self.root/'package.json').read_bytes(), before)

    def test_repository_can_be_configured_and_disabled(self):
        self.assertEqual(self.run_config('--repository', 'https://github.com/example-owner/example-project.git').returncode, 0)
        self.assertEqual(self.run_config('--repository', '').returncode, 0)
        self.assertNotIn('repository', json.loads((self.root/'package.json').read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
