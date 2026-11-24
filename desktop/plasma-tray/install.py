#!/usr/bin/env python3
"""Install/remove the current user's Plasma 6 tray applet, without restarting Plasma."""
import argparse
import datetime as dt
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

PLUGIN = 'org.kde.plasma.routerpersonal'
HERE = Path(__file__).resolve().parent


def _load_launcher():
    # One source for the URL rule, Chrome's app id and the profile marker.
    spec = importlib.util.spec_from_file_location('router_personal_launcher',
                                                  HERE / 'package/contents/code/open_dashboard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


def spanish(environ=os.environ):
    # The same order as gettext: the first non-empty variable decides.
    for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        if environ.get(name):
            return environ[name].lower().startswith('es')
    return False


def say(es, en):
    return es if spanish() else en


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=30).stdout.strip()


def failure(error):
    detail = getattr(error, 'stderr', '') or str(error)
    # TimeoutExpired keeps captured stderr as bytes even with text=True.
    return (detail.decode('utf-8', errors='replace') if isinstance(detail, bytes) else detail).strip()


def remove_browser_profile(data_home):
    """Delete the dedicated profile only if this applet created and marked it. True when nothing is left."""
    directory = data_home / 'router-personal-tray'
    profile = directory / 'browser'
    if directory.is_symlink() or profile.is_symlink():
        print(say(f'No se borra {directory}: es o contiene un enlace simbólico.',
                  f'Not deleting {directory}: it is or contains a symbolic link.'))
        return False
    if profile.exists() or profile.is_symlink():
        marker = profile / launcher.PROFILE_MARKER
        if not profile.is_dir() or marker.is_symlink() or not marker.is_file():
            print(say(f'No se borra {profile}: no tiene la marca del instalador. Bórralo a mano si es tuyo.',
                      f'Not deleting {profile}: it lacks the installer marker. Delete it by hand if it is yours.'))
            return False
        shutil.rmtree(profile)
    lock = directory / 'open.lock'
    if lock.is_file() and not lock.is_symlink():
        lock.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass  # Missing, or holds files this applet did not create.
    return True


RULE = 'router-personal-dashboard'


def rule_keys(url):
    # KWin applies rules before Plasma's task manager sees a new window, so the
    # dashboard never flashes there. Exact match on the app id Chrome derives
    # from the dashboard's host (not its port): see launcher.chrome_app_id.
    return {'Description': 'Router personal: dashboard window', 'wmclass': launcher.chrome_app_id(url),
            'wmclassmatch': '1', 'wmclasscomplete': 'false', 'skiptaskbar': 'true', 'skiptaskbarrule': '2',
            'skipswitcher': 'true', 'skipswitcherrule': '2', 'skippager': 'true', 'skippagerrule': '2'}


def set_window_rule(remove, url=launcher.DEFAULT_URL):
    config = ('--file', 'kwinrulesrc')
    names = [n for n in run('kreadconfig6', *config, '--group', 'General', '--key', 'rules').split(',')
             if n and n != RULE]
    keys = rule_keys(url)
    # Clear the whole group first, so a changed host never leaves stale keys.
    for key in keys:
        run('kwriteconfig6', *config, '--group', RULE, '--key', key, '--delete')
    if not remove:
        for key, value in keys.items():
            run('kwriteconfig6', *config, '--group', RULE, '--key', key, value)
        names.append(RULE)
    run('kwriteconfig6', *config, '--group', 'General', '--key', 'rules', ','.join(names))
    run('kwriteconfig6', *config, '--group', 'General', '--key', 'count', str(len(names)))
    run('qdbus6', 'org.kde.KWin', '/KWin', 'org.kde.KWin.reconfigure')


WARM_SERVICE = 'router-personal-browser.service'


def set_warm_service(config_home, data_home, package, remove):
    """Manage only this applet's user service; Chrome never gets a debug port."""
    unit = config_home / 'systemd/user' / WARM_SERVICE
    if remove:
        if unit.exists():
            run('systemctl', '--user', 'disable', '--now', WARM_SERVICE)
            unit.unlink()
            run('systemctl', '--user', 'daemon-reload')
        return
    helper = str(package / 'contents/code/open_dashboard.py')
    # systemd performs percent expansion even inside quoted ExecStart arguments.
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'
    quoted = quote(helper).replace('$', '$$')
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text('[Unit]\nDescription=Router personal windowless browser\n'
                    'PartOf=graphical-session.target\nAfter=graphical-session.target\n'
                    'StartLimitIntervalSec=60\nStartLimitBurst=3\n\n'
                    '[Service]\nType=exec\nEnvironment=' + quote('XDG_DATA_HOME=' + str(data_home)) + '\n'
                    'ExecStart=/usr/bin/python3 ' + quoted + ' --warm-browser\n'
                    'Restart=on-failure\nRestartSec=10\nKillMode=control-group\nTimeoutStopSec=10\n\n'
                    '[Install]\nWantedBy=graphical-session.target\n')
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'enable', '--now', WARM_SERVICE)


