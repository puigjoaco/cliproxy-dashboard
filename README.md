# cliproxy-dashboard

A local dashboard and operations layer for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), a separate open-source gateway installed from its official releases. This repository is not a fork of the gateway. Requests flow from **T3 Code, Codex CLI or Claude Code → gateway on port 8317 → one of your pooled subscription accounts**. The dashboard runs on loopback port 8320 and provides account sign-in, per-account allowance, request history and routing controls. Collectors, minute-by-minute routing reconciliation, backups and health notices run as services.

Pooling lets several of your own ChatGPT/Codex or Claude subscriptions serve one local endpoint; each account keeps its own allowance. Four routing modes choose among eligible accounts: **round-robin** distributes new sessions; **earliest-weekly-reset** prefers the allowance that resets first; **highest-remaining-quota** prefers the largest remaining weekly percentage; **preferred-account** favours a selected account with eligible fallback. Healthy conversations keep their account through session affinity. See [Claude routing](docs/claude-routing.md) and the [reset preference record](docs/reset-preference.md).

Pooling subscription accounts through a proxy is not officially supported by the providers. Each user accepts that risk.

The private working repository is `cliproxy-dashboard-private`; its scrubbed public copy is `cliproxy-dashboard`. Installed paths and unit names retain `personal-codex-router`, including `/opt/personal-codex-router`, `/etc/personal-codex-router` and `personal-codex-router-*`.

## Set up on your own machine

For the coding agent doing the installation: finish with a loopback-only gateway on 8317, dashboard on 8320, at least two signed-in accounts per provider you use, and a verified request from each client. Keep keys, credentials, databases and backups outside the checkout. Preserve existing tool configuration before changing it.

Only the human can:

- Supply sudo authorization if the machine does not have passwordless sudo.
- Complete browser sign-ins for each subscription account.
- Approve changes to how their existing tools sign in.

Prerequisites: Kubuntu on x86_64 (Linux amd64) with systemd, Python 3.11+, and Node.js 22 or a newer supported LTS at `/usr/bin/node` (the optional preview unit uses that path). Kubuntu packages: `git curl ca-certificates tar python3 python3-yaml python3-bcrypt nodejs gcc libc6-dev`; `openssh-client` for SSH access and `libglib2.0-bin` for desktop notices through `gdbus`. Install Codex CLI, Claude Code and T3 Code from their official distributions if those clients are wanted. Clone this repository and run the following commands from its root. Nightly tests also require the desktop user's `~/.local/bin/heavy` scheduler, which this repository does not supply. If it is unavailable, after step 4 run `systemctl --user disable --now personal-codex-router-nightly-tests.timer` until it is provisioned.

