# Handoff — commandcode-acp Hermes provider plugin

Session date: 2026-11-06/08 (JST). Operator: Kamal (k4ml).
Status at handoff: committed and pushed, verified on this host, two open items
listed at the bottom.

## What this is

`commandcode-acp` is an out-of-tree Hermes model-provider plugin that makes a
Command Code subscription usable directly from Hermes chat, with no Provider
API key. Command Code's Provider API is not on the operator's plan (confirmed
by the operator, Nov 2026), so the plugin drives `commandcode acp` — the ACP
stdio agent endpoint — instead. Authentication rides on the CLI login
(`commandcode login`, account kamalas3s, verified live); billing stays on the
Command Code plan and Hermes makes zero model API calls for chats running on
this provider.

The plugin reuses Hermes' shipped ACP stdio shim (`agent/copilot_acp_client.py`,
the copilot-acp machinery) via a subclass. Do not convert it to a wrapper:
the shim's internal request loop dispatches server messages through `self`, so
wrappers never see `session/request_permission` — this exact bug was the first
implementation's silent-failure mode.

## How the integration works

- `session/new` (protocolVersion 1) advertises the full plan catalog (95 models)
  under `configOptions` id=`model`; Hermes' model discovery reads it. ACP
  session/new also lists effort options and modes.
- After each `session/new` the plugin sends `session/set_mode`
  (`HERMES_COMMANDCODE_ACP_MODE`, default `auto-accept`) so the agent does not
  sit in ask-for-everything default mode.
- Command Code reasons and responds; tool calls it emits are executed by Hermes
  through its own tool loop — command code = brain, Hermes = executor with its
  own approval gates. Two separate permission layers exist:
  1. agent-side ACP `session/request_permission` (only when the agent uses its
     own harness tools — now rare, see steering below), bridged to
     `tools.approval.request_tool_approval` and instrumented with INFO logs
     tagged `CommandCode ACP permission bridge:` in `~/.hermes/logs/agent.log`;
  2. Hermes' normal dangerous-command guard on tool calls (this is the layer
     that produced the live approval card in the interactive test).
- Post-tool-result calls get a trailing steering directive appended to the
  rendered transcript; without it the agent drifts to its own harness tools
  (glob/read_file/read_directory), whose out-of-workspace paths are gated by
  Command Code's own permission engine, producing denied-and-skipped batches
  and empty turns.
- Binary resolution happens at plugin import: prefer `cmd`, then `cmdc`,
  `command-code`, `commandcode` (the npm package installs all four).
  `HERMES_COMMANDCODE_ACP_COMMAND` overrides; `HERMES_COMMANDCODE_ACP_ARGS`
  overrides the spawn args.

## Where everything lives

- Standalone repo (canonical): `/home/kamal/git/hermes-commandcode-acp`,
  GitHub remote `https://github.com/k4ml/hermes-commandcode-acp`, branch
  `main`, ahead-of-worktree commits: `6741f1d` (plugin + installer),
  `1ef6f7f` (README clone URL), `7975113` (binary resolution), `12d1a97`
  (gateway-restart docs). The repo was deliberately kept OUT of
  kafkai-engineering; a temporary worktree branch there was deleted.
- Live install: `~/.hermes/plugins/model-providers/commandcode-acp/` (verified
  current against repo source). Sync with `python3 scripts/install_cmd_acp.py`
  (options: `--status`, `--uninstall`, `--hermes-home <dir>`).
- Model alias: `model_aliases.cmd` in `~/.hermes/config.yaml` =
  `commandcode-acp` / `deepseek/deepseek-v4.1-flash` (the plan's default
  model). Switch with `/model cmd`, or `--provider commandcode-acp -m <id>`
  for any of the 95 models.
- Hermes source (git install): `~/.hermes/hermes-agent`; the hermes launcher is
  `~/.hermes/hermes-agent/.hermes/bin/hermes` (execs the tools-managed Python).

## Verification evidence (all on this host)

- ACP probe script (scratch, `acp_probe.py` mode): initialize + session/new
  return the 95-model catalog, effort options, five modes (default,
  auto-accept, plan, dont-ask, bypass); `session/set_mode` returns success.
- Acting test: `hermes chat -q` asked the agent to create `bridge.txt` and read
  it back; 4 messages, 2 Hermes tool calls (write_file, read_file), file verified
  on disk with exact content.
