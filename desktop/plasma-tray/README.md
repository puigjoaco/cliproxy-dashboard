# Router personal · Plasma 6

A read-only tray widget for the local account router. Left click opens
`http://127.0.0.1:8320` in the default browser; hover shows its health in Spanish.
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
```

The widget ignores the API's overall `ok` and local-app `codex` health fields.
Weekly-limited accounts are informational when another account of the provider
works. Gateway startup with unknown status and empty accounts is treated as
checking for up to three minutes. Two consecutive failed dashboard reads mean
no response. Unknown status lasting over three minutes means attention.

`/api/health` provides account status but neither reset times nor which
allowance window is exhausted, so the tooltip shows only how many accounts are
at their limit.
