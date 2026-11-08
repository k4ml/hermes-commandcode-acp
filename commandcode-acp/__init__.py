"""Command Code ACP provider profile.

Drives the `commandcode acp` ACP agent over stdio (Agent Client Protocol), so the
Command Code subscription's models are usable from Hermes without a Provider API
key — auth rides on `commandcode login` (any plan). Same pattern as the built-in
copilot-acp provider; the ACP stdio shim (`agent.copilot_acp_client`) is extended
via subclassing (NOT wrapping — the shim's internal request loop dispatches
server messages through `self`, so overrides only fire on a subclass):

* Mode preset — after `session/new` the session mode is set via `session/set_mode`
  (default ``auto-accept``: file edits run, riskier actions still prompt).
  Override with ``HERMES_COMMANDCODE_ACP_MODE`` (default|auto-accept|plan|dont-ask|bypass).
* Permission bridge — `session/request_permission` from the agent is escalated to
  Hermes' human-approval gate (`tools.approval.request_tool_approval`), so the
  user answers Command Code's questions with the same approval cards used for
  dangerous shell commands. Unattended contexts fail closed (deny -> cancel).
* Tool visibility — completed agent tool calls are folded into the reply text.
* Continuation steering — after tool-execution results a trailing directive keeps
  the agent on the client's `<tool_call>` protocol instead of drifting to its own
  harness tools (whose out-of-cwd paths its own permission engine gates, which
  produced denied-and-skipped batches and silent empty turns).
"""

import contextlib
import json
import logging
import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "auto-accept"
_MODE_ENV = "HERMES_COMMANDCODE_ACP_MODE"

_STEERING_DIRECTIVE = (
    "You are the ACP agent backend inside Hermes. The tool results above were "
    "executed by the client out-of-band; their outputs are authoritative. You "
    "have no direct tools of your own in this environment — never call glob, "
    "read_file, read_directory, bash, edit or similar harness tools yourself. "
    "Take your next action by emitting a <tool_call>{...}<tool_call> block exactly as "
    "instructed at the top, or reply with your final answer. If a previous "
    "action was denied, say so plainly in your reply."
)


def _client_class() -> type:
    """Build (once per import) the interactive subclass of the copilot ACP shim."""
    from agent.copilot_acp_client import CopilotACPClient

    class CommandCodeACPClient(CopilotACPClient):
        """Copilot ACP stdio shim + commandcode mode preset, permission bridging,
        tool-call visibility, continuation steering."""

        def _create_chat_completion(self, *, messages=None, **kwargs):
            """Append the steering directive after tool-execution results.

            The shim re-renders the whole transcript per call; after a tool round
            the rendered transcript ends on `Tool:` entries, which invites the
            agent to continue with its own harness tools (glob/read_file/...) —
            the drift behind denied-and-skipped batches and silent empty turns.
            Appending one trailing user-role directive keeps it on the
            emit-`<tool_call>` protocol. The base method appends its own final
            "Continue the conversation..." line after this.
            """
            msgs = list(messages or [])
            if any(isinstance(m, dict) and str(m.get("role") or "").strip().lower() == "tool" for m in msgs):
                msgs = msgs + [{"role": "user", "content": _STEERING_DIRECTIVE}]
            return super()._create_chat_completion(messages=msgs, **kwargs)

        @contextlib.contextmanager
        def _session(self, timeout_seconds: float, *, allow_file_requests: bool = True):
            """Wrap a session to preset the commandcode permission mode."""
            with super()._session(timeout_seconds, allow_file_requests=allow_file_requests) as (session, request):
                mode = (os.getenv(_MODE_ENV, "") or "").strip() or _DEFAULT_MODE
                if mode and mode != "default":
                    try:
                        request("session/set_mode", {"sessionId": session.get("sessionId"), "modeId": mode})
                    except Exception as exc:  # best-effort; default mode still works via the permission bridge
                        logger.warning("CommandCode ACP set_mode(%s) failed; continuing with session default: %s",
                                       mode, exc)
                yield session, request

        def _handle_server_message(self, msg, *, process, cwd, text_parts, reasoning_parts,
                                   allow_file_requests=True) -> bool:
            """Intercept permission requests (bridge to Hermes approvals) and tool-call
            updates (visibility lines); everything else delegates to the shim."""
            if not isinstance(msg, dict):
                return False
            method = msg.get("method")
            if method == "session/request_permission":
                return self._bridge_permission(msg, process=process)
            if method == "session/update" and text_parts is not None:
                self._note_tool_call(msg, text_parts)
            return super()._handle_server_message(
                msg, process=process, cwd=cwd, text_parts=text_parts,
                reasoning_parts=reasoning_parts, allow_file_requests=allow_file_requests)

        def _note_tool_call(self, msg: dict, text_parts: list[str]) -> None:
            try:
                update = (msg.get("params") or {}).get("update") or {}
                if str(update.get("sessionUpdate") or "") != "tool_call":
                    return
                content = update.get("content") or {}
                if not isinstance(content, dict) or str(content.get("status") or "") != "end":
                    return
                title = str(content.get("title") or "").strip()
                kind = str(content.get("kind") or "").strip()
                if title:
                    text_parts.append(f"\n[{kind or 'tool'}] {title}")
            except Exception:
                pass

        def _bridge_permission(self, msg: dict, *, process) -> bool:
            """Escalate an ACP permission request to Hermes' human-approval gate and
            relay the verdict back to the agent."""
            if process.stdin is None:
                return True
            params = msg.get("params") or {}
            reason_bits: list[str] = []
            tool_hint = "action"
            for block in params.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "")
                if btype == "text":
                    if str(block.get("text") or "").strip():
                        reason_bits.append(str(block.get("text")).strip()[:400])
                elif btype == "tool_call":
                    title = str(block.get("title") or "").strip()
                    if title:
                        tool_hint = title.split()[0] if title.split() else title
                        reason_bits.append(f"tool: {title}")
                    for sub in block.get("content") or []:
                        if isinstance(sub, dict) and sub.get("type") == "text" and str(sub.get("text") or "").strip():
                            reason_bits.append(str(sub.get("text")).strip()[:300])
            reason = " | ".join(reason_bits)[:600] or "Command Code agent wants to run a tool"
            options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
            verdict: dict[str, Any] = {"approved": False}
            try:
                from tools.approval import request_tool_approval
                verdict = request_tool_approval(f"commandcode-acp:{tool_hint}", reason)
            except Exception as exc:
                logger.warning("CommandCode ACP approval bridge failed (denying): %s", exc)
            try:
                from tools.approval import get_current_session_key
                session_key = get_current_session_key()
            except Exception:
                session_key = "?"
            logger.info(
                "CommandCode ACP permission bridge: tool=%s pending_session=%s verdict=%s options=%s reason=%r",
                tool_hint, session_key, {k: verdict.get(k) for k in ("approved", "status", "message")},
                [(str(o.get("id")), str(o.get("kind"))) for o in options][:6], reason[:160])
            chosen = None
            if verdict.get("approved"):
                for kind_pref in ("allow_once", "allow_always"):
                    chosen = next((o for o in options if str(o.get("kind") or "") == kind_pref), None)
                    if chosen is not None:
                        break
                if chosen is None:
                    chosen = next((o for o in options if str(o.get("kind") or "").startswith("allow")), None)
            payload = ({"outcome": {"outcome": "selected", "optionId": str(chosen.get("id"))}}
                       if chosen is not None else {"outcome": {"outcome": "cancelled"}})
            try:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": payload}) + "\n")
                process.stdin.flush()
            except Exception as exc:
                logger.warning("CommandCode ACP permission response failed: %s", exc)
            return True

    return CommandCodeACPClient


