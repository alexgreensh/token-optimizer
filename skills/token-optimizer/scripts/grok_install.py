#!/usr/bin/env python3
"""Token Optimizer — Grok Build installer.

Wires Token Optimizer into Grok Build's hooks system:

1. Copies the adapter modules into ``<grok_home>/token-optimizer/plugin/``
   so the hook bridge runs from a stable path that survives repo moves.
2. Writes OUR OWN hooks file ``$GROK_HOME/hooks/token-optimizer.json``
   (Grok scans ``~/.grok/hooks/*.json`` as a DIRECTORY of files — unlike
   Cursor's single shared hooks.json — so TO owns its file outright and never
   merges with, or clobbers, another tool's hooks).
3. Refuses on native Windows (``os.name == "nt"``): the persisted ``command``
   string is POSIX-shell quoted and cmd.exe would not parse it.

The hooks JSON format is Grok's documented shape (10-hooks.md "The Hook JSON
Format"): ``{"hooks": {<Event>: [{matcher?, hooks: [{type, command, timeout}]}]}}``.

Idempotent: re-running refreshes the payload and rewrites OUR file only.
Uninstall removes only OUR hooks file and the payload dir; session data stays.

Usage:
    python3 grok_install.py install [--dry-run]
    python3 grok_install.py uninstall [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from py_trust import py_path_is_trusted, py_trust_reason  # noqa: E402
from runtime_env import grok_home  # noqa: E402

# Observe hooks default to 5s; PostToolUse/Stop default to 600s in grok. TO's
# handlers are fast, so pin short explicit timeouts (10-hooks.md "Key Fields").
SESSION_TIMEOUT_SEC = 5
PRETOOL_TIMEOUT_SEC = 5
POSTTOOL_TIMEOUT_SEC = 10
STOP_TIMEOUT_SEC = 10

# Modules the bridge needs at runtime, copied next to it so the installed
# hook never depends on the repo checkout location.
_PAYLOAD_MODULES = (
    "grok_hook_bridge.py",
    "grok_state.py",
    "grok_session.py",
    "bash_hook.py",
    "bash_compress.py",
    # Dependency-free whitelist gate shared by bash_hook + bash_compress.
    # Without it, `import bash_hook` fails in the installed path, the bridge
    # sets _bash_hook = None, and the entire PreToolUse bash compression
    # feature silently no-ops.
    "bash_whitelist.py",
    "hook_io.py",
    "plugin_env.py",
    "runtime_env.py",
    # codex_io supplies atomic_write_json — without it the bridge's tally and
    # observed-events writes silently no-op.
    "codex_io.py",
    # hermes_session supplies compute_quality_score — without it grok_session
    # silently falls back to a single-signal quality estimate.
    "hermes_session.py",
    # spawn_utils supplies spawn_detached — without it the bridge's degraded
    # fallback does NOT detach, so rollup/dashboard spawns die with the hook.
    "spawn_utils.py",
    # hook_runtime supplies lease_lock (tally RMW + stop throttle).
    "hook_runtime.py",
    "utf8_io.py",
)

# One-line locator written next to the bridge, naming the canonical measure.py
# in the checkout (measure.py is never copied into the plugin dir — version-
# drift risk). Mirrors hermes/cursor _MEASURE_LOCATOR_NAME.
_MEASURE_LOCATOR_NAME = "measure-path"


def _plugin_dir(root: Path) -> Path:
    return root / "token-optimizer" / "plugin"


def _hooks_file(root: Path) -> Path:
    return root / "hooks" / "token-optimizer.json"


def _resolve_safe_python() -> str:
    """An ABSOLUTE, trusted python for the persisted Grok hook command.

    Never emit a bare "python3": that string is resolved via $PATH every time
    the hook fires, so a hijacked PATH entry runs attacker code. Resolution:
      1. TOKEN_OPTIMIZER_PYTHON, if it names a trusted file;
      2. sys.executable (absolute, baked in ONCE) — through the same gate;
      3. a $PATH search, accepting only a candidate that passes the gate.
    Raises RuntimeError rather than persist an unsafe command.
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
        if py_path_is_trusted(cand):
            # Persist the RESOLVED realpath, not abspath: the gate validated
            # realpath(cand) (the symlink target + its parent dir), so persisting
            # the original symlink path would leave a swap window between install
            # and hook fire (an attacker with write access to the symlink's parent
            # dir could redirect the symlink to a malicious interpreter).
            return os.path.realpath(cand)
    reasons = [f"{label}={cand}: {py_trust_reason(cand)}"
               for label, cand in candidates]
    raise RuntimeError(
        "no trusted python interpreter found for the Grok hook; "
        "set TOKEN_OPTIMIZER_PYTHON to an absolute python3 path and re-run install. "
        "Candidates: " + "; ".join(reasons)
    )


