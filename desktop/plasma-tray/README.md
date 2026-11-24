# Router personal · Plasma 6

A read-only tray widget for the local account router. Left click opens
`http://127.0.0.1:8320` in its own **Personal Router** window; hover shows its health in Spanish.
The context menu can open the dashboard or copy its address. Middle click does
nothing. It polls `/api/health` every 30 seconds with a five-second timeout and
never sends notifications or changes router settings.

From the desktop user's Plasma 6 session, without sudo:

```sh
python3 desktop/plasma-tray/install.py
```

The installer uses `kpackagetool6` and Plasma's live scripting API to enable the
applet and keep it visible in the system tray. It preserves other tray items.
It backs up `~/.config/plasma-org.kde.plasma.desktop-appletsrc` before changes to
`~/.local/state/router-personal-tray/plasma-backup/`. Plasma is never
restarted. Run the same command after updating the package; Plasma may keep the previous QML in memory until the next login. If an upgrade
keeps the previous behaviour, log out and back in when convenient; the installer
never does this automatically. A new installation loads immediately.

Close the dashboard before installing or updating. The installer also enables and
starts `router-personal-browser.service` in the current user's systemd instance.
It starts at graphical login and stops with the graphical session; it never
restarts Plasma. The unit is written to
`$XDG_CONFIG_HOME/systemd/user/router-personal-browser.service` (normally
`~/.config/systemd/user/router-personal-browser.service`).

The window uses Chrome/Chromium app mode and a dedicated profile under
`$XDG_DATA_HOME/router-personal-tray/browser` (normally
`~/.local/share/router-personal-tray/browser`). The tray icon toggles the window:
if closed, it opens in front; if already open (even behind another window or
minimized), it really closes. The context action **Abrir panel del router** uses
the same toggle. Concurrent requests are serialized to avoid duplicate windows.

The service keeps only this dedicated browser warm, with **no windows** while
idle. Closing the dashboard destroys its window; the browser process stays ready
for the next click. It does not minimize or hide an existing window. The helper
sets `background_mode.enabled=false` atomically in this profile's `Local State`
while Chrome is stopped, which prevents Chrome's background tray icon. A private
`--remote-debugging-pipe` keeps the process alive with `--no-startup-window`;
there is no debugging TCP port, extension, extra tray item, or idle notification.
The supervisor sends no debugging commands. `--disable-features=Translate` and
`--disable-notifications` apply only to this dedicated instance. The dashboard
HTML and normal Chrome preferences are unchanged.

If the service is unavailable, the helper still supports cold app-window launches.
If a dedicated browser is already running when the service starts, the service
waits for it to exit before preparing the warm instance. Warming uses memory
throughout the graphical session; stop it with
`systemctl --user stop router-personal-browser.service` when needed. The next
tray click then uses a cold start. Uninstall disables/stops the service and
removes its unit before deleting the dedicated profile.

The dashboard window is hidden from the taskbar and Alt+Tab. The installer adds
one KWin window rule, `router-personal-dashboard`, that matches only the exact
app id `chrome-127.0.0.1__-Default`; KWin applies it before the task manager sees
the window, so it never flashes there. `--uninstall` removes the rule. A
short-lived KWin script also sets `skipTaskbar` and `skipSwitcher`, clears
attention, and activates the window. The script matches both the app id and the
verified browser PID for this profile; neither selects normal Chrome windows.
It stops responding after eight seconds even if its helper crashes, and is
unloaded after each use. Each toggle loads the script once. If no window exists, the script asks the helper
to launch one while it remains subscribed. Browser startup waits until the script is listening,
so the window is hidden before activation. No application launcher is installed:
the tray icon is the entry point.

Activation needs `/usr/bin/python3` with PyQt6 QtDBus (`python3-pyqt6` on Kubuntu).
If the browser is missing or window control fails, the applet opens the URL in
the default browser instead. Normal Chrome's profile and focus policy are
unchanged. Uninstall removes the dedicated browser profile; close the dashboard
window before uninstalling.

To remove it:

```sh
python3 desktop/plasma-tray/install.py --uninstall
```

The icon follows Plasma's text colour with the approved 16/22 px symbolic mark.
Orange means attention, red means no response, and a dim mark means checking.
The applet always reports Plasma's `Active` status and the installer pins it as
shown, so it stays next to the clock. It does not use `NeedsAttention`, which
would make Plasma pulse the icon; the badge colour alone signals a problem.

The shared state mapping is `package/contents/code/status.js`; verify with:

```sh
node tests/test_plasma_tray.cjs
node --check desktop/plasma-tray/package/contents/code/status.js
node --check desktop/plasma-tray/package/contents/code/activate.js
python3 tests/test_plasma_tray_launcher.py
```

A bounded live check on Chrome 158 / Plasma 6.6 measured approximately 1.0–1.15 s
for a cold helper invocation to KWin activation, versus 396 ms with the warm
instance. Closing took 77 ms and the same browser PID remained alive with zero
windows and no new StatusNotifier item. These timings use direct helper
invocations as the tray-click proxy; no desktop input was injected. About 918 ms
of the baseline was Chrome launch to activation; Qt import was 21 ms, KWin
load/run about 7 ms, and uncontended locking under 1 ms. Navigation load was
90 ms cold / 70 ms warm; the local HTTP response alone was under 1 ms. Results
vary with session load and browser version. The warm lifecycle was tested directly;
installation and the actual systemd login/logout lifecycle were not executed in
that check.

The widget ignores the API's overall `ok` and local-app `codex` health fields.
Weekly-limited accounts are informational when another account of the provider
works. Gateway startup with unknown status and empty accounts is treated as
checking for up to three minutes. Two consecutive failed dashboard reads mean
no response. Unknown status lasting over three minutes means attention.

`/api/health` provides account status but neither reset times nor which
allowance window is exhausted, so the tooltip shows only how many accounts are
at their limit.
