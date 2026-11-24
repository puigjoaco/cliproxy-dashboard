"""Focused launcher/profile and installer checks, without touching the desktop."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = load('launcher', 'desktop/plasma-tray/package/contents/code/open_dashboard.py')
installer = load('installer', 'desktop/plasma-tray/install.py')


class LauncherTests(unittest.TestCase):
    def test_singleton_pid_requires_the_exact_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'browser'
            profile.mkdir()
            (profile / 'SingletonLock').symlink_to('host-1234')
            with patch.object(Path, 'read_bytes', return_value=f'chrome\0--user-data-dir={profile}\0'.encode()):
                self.assertEqual(launcher.browser_pid(profile), 1234)
            with patch.object(Path, 'read_bytes', return_value=b'chrome\0--user-data-dir=/some/other/profile\0'):
                self.assertIsNone(launcher.browser_pid(profile))
            flattened = '/opt/google/chrome/chrome ' + ' '.join(launcher.browser_arguments(profile)) + '\0'
            with patch.object(Path, 'read_bytes', return_value=flattened.encode()):
                self.assertEqual(launcher.browser_pid(profile), 1234)
            with patch.object(Path, 'read_bytes', return_value=flattened.replace(str(profile), str(profile) + '-other').encode()):
                self.assertIsNone(launcher.browser_pid(profile))
            (profile / 'SingletonLock').unlink()
            self.assertIsNone(launcher.browser_pid(profile))

    def test_missing_browser_requests_fallback_without_launching(self):
        with patch.object(launcher.shutil, 'which', return_value=None), patch.object(launcher.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Chrome o Chromium'):
                launcher.open_dashboard()
            launch.assert_not_called()

    def test_uninstall_removes_only_the_dedicated_profile(self):
        with tempfile.TemporaryDirectory(prefix='router tray ') as temp:
            home = Path(temp)
            profile = home / 'router-personal-tray/browser'
            profile.mkdir(parents=True)
            (profile / 'data').write_text('dedicated profile')
            unrelated = home / 'unrelated'
            unrelated.write_text('preserve')
            installer.remove_browser_profile(home)
            self.assertFalse(profile.exists())
            self.assertEqual(unrelated.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
