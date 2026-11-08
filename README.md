# hermes-commandcode-acp

A Hermes Agent model-provider plugin that makes a [Command Code](https://commandcode.ai)
subscription usable directly from Hermes — no Provider API key, no plan upgrade,
no switching to the Command Code CLI.

The provider drives `commandcode acp` (Command Code's hidden Agent Client
Protocol subcommand) as a stdio subprocess. Model inference happens inside that
subprocess, so it bills to your Command Code plan; Hermes makes no model API
calls for chats running on this provider.

## Why ACP

Command Code exposes the Provider API (`api.commandcode.ai`) only on certain
plans. The ACP endpoint reuses the CLI login (`commandcode login`) and works on
every plan — it is the same machinery editors like Zed use. This plugin is the
Hermes-side client for it, following the pattern of Hermes' built-in
`copilot-acp` provider but as a standalone out-of-tree plugin.

## What it adds on top of the stock shim

- **Agentic loop with real execution.** Command Code reasons; tool calls it
  emits run through Hermes' own tools, so edits, searches and terminal commands
  execute in the Hermes session with Hermes' safety rules.
- **Permission bridge.** `session/request_permission` from the agent escalates
  to Hermes' human-approval gate — the same approval cards used for dangerous
  shell commands. Unattended contexts (cron, `hermes chat -q`) fail closed.
- **Permission-mode preset.** After `session/new` the plugin sends
  `session/set_mode` so the agent does not sit in interactive-prompt default
  mode. Default `auto-accept` (edits flow, riskier actions still prompt);
  override with `HERMES_COMMANDCODE_ACP_MODE`
  (default|auto-accept|plan|dont-ask|bypass).
- **Continuation steering.** After tool-execution results a trailing directive
  keeps the agent on the tool-call-block protocol instead of drifting to its own
  harness tools (whose out-of-workspace paths are permission-gated by Command
  Code itself — the cause of denied batches and silent empty turns).
- **Tool visibility.** Actions the agent takes with its own tools are folded
  into the reply as short lines.
- **Model discovery.** All plan models (the same list `commandcode --list-models`
  prints) come from the ACP `session/new` config options, so `hermes model` and
  `/model` list them.

## Install

```bash
git clone https://github.com/k4ml/hermes-commandcode-acp.git
cd hermes-commandcode-acp
python3 scripts/install_cmd_acp.py          # copies plugin + registers alias `cmd`
hermes chat -q 'Reply OK' -m cmd            # verify
```

`--hermes-home` targets a different profile directory. `--status` and
`--uninstall` are supported.

Prerequisites: `npm i -g command-code` and `commandcode login` on the target
host. Hermes itself must be the git install (it ships
`agent/copilot_acp_client.py`, which this plugin reuses).

## Usage

```
/model cmd                                   # switch a session (CLI, WebUI, gateway)
hermes chat -q "..." -m cmd                  # one-shot
--provider commandcode-acp -m <model-id>     # explicit provider + any plan model
```

Make it the default with `/model cmd --global` or the `hermes model` picker.

## Environment overrides

| Variable | Purpose |
| --- | --- |
| `HERMES_COMMANDCODE_ACP_MODE` | Command Code permission mode per session |
| `HERMES_COMMANDCODE_ACP_COMMAND` | override the `commandcode` binary path |
| `HERMES_COMMANDCODE_ACP_ARGS` | override the ACP spawn arguments |

## Troubleshooting

- **`Could not find the '<provider>' CLI command '<name>'`** — the launch binary is
  resolved on PATH at plugin import and the serving process imports plugins once;
  after installing the plugin or editing its source, restart the daemon:
  `hermes gateway restart`. Fresh `hermes chat` processes always pick up the
  current file. `HERMES_COMMANDCODE_ACP_COMMAND` (absolute path) overrides the
  automatic `cmd` / `cmdc` / `command-code` / `commandcode` search.
- Bridge decisions log to `~/.hermes/logs/agent.log` under
  `CommandCode ACP permission bridge:`.
- `hermes chat -q` and cron deny risky actions by design (fail closed).
- If the agent stops mid-task, check the newest session transcript under
  `~/.commandcode/projects/<project>/*.jsonl`; a turn ending on a denied
  tool-result line means the permission bridge denied, not that the turn
  completed.
- **The Command Code desktop app pops open on every turn?** The plugin is
  spawning the Electron desktop build instead of the CLI. The desktop binary
  (`/usr/bin/command-code`, symlinked to `/opt/Command Code/command-code`)
  ignores the `acp` argument and opens a window. The plugin now resolves the
  npm CLI explicitly and rejects the desktop bundle, but a daemon started
  before that fix still holds the old resolution — restart it with
  `hermes gateway restart`. Check the resolution with
  `python3 scripts/install_cmd_acp.py --status`; it prints
  `resolved ACP binary:`. A path under `/opt/Command Code` means the desktop
  app is in use.
- The npm CLI is a Node script, so it needs `node` on PATH (Node 22+). The
  plugin resolves the CLI under fnm/nvm/volta/asdf/bun/pnpm install prefixes
  even when the daemon's PATH omits them; if the CLI lives somewhere else,
  point `HERMES_COMMANDCODE_ACP_COMMAND` at its absolute path.

## Billing

Everything runs on your Command Code subscription exactly as CLI usage does.
Hermes' own auxiliary tasks (title generation etc.) keep using whatever aux
model Hermes is configured with. Zero-retention is available as usual via the
Command Code CLI settings (`CMD_ZDR=1`), since the subprocess owns the client.