class CommandCodeACPProfile(ProviderProfile):
    """Command Code ACP — external process, no REST models endpoint."""

    def create_client(self, **client_kwargs: Any) -> Any:
        """Build the interactive ACP stdio shim rather than an HTTP client."""
        return _client_class()(**client_kwargs)

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 15.0
    ) -> list[str] | None:
        """Models advertised by a short-lived signed-in ACP session (`session/new`).

        The subprocess owns auth (`commandcode login`), so api_key/base_url are
        ignored. None when the CLI is missing, `acp` is refused, or the probe
        fails/times out."""
        from hermes_cli.auth import resolve_external_process_provider_credentials

        try:
            creds = resolve_external_process_provider_credentials(self.name)
            if not str(creds.get("base_url") or "").startswith("acp://"):
                return None
            client = self.create_client(
                api_key=creds.get("api_key"), base_url=creds.get("base_url"),
                command=creds.get("command"), args=creds.get("args"))
            return client.list_models(timeout_seconds=timeout) or None
        except Exception:
            return None


commandcode_acp = CommandCodeACPProfile(
    name="commandcode-acp", aliases=("commandcode-acp-agent", "cmd-acp"),
    api_mode="chat_completions",  # ACP subprocess uses chat_completions routing
    env_vars=(),  # Managed by ACP subprocess (`commandcode login`)
    base_url="acp://commandcode",  # ACP internal scheme
    auth_type="external_process",
    # How to launch the CLI; env overrides follow the copilot-acp convention.
    process_command="commandcode",
    process_args=("acp",),
    process_command_env_vars=("HERMES_COMMANDCODE_ACP_COMMAND",),
    process_args_env_var="HERMES_COMMANDCODE_ACP_ARGS",
    # Plan default per `commandcode --list-models`; used when model discovery /
    # selection is unavailable, and as the auxiliary (title/etc.) model.
    fallback_models=(
        "deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash",
        "zai-org/glm-5.3", "qwen/qwen3.8-flash", "moonshotai/kimi-k3.1-pro",
    ),
    default_aux_model="deepseek/deepseek-v4.1-flash",
)

register_provider(commandcode_acp)
