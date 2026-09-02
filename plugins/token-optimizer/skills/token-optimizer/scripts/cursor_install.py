#!/usr/bin/env python3
"""Token Optimizer — Cursor installer.

Wires Token Optimizer into Cursor's shared user hooks file:

1. Copies the adapter modules into ``<cursor_home>/token-optimizer/plugin/``
   so the hook bridge runs from a stable path that survives repo moves.
2. READ-MERGE-WRITES ``~/.cursor/hooks.json``: existing entries owned by other
   tools are preserved verbatim; only entries whose ``command`` points at our
   bridge path are replaced. Cursor has one shared hooks.json (no per-plugin
   file like Copilot), so clobbering it would destroy other people's hooks.
3. Refuses on native Windows (``os.name == "nt"``): the persisted ``command``
   string is POSIX-shell quoted and cmd.exe would not parse it. Windows work is
   deferred rather than silently writing a no-op hook.

Idempotent: re-running refreshes the payload and replaces only OUR entries.
Uninstall removes only OUR entries and the payload dir; session data stays.

Usage:
    python3 cursor_install.py install [--dry-run]
    python3 cursor_install.py uninstall [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat as _stat
import sys
from contextlib import contextmanager
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from py_trust import py_path_is_trusted, py_trust_reason  # noqa: E402
from runtime_env import cursor_home  # noqa: E402

HOOK_TIMEOUT_SEC = 10

# Modules the bridge needs at runtime, copied next to it so the installed
# hook never depends on the repo checkout location.
_PAYLOAD_MODULES = (
    "cursor_hook_bridge.py",
    "cursor_state.py",
    "cursor_session.py",
    "bash_hook.py",
    "bash_compress.py",
    # Dependency-free whitelist gate shared by bash_hook + bash_compress.
    "bash_whitelist.py",
    "hook_io.py",
    "plugin_env.py",
    "runtime_env.py",
    # codex_io supplies atomic_write_json — without it the bridge's tally and
    # observed-events writes silently no-op (crash recovery would be dead).
    "codex_io.py",
    # hermes_session supplies compute_quality_score — without it cursor_session
    # silently falls back to a single-signal quality estimate.
    "hermes_session.py",
    # spawn_utils supplies spawn_detached — without it the bridge's degraded
    # fallback does NOT detach, so rollup/dashboard spawns die with the hook.
    "spawn_utils.py",
    # hook_runtime supplies lease_lock (tally RMW + stop throttle) and
    # utf8_io is enforced at startup; both degrade silently when absent.
    "hook_runtime.py",
    "utf8_io.py",
)

# One-line locator written next to the bridge, naming the canonical measure.py
# in the checkout. The bridge's detached rollup/dashboard spawns resolve
# measure.py through this locator because measure.py itself is NOT copied into
# the plugin dir (version-drift risk — nothing refreshes a plugin-dir copy on
# update; the checkout's measure.py stays the single source). Mirrors
# hermes_install._MEASURE_LOCATOR_NAME.
_MEASURE_LOCATOR_NAME = "measure-path"


def _plugin_dir(root: Path) -> Path:
    return root / "token-optimizer" / "plugin"


def _host_hooks_path(root: Path) -> Path:
    return root / "hooks.json"


def _py_trust_reason(p: str) -> str | None:
    """None when trusted, else a short human-readable rejection reason."""
    return py_trust_reason(p)


def _py_path_is_trusted(p: str) -> bool:
    """Trusted iff the interpreter's bytes are admin-owned (euid or root) and
    not group/other-writable, and its dir is not world-writable and not
    group-writable by a third party. Pure stat, never runs the target. On
    Windows, stat ownership is unreliable under Git-Bash, so require only that
    the path is a real file."""
    return py_path_is_trusted(p)


def _resolve_safe_python() -> str:
    """An ABSOLUTE, trusted python for the persisted Cursor hook command.

    Never emit a bare "python3": that string is resolved via $PATH every time the
    hook fires, so a hijacked PATH entry runs attacker code. Resolution order:
      1. TOKEN_OPTIMIZER_PYTHON, if it names a trusted file;
      2. sys.executable (absolute path baked in ONCE) -- but only through the
         same trust gate: a writable venv interpreter must never be persisted;
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
        "no trusted python interpreter found for the Cursor hook; "
        "set TOKEN_OPTIMIZER_PYTHON to an absolute python3 path and re-run install. "
        "Candidates: " + "; ".join(reasons)
    )


