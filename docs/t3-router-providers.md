# T3 agents through the local gateway

T3 can run the official Codex, Claude Code and Antigravity agents against the
same CLIProxyAPI gateway. The agents retain their native installation and update
mechanisms. The gateway owns the upstream subscription sign-ins; each agent uses
a gateway client key to reach it.

This supplements [setup steps 5–9](../README.md#set-up-on-your-own-machine).
Keep existing provider instances and their homes when adding the gateway: those
homes contain login and session state. Configure providers on the environment
where the agents run. Loopback addresses below refer to that machine.

## Codex: preserve the instance and use an updateable CLI

An **Existing CLI** instance pointing inside a versioned T3-managed installation
can run successfully while leaving updates manual. Updating the T3 application
does not necessarily update that executable.

1. Inspect the existing instance's **Binary path**, **Home path** and launch
   arguments in **Settings → Providers**. Preserve its home, router configuration
   and existing environment variables.
2. Use an official Codex CLI installation. Follow the
   [official CLI installation guide](https://learn.chatgpt.com/docs/codex/cli).
   When using the standalone installer, retain the same Codex home so its package
   metadata and the instance's state use the intended location. If a valid native
   CLI is already installed, reuse it.
3. Point **Binary path** at its stable command, such as the absolute expansion of
   `~/.local/bin/codex`, rather than a file inside a particular release directory.
   Preserve the provider's existing ID and router settings from README step 7.
4. Check the CLI version and the update action T3 offers. A recognized standalone
   installation supports `codex update`; package-manager installations should
   use their owning package manager. Do not replace an installation with a
   wrapper just to make the version check pass.

This does not require another ChatGPT account or another T3 provider instance.
T3's managed **Connect with ChatGPT** setup is also supported by T3, but converting
an existing router instance into that setup is a separate authentication change.

## Claude Code

Keep its official CLI installation and the existing Claude config directory.
README step 9 lists the gateway environment variables, including the local
`ANTHROPIC_BASE_URL` and sensitive `ANTHROPIC_AUTH_TOKEN`. A native Claude Code
installation can be updated through T3 using `claude update`.

## Antigravity: managed agent, existing gateway account

First connect the Antigravity subscription account to CLIProxyAPI. Installing
the agent in T3 and signing the upstream account into the gateway are separate
operations.

### Give the agent known model names

The agent builds T3's model picker itself. When `AGY_LLM_GATEWAY_MODELS` lists
model IDs the agent recognizes, such as `gemini-4-neon-low` through
`gemini-4-neon-xhigh`, it groups them into one model with a friendly name
(**Gemini 4 Neon**) and a separate **Thinking Level** option. It then calls the
gateway with the bare family name (`gemini-4-neon`) and sends the level in the
request. Unrecognized IDs, including any `antigravity/…` prefix, appear as raw
IDs without thinking levels.

The gateway's Antigravity account exposes each family only at its default level
(for example `gemini-4-neon-xhigh`), so add one bare-family alias per family
under `oauth.model-alias.antigravity`. Keep existing aliases. `fork: true`
keeps the original IDs available to other clients.

```yaml
oauth:
  model-alias:
    antigravity:
      - {"name": "gemini-4-neon-xhigh", "alias": "gemini-4-neon", "fork": true}
      - {"name": "gemini-4-carbon-high", "alias": "gemini-4-carbon", "fork": true}
      - {"name": "gemini-4-argon-high", "alias": "gemini-4-argon", "fork": true}
      - {"name": "gemini-4-hydrogen-xhigh", "alias": "gemini-4-hydrogen", "fork": true}
      - {"name": "gemini-3.8-flash-high", "alias": "gemini-3.8-flash", "fork": true}
```

Use the families the account actually offers. The upstream model honours the
requested level: on one prompt, Neon used about 2,250, 3,314 and 8,423 thought
tokens at Low, High and Extra.

With the root-owned config of README step 6, the management API cannot save:
`PATCH /v0/management/oauth-model-alias` returns 500 and still changes the
running handler's copy. Edit the file privately instead, preserving its inode,
owner and permissions (`scripts/gateway_config.write_same_inode`), as described
in README step 6. Check that the watcher logs the reload and that an
authenticated `GET /v1beta/models` lists `models/gemini-4-neon`. Do not restart
an active gateway just to add aliases.

These bare names would clash if another gateway provider ever exposed a model
with the same ID.

### Configure the T3 instance

In **Settings → Providers**, enable Antigravity and install its official managed
runtime. Leave **Binary path** empty so T3 continues to own installation and
updates. Configure these environment variables:

| Variable | Value |
| --- | --- |
| `GOOGLE_GEMINI_BASE_URL` | `http://127.0.0.1:8317` |
| `AGY_LLM_GATEWAY_URL` | `http://127.0.0.1:8317` |
| `AGY_LLM_GATEWAY_WIRE_PROTOCOL` | `genai` |
| `AGY_LLM_GATEWAY_MODELS` | see below |
| `AGY_LLM_GATEWAY_TIER_PRO` | `gemini-4-neon-xhigh` |
| `AGY_LLM_GATEWAY_TIER_FLASH` | `gemini-3.8-flash-high` |
| `AGY_LLM_GATEWAY_TIER_FLASH_LITE` | `gemini-3.1-flash-lite` |
| `NO_PROXY` | `127.0.0.1,localhost,::1` |

`AGY_LLM_GATEWAY_MODELS` lists every level to offer, comma-separated:

```text
gemini-4-neon-low,gemini-4-neon-medium,gemini-4-neon-high,gemini-4-neon-xhigh,
gemini-4-carbon-low,gemini-4-carbon-medium,gemini-4-carbon-high,
gemini-4-argon-low,gemini-4-argon-medium,gemini-4-argon-high,
gemini-4-hydrogen-low,gemini-4-hydrogen-medium,gemini-4-hydrogen-high,gemini-4-hydrogen-xhigh,
gemini-3.8-flash-low,gemini-3.8-flash-medium,gemini-3.8-flash-high,gemini-3.1-flash-lite
```

Enter it as one line without spaces. The picker then shows six models: Neon and
Hydrogen with Low/Medium/High/Extra, Carbon, Argon and 3.8 Flash with
Low/Medium/High, and Gemini 3.1 Flash Lite. Leave Claude and other non-Gemini
models out: the agent shows them as raw IDs, and Claude is better used through
T3's Claude instance. Threads saved with a model ID that is no longer listed
must select a model again. This list does not track future account model
changes. Exclude image-only models.

Choose **Gemini API key** as the sign-in method, enter the **gateway client key**
created in README step 6, and choose **Connect**. This is the local key accepted
by CLIProxyAPI, not its management key or an upstream Google credential. T3
stores this secret outside its settings file. The account already signed into
the gateway supplies the upstream access; this setup does not provision a paid
Google API key.

Use T3's supported settings UI for this instance. Do not hand-edit the generated
Antigravity ACP settings, which T3 can overwrite. The working setup uses the
standard `gemini-api-key` method; it does not require the CLI's experimental
gateway authentication method.

## Updates and connection labels

Apply compatible provider updates in **Settings → Providers**. Antigravity uses
T3's managed updater; Codex and Claude use the updater T3 recognizes for their
installation. Updating T3 itself and updating provider CLIs are different
operations. No extra cron job, timer or update watcher is required by this guide.
Updating a client does not update CLIProxyAPI or the dashboard.

T3's authentication subtitles describe different things. Codex can show the
plan of a saved ChatGPT login even when requests use a custom endpoint. Claude
reports **Custom API endpoint** for the router URL. Antigravity reports
**Gemini API key** for the selected local authentication method. None of these
labels identifies which pooled account handled a particular request. Use the
router's account cards and request history for that information.

## Verification and limits

After configuration, check that each instance is ready and that the intended
models are available. Send a short request through each agent and confirm its
provider/account in the gateway's request history. For Antigravity, also ask the
agent to read a temporary file containing a fresh test value and verify the
returned content. This exercises a tool turn as well as text generation.

The integration was checked with CLIProxyAPI 8.0.79, Codex 0.171.0, Claude Code
2.1.330, Antigravity 1.5.15 and T3 0.1.12-nightly.20261123.5151. Antigravity
completed both a real response and a file-read tool turn through the gateway.
The thinking-level picker setup above was checked with a Gemini 4 Neon Low
request from T3 through the gateway.
These observations describe a configured Linux installation, not an end-to-end
clean-machine installer test or a guarantee for future releases. No account
identifiers, credentials, logs or host settings are included here.

The dashboard's optional T3 health probe checks the desktop user's default
`~/.t3/userdata/server-runtime.json`, the owner and name of its Linux `t3code`
process, and the loopback listener (IPv4 or `::1`). It reports a separate
**T3 Code server** light beside **Codex server**; the Codex daemon used by
Phone Remote and Mac SSH is always checked as before. An unrecognized
process/runtime reports unknown; without the T3 runtime file, no T3 light is
shown. This is a local liveness check, not an inference or upstream status
check.

Sources:

- [T3 provider installation and updates](https://github.com/pingdotgg/t3code/blob/main/docs/user/install.md#providers)
- [T3 Antigravity setup and managed runtime](https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-antigravity.md)
- [Antigravity CLI settings](https://antigravity.google/docs/settings?tab=cli)
- [CLIProxyAPI 8.0.79 configuration example](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.79/config.example.yaml)