def _hook_entries(bridge_path: Path) -> dict:
    """The hook entries TO writes to $GROK_HOME/hooks/token-optimizer.json.

    Format is Grok's documented 10-hooks.md shape. ``command`` is assumed to run
    through a POSIX shell, so the two paths are shlex-quoted and the runtime is
    pinned so the bridge never process-scans on the hot path.
    """
    py_q = shlex.quote(_resolve_safe_python())
    bridge_q = shlex.quote(str(bridge_path))

    def handler(event: str, timeout: int) -> dict:
        return {
            "type": "command",
            "command": f"TOKEN_OPTIMIZER_RUNTIME=grok {py_q} {bridge_q} {event}",
            "timeout": timeout,
        }

    def entry(event: str, timeout: int, matcher: str | None = None) -> dict:
        e = {"hooks": [handler(event, timeout)]}
        if matcher is not None:
            e["matcher"] = matcher
        return e

    return {
        "SessionStart": [entry("SessionStart", SESSION_TIMEOUT_SEC)],
        "UserPromptSubmit": [entry("UserPromptSubmit", SESSION_TIMEOUT_SEC)],
        # Only the bash tool is rewritten; matcher "Bash" maps to grok's own
        # ``run_terminal_command`` (10-hooks.md "Tool Name Aliases") and keeps
        # every other tool out of the rewrite hot path.
        "PreToolUse": [entry("PreToolUse", PRETOOL_TIMEOUT_SEC, matcher="Bash")],
        "PostToolUse": [entry("PostToolUse", POSTTOOL_TIMEOUT_SEC)],
        "Stop": [entry("Stop", STOP_TIMEOUT_SEC)],
    }


def _hooks_payload(bridge_path: Path) -> dict:
    return {"hooks": _hook_entries(bridge_path)}


def _read_hooks_file(path: Path) -> dict:
    """Read OUR hooks file. Missing -> {}. Symlink/unreadable/invalid -> raise.

    TO owns this file outright, but it lives in a user-writable dir: writing
    through a symlink (or silently overwriting an invalid root) would redirect
    the installer's output. Refuse rather than write through an attacker-chosen
    target.
    """
    if path.is_symlink():
        raise RuntimeError(
            f"{path} is a symlink; refusing to write through it. "
            "Remove the symlink and re-run."
        )
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} has a non-object root; refusing to overwrite it.")
    return data


