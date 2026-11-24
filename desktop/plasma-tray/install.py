#!/usr/bin/env python3
"""Install/remove the current user's Plasma 6 tray applet, without restarting Plasma."""
import argparse
import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import sys

PLUGIN = 'org.kde.plasma.routerpersonal'
HERE = Path(__file__).resolve().parent


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=30).stdout.strip()


def remove_browser_profile(data_home):
    profile = data_home / 'router-personal-tray/browser'
    if profile.exists():
        shutil.rmtree(profile)
    (data_home / 'router-personal-tray/open.lock').unlink(missing_ok=True)


RULE = 'router-personal-dashboard'
# KWin applies rules before Plasma's task manager sees a new window, so the
# dashboard never flashes there. Exact class match: only the dedicated window.
RULE_KEYS = {'Description': 'Router personal: dashboard window', 'wmclass': 'chrome-127.0.0.1__-Default',
             'wmclassmatch': '1', 'wmclasscomplete': 'false', 'skiptaskbar': 'true', 'skiptaskbarrule': '2',
             'skipswitcher': 'true', 'skipswitcherrule': '2', 'skippager': 'true', 'skippagerrule': '2'}


def set_window_rule(remove):
    config = ('--file', 'kwinrulesrc')
    names = [n for n in run('kreadconfig6', *config, '--group', 'General', '--key', 'rules').split(',')
             if n and n != RULE]
    if remove:
        for key in RULE_KEYS:
            run('kwriteconfig6', *config, '--group', RULE, '--key', key, '--delete')
    else:
        for key, value in RULE_KEYS.items():
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    for tool in ('kpackagetool6', 'qdbus6', 'kreadconfig6', 'kwriteconfig6', 'systemctl'):
        if not shutil.which(tool):
            parser.error(f'Falta {tool}; ejecuta este script en una sesión de Plasma 6.')
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    config = config_home / 'plasma-org.kde.plasma.desktop-appletsrc'
    state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
    backup_dir = state_home / 'router-personal-tray/plasma-backup'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if config.exists():
        backup = backup_dir / (config.name + '.' + stamp)
        shutil.copy2(config, backup)
        print(f'Copia de configuración: {backup}')
    data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    package = data_home / 'plasma/plasmoids' / PLUGIN
    if not args.uninstall:
        action = '--upgrade' if package.exists() else '--install'
        print(run('kpackagetool6', '--type', 'Plasma/Applet', action, str(HERE / 'package')))
    # Current Plasma embeds the tray containment; older Plasma 6 versions use
    # SystrayContainmentId. Configure through its live scripting API in either case.
    script = r'''
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
print("Bandejas actualizadas: " + count);
if (!count) throw new Error("No se encontró una bandeja del sistema; añade una al panel.");
'''.replace('UNINSTALL', 'true' if args.uninstall else 'false')
    result = run('qdbus6', 'org.kde.plasmashell', '/PlasmaShell', 'org.kde.PlasmaShell.evaluateScript', script)
    print(result)
    if 'Error' in result or 'Bandejas actualizadas: 0' in result:
        raise RuntimeError('Plasma no pudo actualizar la bandeja; no se reinició la sesión.')
    set_window_rule(args.uninstall)
    set_warm_service(config_home, data_home, package, args.uninstall)
    if args.uninstall:
        print(run('kpackagetool6', '--type', 'Plasma/Applet', '--remove', PLUGIN))
        remove_browser_profile(data_home)
    print('Router personal desinstalado.' if args.uninstall else 'Router personal instalado y visible en la bandeja.')


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(getattr(error, 'stderr', '') or str(error), file=sys.stderr)
        sys.exit(1)
