#!/usr/bin/env python3
"""Install the `commandcode-acp` model-provider plugin into a Hermes installation.

Makes Command Code's subscription usable from Hermes over ACP (`commandcode acp`
stdio subprocess) without a Provider API key — approval questions from the agent
come back as Hermes approval cards, and billing stays on the Command Code plan.

    python3 scripts/install_cmd_acp.py              # install (idempotent, syncs files)
    python3 scripts/install_cmd_acp.py --status
    python3 scripts/install_cmd_acp.py --uninstall
    python3 scripts/install_cmd_acp.py --hermes-home /path/to/profile

Targets $HERMES_HOME (default ~/.hermes). Prerequisites on the target host:
the `command-code` npm package installed and `commandcode login` done (auth rides
on that login; any plan without Provider API access works). The installer copies
the plugin files from ./commandcode-acp, then registers the model alias `cmd` via
`hermes config set` when the hermes CLI is on PATH (otherwise it prints the two
commands). After install: `hermes chat -q 'Reply OK' -m cmd`.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "commandcode-acp"
FILES = ("plugin.yaml", "__init__.py")
ALIAS_SETTINGS = (
    ("model_aliases.cmd.provider", "commandcode-acp"),
    ("model_aliases.cmd.model", "deepseek/deepseek-v4.1-flash"),
)

# The npm CLI installs several bin names; PATH alone is not enough because a
# daemon launched without the npm prefix in PATH finds only the desktop app's
# /usr/bin/command-code, which pops a GUI window instead of serving ACP.
CLI_NAMES = ("cmd", "cmdc", "commandcode", "command-code")
EXTRA_BIN_DIR_GLOBS = (
    "~/.local/share/fnm/node-versions/*/installation/bin",
    "~/.local/share/fnm/node-versions/*/bin",
    "~/.nvm/versions/node/*/bin",
    "~/.volta/bin",
    "~/.asdf/shims",
    "~/.bun/bin",
    "~/.local/share/pnpm",
    "~/.npm-global/bin",
    "/usr/local/bin",
)


def resolve_cli() -> str:
    """Absolute path to the Command Code CLI that can serve ACP over stdio.

    Returns the bare name "cmd" when nothing verifies (the plugin then falls
    back to the HERMES_COMMANDCODE_ACP_COMMAND hint).
    """
    candidates: list[str] = []
    for name in CLI_NAMES:
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for pattern in EXTRA_BIN_DIR_GLOBS:
        for directory in sorted(glob.glob(os.path.expanduser(pattern))):
            if not os.path.isdir(directory):
                continue
            for name in CLI_NAMES:
                candidate = os.path.join(directory, name)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    candidates.append(candidate)

    seen: set[str] = set()
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        # The desktop app is an Electron bundle; it cannot speak ACP.
        try:
            if os.path.getsize(real) > 20 * 1024 * 1024:
                continue
            with open(real, "rb") as fh:
                if fh.read(4) == b"\x7fELF":
                    continue
        except OSError:
            continue
        try:
            probe = subprocess.run(
                [candidate, "acp", "--help"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=25,
                stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "agent client protocol" in ((probe.stdout or "") + (probe.stderr or "")).lower():
            return candidate
    return "cmd"


def hermes_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser().resolve()


def plugin_dir(home: Path) -> Path:
    return home / "plugins" / "model-providers" / "commandcode-acp"


def source_missing() -> str | None:
    for name in FILES:
        if not (SOURCE_DIR / name).is_file():
            return f"missing source file {SOURCE_DIR / name} (run from a clone of this repo)"
    return None


def copy_files(home: Path) -> tuple[list[str], list[str]]:
    """Sync plugin files; returns (updated, already-current) lists."""
    dest = plugin_dir(home)
    dest.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []
    current: list[str] = []
    for name in FILES:
        src, dst = SOURCE_DIR / name, dest / name
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            current.append(name)
        else:
            shutil.copyfile(src, dst)
            updated.append(name)
    return updated, current


def has_hermes_cli() -> bool:
    return shutil.which("hermes") is not None


def alias_from_config(home: Path) -> str:
    """Read model_aliases.cmd.provider straight from config.yaml.

    `hermes config get` is not used here: on this alias key it has been observed
    to hang, and the config file is the authoritative store regardless of the
    CLI's key validator."""
    cfg = home / "config.yaml"
    if not cfg.is_file():
        return "(no config.yaml)"
    text = cfg.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"model_aliases:\s*\n\s+cmd:\s*\n\s+provider:\s*(\S+)", text)
    return m.group(1) if m else "(unset)"