def _write_hooks_file(path: Path, data: dict) -> None:
    """Atomically write OUR hooks file, never following a symlink at dest.

    codex_io.atomic_write_json (in this checkout) resolves a symlink before
    writing; that would follow an attacker-planted symlink in the user-writable
    hooks dir. tempfile + os.replace swaps the symlink itself instead, matching
    _copy_no_follow.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)  # swaps a symlink, never follows it
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def _hooks_lock(hooks_path: Path):
    """Exclusive lease around the hooks-file read-modify-write.

    Two concurrent installs (or install racing uninstall) would otherwise race.
    Uses hook_runtime.lease_lock (the same portable lock the bridges use).
    """
    from hook_runtime import lease_lock

    lock_path = hooks_path.parent / f".{hooks_path.name}.to-install.lock"
    # cohort_throttle=False: install and uninstall are distinct mutations
    # (write the payload vs remove it) and a trailing distinct-PID writer must
    # be able to immediately reclaim a released lease rather than be suppressed
    # for the full lease window — otherwise a user who installs then quickly
    # uninstalls is told "another install is in progress" for 120s.
    with lease_lock(lock_path, acquire_timeout=5.0, lease_seconds=120.0,
                    cohort_throttle=False) as acquired:
        if not acquired:
            raise RuntimeError(
                "another Grok install/uninstall is in progress "
                f"(could not acquire {lock_path}); re-run in a moment"
            )
        yield


def _copy_no_follow(src: Path, dest: Path) -> None:
    """Copy src to dest without ever writing through a symlink at dest.

    A pre-write is_symlink() check is TOCTOU-racy; tempfile + os.replace swaps
    whatever is at dest (a symlink included) instead of following it.
    """
    import tempfile

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install(*, dry_run: bool = False, home: Path | None = None) -> dict:
    """Install the adapter. Returns a summary dict of actions taken."""
    if os.name == "nt":
        raise RuntimeError(
            "Grok install on native Windows is not supported yet: the persisted "
            "hook command is POSIX-shell quoted and cmd.exe would not parse it. "
            "Install from a POSIX shell or WSL."
        )

    root = home if home is not None else grok_home()
    actions = {"copied": [], "hook_file": None, "skipped": [], "dry_run": dry_run}
    plugin_dir = _plugin_dir(root)

    for name in _PAYLOAD_MODULES:
        src = _SCRIPT_DIR / name
        if not src.exists():
            actions["skipped"].append(name)
            continue
        dest = plugin_dir / name
        if not dry_run:
            try:
                _copy_no_follow(src, dest)
            except OSError as exc:
                raise RuntimeError(f"failed copying {name}: {exc}") from exc
        actions["copied"].append(name)

    if actions["skipped"]:
        raise RuntimeError(
            "missing payload modules in this checkout: "
            f"{actions['skipped']} — refusing to wire hooks against an incomplete bridge."
        )

    # Write the measure.py locator so the bridge's detached rollup/dashboard
    # spawns resolve the canonical measure.py.
    measure_py = _SCRIPT_DIR / "measure.py"
    if measure_py.is_file():
        locator = plugin_dir / _MEASURE_LOCATOR_NAME
        if not dry_run:
            try:
                tmp = locator.with_name(f".{locator.name}.tmp")
                tmp.write_text(f"{measure_py}\n", encoding="utf-8")
                os.replace(tmp, locator)  # swaps a symlink, never follows it
            except OSError as exc:
                raise RuntimeError(f"failed writing measure-path locator: {exc}") from exc
        actions["measure_locator"] = str(measure_py)

    bridge_path = plugin_dir / "grok_hook_bridge.py"
    hooks_path = _hooks_file(root)
    with _hooks_lock(hooks_path):
        # TO owns this file; replace its contents outright with the current
        # payload definition. No foreign entries exist to preserve (unlike
        # Cursor's shared hooks.json).
        _read_hooks_file(hooks_path)  # only to trip the symlink/invalid guard
        payload = _hooks_payload(bridge_path)
        if not dry_run:
            try:
                _write_hooks_file(hooks_path, payload)
            except OSError as exc:
                raise RuntimeError(f"failed writing {hooks_path}: {exc}") from exc
    actions["hook_file"] = str(hooks_path)
    return actions


def uninstall(*, dry_run: bool = False, home: Path | None = None) -> dict:
    """Remove OUR hooks file and the payload dir. Session data stays."""
    root = home if home is not None else grok_home()
    actions = {"removed": [], "dry_run": dry_run}

    hooks_path = _hooks_file(root)
    with _hooks_lock(hooks_path):
        if hooks_path.exists() or hooks_path.is_symlink():
            if not dry_run:
                try:
                    # Unlink a symlink (never its target) or a regular file.
                    hooks_path.unlink()
                except OSError as exc:
                    raise RuntimeError(f"failed removing {hooks_path}: {exc}") from exc
            actions["removed"].append(str(hooks_path))

    plugin_dir = _plugin_dir(root)
    if plugin_dir.exists():
        if not dry_run:
            try:
                shutil.rmtree(plugin_dir)
            except OSError as exc:
                raise RuntimeError(f"failed removing {plugin_dir}: {exc}") from exc
        actions["removed"].append(str(plugin_dir))

    return actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--dry-run", action="store_true")
    # --home overrides the resolved Grok home (install.sh forwards the
    # WSL-aware resolved path so the install and its banner agree, mirroring
    # copilot_install --home / issue #78).
    parser.add_argument("--home", type=Path, default=None)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "install":
            result = install(dry_run=args.dry_run, home=args.home)
            verb = "Would install" if args.dry_run else "Installed"
            print(f"{verb} Token Optimizer for Grok Build.")
            print(f"  Hooks written to: {result['hook_file']}")
            print(f"  Modules: {len(result['copied'])} copied"
                  + (f", {len(result['skipped'])} missing: {result['skipped']}" if result["skipped"] else ""))
            print("  Run `python3 measure.py grok-doctor` to verify readiness.")
        else:
            result = uninstall(dry_run=args.dry_run, home=args.home)
            verb = "Would remove" if args.dry_run else "Removed"
            for item in result["removed"] or ["(nothing installed)"]:
                print(f"{verb}: {item}")
        return 0
    except RuntimeError as exc:
        print(f"[Token Optimizer] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
