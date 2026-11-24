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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    for tool in ('kpackagetool6', 'qdbus6'):
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
    package = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'plasma/plasmoids' / PLUGIN
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
    if args.uninstall:
        print(run('kpackagetool6', '--type', 'Plasma/Applet', '--remove', PLUGIN))
    print('Router personal desinstalado.' if args.uninstall else 'Router personal instalado y visible en la bandeja.')


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(getattr(error, 'stderr', '') or str(error), file=sys.stderr)
        sys.exit(1)