def _hook_entries(bridge_path: Path) -> dict:
    """The hook entries TO appends to ~/.cursor/hooks.json.

    Cursor's per-hook schema is ``{command, type, matcher?, timeout,
    failClosed?, loop_limit?}`` (verified 2026-09-01). ``command`` is assumed to
    run through a POSIX shell, so the two paths are shlex-quoted and the runtime
    is pinned so the bridge never process-scans on the hot path.
    """
    py_q = shlex.quote(_resolve_safe_python())
    bridge_q = shlex.quote(str(bridge_path))

    def entry(event: str, *, matcher: str | None = None) -> dict:
        e = {
            "command": f"TOKEN_OPTIMIZER_RUNTIME=cursor {py_q} {bridge_q} {event}",
            "type": "command",
            "timeout": HOOK_TIMEOUT_SEC,
        }
        if matcher is not None:
            e["matcher"] = matcher
        return e

    return {
        "sessionStart": [entry("sessionStart")],
        # Only the Shell tool is rewritten; matcher keeps every other tool out
        # of the bridge's hot path (Cursor matchers are JS regex over tool name).
        "preToolUse": [entry("preToolUse", matcher="Shell")],
        "postToolUse": [entry("postToolUse")],
        "preCompact": [entry("preCompact")],
        "stop": [entry("stop")],
        "sessionEnd": [entry("sessionEnd")],
    }


def _is_ours(entry, bridge_path: Path) -> bool:
    if not isinstance(entry, dict):
        return False
    cmd = entry.get("command")
    return isinstance(cmd, str) and str(bridge_path) in cmd