1. **Install the gateway.** Download the Linux archive matching `uname -m` from the [official 8.0.79 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.79). Verify its SHA-256 against the checksum asset on that release page before extracting. This is the example version; dashboard sign-in is validated on 8.0.30, 8.0.43, 8.0.47 and 8.0.79, not arbitrary newer releases. Keep `server.kv-store.disabled: true` from the example: session/date-pin/replay state stays in memory, avoiding a bbolt backup requirement ([decision](docs/gateway-upgrade-8.0.79-2026-11-19.md#decisions-and-material-risks)).

   On a new host, create the system user/group and directories:

   ```sh
   sudo useradd --system --user-group --home-dir /var/lib/cliproxyapi --shell /usr/sbin/nologin cliproxyapi
   sudo install -d -o root -g root -m 755 /opt/cliproxyapi/8.0.79
   sudo install -d -o root -g cliproxyapi -m 750 /etc/cliproxyapi
   sudo install -d -o cliproxyapi -g cliproxyapi -m 700 /var/lib/cliproxyapi /var/lib/cliproxyapi/auth
   sudo install -o root -g cliproxyapi -m 660 config/gateway.example.yaml /etc/cliproxyapi/config.yaml
   sudo install -o root -g root -m 644 config/cliproxyapi.example.service /etc/systemd/system/cliproxyapi.service
   ```

   Install the extracted `cli-proxy-api` executable at `/opt/cliproxyapi/8.0.79/cli-proxy-api`, owned by root, mode 0755. Read the [example config](config/gateway.example.yaml) and [unit](config/cliproxyapi.example.service); keep the empty management secret for step 3. Config mode 0660 and the unit's `ReadWritePaths` let management save changes. Start this new service with `sudo systemctl daemon-reload` then `sudo systemctl enable --now cliproxyapi.service`. Do not overwrite an existing installation or restart an active gateway blindly.

   Check: `systemctl is-active cliproxyapi.service` says `active`; `ss -ltn` shows only `127.0.0.1:8317` for this port. `/v1/models` refuses an unauthenticated request (401). Management routes are absent while the secret is empty.

2. **Set host settings.** Create the root-owned site config:

   ```sh
   sudo install -d -m 755 /etc/personal-codex-router
   sudo install -o root -g root -m 644 config/site.example.json /etc/personal-codex-router/site.json
   ```

   The [example](config/site.example.json) has `devices: []` for local access. Set `desktop_user` to the existing desktop login if automatic detection is unsuitable; it defaults to `SUDO_USER`, then the checkout owner. Optional `devices` entries contain `name` and `tailscale_ip`; `tailscale_hostname` supplies private DNS. Optional `claude_b` contains `email` and `plan` for the separate second allowance watcher. No credentials belong here; [allowed fields](scripts/site_config.py) are validated.

   Check: `python3 scripts/install-operations.py --site-config /etc/personal-codex-router/site.json` prints a plan with the intended desktop user and zero devices for a local install. It writes nothing without `--apply`.

3. **Activate management once.** Download the pinned, untracked panel asset into the checkout:

   ```sh
   curl --fail --location --output references/management.html https://github.com/router-for-me/Cli-Proxy-API-Management-Center/releases/download/v1.24.2/management.html
   sudo python3 scripts/enable-dashboard.py
   ```

   The script checks the [recorded digest](references/management-release.json), installs the panel, generates a private management key, enables usage collection and edits the config in place. It requires `management.allow-remote: false`, an empty secret and no existing dashboard key; it is not a repeatable updater. The management key is separate from client keys.

   Check: `http://127.0.0.1:8317/management.html` loads; a management API call without its key returns 401 after hot reload. Verify an authenticated management read using the private key without printing it. If reload has not taken effect, diagnose the watched file before proceeding; see [hot reload](docs/standard-management-setup.md#maintenance-and-rollback).

4. **Install the dashboard, operations and fast-mode plugin.** From the intended desktop user's session, review the full plan, then apply:

   ```sh
   sudo python3 scripts/install-operations.py
   sudo python3 scripts/install-operations.py --apply
   ```

   A full install means no scope flag. `--preview-only`, `--claude-watch-only`, `--account-access-only` and `--t3-usage-only` update selected components on an existing installation. The full installer adds services/timers, backups and desktop-user jobs, then builds and hot-loads the fast-mode plugin without restarting the gateway. If a sufficient Go is unavailable, the build downloads pinned Go 1.26.10 as the checkout owner and verifies its SHA-256; no Go installation is required. An already loaded current plugin is reused, and an explicit disable is preserved. Use `--skip-fast-mode-plugin` to leave it unchanged; scoped installs never touch it. Plugin installation works before Codex sign-in. A final plugin failure exits 4 with the rest installed; fix the reported issue and rerun the full command. The gateway itself must already be installed. User lingering is needed if user jobs must survive logout. The Tailscale host guard is optional and only needed for access from other devices: configuring devices requires signed-in, same-owner peers and the existing [private-preview guard layout](docs/private-previews.md); the installer does not create that firewall from scratch.

   Check: `personal-codex-router-dashboard.service` is active on `127.0.0.1:8320`; the dashboard reports the gateway online. Check the usage and preference timers with `systemctl list-timers 'personal-codex-router-*'`.

5. **Pool accounts.** Open `http://127.0.0.1:8320/` and use Add account to sign in two or more distinct accounts for each provider you want to pool. The human completes each browser sign-in. If a remote sign-in ends at an unreachable `localhost` callback, paste that full callback address into the dashboard's sign-in dialog. See [account access](docs/account-access.md).

   Check: each account appears under the correct provider, is enabled for routing and reports allowance or an explicit unavailable state. Choose a routing mode in Overview; split the selectors for independent provider choices. Check that the saved choice is applied by the next minute tick. Quota-based modes hold when allowance is uncertain; round-robin needs no quota observation.

6. **Create a client key.** The dashboard's API-key account feature adds upstream provider credentials ([contract](docs/api-keys-backend.md)); it does not issue gateway client keys. Generate a random key into a private file, without displaying it:

   ```sh
   python3 -c 'import os,secrets; from pathlib import Path; p=Path.home()/".config/personal-codex-router/claude-gateway.key"; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,(secrets.token_urlsafe(36)+"\n").encode()); os.close(fd)'
   ```

   Replace the example entry under `access.api-keys` in `/etc/cliproxyapi/config.yaml` with that value through a private edit; preserve the file's inode, owner and mode, then flush/fsync. Do not put it in shell arguments, logs or Git. The gateway watches the config and hot-reloads client-key changes without a restart; verify this rather than assuming an atomic file replacement is noticed. Separate client keys may be added as further list entries.

   Check: `/v1/models` returns 200 with the new key and 401 without it or with the removed example key. Read the key inside the checking script; never print request headers.

7. **Connect Codex CLI.** With the human's approval, merge this into the Codex home's `config.toml` (normally `~/.codex/config.toml`), inserting the client key privately and keeping the file mode 0600:

   ```toml
   model_provider = "personal_codex_gateway"

   [model_providers.personal_codex_gateway]
   name = "Local gateway"
   base_url = "http://127.0.0.1:8317/v1"
   wire_api = "responses"
   supports_websockets = true
   requires_openai_auth = true
   experimental_bearer_token = "<gateway-client-key>"
   ```

   Select a model returned by the gateway. Optional retries from the working setup are `request_max_retries = 8` and `stream_max_retries = 8` within the provider table. WebSocket transport also needs gateway account metadata; follow [Codex WebSockets](docs/codex-websockets.md) before enabling that transport on accounts.

   Check: ask Codex for a short reply and find the request, provider and account in the dashboard's requests list.

8. **Connect Claude Code.** The key file from step 6 is read by `python3 scripts/claude-gateway.py`; pass normal Claude arguments after the script. Use a shell function with an absolute checkout path if desired. The launcher requires mode 0600, sets `ANTHROPIC_BASE_URL=http://127.0.0.1:8317` and `ANTHROPIC_AUTH_TOKEN` for that process, clears conflicting provider credentials and leaves direct allowance collectors alone.

   Check: launch a short Claude conversation through the script and find its request and Claude account in the dashboard's requests list.

9. **Connect T3 Code.** Configure provider instances through T3 settings, preserving other instances. The settings live at `~/.t3/userdata/settings.json` under `providerInstances`: the `codex` instance's `config.homePath` must name the Codex home from step 7; `config.binaryPath` selects the Codex executable and `config.launchArgs` holds its arguments. The `claudeAgent` instance needs environment rows for `ANTHROPIC_BASE_URL=http://127.0.0.1:8317`, `ANTHROPIC_AUTH_TOKEN=<gateway-client-key>` marked sensitive, `ENABLE_TOOL_SEARCH=true`, `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1`, `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, and `NO_PROXY=127.0.0.1,localhost`. Clear conflicting direct-provider auth/backend variables as the [Claude launcher](scripts/claude-gateway.py) does.

   Use a stable CLI entry point that T3 recognizes, rather than a path into one version's installation directory. The [T3 provider guide](docs/t3-router-providers.md) covers native updates, preserving an existing Codex instance, and connecting T3's managed Antigravity agent through the gateway with its existing subscription accounts.

   Check: start a new conversation with each configured T3 provider and find its requests and account in the dashboard's requests list. Also verify a tool call. A configured endpoint or an authentication label alone does not prove the client uses it.

- Optional: [T3 usage hub](docs/t3-usage-hub.md) adds separately keyed, read-only allowance reporting.
- Optional: [Tailscale access](docs/private-previews.md) provides private access from other devices; SSH forwarding keeps gateway management on loopback.
- Optional: [backups and recovery](docs/backups-and-recovery.md) explains verification and off-machine copies; local backups are included in the full install.
- Optional: [notifications](docs/health-and-notifications.md) explains health and account notices included in the full install.

Installation files and private state:

| Files | Purpose |
| --- | --- |
| `/etc/personal-codex-router/site.json`, `previews.json` | Host settings and generated preview registrations; root-owned, no secrets. |
| `/etc/cliproxyapi/config.yaml`, `dashboard-management-key`, `static/management.html` | Existing gateway config gains the hashed management secret; activation creates the private key (0640) and pinned panel. |
| `~/.config/personal-codex-router/management-key` | Desktop user's private management-key copy (0600). |
| `/var/lib/cliproxyapi/auth/` | Provider credentials created by account sign-in; never commit or share them. |
| `/opt/personal-codex-router/`, `/etc/systemd/system/personal-codex-router-*`, `~/.config/systemd/user/personal-codex-router-*`, `/usr/local/bin/router-preview` | Installed helpers, dashboard and units. |
| `/var/lib/personal-codex-router/`, `/var/lib/personal-codex-router-routing/`, `/var/lib/personal-codex-router-ops/`, `/var/lib/personal-codex-router-watch/` | Usage SQLite/spool, controls, routing choices, health and watch snapshots created as services run. |
| `~/.local/state/personal-codex-router/`, `/run/personal-codex-router-move-request/` | Private Claude operation state/socket and temporary conversation-move requests. |
| `/var/lib/cliproxyapi-dashboard-backups/`, `/var/lib/personal-codex-router-backups/`, `docs/activation.json` | Activation/deployment backups and activation manifest. Backups contain secrets; keep them private and off the repo. |

What has been tested: this setup runs daily on the original Kubuntu machine. The current example was checked with CLIProxyAPI 8.0.79 and local KV disabled as an unprivileged scratch process: loopback-only listener, authenticated model listing, unauthenticated inference refusal and inaccessible management with an empty secret; startup left the config unchanged. In-place management activation hot-reloaded under the same PID; strict nested-v8 PATCH passed without config warnings and no KV database was created. Client-key replacement hot reload was previously checked on 8.0.47. The example unit passed `systemd-analyze verify`. The minimal Codex provider snippet above produced a real reply through the running gateway from a throwaway Codex home. A clean-machine installation has not been run end to end. The installer requires Linux with systemd; Windows and macOS hosts are not supported by it.

## Update an existing installation

From the checkout root, run `git pull`, review the plan from `sudo python3 scripts/install-operations.py`, then run `sudo python3 scripts/install-operations.py --apply` without a scope flag. Ensure the C compiler packages `gcc libc6-dev` are installed; Go is provisioned as in setup step 4. Preserve local dashboard edits before updating: checkout edits can conflict with the pull, and the install replaces dashboard files under `/opt/personal-codex-router` with checkout copies.

The final `Fast-mode plugin:` step reports a build/install/hot-reload verification, `already loaded` at the current version, or `left off` for an explicit disable. `--skip-fast-mode-plugin` reports `skipped`. Exit 4 means only that final step failed; the rest stays installed. Check the build output and private plugin backup, fix compiler/download/reload errors, then rerun the full command; see [plugin recovery](docs/fast-mode-policy.md).

## Pages

- **Overview:** accounts grouped by provider, remaining allowance, reset windows, OpenAI usage **Credits** with a per-account **Spend credits** switch, speed controls from each account's model list, routing controls, health, today's recorded tokens and recent requests. Unconfirmed billing dates are labelled; see [billing renewals](docs/billing-renewals.md).
- **Usage:** today, the last seven days or all recorded history, with account/model totals and a request chart. All-history charts show up to the latest 30 days.
- **System:** gateway status, routing and affinity settings, recording status and advanced management.
- The theme button switches between dark and light and remembers the browser's preference.

Antigravity cards show [reported Gemini allowance and account details](docs/antigravity-allowance.md), including shared quota grouping and explicit unknown billing dates.

Unknown allowance is shown as unavailable. Refresh failures preserve and label the last known reading. Recorded usage refreshes every 15 seconds.

## Everyday access

Open `http://127.0.0.1:8320/` on the gateway machine. The dashboard relies on the local/private connection for access and needs no management-key login; raw management routes and upstream tokens are not forwarded to its browser. For other computers, use SSH forwarding or the optional [private preview](docs/private-previews.md). A `.localhost` address always refers to the device opening it.

Advanced management is the separate pinned Management Center at `http://127.0.0.1:8317/management.html`. Its key is stored at `~/.config/personal-codex-router/management-key`; it is different from a gateway client key. Automatic panel updates are disabled.

## Subscription allowance versus token history

The provider reports each account’s current allowance and reset window independently. These are not added into a single interchangeable quota. Window lengths can differ by plan; the dashboard shows the windows reported for each account.

Codex **Credits** (`usage_credits`) displays a positive or unlimited OpenAI usage balance from the same allowance reading, retained when refresh fails. It adds no provider request or routing change. It is separate from **Resets**, the reset credits managed by `dashboard/reset_credits.py`; see [usage credits](docs/account-access.md#usage-credits).

Token history begins when your collector starts recording. Cached input can form much of a request's token total. These are operational records, not a dollar bill or a calculation of subscription allowance.

The upstream usage queue removes records when read. Records already collected persist in SQLite, but a crash during dequeue/commit or expired uncollected records can leave gaps. Treat this as best-effort operational history. Run only one queue consumer.

## Components

| Component | Location / service |
| --- | --- |
| Separate model gateway | `cliproxyapi.service`, example CLIProxyAPI 8.0.79, loopback 8317 |
| Dashboard backend and static site | `personal-codex-router-dashboard.service`, `/opt/personal-codex-router/dashboard`, loopback 8320 |
| Usage collector | `personal-codex-router-usage.timer`, every 15 seconds; SQLite at `/var/lib/personal-codex-router/usage.sqlite3` |
| Codex speed policy | Native `fast-mode-policy` plugin, included in the full install; [policy and deployment](docs/fast-mode-policy.md) |
| Routing controller | `personal-codex-router-preference.timer`, every minute; [routing contracts](docs/claude-routing.md) |
| Operations | Backup, health, retention, release-check and desktop-user timers; [automation](docs/operations-automation.md) |
| Optional private access | Preview proxy and SSH forwarding; [private previews](docs/private-previews.md) |

Per-account Fast and Ultrafast follow each account's model catalog, independently of its plan label; the plugin enforces the resulting policy. See [speed availability](docs/fast-mode-policy.md#model-catalog).

## Development and validation

The public copy, `cliproxy-dashboard`, is generated from the private repository, `cliproxy-dashboard-private`, by `.github/workflows/update-shareable-copy.yml` after every push to `main`. Do not edit the public copy directly: the next update overwrites it. The workflow is kept in the private repository and omitted from the public copy. Rehearse locally with `python3 scripts/export-public.py --out <empty dir>` followed by `python3 scripts/export-public.py --check <dir>`; export uses the committed tree and scrubs personal identifiers and Git history.

Use [Update an existing installation](#update-an-existing-installation) for full updates; scoped updates leave the plugin unchanged.

Source files are in `dashboard/`; installed runtime copies live under `/opt/personal-codex-router`. Preview installer plans before applying scoped updates. Backend changes need a dashboard restart; static assets do not. Dashboard work should not restart the gateway. See [account access](docs/account-access.md) for scope and sign-in interruption limits.

Run focused checks for touched code. The complete Python and Node runner continues after failures and prints a summary:

```sh
~/.local/bin/heavy python3 scripts/run-tests.py
git diff --check
```

The full operations installer also includes daily verified raw-usage retention
(at least 90 days of detail, permanent hourly totals), weekly anonymous gateway
release checks with dashboard notices, and a nightly desktop-user test timer.
Nightly tests use `site.json`'s `source_checkout` through `heavy`, keep the last
14 logs in `~/.cache/personal-codex-router/test-runs/`, and notify only on failure.
See [operations automation](docs/operations-automation.md) for archive verification,
history boundaries, timer behavior and failure handling. These jobs never upgrade
or restart the gateway.

Dated operational evidence is retained in `docs/` and below. It records the original deployment, not a clean-machine acceptance test.

## Notes on the original deployment

These records are preserved from the original machine, including dated version, network and account details. Some describe earlier states; use the setup above for a new installation.

Claude usage reads existing `~/.claude/.credentials.json`; the optional second watch reads `~/.config/personal-codex-router/claude-b.token` (0600), provisioned separately through Claude Code's setup-token flow. Installers create neither token; these differ from gateway sign-in credentials.

Existing deployment without `site.json`: **do not copy the empty example over it**. The first full install imports devices/DNS from `previews.json`, Claude B email/plan from its installed user unit, and the invoking/checkout user. It saves these to `site.json` and retains preview registrations and friendly aliases. Run this full migration before scoped installs. The remaining notes describe the original deployment, not new-machine defaults.

Shared dashboard: **[https://router.tailnet-example.ts.net:8409/](https://router.tailnet-example.ts.net:8409/)** over Tailscale. MagicDNS is enabled. The TLS proxy uses the full Tailscale name; old HTTP bookmarks redirect to it. For preview TLS upgrades, use `python3 scripts/install-operations.py --preview-only`, then `sudo -n python3 scripts/install-operations.py --preview-only --apply`; see [certificate renewal and rollback](docs/private-previews.md). The existing computer-only address `http://router.localhost/` retains full management controls through loopback/SSH.

September 24 operations: [daily backups and recovery](docs/backups-and-recovery.md), [compact health and account notifications](docs/health-and-notifications.md), and [private phone previews](docs/private-previews.md) run on Linux independently of the Mac. **Shared dashboard: https://router.tailnet-example.ts.net:8409/**. Project previews: **https://router.tailnet-example.ts.net:8400/**. Accepted limitation: backups in `/var/lib/personal-codex-router-backups/` contain credentials on the same disk as the originals, so disk loss or compromise affects both. Linux desktop notifications continue; the dashboard no longer shows notice tiles.

Project on Linux: `/home/user/Projects/personal-codex-router`.

Personal Router shows the configured Codex Pro account (`account-a@example.com`) and its gateway usage, with earned-reset controls. The Free Codex account was retired on October 29; both Claude accounts remain independent. It is separate from the general Management Center and from the unrelated ai-web-assistant checkout.

[Codex account access](docs/account-access.md) lets you route Free and paid accounts with a per-account preference, excludes signed-out credentials from routing, offers device-code sign-in on desktop, Mac and phone, and documents scoped deployment.

[Claude subscription usage](docs/claude-usage.md) shows both Claude subscriptions with independent gateway routing. The allowance collectors use Claude Code's current token for A (Max 20x, with confirmed limit resets) and a long-lived setup-token for B, on Max (20x). Inference uses separate gateway-owned Claude credentials.

### Earned reset credits

Click Resets at the bottom of the Codex account card to open its modal. It lists expiry dates, changes Use reset to Confirm on the same control, and includes automatic expiry redemption settings. Only the earliest-expiring available credit can be redeemed; the server also enforces this against a fresh balance. The remaining Codex account retains its saved automatic expiry redemption setting.

The Linux dashboard service checks cached deadlines every ten seconds and attempts the specific credit during its final five minutes. Failed or unconfirmed attempts retry on subsequent ten-second checks until success, expiry, or opt-out; network request time can lengthen the interval. It works with the browser closed. Automatic and manual redemption impose no remaining-allowance threshold. The provider decides whether it can restore allowance and returns its result. Expiry redemption still verifies the earliest available, supported credit against a fresh balance. Credits with unknown expiry or already-expired credits are skipped. Network outages or Linux downtime can prevent redemption; exact timing is best effort. This never purchases credits.

Each account/credit has one durable redemption ID shared by manual and automatic attempts, including retries after restarts. Settings, redemption outcomes and the last event are stored privately in `/var/lib/personal-codex-router/reset-controls.json`. The browser only receives allowlisted credit metadata; the gateway retains credentials. Turn off the checkbox per account to disable automation.

Validation: 26 focused Python tests passed on Mac and Linux, JavaScript syntax checked, live balances and enabled toggles verified in the browser. All redemption tests used mocked provider responses; no real credit was consumed. Provider request shape follows the upstream Codex backend client's `redeem_request_id` and `credit_id` contract. This direct provider adapter fails closed when fresh eligibility or response shape cannot be verified.

With one configured Codex account, every Codex routing mode uses neutral priority without needing quota observations. There is no Codex fallback account. The configured roster is `EMAILS` in `dashboard/routing_policy.py`; adding a second identity there restores account selection. The overview publishes `gateway.codex_account_count` (currently `1`), including while an unconfigured auth file awaits deletion. Reset credits, fast mode and recorded usage remain available.

Choose routing from the shared Overview dropdown to apply the same choice to Codex and Claude: round-robin, earliest weekly reset, higher remaining quota, or a preferred email. Split the controls to choose independently per gateway; changing the link setting alone leaves both saved selections intact. Higher remaining quota favours the eligible account with the larger weekly allowance percentage and uses round-robin when percentages match. The controller checks the saved choices every minute. Healthy existing sessions keep their assigned account unless you opt in to **Move all conversations now** after a routing change; that releases both providers' affinity pins for their next request, at most once a minute. In-flight requests finish on their existing account. Moves need the full `install-operations.py --apply`. Scoped installs don't install or update the move helper or its units, so moves keep using what the last full install set up; run a full install whenever the helper or its units change. Without a full install, or on a gateway version moves have not been verified on, routing changes apply immediately as before. See [Claude routing and API contracts](docs/claude-routing.md), [moving conversations](docs/move-conversations-2026-10-17.md), and `docs/routing-selector-2026-09-26.md`.

Claude routing retains all five choices, including either Claude email. Split routing to prefer Claude B, whose email is no longer a configured Codex identity. It defaults to Claude A; the timer leaves Claude priorities untouched until a selection is saved. Claude cards have the same Use for routing switch as Codex, including confirmation before disabling the last routable account. The same minute timer reconciles saved choices independently, using Claude's local usage snapshots (5-hour and weekly windows, at most 15 minutes old). Missing, stale, or uncertain usage on enabled accounts holds the existing priorities until fresh observations arrive. Disabled accounts are excluded. Conversation moves always release both providers. Claude state lives separately under `/var/lib/personal-codex-router-routing/claude/`; Codex priorities, credentials, affinity, and extra-usage settings are independent.

### Everyday access

Open **[https://router.tailnet-example.ts.net:8409/](https://router.tailnet-example.ts.net:8409/)** with Tailscale connected and Tailscale DNS enabled. The shared address shows accounts, usage, health and resets, and allows data refresh, routing changes, automatic reset policy changes, reset redemption, Codex account sign-in, and per-account routing switches. It uses the same private device restrictions as the original IP address; raw gateway management routes are not forwarded.

For computer access through loopback/SSH, use **http://router.localhost/**. On the Mac, an automatic SSH tunnel carries the connection privately to the Linux desktop over Tailscale. On Linux, the local address reaches the dashboard directly. This `.localhost` address does not work on the phone because it refers to the device opening it. Advanced gateway management uses the separate Management Center described below.

The everyday overview needs no management-key login: access comes through the local/private connection. No management key or upstream OAuth token is sent to the browser. Tailscale and the Linux desktop must be available when using it from the Mac. The Mac tunnel starts at user login and retries after interruptions.

Fallback addresses:

- Mac: `http://router.localhost:18320/`
- Linux: `http://127.0.0.1:8320/`
- Shared dashboard with Tailscale: `https://router.tailnet-example.ts.net:8409/`. Old HTTP short-name/IP bookmarks redirect to HTTPS and require Tailscale DNS at the destination.

### Pages

- **Overview:** the Codex account and both Claude account cards together at the top, in a **Codex accounts** group and a **Claude accounts** group: side by side on desktop, stacked below 1180px, one card per row on phones. Codex cards show billing renewal date and countdown, remaining allowance, allowance reset countdown and routing availability; Claude A and Claude B cards show usage by email, with a Claude routing selector in their section. Below the cards, the **Codex** region holds routing, health, today’s recorded tokens and requests, cached input, and recent requests. Billing dates include a two-day review buffer; unconfirmed dates are visibly labeled. See `docs/billing-renewals.md` for sources and maintenance.
- **Usage:** **Codex usage**, for today, the last seven days, or all recorded history; account and model totals; a request chart. All-history totals include all records, while its chart shows up to the latest 30 days.
- **System:** Codex gateway status, routing and affinity settings, recording status, and a link to advanced management.
- The theme button switches between dark and light and remembers the preference in this browser.

Codex quotas refresh every minute, with a manual refresh button. Recorded usage refreshes every 15 seconds. Unknown or unavailable data is shown as unavailable, rather than as a zero allowance. Provider refresh failures preserve and label the last known quota.

Usage history starts with the collector’s first recorded request on September 22, 2026. A request can contain many cached input tokens. Token totals are operational records, not a dollar bill or a calculation of subscription allowance.

### Components

| Component | Location / service |
| --- | --- |
| Existing model gateway | `cliproxyapi.service`, stock CLIProxyAPI 8.0.79 live since 2026-11-20 with local KV disabled ([upgrade record](docs/gateway-upgrade-8.0.79-2026-11-19.md)); 8.0.47 is the rollback, `*:8317` behind the host firewall |
| Overview backend + static site | `personal-codex-router-dashboard.service`, `/opt/personal-codex-router/dashboard`, Linux loopback 8320 |
| Linux friendly address | `personal-codex-router-url.socket` and `.service`, loopback port 80 |
| Claude gateway routing | Active for both subscriptions; T3 `claudeAgent` on Linux and Mac, Linux shell `claude` via `scripts/claude-gateway.py` ([routing](docs/claude-routing.md), [activation and acceptance](docs/claude-gateway-2026-10-15.md)) |
| Usage collector | `personal-codex-router-usage.timer`, every 15 seconds |
| Usage database | `/var/lib/personal-codex-router/usage.sqlite3` |
| Mac overview tunnel | `~/Library/LaunchAgents/com.user.personal-router.plist`, loopback 18320 → Linux 8320 |
| Mac friendly address | `/Library/LaunchDaemons/com.user.personal-router-url.plist`, loopback 80 → Mac 18320 |
| Mac address helper | `/Library/Application Support/PersonalCodexRouter/loopback-forwarder.py` |
| Mac management tunnel | `~/Library/LaunchAgents/com.user.codex-router-dashboard.plist`, loopback 18317 → Linux 8317 |

Per-account fast mode: [plugin policy, deployment and websocket limits](docs/fast-mode-policy.md).

Codex WebSockets: [rollout with response steering kept off](docs/codex-websockets.md); response steering must remain explicitly off on 8.0.79.

T3 usage hub: [read-only, separately keyed limits for T3 Code](docs/t3-usage-hub.md).

Gateway upgrade, verification and rollback: [8.0.79 upgrade](docs/gateway-upgrade-8.0.79-2026-11-19.md), live since 2026-11-20; 8.0.47 is the rollback target. Local KV is disabled (`server.kv-store.disabled: true`), keeping session/date-pin/replay state in memory as on 8.0.47, so backups need no bbolt support. Rollback removes this key first because 8.0.47 strict PATCH rejects it. The checkout validates sign-in and conversation moves on 8.0.30, 8.0.43, 8.0.47 and 8.0.79, and marks account-add tested on 8.0.47/8.0.79. Dashboard Codex sign-in uses the gateway credential lock and runtime confirmation; extend its exact-version allowlist only after `tests/gateway_replace_integration.py` passes. Legacy 8.0.7 remains excluded. The nested v8 config needs no schema migration; see the [migration history](docs/gateway-config-v8-migration.md).

Verified 2026-10-27: `zz-gateway-8.0.30.conf` selects the live executable. `server.host` is empty, so inference listens on `*:8317`; nftables admits loopback and only the Mac over Tailscale. Management remains loopback-only (`management.allow-remote: false`). Mac inference connects directly to `http://100.64.0.10:8317`; management and the computer-only dashboard address use SSH forwards. Gateway retry policy is `request-retry: 5`, `max-retry-interval: 45` seconds and `max-retry-credentials: 2`. The Free Codex account's model listing excludes `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol` and `gpt-5.6-sol`; see [model routes](docs/standard-management-setup.md#model-routes).

The Mac system launch daemon reserves port 80 but runs its forwarding process as `user`. Its log belongs in `~/Library/Logs/PersonalCodexRouter/url-helper.log`; a path under `/var/log` prevented that unprivileged process from starting and has a dedicated repair script.

Linux services and the Mac address helper/tunnels start automatically through systemd and launchd. No DNS/hosts-file edits, public exposure, exit node, or new inbound tailnet firewall rules are required. Chrome resolves the `.localhost` address locally.

### Advanced management

The original Management Center remains available:

- Mac: `http://127.0.0.1:18317/management.html#/quota`
- Linux: `http://127.0.0.1:8317/management.html#/quota`

It uses a separate management key, stored privately at `~/.config/personal-codex-router/management-key` on each host. Its Remember password option is separate from the everyday overview, which does not ask for a key. The existing service filesystem restrictions can block config-save operations in the Management Center; reviewed SSH changes remain the supported way to edit runtime configuration.

The official panel is pinned to v1.24.2 and checked against its release SHA-256. Automatic panel updates remain disabled.

Source files are in `dashboard/`; installed runtime copies live under `/opt/personal-codex-router`. For dashboard/watch changes, preview `python3 scripts/install-operations.py --claude-watch-only`, then deploy with `sudo -n python3 scripts/install-operations.py --claude-watch-only --apply`; this backs up replaced runtime files and watch snapshots. Run B's watch with `systemctl --user start personal-codex-router-claude-b-usage.service`; it reads only when a read is due. Restart only `personal-codex-router-dashboard` for backend changes. Static assets do not require a service restart. Avoid restarting the model gateway for dashboard work.

For account-access changes, preview `python3 scripts/install-operations.py --account-access-only`, then apply with `sudo -n python3 scripts/install-operations.py --account-access-only --apply`. This explicit manifest backs up replaced files, installs the dashboard unit (write access to the Codex auth dir for sign-in), reloads systemd and restarts only the dashboard and Tailscale preview proxy, never the gateway. Both this scope and `--claude-watch-only` ship the credential writer, dashboard backend and static assets, preview proxy and updated `router-backup.py` (mode `0755`), so scheduled backups skip unfinished credential writes. Sign-in sessions and outcomes are in memory; restarting the dashboard interrupts pending sign-ins. See [account access](docs/account-access.md) for the isolated visual fixture and security limits.

The tests cover device sign-in, identity validation, the locked credential writer, immediate disk/listing/runtime-token verification, post-import refresh preservation, serialized mutation races, routing switches, proxy and installer scope, unknown quota handling, timezone-aware usage boundaries, credential filtering, host/origin restrictions, and denial of management/traversal paths. The overview never forwards arbitrary management routes. All its SQL connections use read-only mode; the service has write access to SQLite’s directory only because SQLite needs its WAL shared-memory sidecar.

The initial setup record is in `docs/standard-management-setup.md`. Dated evidence is in `docs/`. Model streaming was verified for both accounts during the management setup. On September 22, automatic account failover passed in both directions in an isolated instance of then-installed 7.3.11 using that date's production routing/retry policy. Each test invalidated a copied credential after establishing session affinity; the same request recovered through the other account and the following request stayed there. That test did not cover real quota exhaustion or mid-stream failures; see `docs/failover-verification.json`. The [8.0.47 preparation](docs/gateway-upgrade-8.0.47-2026-10-29.md) separately records synthetic 429/503 failover checks. Phone pairing requirements are in `docs/mobile-remote.md`.

### Stop or roll back the overview

Stopping the overview does not change the model gateway or either account’s authentication.

On Linux:

```sh
sudo systemctl disable --now personal-codex-router-url.socket
sudo systemctl stop personal-codex-router-url.service
sudo systemctl disable --now personal-codex-router-dashboard.service
```

On the Mac:

```sh
sudo launchctl bootout system/com.user.personal-router-url
launchctl bootout "gui/$(id -u)/com.user.personal-router"
```

These commands stop the current Mac jobs; move their plist files out of the LaunchDaemons/LaunchAgents folders if they should remain disabled after a reboot/login. Preserve the files to restore them later. The prior Management Center and its tunnel remain available.

To also roll back the original management/statistics activation, follow the [manual procedure after config migration](docs/gateway-config-v8-migration.md#removing-the-dashboard-after-migration). `scripts/rollback-dashboard.py` deliberately refuses the live nested config and also guards against changes since activation. Preserve later gateway settings, usage records and secure backups.

No code was committed or pushed during setup. Before future commits or pushes, follow this project's AGENTS.md review requirement.
