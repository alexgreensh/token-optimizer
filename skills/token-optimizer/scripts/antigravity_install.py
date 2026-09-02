#!/usr/bin/env python3
"""Token Optimizer — Google Antigravity adapter installer.

Wires Token Optimizer into an Antigravity (CLI ``agy``, Antigravity 2.0 app,
or IDE) setup as a USER-LEVEL PLUGIN directory:

1. Copies the adapter payload modules into
   ``<home>/config/plugins/token-optimizer/`` so the hook bridge runs from a
   stable path that survives repo moves.
2. Writes ``hooks.json`` and ``plugin.json`` inside that plugin directory —
   never touching the user-owned ``<home>/config/hooks.json``, ``config.json``,
   or any ``settings.json`` (issue #147 / R2).
3. Records consent in ``<home>/token-optimizer/config.json`` (R20).

Idempotent: re-running refreshes the payload and rewrites OUR files only.
Uninstall removes only the plugin directory; conversation data and trends stay.

Usage:
    python3 antigravity_install.py install [--dry-run]
    python3 antigravity_install.py uninstall [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from py_trust import py_path_is_trusted, py_trust_reason  # noqa: E402
from runtime_env import antigravity_home  # noqa: E402

HOOK_NAME = "token-optimizer"
PRE_TIMEOUT_SEC = 10
STOP_TIMEOUT_SEC = 15

# Consent flag recorded in <home>/token-optimizer/config.json. The bridge and
# the rollup are no-ops until it is true (R20).
CONSENT_KEY = "antigravity_consent"

# Modules the bridge needs at runtime, copied next to it so the installed hook
# never depends on the repo checkout location. antigravity_session is NOT here:
# it is only imported by measure.py's collector, which runs from the checkout
# via the measure-path locator, never from the plugin directory.
_PAYLOAD_MODULES = (
    "antigravity_hook_bridge.py",
    "antigravity_proto.py",
    "antigravity_state.py",
    "runtime_env.py",
    "plugin_env.py",
    "bash_hook.py",
    "bash_compress.py",
    # Dependency-free whitelist gate shared by bash_hook + bash_compress.
    "bash_whitelist.py",
    "command_filters.py",
    "spawn_utils.py",
    "utf8_io.py",
)


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
    """An ABSOLUTE, trusted python for the persisted Antigravity hook command.

    Never emit a bare "python3": that string is resolved via $PATH every time
    the hook fires, so a hijacked PATH entry runs attacker code. Resolution
    order:
      1. TOKEN_OPTIMIZER_PYTHON, if it names a trusted file;
      2. sys.executable (absolute path baked in ONCE) -- but only through the
         same trust gate: a writable venv interpreter must never be persisted;
      3. a $PATH search, accepting only a candidate that passes the gate.
    The RESOLVED realpath is persisted, not abspath: the gate validated
    realpath(cand) (the symlink target + its parent dir), so persisting the
    original symlink path would leave a swap window between install and hook
    fire. Raises RuntimeError rather than persist an unsafe command.
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
            return os.path.realpath(cand)
    reasons = [f"{label}={cand}: {_py_trust_reason(cand)}"
               for label, cand in candidates]
    raise RuntimeError(
        "no trusted python interpreter found for the Antigravity hook; "
        "set TOKEN_OPTIMIZER_PYTHON to an absolute python3 path and re-run install. "
        "Candidates: " + "; ".join(reasons)
    )


def plugin_dir(home: Path) -> Path:
    return home / "config" / "plugins" / HOOK_NAME


def data_dir(home: Path) -> Path:
    return home / "token-optimizer"


def _hooks_config(bridge_path: Path) -> dict:
    """hooks.json per the Antigravity hooks contract (builtin agy-customizations
    docs): a JSON object keyed by hook NAME; PreToolUse uses a matcher group,
    PreInvocation/Stop are flat handler lists."""
    py = _resolve_safe_python()
    py_q = shlex.quote(py)
    bridge_q = shlex.quote(str(bridge_path))

    def cmd(event: str, timeout: int) -> dict:
        return {
            "type": "command",
            # -E -s: ignore PYTHONPATH + user site so an inherited env cannot
            # hijack payload imports (R16). TOKEN_OPTIMIZER_RUNTIME is pinned so
            # the bridge never process-scans on the hot path.
            "command": f"TOKEN_OPTIMIZER_RUNTIME=antigravity {py_q} -E -s {bridge_q} {event}",
            "timeout": timeout,
        }

    pre_tool = cmd("pre-tool-use", PRE_TIMEOUT_SEC)
    return {
        HOOK_NAME: {
            "PreToolUse": [
                # Only the run_command tool is rewritten; the matcher keeps every
                # other tool call out of the bridge's hot path entirely.
                {"matcher": "run_command", "hooks": [pre_tool]},
            ],
            "PreInvocation": [cmd("pre-invocation", PRE_TIMEOUT_SEC)],
            "Stop": [cmd("stop", STOP_TIMEOUT_SEC)],
        }
    }