def _read_hooks(path: Path) -> dict:
    """Read hooks.json. Missing -> {} (fresh install). A symlink, an unreadable,
    invalid, or non-object root -> RuntimeError: Cursor has ONE shared
    hooks.json, so silently treating a corrupt file as empty would clobber
    other tools' (and Cursor's own) entries on the next write, and writing
    through a symlink would redirect the installer's output to an
    attacker-chosen target."""
    if path.is_symlink():
        raise RuntimeError(
            f"{path} is a symlink; refusing to read-merge-write through it. "
            "Remove the symlink (or point it at the real hooks.json) and re-run."
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
        raise RuntimeError(
            f"{path} has a non-object root; refusing to overwrite it."
        )
    return data


def _write_hooks(path: Path, data: dict) -> None:
    try:
        from codex_io import atomic_write_json

        # replace_symlink: even if a symlink appears between _read_hooks and
        # here, os.replace swaps the symlink itself, never its target.
        atomic_write_json(path, data, replace_symlink=True)
    except ImportError:  # pragma: no cover - broken checkout
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@contextmanager
def _hooks_lock(hooks_path: Path):
    """Exclusive lease around the hooks.json read-merge-write.

    Two concurrent installs (or install racing uninstall) would otherwise both
    read stale content and the second write would silently drop the first's
    entries. Uses hook_runtime.lease_lock (the same portable lock the bridges
    use); a failed acquire aborts loudly instead of racing.
    """
    from hook_runtime import lease_lock

    lock_path = hooks_path.parent / f".{hooks_path.name}.to-install.lock"
    # 120s lease: the critical section copies the payload and rewrites
    # hooks.json; a 30s lease could lapse mid-RMW on a slow disk and let a
    # concurrent installer reclaim it, reintroducing the clobber.
    with lease_lock(lock_path, acquire_timeout=5.0, lease_seconds=120.0) as acquired:
        if not acquired:
            raise RuntimeError(
                "another Cursor install/uninstall is in progress "
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


def install(*, dry_run: bool = False, home: Path = None) -> dict:
    """Install the adapter. Returns a summary dict of actions taken."""
    # Windows refusal (deferred quoting: cmd.exe would not parse shlex.quote).
    if os.name == "nt":
        raise RuntimeError(
            "Cursor install on native Windows is not supported yet: the persisted "
            "hook command is POSIX-shell quoted and cmd.exe would not parse it. "
            "Install from a POSIX shell or WSL."
        )

    root = home if home is not None else cursor_home()
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
                # A partial payload would silently break the bridge; abort BEFORE
                # touching hooks.json.
                raise RuntimeError(f"failed copying {name}: {exc}") from exc
        actions["copied"].append(name)

    if actions["skipped"]:
        raise RuntimeError(
            "missing payload modules in this checkout: "
            f"{actions['skipped']} — refusing to wire hooks against an incomplete bridge."
        )

    # Write the measure.py locator so the bridge's detached rollup/dashboard
    # spawns resolve the canonical measure.py (measure.py is never copied into
    # the plugin dir — version-drift risk). Only written when measure.py exists
    # next to the installer; the bridge degrades to "rollups paused" otherwise.
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

    bridge_path = plugin_dir / "cursor_hook_bridge.py"
    hooks_path = _host_hooks_path(root)
    with _hooks_lock(hooks_path):
        existing = _read_hooks(hooks_path)
        hooks = existing.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        for event, new_entries in _hook_entries(bridge_path).items():
            old = hooks.get(event, [])
            if not isinstance(old, list):
                old = []
            hooks[event] = [e for e in old if not _is_ours(e, bridge_path)] + new_entries
        existing["hooks"] = hooks
        existing.setdefault("version", 1)

        if not dry_run:
            try:
                _write_hooks(hooks_path, existing)
            except OSError as exc:
                raise RuntimeError(f"failed writing {hooks_path}: {exc}") from exc
    actions["hook_file"] = str(hooks_path)
    return actions


def uninstall(*, dry_run: bool = False, home: Path = None) -> dict:
    """Remove only entries install() added, plus the payload dir.

    Session data (``<cursor_home>/token-optimizer/sessions``,
    ``restore-context``, ``observed-events.jsonl``) and trends are left in place
    for the user to purge manually, matching the Copilot uninstall contract.
    """
    root = home if home is not None else cursor_home()
    actions = {"removed": [], "dry_run": dry_run}

    bridge_path = _plugin_dir(root) / "cursor_hook_bridge.py"
    hooks_path = _host_hooks_path(root)
    with _hooks_lock(hooks_path):
        existing = _read_hooks(hooks_path)
        hooks = existing.get("hooks")
        if isinstance(hooks, dict):
            changed = False
            pruned = {}
            for event, entries in hooks.items():
                if not isinstance(entries, list):
                    pruned[event] = entries
                    continue
                kept = [e for e in entries if not _is_ours(e, bridge_path)]
                if len(kept) != len(entries):
                    changed = True
                pruned[event] = kept
            if changed:
                existing["hooks"] = pruned
                if not dry_run:
                    try:
                        _write_hooks(hooks_path, existing)
                    except OSError as exc:
                        raise RuntimeError(f"failed writing {hooks_path}: {exc}") from exc
                actions["removed"].append(f"{hooks_path} (Cursor entries)")

    plugin_dir = _plugin_dir(root)
    if plugin_dir.exists():
        if not dry_run:
            try:
                shutil.rmtree(plugin_dir)
            except OSError as exc:
                # main() only surfaces RuntimeError; a raw OSError would escape
                # as a traceback and leave a half-uninstalled state unexplained.
                raise RuntimeError(f"failed removing {plugin_dir}: {exc}") from exc
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
            print(f"{verb} Token Optimizer for Cursor.")
            print(f"  Hooks merged into: {result['hook_file']}")
            print(f"  Modules: {len(result['copied'])} copied"
                  + (f", {len(result['skipped'])} missing: {result['skipped']}" if result["skipped"] else ""))
            print("  Run `python3 measure.py cursor-doctor` to verify readiness.")
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