# Current Plasma embeds the tray containment; older Plasma 6 versions use
# SystrayContainmentId. Configure through its live scripting API in either case.
TRAY_SCRIPT = r'''
var count = 0;
var id = "org.kde.plasma.routerpersonal";
var remove = UNINSTALL;
function change(tray) {
    tray.currentConfigGroup = ["General"];
    function list(key) {
        var value = tray.readConfig(key, []);
        return Array.isArray(value) ? value : String(value).split(",").filter(Boolean);
    }
    ["extraItems", "shownItems", "hiddenItems"].forEach(function(key) {
        var items = list(key).filter(function(item) { return item !== id; });
        if (!remove && key !== "hiddenItems") items.push(id);
        tray.writeConfig(key, items);
    });
    tray.reloadConfig();
    count++;
}
panels().forEach(function(panel) {
    panel.widgets().forEach(function(widget) {
        if (widget.type !== "org.kde.plasma.systemtray") return;
        widget.currentConfigGroup = ["General"];
        var containmentId = widget.readConfig("SystrayContainmentId", 0);
        widget.currentConfigGroup = [];
        containmentId = widget.readConfig("SystrayContainmentId", containmentId);
        change(containmentId ? desktopById(containmentId) : widget);
    });
});
print("TRAYS=" + count);
'''


def update_trays(remove):
    """Return how many system trays were updated; raise if Plasma could not run the script."""
    result = run('qdbus6', 'org.kde.plasmashell', '/PlasmaShell', 'org.kde.PlasmaShell.evaluateScript',
                 TRAY_SCRIPT.replace('UNINSTALL', 'true' if remove else 'false'))
    count = next((line.split('=', 1)[1] for line in result.splitlines() if line.startswith('TRAYS=')), None)
    if 'Error' in result or count is None or not count.isdigit():
        raise RuntimeError(result or say('Plasma no respondió', 'Plasma did not answer'))
    return int(count)


def backup_config(config_home, state_home):
    config = config_home / 'plasma-org.kde.plasma.desktop-appletsrc'
    backup_dir = state_home / 'router-personal-tray/plasma-backup'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    if config.exists():
        backup = backup_dir / (config.name + '.' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        shutil.copy2(config, backup)
        print(say(f'Copia de configuración: {backup}', f'Configuration backup: {backup}'))


def install(config_home, data_home, url):
    for tool in ('kpackagetool6', 'qdbus6', 'kreadconfig6', 'kwriteconfig6', 'systemctl'):
        if not shutil.which(tool):
            raise RuntimeError(say(f'Falta {tool}; ejecuta este script en una sesión de Plasma 6.',
                                   f'{tool} is missing; run this script in a Plasma 6 session.'))
    package = data_home / 'plasma/plasmoids' / PLUGIN
    action = '--upgrade' if package.exists() else '--install'
    print(run('kpackagetool6', '--type', 'Plasma/Applet', action, str(HERE / 'package')))
    if not update_trays(False):
        raise RuntimeError(say('No se encontró una bandeja del sistema; añade una al panel.',
                               'No system tray was found; add one to a panel.'))
    set_window_rule(False, url)
    set_warm_service(config_home, data_home, package, False)
    print(say('Router personal instalado y visible en la bandeja.', 'Router personal installed and shown in the tray.'))


def uninstall(config_home, data_home, problems=None):
    """Remove what can be removed; a missing tray, session or tool never stops the other steps."""
    problems = list(problems or [])

    def step(name, tools, action):
        missing = [tool for tool in tools if not shutil.which(tool)]
        if missing:
            problems.append(f'{name}: ' + say('falta ', 'missing ') + ', '.join(missing))
            return False
        try:
            action()
            return True
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
            problems.append(f'{name}: {failure(error)}')
            return False

    def trays():
        if not update_trays(True):
            print(say('No hay bandeja del sistema; no hay nada que quitar de ella.',
                      'No system tray found; nothing to remove from it.'))

    package = data_home / 'plasma/plasmoids' / PLUGIN
    step(say('bandeja', 'tray'), ('qdbus6',), trays)
    step(say('regla de ventana', 'window rule'), ('kreadconfig6', 'kwriteconfig6', 'qdbus6'),
         lambda: set_window_rule(True))
    unit = config_home / 'systemd/user' / WARM_SERVICE
    stopped = step(say('servicio', 'service'), ('systemctl',) if unit.exists() else (),
                   lambda: set_warm_service(config_home, data_home, package, True))
    if package.exists():
        step(say('paquete', 'package'), ('kpackagetool6',),
             lambda: print(run('kpackagetool6', '--type', 'Plasma/Applet', '--remove', PLUGIN)))
    if not stopped:
        # Its browser may still be using the profile.
        problems.append(say('perfil del navegador: se conserva porque el servicio no se detuvo',
                            'browser profile: kept because the service did not stop'))
    elif not remove_browser_profile(data_home):
        problems.append(say('perfil del navegador: se conserva', 'browser profile: kept'))
    if problems:
        raise RuntimeError(say('Desinstalación incompleta: ', 'Uninstall incomplete: ') + '; '.join(problems))
    print(say('Router personal desinstalado.', 'Router personal uninstalled.'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--url', default=launcher.DEFAULT_URL,
                        help='dashboard address set in the widget; its host selects the window rule')
    args = parser.parse_args()
    url = launcher.normalize_url(args.url)
    if url is None:
        parser.error(say('--url debe ser una dirección http o https de este equipo, como http://127.0.0.1:8320.',
                         '--url must be an http or https address on this computer, such as http://127.0.0.1:8320.'))
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
    data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    problems = []
    try:
        backup_config(config_home, state_home)
    except OSError as error:
        if not args.uninstall:
            raise
        problems.append(say('copia de configuración: ', 'configuration backup: ') + failure(error))
    if args.uninstall:
        uninstall(config_home, data_home, problems)
    else:
        install(config_home, data_home, url)


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(failure(error), file=sys.stderr)
        sys.exit(1)