def _ensure_config_dir_permissions(path: Path) -> None:
    """Create a directory 0o700 (private data dir). Never follows symlinks."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise RuntimeError(f"failed to create {path}: {exc}") from exc


def _write_consent(home: Path) -> None:
    """Record the consent flag in <home>/token-optimizer/config.json (R20)."""
    dd = data_dir(home)
    _ensure_config_dir_permissions(dd)
    config_path = dd / "config.json"
    cfg: dict = {}
    try:
        if config_path.is_file():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
    except (OSError, json.JSONDecodeError, ValueError):
        cfg = {}
    cfg[CONSENT_KEY] = True
    try:
        config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"failed to write consent record {config_path}: {exc}"
        ) from exc


def install(*, dry_run: bool = False, home: Path | None = None) -> dict:
    """Install the adapter. Returns a summary dict of actions taken."""
    # Windows refusal (deferred quoting: cmd.exe would not parse shlex.quote,
    # and the env-prefix form of the persisted hook command is POSIX-only).
    # Refuse loudly rather than register hooks that can never fire.
    if os.name == "nt":
        raise RuntimeError(
            "Antigravity install on native Windows is not supported yet: the "
            "persisted hook command is POSIX-shell quoted and cmd.exe would "
            "not parse it. Install from a POSIX shell or WSL."
        )

    root = home if home is not None else antigravity_home()
    actions = {
        "copied": [],
        "plugin_dir": None,
        "consent": None,
        "skipped": [],
        "dry_run": dry_run,
    }

    pdir = plugin_dir(root)
    if pdir.exists() and (pdir.is_symlink() or not pdir.is_dir()):
        raise RuntimeError(
            f"{pdir} exists but is not a directory — refusing to install. "
            "Move it aside and re-run."
        )
    if pdir.exists():
        try:
            resolved = pdir.resolve(strict=False)
            if not resolved.is_relative_to(root.resolve(strict=False)):
                raise RuntimeError(
                    f"{pdir} resolves outside the Antigravity home — refusing to install."
                )
        except (OSError, ValueError):
            raise RuntimeError(f"{pdir} is unsafe — refusing to install.")

    # Resolve/validate the payload before writing anything.
    for name in _PAYLOAD_MODULES:
        src = _SCRIPT_DIR / name
        if not src.is_file():
            actions["skipped"].append(name)
    if actions["skipped"]:
        raise RuntimeError(
            "missing payload modules in this checkout: "
            f"{actions['skipped']} — refusing to wire hooks against an incomplete bridge."
        )

    if not dry_run:
        try:
            _ensure_config_dir_permissions(pdir)
            for name in _PAYLOAD_MODULES:
                src = _SCRIPT_DIR / name
                shutil.copy2(src, pdir / name)
                actions["copied"].append(name)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"failed copying payload: {exc}") from exc

        # measure-path locator: name the checkout's measure.py (KTD5).
        try:
            (pdir / "measure-path").write_text(
                str(_SCRIPT_DIR / "measure.py") + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise RuntimeError(f"failed writing measure-path locator: {exc}") from exc

        # Write hooks.json, then plugin.json LAST so a partial payload never
        # registers hooks (R1/U5).
        try:
            (pdir / "hooks.json").write_text(
                json.dumps(_hooks_config(pdir / "antigravity_hook_bridge.py"), indent=2)
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise RuntimeError(f"failed writing hooks.json: {exc}") from exc
        try:
            (pdir / "plugin.json").write_text(
                json.dumps({"name": HOOK_NAME}, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise RuntimeError(f"failed writing plugin.json: {exc}") from exc

        _write_consent(root)
        actions["consent"] = str(data_dir(root) / "config.json")

    actions["plugin_dir"] = str(pdir)
    return actions


def uninstall(*, dry_run: bool = False, home: Path | None = None) -> dict:
    """Remove ONLY what install() created (the plugin directory). Session data
    and trends stay in place (R3)."""
    root = home if home is not None else antigravity_home()
    pdir = plugin_dir(root)
    actions = {"removed": [], "dry_run": dry_run}
    if pdir.exists() or pdir.is_symlink():
        if not dry_run:
            # rmtree on the symlink itself raises; rmdir is used for symlinks.
            if pdir.is_symlink() or pdir.is_file():
                pdir.unlink()
            else:
                shutil.rmtree(pdir)
        actions["removed"].append(str(pdir))
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
            print(f"{verb} Token Optimizer for Google Antigravity.")
            print(f"  Plugin directory: {result['plugin_dir']}")
            if not args.dry_run:
                print(f"  Modules: {len(result['copied'])} copied")
                print(f"  Consent: {result['consent']}")
            print("  Run `python3 measure.py antigravity-doctor` to verify readiness.")
        else:
            result = uninstall(dry_run=args.dry_run)
            verb = "Would remove" if args.dry_run else "Removed"
            for item in result["removed"] or ["(nothing installed)"]:
                print(f"{verb}: {item}")
            if not args.dry_run and result["removed"]:
                print(
                    "  Note: <home>/config/config.json may keep a stale `plugins` "
                    "entry, which Antigravity ignores once the plugin dir is gone."
                )
        return 0
    except RuntimeError as exc:
        print(f"[Token Optimizer] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