- Interactive approval test (tmux): asked for `python3 -c "print(6*7)"`;
  Hermes' dangerous-command card rendered, allowed once from outside (tmux
  send-keys), command executed, agent replied 42.
- Multi-step steering test (post-fix): README heading + out-of-workspace skill
  listing in one prompt; two API calls (6.0s, 12.9s), zero permission events,
  full correct answer. The dead pattern did not reproduce.
- Fresh-process binary test: `hermes chat -q ... -m cmd` after the
  binary-resolution commit answered BIN-OK-7.

## The "stops at the first response" debugging chain

Symptom: turns on the provider produced one reply and nothing after it.
Root cause (from Command Code session transcripts under
`~/.commandcode/projects/<project>/<session>.jsonl`): in post-tool-result
calls the agent used its own harness tools; Command Code's engine gated an
out-of-workspace read; the plugin's approval gate failed closed instantly on
the model-call thread (no card, and at the time no log line); Command Code
marked the batch "Permission denied by the user." plus "Skipped — another tool
call in this batch was denied."; the ACP turn ended with zero message chunks;
Hermes received an empty completion and ended the turn silently.
Fixes: subclass instead of wrapper (dispatch reach), steering directive (removes
the drift, kills the trigger), bridge INFO instrumentation (visibility for the
rare gated path). Debugging reference: read the newest `.jsonl` for the
project — a turn ending on a denied tool-result line means the bridge denied,
not that the turn completed.

## Process-restart requirement

Hermes imports provider plugins once per process ("a rescan never re-imports",
providers/__init__.py). After installing or editing the plugin, restart the
daemon: `hermes gateway restart`. Fresh `hermes chat` processes always load the
current file. The installer prints this reminder; README Troubleshooting
documents it.

## Open items

1. The permission bridge's card path (agent-side, layer 1) is instrumented but
   has not fired live since the steering fix (agent.log count is 0 — expected,
   the agent stays on the tool protocol). If a turn ever produces a bridge
   verdict that lacks a card or denies without asking, debug the session-key /
   presence resolution on the model-call thread next.
2. This WebUI conversation (session 5a0ea3062396) is currently back on the
   Hermes-side model (glm-5.3-flash via ollama-cloud) after manual switching;
   the plugin is idle until `/model cmd` is used again. Aux auto-detect rides
   the main provider when it is this one, so aux tasks bill to Command Code
   while cmd is active.
3. If a host lacks all four binaries on PATH, the provider surfaces the
   `HERMES_COMMANDCODE_ACP_COMMAND` hint; point it at an absolute path.

## Gotchas for future agents

- Do not emit the literal opening of a tool-invocation block (the angle-bracket
  sequence that starts `<tool_call>`) in plain chat text: it is parsed as a real tool
  call and everything after it in the reply is silently dropped. This caused
  three truncated replies during the handoff session. Paraphrase, or keep the
  sequence inside fenced code or files only.
- `hermes config get` on `model_aliases.cmd.provider` hangs (observed twice);
  the installer reads config.yaml directly instead.
- The webui gateway logs watch-notification spam (`Watch notification not
  admitted ... invalid_params` every 2s in errors.log); unrelated to this
  plugin, do not chase it.
- **The Command Code desktop app opening on every turn** (fixed in 9619806):
  the daemon's systemd PATH has no npm prefix, so a PATH-only lookup for the
  CLI fell through to `command-code` -> `/usr/bin/command-code` ->
  `/opt/Command Code/command-code`, the Electron desktop build. It ignores the
  `acp` argument and opens a GUI window; the JSON-RPC handshake never happens,
  and the turn dies with `Copilot ACP process exited early: libva error: ...`
  (that libva/ozone stderr is the Electron app booting, not a broken CLI).
  The npm CLI lives at
  `~/.local/share/fnm/node-versions/v22.23.4/installation/bin/cmd`. Resolution
  now searches fnm/nvm/volta/asdf/bun/pnpm prefixes, rejects the desktop
  bundle, and probes `acp --help` for the ACP blurb. Check with
  `python3 scripts/install_cmd_acp.py --status` (`resolved ACP binary:`).
- The npm CLI is a Node script (`#!/usr/bin/env node`), so the child needs
  `node` on PATH and Node 22+.
