#!/usr/bin/env python3
"""Token Optimizer — GitHub Copilot CLI installer.

Wires Token Optimizer into a Copilot CLI setup:

1. Copies the adapter modules into <copilot_home>/token-optimizer/plugin/
   so the hook bridge runs from a stable path that survives repo moves.
2. Writes the hooks config to <copilot_home>/hooks/token-optimizer.json
   (USER-LEVEL ONLY — never .github/hooks/, which would silently affect a
   whole team's repo without consent; user-level hooks load in all modes
   including non-interactive `copilot -p`, per github/copilot-cli#3345).
3. Seeds capabilities.json for the installed CLI version.

Idempotent: re-running refreshes the payload and rewrites OUR hook file only.
Uninstall removes only files Token Optimizer created.

Usage:
    python3 copilot_install.py install [--dry-run]
    python3 copilot_install.py uninstall [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat as _stat
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from py_trust import py_path_is_trusted, py_trust_reason  # noqa: E402
from runtime_env import copilot_home  # noqa: E402

HOOK_FILE_NAME = "token-optimizer.json"
HOOK_TIMEOUT_SEC = 10

# Modules the bridge needs at runtime, copied next to it so the installed
# hook never depends on the repo checkout location.
_PAYLOAD_MODULES = (
    "copilot_hook_bridge.py",
    "copilot_state.py",
    "copilot_session.py",
    "copilot_vscode.py",
    "bash_hook.py",
    "bash_compress.py",
    # Dependency-free whitelist gate shared by bash_hook + bash_compress.
    "bash_whitelist.py",
    "hook_io.py",
    "plugin_env.py",
    "runtime_env.py",
    # codex_io supplies atomic_write_json — without it the bridge's tally and
    # capabilities writes silently no-op (crash recovery would be dead).
    "codex_io.py",
    # hermes_session supplies compute_quality_score — without it copilot_session
    # silently falls back to a single-signal estimate (degraded quality grades).
    "hermes_session.py",
)


def _plugin_dir(root: Path) -> Path:
    return root / "token-optimizer" / "plugin"


def _hooks_dir(root: Path) -> Path:
    return root / "hooks"


def _py_trust_reason(p: str) -> str | None:
    """None when trusted, else a short human-readable rejection reason."""
    return py_trust_reason(p)


def _py_path_is_trusted(p: str) -> bool:
    """Trusted iff the interpreter's bytes are admin-owned (euid or root) and
    not group/other-writable, and its dir is not world-writable and not
    group-writable by a third party -- the launcher's ownership boundary
    (ssh/sudo/git), in Python. Pure stat, never runs the target. On Windows,
    stat ownership is unreliable under Git-Bash, so require only that the path
    is a real file (the launcher leans on hardcoded allowlists there too)."""
    return py_path_is_trusted(p)


def _resolve_safe_python() -> str:
    """An ABSOLUTE, trusted python for the persisted Copilot hook command.

    Never emit a bare "python3": that string is resolved via $PATH every time the
    hook fires, so a hijacked PATH entry runs attacker code (the launcher's whole
    allowlist exists to stop exactly this, and the Copilot bridge does not use the
    launcher). Resolution order:
      1. TOKEN_OPTIMIZER_PYTHON, if it names a trusted file (user escape hatch,
         same env the launcher honours);
      2. sys.executable -- the interpreter already running this installer, an
         absolute path baked in ONCE here so the hook never does a PATH lookup,
         but only through the same ownership gate (a writable venv interpreter
         must never be persisted);
      3. a $PATH search, but only accepting a candidate that passes the ownership
         gate above.
    Raises RuntimeError rather than persist an unsafe command -- recoverable by
    setting TOKEN_OPTIMIZER_PYTHON.
    """
    override = os.environ.get("TOKEN_OPTIMIZER_PYTHON", "").strip()
    candidates = []
    if override:
        candidates.append(("TOKEN_OPTIMIZER_PYTHON", override))
    if sys.executable:
        candidates.append(("sys.executable", sys.executable))
    for name in ("python3", "python"):
        cand = shutil.which(name)
        if cand:
            candidates.append((name, cand))
    for _label, cand in candidates:
        if _py_path_is_trusted(cand):
            # Persist the RESOLVED realpath, not abspath: the gate validated
            # realpath(cand) (the symlink target + its parent dir), so persisting
            # the original symlink path would leave a swap window between install
            # and hook fire (an attacker with write access to the symlink's parent
            # dir could redirect the symlink to a malicious interpreter).
            return os.path.realpath(cand)
    reasons = [f"{label}={cand}: {_py_trust_reason(cand)}"
               for label, cand in candidates]
    raise RuntimeError(
        "no trusted python interpreter found for the Copilot hook; "
        "set TOKEN_OPTIMIZER_PYTHON to an absolute python3 path and re-run install. "
        "Candidates: " + "; ".join(reasons)
    )


def _hook_config(bridge_path: Path) -> dict:
    """The hooks file Copilot loads. Format per the official hooks reference:
    {"version": 1, "hooks": {event: [{"type": "command", "bash": ...}]}}.
    """
    py = _resolve_safe_python()
    # shlex.quote both paths: a HOME/COPILOT_HOME containing a space, $, or
    # backtick would otherwise break the bash string or inject a subcommand.
    # TOKEN_OPTIMIZER_RUNTIME is pinned so the bridge never process-scans on
    # the hot path and never falls through to the Claude default.
    py_q = shlex.quote(py)
    bridge_q = shlex.quote(str(bridge_path))

    def cmd(event: str) -> dict:
        return {
            "type": "command",
            "bash": f"TOKEN_OPTIMIZER_RUNTIME=copilot {py_q} {bridge_q} {event}",
            "timeoutSec": HOOK_TIMEOUT_SEC,
        }

    pre = cmd("pre-tool-use")
    # Only the bash tool is rewritten; the matcher keeps every other tool
    # call out of the bridge's hot path entirely.
    pre["matcher"] = {"toolName": "bash"}

    return {
        "version": 1,
        "hooks": {
            "sessionStart": [cmd("session-start")],
            "preToolUse": [pre],
            "postToolUse": [cmd("post-tool-use")],
            "stop": [cmd("stop")],
        },
    }


def install(*, dry_run: bool = False, home: Path = None) -> dict:
    """Install the adapter. Returns a summary dict of actions taken."""
    root = home if home is not None else copilot_home()
    actions = {"copied": [], "hook_file": None, "skipped": [], "dry_run": dry_run}

    plugin_dir = _plugin_dir(root)
    hooks_dir = _hooks_dir(root)

    if hooks_dir.exists() and not hooks_dir.is_dir():
        raise RuntimeError(
            f"{hooks_dir} exists but is not a directory — refusing to install. "
            "Move it aside and re-run."
        )

    for name in _PAYLOAD_MODULES:
        src = _SCRIPT_DIR / name
        if not src.exists():
            actions["skipped"].append(name)
            continue
        dest = plugin_dir / name
        if not dry_run:
            try:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            except OSError as exc:
                # A partial payload would silently break the bridge. Abort
                # BEFORE writing the hook config so a broken install never wires
                # hooks that point at missing modules.
                raise RuntimeError(f"failed copying {name}: {exc}") from exc
        actions["copied"].append(name)

    if actions["skipped"]:
        raise RuntimeError(
            "missing payload modules in this checkout: "
            f"{actions['skipped']} — refusing to wire hooks against an incomplete bridge."
        )

    hook_path = hooks_dir / HOOK_FILE_NAME
    config = _hook_config(plugin_dir / "copilot_hook_bridge.py")
    if not dry_run:
        try:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            hook_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"failed writing hook config: {exc}") from exc
    actions["hook_file"] = str(hook_path)

    # Seed capabilities for the installed CLI version (best-effort).
    if not dry_run:
        try:
            import copilot_hook_bridge

            copilot_hook_bridge.load_capabilities(refresh=True)
        except Exception:
            pass

    return actions


def uninstall(*, dry_run: bool = False, home: Path = None) -> dict:
    """Remove ONLY what install() created. Session data and trends stay."""
    root = home if home is not None else copilot_home()
    actions = {"removed": [], "dry_run": dry_run}

    hook_path = _hooks_dir(root) / HOOK_FILE_NAME
    if hook_path.exists():
        if not dry_run:
            hook_path.unlink()
        actions["removed"].append(str(hook_path))

    plugin_dir = _plugin_dir(root)
    if plugin_dir.exists():
        if not dry_run:
            shutil.rmtree(plugin_dir)
        actions["removed"].append(str(plugin_dir))

    return actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "install":
            result = install(dry_run=args.dry_run)
            verb = "Would install" if args.dry_run else "Installed"
            print(f"{verb} Token Optimizer for GitHub Copilot CLI.")
            print(f"  Hook config: {result['hook_file']}")
            print(f"  Modules: {len(result['copied'])} copied"
                  + (f", {len(result['skipped'])} missing: {result['skipped']}" if result["skipped"] else ""))
            print("  Run `python3 measure.py copilot-doctor` to verify readiness.")
        else:
            result = uninstall(dry_run=args.dry_run)
            verb = "Would remove" if args.dry_run else "Removed"
            for item in result["removed"] or ["(nothing installed)"]:
                print(f"{verb}: {item}")
        return 0
    except RuntimeError as exc:
        print(f"[Token Optimizer] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
