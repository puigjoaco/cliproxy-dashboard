"""Focused launcher/profile and installer checks, without touching the desktop."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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

    def test_windowless_pid_and_extra_flags_still_require_exact_profile(self):
        with tempfile.TemporaryDirectory(prefix='router tray ') as temp:
            profile = Path(temp) / 'browser'
            profile.mkdir()
            (profile / 'SingletonLock').symlink_to('host-1234')
            common = launcher.browser_arguments(profile)[:-1]
            flattened = '/opt/google/chrome/chrome ' + ' '.join(common + [
                '--remote-debugging-pipe', '--no-startup-window']) + '\0'
            with patch.object(Path, 'read_bytes', return_value=flattened.encode()):
                self.assertEqual(launcher.browser_pid(profile), 1234)
            with patch.object(Path, 'read_bytes', return_value=flattened.replace(str(profile), str(profile) + '-other').encode()):
                self.assertIsNone(launcher.browser_pid(profile))

    def test_warm_toggle_closes_without_launching_or_loading_a_second_script(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch.object(launcher, 'browser_context', return_value=('/chrome', directory, directory / 'browser')), patch.object(
                launcher, 'browser_pid', side_effect=[1234, None]), patch.object(
                launcher, 'control_window', return_value='closed') as control, patch.object(
                launcher.subprocess, 'Popen') as launch:
                launcher.open_dashboard()
                self.assertEqual(control.call_count, 1)
                self.assertTrue(control.call_args.args[2])
                launch.assert_not_called()

    def test_warm_launch_disables_translation_in_its_dedicated_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            def activate(pid, profile, wait, launch):
                launch()
                return 'activated'
            with patch.object(launcher, 'browser_context', return_value=('/chrome', directory, directory / 'browser')), patch.object(
                launcher, 'browser_pid', return_value=1234), patch.object(
                launcher, 'control_window', side_effect=activate), patch.object(
                launcher.subprocess, 'Popen') as launch:
                launcher.open_dashboard()
                args = launch.call_args.args[0]
                self.assertIn('--disable-features=Translate', args)
                self.assertIn('--user-data-dir=' + str(directory / 'browser'), args)
                self.assertNotIn('--remote-debugging-port=0', args)
                self.assertEqual(launch.call_count, 1)

    def test_warm_preferences_preserve_unrelated_state_and_use_private_pipes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            profile = directory / 'browser'
            profile.mkdir()
            state = profile / 'Local State'
            state.write_text(json.dumps({'existing': {'value': 42}, 'background_mode': {'other': True}}))
            child = MagicMock(pid=1234)
            child.wait.return_value = 0
            with patch.object(launcher, 'browser_context', return_value=('/chrome', directory, profile)), patch.object(
                launcher, 'browser_pid', side_effect=[None, None, 1234]), patch.object(
                launcher.subprocess, 'Popen', return_value=child) as launch:
                launcher.warm_browser()
            self.assertEqual(json.loads(state.read_text()), {'existing': {'value': 42},
                'background_mode': {'other': True, 'enabled': False}})
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(launch.call_args.kwargs['stdin'], launcher.subprocess.PIPE)
            self.assertEqual(launch.call_args.kwargs['stdout'], launcher.subprocess.PIPE)
            self.assertIn('--pipe-browser', launch.call_args.args[0])
            child.stdin.close.assert_called_once()
            child.stdout.close.assert_called_once()

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


class WarmServiceTests(unittest.TestCase):
    def test_install_and_remove_only_the_owned_service(self):
        with tempfile.TemporaryDirectory(prefix='router tray % $ ') as temp:
            config = Path(temp) / 'config'
            package = Path(temp) / 'package'
            unrelated = config / 'systemd/user/unrelated.service'
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text('preserve')
            unit = config / 'systemd/user' / installer.WARM_SERVICE
            with patch.object(installer, 'run') as run:
                installer.set_warm_service(config, Path(temp), package, False)
                text = unit.read_text()
                self.assertIn(' --warm-browser\n', text)
                self.assertIn(str(package).replace('%', '%%').replace('$', '$$'), text)
                self.assertIn('Environment="XDG_DATA_HOME=' + str(Path(temp)).replace('%', '%%') + '"', text)
                self.assertIn('PartOf=graphical-session.target', text)
                self.assertIn('KillMode=control-group', text)
                run.assert_called_with('systemctl', '--user', 'enable', '--now', installer.WARM_SERVICE)
                installer.set_warm_service(config, Path(temp), package, True)
                self.assertFalse(unit.exists())
                self.assertEqual(unrelated.read_text(), 'preserve')
                self.assertIn(('systemctl', '--user', 'disable', '--now', installer.WARM_SERVICE),
                              [c.args for c in run.call_args_list])



class WindowRuleTests(unittest.TestCase):
    """KWin rule install/remove with every KDE command faked; never runs kreadconfig6 or kwriteconfig6."""

    def apply(self, existing, remove):
        calls = []

        def fake_run(*args):
            calls.append(args)
            return existing if args[0] == 'kreadconfig6' else ''

        with patch.object(installer, 'run', side_effect=fake_run):
            installer.set_window_rule(remove)
        general = {args[args.index('--key') + 1]: args[-1] for args in calls
                   if args[0] == 'kwriteconfig6' and args[args.index('--group') + 1] == 'General'}
        rule = [args for args in calls if args[0] == 'kwriteconfig6' and args[args.index('--group') + 1] == installer.RULE]
        self.assertEqual(calls[-1], ('qdbus6', 'org.kde.KWin', '/KWin', 'org.kde.KWin.reconfigure'))
        return general, rule

    def test_install_appends_rule_and_keeps_unrelated_rules(self):
        general, rule = self.apply('whatsapp-companion-tray-only', remove=False)
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only,' + installer.RULE, 'count': '2'})
        self.assertEqual({args[-2]: args[-1] for args in rule}, installer.RULE_KEYS)

    def test_reinstall_does_not_duplicate_the_rule(self):
        general, _ = self.apply('whatsapp-companion-tray-only,' + installer.RULE, remove=False)
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only,' + installer.RULE, 'count': '2'})

    def test_uninstall_deletes_only_the_dedicated_rule(self):
        general, rule = self.apply(installer.RULE + ',whatsapp-companion-tray-only', remove=True)
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only', 'count': '1'})
        self.assertEqual(sorted(args[-2] for args in rule), sorted(installer.RULE_KEYS))
        self.assertTrue(all(args[-1] == '--delete' for args in rule))

    def test_first_install_without_existing_rules(self):
        general, _ = self.apply('', remove=False)
        self.assertEqual(general, {'rules': installer.RULE, 'count': '1'})


if __name__ == '__main__':
    unittest.main()
