#!/usr/bin/env python3
"""Install Token Optimizer into Hermes as a plugin.

Copies the ``hermes/`` payload directory from the Token Optimizer repo into
``~/.hermes/plugins/token-optimizer/`` (or ``$HERMES_HOME/plugins/token-optimizer/``)
AND the runtime modules the plugin imports at load time (the hermes_*.py shims
that live next to this installer in scripts/).

v5.X.Y (#58): the payload alone (plugin.yaml + __init__.py + README) is not a
working plugin — __init__.py imports hermes_hook_bridge / hermes_state /
hermes_session from its own directory. Earlier installs copied only the payload,
leaving those imports broken (dead /token-optimizer command, dashboard launcher,
and rollups). We now also copy the three runtime modules and write a one-line
``measure-path`` locator pointing at the canonical measure.py in the checkout
(rather than copying measure.py + its dependency tree, which would silently go
stale because nothing refreshes plugin-dir copies on update).

Hermes (v0.15.x) does NOT auto-discover plugins by directory presence: the
plugin must be allow-listed in the Hermes config under ``plugins.enabled``.
``--enable`` patches that config idempotently; without it we print the exact
snippet for the user to add.

The operation is idempotent: re-running replaces files in place without
corrupting any existing Hermes state or other plugins.

Usage (from measure.py dispatch or directly):
    python3 hermes_install.py [--dry-run] [--uninstall] [--enable] [--json]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

from runtime_env import hermes_home

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PLUGIN_NAME = "token-optimizer"
_HERMES_PLUGIN_DIR_NAME = "plugins"

# Runtime modules the Hermes plugin imports from its own directory at load time
# (see hermes/__init__.py "sys.path assumption"). Sourced from the scripts/ dir
# that contains THIS installer, so install never depends on cwd or the checkout
# layout beyond "the installer lives next to the modules it ships".
_RUNTIME_MODULES = (
    "hermes_hook_bridge.py",
    "hermes_state.py",
    "hermes_session.py",
    "runtime_env.py",
)

# Plain-text locator file written into the plugin dir: one line, the absolute
# path to the canonical measure.py. hermes_hook_bridge._locate_measure_py reads
# it. We do NOT copy measure.py itself (version-drift risk — nothing refreshes a
# plugin-dir copy on update; the checkout's measure.py stays the single source).
_MEASURE_LOCATOR_NAME = "measure-path"

# Hermes config: the plugin must be allow-listed under plugins.enabled. No other
# part of the codebase references a canonical config path, so we default to
# config.yaml and fall back to config.yml when only that exists.
_CONFIG_PRIMARY = "config.yaml"
_CONFIG_FALLBACK = "config.yml"

_ENABLE_SNIPPET = "plugins:\n  enabled:\n    - token-optimizer\n"


def _scripts_dir() -> Path:
    """Return the scripts/ dir that contains this installer and the runtime modules."""
    return Path(__file__).resolve().parent


def _repo_root() -> Path:
    """Return the token-optimizer repo root (3 parents up from this script)."""
    return Path(__file__).resolve().parents[3]


def _payload_dir() -> Path:
    """Return the ``hermes/`` payload directory bundled in the repo."""
    return _repo_root() / "hermes"


def _plugin_install_dir(hermes_root: Path) -> Path:
    """Return the path where Hermes should find our plugin."""
    return hermes_root / _HERMES_PLUGIN_DIR_NAME / _PLUGIN_NAME


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

def _assert_no_symlink_escape(path: Path, expected_parent: Path) -> None:
    """Raise ValueError if *path* would escape *expected_parent* after resolution."""
    try:
        resolved = path.resolve(strict=False)
        expected_resolved = expected_parent.resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Path resolution failed for {path}: {exc}") from exc
    if not resolved.is_relative_to(expected_resolved):
        raise ValueError(f"{path} escapes {expected_parent}")


# ---------------------------------------------------------------------------
# Hermes config activation (--enable)
# ---------------------------------------------------------------------------

def _config_path(hermes_root: Path) -> Path:
    """Return the Hermes config path to patch.

    Prefer config.yaml. If it is absent but config.yml exists, target that
    instead (so we don't create a second competing file).
    """
    primary = hermes_root / _CONFIG_PRIMARY
    fallback = hermes_root / _CONFIG_FALLBACK
    if not primary.exists() and fallback.exists():
        return fallback
    return primary


def _timestamp() -> str:
    # v5.11.1 (#58): include microseconds so two backups within the same second
    # (e.g. a retried install) don't collide on the same filename.
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _enabled_in_text(text: str) -> bool:
    """Conservative text scan: is token-optimizer already under plugins.enabled?

    Handles both the block-list form::

        plugins:
          enabled:
            - token-optimizer

    and the inline form ``enabled: [token-optimizer, ...]``. Does not attempt a
    full YAML parse (PyYAML may be absent and would lose comments on dump).
    """
    # v5.11.1 (#58): bail on pathologically large config text rather than scan
    # line-by-line (a malformed/huge file is the user's to fix, not ours to chew).
    if len(text) > 1_000_000:
        return False
    lines = text.splitlines()
    in_plugins = False
    in_enabled = False
    plugins_indent = -1
    enabled_indent = -1
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        # Leaving the plugins block once we dedent back to/under its key indent.
        if in_plugins and indent <= plugins_indent and not raw.lstrip().startswith("plugins:"):
            in_plugins = False
            in_enabled = False
        # v5.11.1 (#58): only a TOP-LEVEL plugins: key (indent == 0) is the
        # Hermes plugin allow-list. A nested "plugins:" (e.g. outer.plugins) is
        # an unrelated mapping and must not be matched.
        if stripped.startswith("plugins:") and indent == 0:
            in_plugins = True
            plugins_indent = indent
            # Inline form on the plugins line is not expected; keep scanning.
            continue
        if in_plugins and not in_enabled and stripped.startswith("enabled:"):
            in_enabled = True
            enabled_indent = indent
            # Inline list form: enabled: [a, token-optimizer]
            rest = stripped[len("enabled:"):].strip()
            if rest.startswith("[") and _PLUGIN_NAME in [
                item.strip().strip("'\"") for item in rest.strip("[]").split(",")
            ]:
                return True
            continue
        if in_enabled:
            # Block-list items are more-indented "- name" entries.
            if indent <= enabled_indent and not stripped.startswith("-"):
                in_enabled = False
            elif stripped.startswith("-") and stripped[1:].strip().strip("'\"") == _PLUGIN_NAME:
                return True
    return False


def _patch_config_text(text: str) -> tuple[str | None, str]:
    """Insert token-optimizer into an existing plugins.enabled list via text surgery.

    Returns (new_text, status). status is one of:
      - "already-enabled": no change needed.
      - "enabled": new_text contains the patched config.
      - "manual-required": structure is ambiguous; caller must not write.

    Preserves comments and surrounding formatting (no yaml load/dump rewrite).
    """
    # v5.11.1 (#58): refuse pathologically large configs (caller treats this as
    # manual-required and won't write).
    if len(text) > 1_000_000:
        return None, "manual-required"
    if _enabled_in_text(text):
        return None, "already-enabled"

    # v5.11.1 (#58): detect the file's line ending once and reuse it for every
    # line we synthesize, so a CRLF config stays uniformly CRLF after patching.
    eol = "\r\n" if "\r\n" in text else "\n"

    lines = text.splitlines(keepends=True)
    in_plugins = False
    saw_plugins = False
    plugins_indent = -1
    enabled_lead = ""  # literal leading whitespace of the enabled: line
    for i, raw in enumerate(lines):
        body = raw.rstrip("\r\n")
        stripped = body.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = body[: len(body) - len(body.lstrip())]
        indent = len(lead)
        if in_plugins and indent <= plugins_indent and not body.lstrip().startswith("plugins:"):
            in_plugins = False
        # v5.11.1 (#58): only a TOP-LEVEL plugins: key (indent == 0) is the
        # Hermes plugin allow-list; nested plugins: mappings are unrelated.
        if stripped.startswith("plugins:") and indent == 0:
            in_plugins = True
            saw_plugins = True
            plugins_indent = indent
            continue
        if in_plugins and stripped.startswith("enabled:"):
            enabled_lead = lead
            rest = stripped[len("enabled:"):].strip()
            # v5.11.1 (#58): strip a trailing comment so `enabled:  # note` is
            # treated as an empty block-list, not a scalar.
            rest = rest.split("#", 1)[0].strip()
            if rest.startswith("[") and rest.endswith("]"):
                # Inline list form — rewrite this one line, preserving prefix.
                inner = rest[1:-1].strip()
                items = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                items.append(_PLUGIN_NAME)
                prefix = lead
                newline = eol if (raw.endswith("\n") or raw.endswith("\r")) else ""
                lines[i] = f"{prefix}enabled: [{', '.join(items)}]{newline}"
                return "".join(lines), "enabled"
            if rest and not rest.startswith("["):
                # enabled: <scalar> — not a list we can safely extend.
                return None, "manual-required"
            # Block-list form: insert a "- token-optimizer" item.
            # v5.11.1 (#58): derive the inserted line's leading whitespace from
            # an EXISTING item line's literal prefix (the actual chars before
            # "-"), preserving tabs. If there is no existing item line and the
            # surrounding indentation uses tabs, bail manual-required rather than
            # guess a space count that yaml would reject.
            item_lead = None
            insert_at = i + 1
            for j in range(i + 1, len(lines)):
                jbody = lines[j].rstrip("\r\n")
                jstripped = jbody.strip()
                if not jstripped or jstripped.startswith("#"):
                    insert_at = j + 1
                    continue
                jlead = jbody[: len(jbody) - len(jbody.lstrip())]
                jindent = len(jlead)
                if jstripped.startswith("-") and jindent > indent:
                    item_lead = jlead
                    insert_at = j + 1
                    continue
                break
            if item_lead is None:
                # No existing item to copy indentation from. If the enabled:/
                # plugins: indentation contains tabs, we cannot safely pick an
                # indent — bail conservatively.
                if "\t" in enabled_lead or "\t" in lead:
                    return None, "manual-required"
                item_lead = " " * (indent + 2)
            new_line = f"{item_lead}- {_PLUGIN_NAME}{eol}"
            lines.insert(insert_at, new_line)
            return "".join(lines), "enabled"

    if not saw_plugins:
        # No plugins: block anywhere — appending a fresh top-level block at the
        # end of the file is unambiguous (cannot interact with other keys).
        suffix = "" if (not text or text.endswith("\n") or text.endswith("\r")) else eol
        snippet = _ENABLE_SNIPPET.replace("\n", eol) if eol != "\n" else _ENABLE_SNIPPET
        return text + suffix + snippet, "enabled"

    # plugins: exists but no enabled: list we can safely extend — ambiguous.
    return None, "manual-required"


def enable_in_config(hermes_root: Path, *, dry_run: bool = False) -> dict:
    """Idempotently allow-list token-optimizer in the Hermes config.

    Returns a dict with keys: status, config_path, and optionally backup/note.
    status ∈ {"enabled", "already-enabled", "manual-required"}.

    A timestamped backup is written ONLY when we actually mutate the file.
    """
    cfg = _config_path(hermes_root)
    _assert_no_symlink_escape(cfg, hermes_root)
    result: dict = {"config_path": str(cfg)}

    if not cfg.exists():
        if dry_run:
            # v5.11.1 (#58): dry-run must not claim "enabled" when nothing was
            # written. Report the action we WOULD take ("would-enable").
            result["status"] = "would-enable"
            result["note"] = "config does not exist; would create minimal plugins.enabled"
            # v5.11.1 (#58): pre-flight writability of the nearest existing
            # ancestor so the dry-run preview is honest about a likely failure.
            check_dir = cfg.parent if cfg.parent.exists() else None
            if check_dir is not None and not os.access(check_dir, os.W_OK):
                result["status"] = "manual-required"
                result["note"] = (
                    f"config does not exist and {check_dir} is not writable; "
                    "create plugins.enabled manually"
                )
            return result
        # v5.11.1 (#58): wrap the new-config write — set status only AFTER the
        # write succeeds so a failure doesn't claim success.
        try:
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(_ENABLE_SNIPPET, encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"Could not create Hermes config at {cfg}: {exc}. "
                "No config was written; add plugins.enabled manually."
            ) from exc
        result["status"] = "enabled"
        result["note"] = "config did not exist; created minimal plugins.enabled"
        return result

    try:
        # v5.11.1 (#58): newline="" disables universal-newline translation so a
        # CRLF config arrives with its \r\n intact, letting _patch_config_text
        # detect and preserve the line ending.
        with open(cfg, "r", encoding="utf-8", newline="") as _f:
            text = _f.read()
    except OSError as exc:
        result["status"] = "manual-required"
        result["note"] = f"could not read config ({exc}); add the snippet manually"
        return result

    new_text, status = _patch_config_text(text)
    if status == "manual-required":
        result["status"] = status
        result["note"] = "plugins.enabled structure is ambiguous; file left untouched"
        return result
    if status == "already-enabled":
        result["status"] = status
        result["note"] = "token-optimizer already allow-listed; no change"
        return result

    # status == "enabled": back up then write.
    backup = cfg.with_name(f"{cfg.name}.bak-{_timestamp()}")
    result["backup"] = str(backup)
    if dry_run:
        # v5.11.1 (#58): nothing written under dry-run -> "would-enable".
        result["status"] = "would-enable"
        return result
    # v5.11.1 (#58): new_text is guaranteed non-None when status == "enabled".
    assert new_text is not None
    # v5.11.1 (#58): wrap backup + write so a disk/permission failure surfaces
    # as a clean actionable error naming the backup, not a raw traceback.
    try:
        shutil.copy2(cfg, backup)
        # v5.11.1 (#58): newline="" so the \r\n we preserved in new_text is
        # written verbatim (no platform os.linesep re-translation).
        with open(cfg, "w", encoding="utf-8", newline="") as _f:
            _f.write(new_text)
    except OSError as exc:
        raise ValueError(
            f"Could not update Hermes config {cfg}: {exc}. A backup may exist at "
            f"{backup}; the original config may be partially modified -- restore "
            "from the backup if needed and add plugins.enabled manually."
        ) from exc
    result["status"] = "enabled"
    return result


# ---------------------------------------------------------------------------
# Core install / uninstall
# ---------------------------------------------------------------------------

def install(
    *,
    dry_run: bool = False,
    enable: bool = False,
) -> tuple[Path, str, dict]:
    """Copy the ``hermes/`` payload + runtime modules into Hermes's plugins dir.

    Returns (install_dir, action, details).

    Idempotent: existing files are overwritten; extra files in the target dir
    that came from a previous install are retained (they may have been written
    by Hermes or the user). We only copy *our* payload files, never delete.
    """
    payload = _payload_dir()
    if not payload.is_dir():
        raise ValueError(
            f"Hermes plugin payload not found at {payload}. "
            "The repo's hermes/ directory must exist before installing."
        )

    scripts_dir = _scripts_dir()
    # Resolve the runtime modules up front so a missing one fails fast with the
    # same shape of error as the missing-payload case (don't half-install).
    runtime_srcs: list[Path] = []
    for mod in _RUNTIME_MODULES:
        src = scripts_dir / mod
        if not src.is_file():
            raise ValueError(
                f"Hermes runtime module not found at {src}. "
                "It must sit next to hermes_install.py for the plugin to import it."
            )
        runtime_srcs.append(src)

    hermes_root = hermes_home()
    install_dir = _plugin_install_dir(hermes_root)

    # Verify the install_dir is inside the hermes root (not a symlink escape).
    _assert_no_symlink_escape(install_dir, hermes_root)

    # v5.11.1 (#58): never ship compiled bytecode — exclude __pycache__ dirs and
    # any .pyc so the plugin dir contains only source.
    payload_files = [
        f for f in sorted(payload.rglob("*"))
        if f.is_file()
        and "__pycache__" not in f.parts
        and f.suffix != ".pyc"
    ]

    # The locator points at the canonical measure.py in the checkout, written
    # only if measure.py actually exists next to the installer.
    measure_py = scripts_dir / "measure.py"
    write_locator = measure_py.is_file()

    listed_files = [str(f.relative_to(payload)) for f in payload_files]
    listed_files += [mod for mod in _RUNTIME_MODULES]
    if write_locator:
        listed_files.append(_MEASURE_LOCATOR_NAME)

    details: dict = {
        "plugin_dir": str(install_dir),
        "hermes_home": str(hermes_root),
        "files": listed_files,
        "dry_run": dry_run,
    }

    if dry_run:
        details["activation"] = _activation_dry_run_note(hermes_root, enable)
        if enable:
            details["activation_result"] = enable_in_config(hermes_root, dry_run=True)
        else:
            # v5.11.1 (#58): keep the JSON schema stable -- activation_result is
            # always present, even on a plain --dry-run with no --enable.
            details["activation_result"] = {
                "status": "manual-required",
                "config_path": str(_config_path(hermes_root)),
            }
        return install_dir, "would-install", details

    # v5.11.1 (#58): wrap every user-state write so a disk/permission failure
    # surfaces as a clean actionable error naming the failing path, not a raw
    # traceback. main() turns ValueError into a clean message + exit 1.
    try:
        # Create the target directory tree.
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"Could not create Hermes plugin directory {install_dir}: {exc}."
        ) from exc

    # Copy each payload file, preserving relative structure.
    for src in payload_files:
        rel = src.relative_to(payload)
        dst = install_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            raise ValueError(
                f"Could not copy plugin file to {dst}: {exc}."
            ) from exc

    # Copy the runtime modules flat into the plugin dir.
    for src in runtime_srcs:
        dst = install_dir / src.name
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise ValueError(
                f"Could not copy runtime module to {dst}: {exc}."
            ) from exc

    # Write the measure.py locator (one line, absolute path).
    if write_locator:
        locator = install_dir / _MEASURE_LOCATOR_NAME
        try:
            locator.write_text(f"{measure_py}\n", encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"Could not write measure.py locator to {locator}: {exc}."
            ) from exc

    # Activation: Hermes requires the plugin to be allow-listed. Either patch
    # the config (--enable) or print the snippet the user must add.
    if enable:
        activation = enable_in_config(hermes_root, dry_run=False)
        details["activation_result"] = activation
        details["activation"] = _activation_message(activation)
    else:
        cfg_hint = _config_path(hermes_root)
        details["activation_result"] = {
            "status": "manual-required",
            "config_path": str(cfg_hint),
        }
        details["activation"] = (
            "Hermes requires this plugin to be allow-listed in its config. Add:\n"
            f"{_ENABLE_SNIPPET}"
            f"to {cfg_hint} "
            "(or re-run with --enable to patch it automatically)."
        )

    return install_dir, "installed", details


def _activation_dry_run_note(hermes_root: Path, enable: bool) -> str:
    cfg = _config_path(hermes_root)
    if enable:
        return f"Would allow-list token-optimizer in {cfg}."
    return (
        "Hermes requires this plugin to be allow-listed. Would print snippet for:\n"
        f"{cfg}"
    )


def _activation_message(activation: dict) -> str:
    status = activation.get("status")
    cfg = activation.get("config_path", "")
    if status == "enabled":
        return f"Allow-listed token-optimizer under plugins.enabled in {cfg}."
    if status == "already-enabled":
        return f"token-optimizer already allow-listed in {cfg}; no change."
    # manual-required
    return (
        f"Could not safely patch {cfg} ({activation.get('note', 'ambiguous')}). "
        "Add this manually:\n" + _ENABLE_SNIPPET
    )


def uninstall(*, dry_run: bool = False) -> tuple[Path, str, dict]:
    """Remove Token Optimizer from Hermes's plugins directory.

    Does NOT auto-remove the plugins.enabled config entry (the user may have
    edited the config); we only print a note that they may remove it.
    """
    hermes_root = hermes_home()
    install_dir = _plugin_install_dir(hermes_root)

    _assert_no_symlink_escape(install_dir, hermes_root)

    details: dict = {
        "plugin_dir": str(install_dir),
        "hermes_home": str(hermes_root),
        "dry_run": dry_run,
        "existed": install_dir.exists(),
        "activation_note": (
            "Left the plugins.enabled entry in your Hermes config untouched; "
            f"remove '- {_PLUGIN_NAME}' from {_config_path(hermes_root)} if desired."
        ),
    }

    if not install_dir.exists():
        return install_dir, "not-found", details

    if not dry_run:
        # v5.11.1 (#58): a running Hermes can hold file locks (notably on
        # Windows). Surface a clean, actionable error naming the dir instead of
        # a raw PermissionError traceback.
        try:
            shutil.rmtree(install_dir)
        except (PermissionError, OSError) as exc:
            raise ValueError(
                f"Could not remove {install_dir}: {exc}. Stop Hermes first "
                "(it may be holding file locks), then re-run --uninstall."
            ) from exc

    return install_dir, "removed" if not dry_run else "would-remove", details


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Token Optimizer as a Hermes plugin."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print intended action without writing",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove Token Optimizer from Hermes plugins",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Also allow-list the plugin in the Hermes config (plugins.enabled)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.uninstall:
            install_dir, action, details = uninstall(dry_run=args.dry_run)
        else:
            install_dir, action, details = install(dry_run=args.dry_run, enable=args.enable)
    except ValueError as exc:
        print(f"[Token Optimizer] {exc}", file=sys.stderr)
        return 1

    # Activation status lives at details.activation_result only (no duplicate
    # top-level copy — one canonical location for JSON consumers).
    payload = {
        "action": action,
        "plugin_dir": str(install_dir),
        "dry_run": args.dry_run,
        "details": details,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        prefix = "Would update" if args.dry_run else "Updated"
        n_files = len(details.get("files", []))
        print(f"[Token Optimizer] {prefix} {install_dir} ({action}; {n_files} files)")
        if "activation" in details:
            print(f"[Token Optimizer] {details['activation']}")
        # v5.11.1 (#58): print the activation note on ALL uninstall outcomes,
        # including "not-found" -- the user may still have a stale plugins.enabled
        # entry to clean up.
        if action in ("removed", "would-remove", "not-found") and "activation_note" in details:
            print(f"[Token Optimizer] {details['activation_note']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
