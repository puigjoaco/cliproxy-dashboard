"""Focused launcher/profile and installer checks, without touching the desktop."""
import importlib.util
import json
from pathlib import Path
import subprocess
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
            common = launcher.browser_arguments(profile)
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
            def activate(pid, profile, wait, url, launch):
                self.assertEqual(url, 'http://localhost:9000')
                launch()
                return 'activated'
            with patch.object(launcher, 'browser_context', return_value=('/chrome', directory, directory / 'browser')), patch.object(
                launcher, 'browser_pid', return_value=1234), patch.object(
                launcher, 'control_window', side_effect=activate), patch.object(
                launcher.subprocess, 'Popen') as launch:
                launcher.open_dashboard('http://LOCALHOST:9000/')
                args = launch.call_args.args[0]
                self.assertEqual(args[-1], '--app=http://localhost:9000')
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

    def test_invalid_address_never_launches(self):
        with patch.object(launcher, 'browser_context') as context, patch.object(launcher.subprocess, 'Popen') as launch:
            for url in ('http://example.com:8320', 'http://127.0.0.1:8320/../x',
                        'http://[::1]:9000', 'http://127.0.0.2:9000', ''):
                with self.assertRaisesRegex(RuntimeError, 'no es local'):
                    launcher.open_dashboard(url)
            context.assert_not_called()
            launch.assert_not_called()

    def test_addresses_and_app_ids_match_the_widget_rule(self):
        for url, normalized, app_id in [
                ('http://127.0.0.1:8320', 'http://127.0.0.1:8320', 'chrome-127.0.0.1__-Default'),
                # Chrome leaves the port out of the app id.
                ('http://127.0.0.1:9000/', 'http://127.0.0.1:9000', 'chrome-127.0.0.1__-Default'),
                ('HTTP://LOCALHOST:8320', 'http://localhost:8320', 'chrome-localhost__-Default'),
                ('https://127.0.0.1:8443', 'https://127.0.0.1:8443', 'chrome-127.0.0.1__-Default'),
                ('http://ROUTER.LOCALHOST:9000/', 'http://router.localhost:9000', 'chrome-router.localhost__-Default')]:
            self.assertEqual(launcher.normalize_url(url), normalized)
            self.assertEqual(launcher.chrome_app_id(url), app_id)
        for url in ('http://example.com', 'http://127.0.0.010', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
                    'http://127.0.0.1:8320/x', 'http://127.0.0.١', 'http://127.0.0.2:9000',
                    'http://127.1.2.3', 'http://[::1]:8320', 'http://foo.localhost', None, 'http://127.0.0.1:8320\n?'):
            self.assertIsNone(launcher.normalize_url(url), url)

    def test_profile_is_marked_only_when_this_helper_creates_it(self):
        with tempfile.TemporaryDirectory() as temp:
            created = Path(temp) / 'created'
            launcher.ensure_profile(created)
            self.assertTrue((created / launcher.PROFILE_MARKER).is_file())
            self.assertEqual(created.stat().st_mode & 0o777, 0o700)
            existing = Path(temp) / 'existing'
            existing.mkdir()
            launcher.ensure_profile(existing)
            self.assertFalse((existing / launcher.PROFILE_MARKER).exists())

    def test_symlinked_profile_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            target = data / 'elsewhere'
            target.mkdir()
            (data / 'router-personal-tray').mkdir()
            (data / 'router-personal-tray/browser').symlink_to(target)
            with patch.dict(launcher.os.environ, {'XDG_DATA_HOME': str(data)}), patch.object(
                    launcher.shutil, 'which', return_value='/chrome'):
                with self.assertRaisesRegex(RuntimeError, 'enlace'):
                    launcher.browser_context()


class ProfileRemovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='router tray ')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.directory = self.home / 'router-personal-tray'
        self.profile = self.directory / 'browser'
        self.unrelated = self.home / 'unrelated'
        self.unrelated.mkdir()
        (self.unrelated / 'keep').write_text('preserve')
        (self.unrelated / launcher.PROFILE_MARKER).write_text('a marker that is not ours to follow')

    def assertUnrelatedKept(self):
        self.assertEqual((self.unrelated / 'keep').read_text(), 'preserve')
        self.assertTrue((self.unrelated / launcher.PROFILE_MARKER).exists())

    def test_marked_profile_and_lock_are_removed(self):
        self.directory.mkdir()
        launcher.ensure_profile(self.profile)
        (self.profile / 'data').write_text('dedicated profile')
        (self.directory / 'open.lock').write_text('')
        self.assertTrue(installer.remove_browser_profile(self.home))
        self.assertFalse(self.directory.exists())
        self.assertUnrelatedKept()

    def test_unmarked_profile_is_kept(self):
        self.profile.mkdir(parents=True)
        (self.profile / 'data').write_text('not proven ours')
        self.assertFalse(installer.remove_browser_profile(self.home))
        self.assertEqual((self.profile / 'data').read_text(), 'not proven ours')

    def test_symlinked_profile_is_not_followed(self):
        self.directory.mkdir()
        self.profile.symlink_to(self.unrelated)
        self.assertFalse(installer.remove_browser_profile(self.home))
        self.assertTrue(self.profile.is_symlink())
        self.assertUnrelatedKept()

    def test_symlinked_parent_is_not_followed(self):
        (self.unrelated / 'browser').mkdir()
        launcher.ensure_profile(self.unrelated / 'browser/inner')
        (self.unrelated / 'browser' / launcher.PROFILE_MARKER).write_text('marker')
        self.directory.symlink_to(self.unrelated)
        self.assertFalse(installer.remove_browser_profile(self.home))
        self.assertTrue((self.unrelated / 'browser' / launcher.PROFILE_MARKER).exists())
        self.assertUnrelatedKept()

    def test_symlinked_marker_does_not_count(self):
        self.profile.mkdir(parents=True)
        (self.profile / launcher.PROFILE_MARKER).symlink_to(self.unrelated / launcher.PROFILE_MARKER)
        self.assertFalse(installer.remove_browser_profile(self.home))
        self.assertTrue(self.profile.exists())
        self.assertUnrelatedKept()

    def test_nothing_installed_is_fine(self):
        self.assertTrue(installer.remove_browser_profile(self.home))
        self.assertUnrelatedKept()


class FakeDesktop:
    """Every KDE and systemd command is faked; nothing reaches Plasma, KWin or systemd."""

    def __init__(self, trays='1', fail=(), tools=('kpackagetool6', 'qdbus6', 'kreadconfig6', 'kwriteconfig6', 'systemctl')):
        self.calls, self.trays, self.fail, self.tools = [], trays, fail, tools

    def run(self, *args):
        self.calls.append(args)
        if args[0] in self.fail:
            raise subprocess.CalledProcessError(1, args[0], stderr=args[0] + ' failed')
        if args[0] == 'qdbus6' and 'evaluateScript' in args[3]:
            return 'TRAYS=' + self.trays
        return ''

    def which(self, tool):
        return '/usr/bin/' + tool if tool in self.tools else None

    def __enter__(self):
        self.patches = [patch.object(installer, 'run', side_effect=self.run),
                        patch.object(installer.shutil, 'which', side_effect=self.which)]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *error):
        for item in self.patches:
            item.stop()

    def ran(self, *prefix):
        return any(args[:len(prefix)] == prefix for args in self.calls)


class InstallerFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='router tray ')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.config, self.data = root / 'config', root / 'data'
        (self.data / 'plasma/plasmoids' / installer.PLUGIN).mkdir(parents=True)
        self.unit = self.config / 'systemd/user' / installer.WARM_SERVICE
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text('[Service]\n')
        self.profile = self.data / 'router-personal-tray/browser'
        self.profile.parent.mkdir(parents=True)
        launcher.ensure_profile(self.profile)

    def test_uninstall_without_a_tray_cleans_everything_else(self):
        with FakeDesktop(trays='0') as desktop:
            installer.uninstall(self.config, self.data)
        self.assertTrue(desktop.ran('kpackagetool6', '--type', 'Plasma/Applet', '--remove'))
        self.assertTrue(desktop.ran('qdbus6', 'org.kde.KWin', '/KWin', 'org.kde.KWin.reconfigure'))
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.profile.exists())

    def test_uninstall_without_plasma_continues_and_reports(self):
        with FakeDesktop(tools=('kpackagetool6', 'systemctl', 'kreadconfig6', 'kwriteconfig6')) as desktop:
            with self.assertRaisesRegex(RuntimeError, 'qdbus6'):
                installer.uninstall(self.config, self.data)
        self.assertTrue(desktop.ran('kpackagetool6', '--type', 'Plasma/Applet', '--remove'))
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.profile.exists())

    def test_a_failed_backup_is_reported_and_cleanup_continues(self):
        env = {'XDG_CONFIG_HOME': str(self.config), 'XDG_DATA_HOME': str(self.data), 'LC_ALL': 'en_US.UTF-8'}
        with FakeDesktop() as desktop, \
             patch.object(installer.sys, 'argv', ['install.py', '--uninstall']), \
             patch.dict(installer.os.environ, env), \
             patch.object(installer, 'backup_config', side_effect=PermissionError('State directory is read-only')):
            with self.assertRaisesRegex(RuntimeError, 'configuration backup: State directory is read-only'):
                installer.main()
        self.assertTrue(desktop.ran('kpackagetool6', '--type', 'Plasma/Applet', '--remove'))
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.profile.exists())

    def test_a_failing_tray_script_does_not_stop_cleanup(self):
        with FakeDesktop(fail=('qdbus6',)) as desktop:
            with self.assertRaisesRegex(RuntimeError, 'qdbus6 failed'):
                installer.uninstall(self.config, self.data)
        self.assertTrue(desktop.ran('kpackagetool6', '--type', 'Plasma/Applet', '--remove'))
        self.assertFalse(self.profile.exists())

    def test_tray_timeout_bytes_are_decoded_and_do_not_stop_cleanup(self):
        with FakeDesktop() as desktop:
            original_run = desktop.run
            def run(*args):
                if args[0] == 'qdbus6' and 'evaluateScript' in args[3]:
                    raise subprocess.TimeoutExpired('qdbus6', 30, stderr=b' Tray timed out: \xff \n')
                return original_run(*args)
            with patch.object(installer, 'run', side_effect=run), self.assertRaises(RuntimeError) as error:
                installer.uninstall(self.config, self.data)
        self.assertIn('Tray timed out: \ufffd', str(error.exception))
        self.assertNotIn("b'", str(error.exception))
        self.assertTrue(desktop.ran('kpackagetool6', '--type', 'Plasma/Applet', '--remove'))
        self.assertFalse(self.profile.exists())

    def test_installer_url_argument_uses_the_launchers_host_rule(self):
        with patch.object(installer, 'backup_config'), patch.object(installer, 'install') as install:
            for url in ('http://localhost:9000', 'http://router.localhost:9000', 'http://127.0.0.1:9000'):
                with patch('sys.argv', ['install.py', '--url', url]):
                    installer.main()
                self.assertEqual(install.call_args.args[-1], url)
            install.reset_mock()
            for url in ('http://[::1]:9000', 'http://127.0.0.2:9000'):
                with patch('sys.argv', ['install.py', '--url', url]), patch('sys.stderr'):
                    with self.assertRaises(SystemExit) as error:
                        installer.main()
                self.assertEqual(error.exception.code, 2)
            install.assert_not_called()

    def test_a_service_that_does_not_stop_keeps_the_profile(self):
        with FakeDesktop(fail=('systemctl',)) as desktop:
            with self.assertRaisesRegex(RuntimeError, 'servicio|service'):
                installer.uninstall(self.config, self.data)
        self.assertTrue(desktop.ran('kpackagetool6', '--type', 'Plasma/Applet', '--remove'))
        self.assertTrue((self.profile / launcher.PROFILE_MARKER).exists())

    def test_install_writes_the_rule_for_the_configured_host(self):
        with FakeDesktop() as desktop:
            installer.install(self.config, self.data, 'http://localhost:9000')
        self.assertTrue(desktop.ran('kwriteconfig6', '--file', 'kwinrulesrc', '--group', installer.RULE,
                                    '--key', 'wmclass', 'chrome-localhost__-Default'))

    def test_install_without_a_tray_fails_before_window_changes(self):
        with FakeDesktop(trays='0') as desktop, self.assertRaises(RuntimeError):
            installer.install(self.config, self.data, installer.launcher.DEFAULT_URL)
        self.assertFalse(desktop.ran('kwriteconfig6'))

    def test_messages_follow_the_system_language(self):
        self.assertTrue(installer.spanish({'LANG': 'es_CL.UTF-8'}))
        self.assertTrue(installer.spanish({'LC_ALL': 'es_ES.UTF-8', 'LANG': 'en_US.UTF-8'}))
        self.assertFalse(installer.spanish({'LC_MESSAGES': 'en_US.UTF-8', 'LANG': 'es_CL.UTF-8'}))
        self.assertFalse(installer.spanish({'LANG': 'de_DE.UTF-8'}))
        self.assertFalse(installer.spanish({}))


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

    def apply(self, existing, remove, url='http://127.0.0.1:8320'):
        calls = []

        def fake_run(*args):
            calls.append(args)
            return existing if args[0] == 'kreadconfig6' else ''

        with patch.object(installer, 'run', side_effect=fake_run):
            installer.set_window_rule(remove, url)
        general = {args[args.index('--key') + 1]: args[-1] for args in calls
                   if args[0] == 'kwriteconfig6' and args[args.index('--group') + 1] == 'General'}
        rule = [args for args in calls if args[0] == 'kwriteconfig6' and args[args.index('--group') + 1] == installer.RULE]
        self.assertEqual(calls[-1], ('qdbus6', 'org.kde.KWin', '/KWin', 'org.kde.KWin.reconfigure'))
        return general, rule

    def test_install_appends_rule_and_keeps_unrelated_rules(self):
        general, rule = self.apply('whatsapp-companion-tray-only', remove=False)
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only,' + installer.RULE, 'count': '2'})
        self.assertEqual({args[-2]: args[-1] for args in rule if args[-1] != '--delete'},
                         installer.rule_keys('http://127.0.0.1:8320'))
        self.assertEqual(installer.rule_keys('http://127.0.0.1:8320')['wmclass'], 'chrome-127.0.0.1__-Default')

    def test_reinstall_does_not_duplicate_the_rule(self):
        general, _ = self.apply('whatsapp-companion-tray-only,' + installer.RULE, remove=False)
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only,' + installer.RULE, 'count': '2'})

    def test_uninstall_deletes_only_the_dedicated_rule(self):
        general, rule = self.apply(installer.RULE + ',whatsapp-companion-tray-only', remove=True)
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only', 'count': '1'})
        self.assertEqual(sorted(args[-2] for args in rule), sorted(installer.rule_keys(installer.launcher.DEFAULT_URL)))
        self.assertTrue(all(args[-1] == '--delete' for args in rule))

    def test_a_changed_host_replaces_the_rule_without_duplicates(self):
        general, rule = self.apply('whatsapp-companion-tray-only,' + installer.RULE, remove=False, url='http://router.localhost:9000')
        self.assertEqual(general, {'rules': 'whatsapp-companion-tray-only,' + installer.RULE, 'count': '2'})
        self.assertIn(('kwriteconfig6', '--file', 'kwinrulesrc', '--group', installer.RULE, '--key', 'wmclass',
                       'chrome-router.localhost__-Default'), rule)

    def test_first_install_without_existing_rules(self):
        general, _ = self.apply('', remove=False)
        self.assertEqual(general, {'rules': installer.RULE, 'count': '1'})


class PackageTests(unittest.TestCase):
    PACKAGE = ROOT / 'desktop/plasma-tray/package'

    def test_settings_hold_the_address_and_language(self):
        schema = (self.PACKAGE / 'contents/config/main.xml').read_text()
        self.assertIn('<entry name="dashboardUrl" type="String">\n      <default>http://127.0.0.1:8320</default>', schema)
        self.assertIn('<entry name="language" type="String">', schema)
        page = (self.PACKAGE / 'contents/ui/configGeneral.qml').read_text()
        self.assertIn('property alias cfg_dashboardUrl: urlField.text', page)
        qml = (self.PACKAGE / 'contents/ui/main.qml').read_text()
        self.assertIn('RouterStatus.dashboardUrl(Plasmoid.configuration.dashboardUrl)', qml)
        self.assertIn('" --url " + quote(dashboardUrl)', qml)
        self.assertIn('Plasmoid.configuration.language = RouterStatus.otherLanguage(root.language)', qml)
        self.assertIn('String(window.resourceClass) === expectedClass',
                      (self.PACKAGE / 'contents/code/activate.js').read_text())


if __name__ == '__main__':
    unittest.main()
