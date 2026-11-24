#!/usr/bin/python3
"""Toggle only the tray's dedicated Chrome window; nonzero requests URL fallback."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

URL = 'http://127.0.0.1:8320'
HERE = Path(__file__).resolve().parent
_application = None


def browser_arguments(profile):
    return [f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', f'--app={URL}']


def browser_pid(profile):
    """Chrome's singleton lock names its browser PID; verify its actual profile."""
    try:
        pid = int((profile / 'SingletonLock').readlink().name.rsplit('-', 1)[1])
        args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        for arg in args:
            if arg.startswith('--user-data-dir=') and Path(arg.split('=', 1)[1]).resolve() == profile.resolve():
                return pid
        # Chrome on this host rewrites argv into one unquoted process-title
        # string. Match the full exact suffix we launch, including path spaces.
        if len(args) == 2 and args[-1] == '' and args[0].endswith(' ' + ' '.join(browser_arguments(profile))):
            return pid
    except (OSError, ValueError, IndexError, UnicodeError):
        pass
    return None


def control_window(pid, profile, wait, launch=None):
    # Optional at runtime: missing QtDBus is a normal fallback to the default browser.
    from PyQt6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, pyqtClassInfo, pyqtSlot
    from PyQt6.QtDBus import QDBus, QDBusConnection, QDBusMessage

    global _application
    _application = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    bus = QDBusConnection.sessionBus()
    loop = QEventLoop()

    @pyqtClassInfo('D-Bus Interface', 'org.kde.routerpersonal.Activation')
    class Reply(QObject):
        result = None

        @pyqtSlot(str, str)
        def finished(self, result, window_id):
            self.result = result
            print(f'{result}: pid={pid}, window={window_id}', flush=True)
            loop.quit()

    receiver = Reply()
    service = f'org.kde.routerpersonal.Open.p{os.getpid()}'
    if not bus.registerService(service):
        raise RuntimeError('No se pudo registrar el servicio de activación')
    if not bus.registerObject('/Activation', receiver, QDBusConnection.RegisterOption.ExportAllSlots):
        bus.unregisterService(service)
        raise RuntimeError('No se pudo registrar la respuesta de activación')

    def call(path, interface, method, arguments):
        message = QDBusMessage.createMethodCall('org.kde.KWin', path, interface, method)
        message.setArguments(arguments)
        reply = bus.call(message, QDBus.CallMode.Block, 2000)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            raise RuntimeError(reply.errorMessage())
        return reply.arguments()

    name = 'router-personal-tray-' + hashlib.sha256(str(profile).encode()).hexdigest()[:12]
    scripting = 'org.kde.kwin.Scripting'
    timeout_ms = 8000 if wait else 2000
    source = ('var expectedPid = ' + str(pid) + ';\nvar expiresAt = ' +
              str(int(time.time() * 1000) + timeout_ms) + ';\nvar callbackService = ' +
              json.dumps(service) + ';\nvar waitForWindow = ' + json.dumps(wait) + ';\n' +
              'var toggleExisting = ' + json.dumps(not wait) + ';\n' +
              (HERE / 'activate.js').read_text())
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    try:
        # Also dispose of a script left behind by a previous helper/session crash.
        call('/Scripting', scripting, 'unloadScript', [name])
        with tempfile.TemporaryDirectory(prefix='router-personal-activation-') as temporary:
            path = Path(temporary) / 'activate.js'
            path.write_text(source)
            script_id = call('/Scripting', scripting, 'loadScript', [str(path), name])[0]
            if script_id < 0:
                raise RuntimeError('KWin no pudo cargar el activador')
            call(f'/Scripting/Script{script_id}', 'org.kde.kwin.Script', 'run', [])
            if launch is not None:
                launch()
            if receiver.result is None:
                timer.start(timeout_ms)
                loop.exec()
    finally:
        timer.stop()
        try:
            call('/Scripting', scripting, 'unloadScript', [name])
        finally:
            bus.unregisterObject('/Activation')
            bus.unregisterService(service)
    if receiver.result is None:
        raise RuntimeError('KWin no confirmó la activación a tiempo')
    return receiver.result


def open_dashboard():
    browser = next((path for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')
                    if (path := shutil.which(name))), None)
    if browser is None:
        raise RuntimeError('No se encontró Chrome o Chromium')
    data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    directory = data_home / 'router-personal-tray'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = directory / 'browser'
    # Serialize requests, including rapid clicks during browser startup.
    with (directory / 'open.lock').open('a') as lock:
        end = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise RuntimeError('Otra apertura del panel no terminó a tiempo')
                time.sleep(0.05)
        pid = browser_pid(profile)
        if pid is not None:
            result = control_window(pid, profile, False)
            if result == 'closed':
                # Let Chrome finish shutting down before a rapid next toggle.
                end = time.monotonic() + 1
                while browser_pid(profile) == pid and time.monotonic() < end:
                    time.sleep(0.05)
                return
            if result != 'missing':
                raise RuntimeError('KWin no pudo cerrar la ventana del panel')
        if pid is not None:
            def launch():
                subprocess.Popen([browser] + browser_arguments(profile), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            result = control_window(pid, profile, True, launch)
        else:
            # Gate exec until KWin is listening, so skipTaskbar/skipSwitcher are
            # applied at window creation, before any attention request can flash.
            read_fd, write_fd = os.pipe()
            try:
                child = subprocess.Popen(['/usr/bin/python3', str(HERE / 'open_dashboard.py'),
                                          '--launch-browser', str(read_fd), browser, str(profile)],
                                         pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                os.close(read_fd)
                read_fd = None
                result = control_window(child.pid, profile, True, lambda: os.write(write_fd, b'1'))
            finally:
                if read_fd is not None:
                    os.close(read_fd)
                os.close(write_fd)
        if result != 'activated':
            raise RuntimeError('No se pudo abrir y activar la ventana del panel')


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--launch-browser':
        fd = int(sys.argv[2])
        ready = os.read(fd, 1) == b'1'
        os.close(fd)
        if not ready:
            sys.exit(1)
        os.execv(sys.argv[3], [sys.argv[3]] + browser_arguments(Path(sys.argv[4])))
    try:
        open_dashboard()
    except Exception as error:
        print(str(error), file=sys.stderr)
        # The applet uses Qt.openUrlExternally on a nonzero exit.
        sys.exit(1)