def set_alias() -> bool:
    """Register the `cmd` model alias via the hermes CLI. False when CLI is absent."""
    if not has_hermes_cli():
        for key, value in ALIAS_SETTINGS:
            print(f"  hermes not on PATH — run manually: hermes config set {key} '{value}'")
        return False
    for key, value in ALIAS_SETTINGS:
        try:
            proc = subprocess.run(["hermes", "config", "set", key, value],
                                  capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            print(f"  warning: hermes config set {key} timed out; run manually: hermes config set {key} '{value}'")
            continue
        if proc.returncode != 0:
            print(f"  warning: hermes config set {key} failed: {(proc.stderr or proc.stdout).strip()[:200]}")
        else:
            print(f"  alias set: {key} = {value}")
    return True


def cmd_install(args: argparse.Namespace) -> int:
    home = hermes_home(args.hermes_home)
    if reason := source_missing():
        print(f"ABORT: {reason}")
        return 1
    print(f"Installing commandcode-acp provider into {home}")
    updated, current = copy_files(home)
    for name in updated:
        print(f"  wrote  {plugin_dir(home) / name}")
    for name in current:
        print(f"  ok     {plugin_dir(home) / name} (current)")
    set_alias()
    if has_hermes_cli():
        print("  next: hermes chat -q 'Reply OK' -m cmd")
    print("  note: provider plugins are imported once per process — restart a running")
    print("        gateway to pick up plugin updates: hermes gateway restart")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    home = hermes_home(args.hermes_home)
    dest = plugin_dir(home)
    print(f"Hermes home : {home}")
    print(f"Plugin dir  : {dest}")
    if reason := source_missing():
        print(f"Source      : {reason}")
        return 1
    for name in FILES:
        src, dst = SOURCE_DIR / name, dest / name
        if not dst.is_file():
            print(f"  {name}: NOT INSTALLED")
        elif dst.read_bytes() == src.read_bytes():
            print(f"  {name}: current")
        else:
            print(f"  {name}: INSTALLED but differs from repo source (run install to update)")
    if has_hermes_cli():
        value = alias_from_config(home)
        print(f"  alias cmd.provider: {value}")
    else:
        print("  hermes CLI not on PATH — alias status unknown")
    print("  commandcode CLI:", "; ".join(
        f"{n}={shutil.which(n)}" for n in ("cmd", "cmdc", "command-code", "commandcode")
        if shutil.which(n)) or "NOT FOUND (none of cmd, cmdc, command-code, commandcode)")
    resolved = resolve_cli()
    print(f"  resolved ACP binary: {resolved}")
    if resolved == "cmd":
        print("    NOT RESOLVED — set HERMES_COMMANDCODE_ACP_COMMAND to the npm CLI's absolute path")
    elif "Command Code" in resolved or "command-code" in os.path.basename(resolved):
        print("    WARNING: that path looks like the desktop app; it cannot serve ACP over stdio")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    home = hermes_home(args.hermes_home)
    dest = plugin_dir(home)
    if dest.is_dir():
        shutil.rmtree(dest)
        print(f"Removed {dest}")
    else:
        print(f"Nothing to remove at {dest}")
    print("  alias keys left in place (harmless); remove from ~/.hermes/config.yaml if wanted")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hermes-home", help="override HERMES_HOME / ~/.hermes (e.g. a profile dir)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show install state")
    group.add_argument("--uninstall", action="store_true", help="remove the plugin directory")
    args = parser.parse_args()
    if args.status:
        return cmd_status(args)
    if args.uninstall:
        return cmd_uninstall(args)
    return cmd_install(args)


if __name__ == "__main__":
    sys.exit(main())
