#!/usr/bin/env python3
"""Token Optimizer for Claude Code cloud sessions (claude.ai/code).

Cloud VMs load no plugins, so the plugin install never reaches them. What a
cloud VM does run is the environment's *setup script* (as root, before Claude
Code starts, for every repository and multi-repo session that uses the
environment) and the hooks in the VM's user-level ``~/.claude/settings.json``.

This module puts the REAL Token Optimizer there, not a reimplementation:

``measure.py cloud-setup-script``
    Prints a short, paste-ready setup script. It downloads the published npm
    tarball ``@alexgreensh/token-optimizer@<version>`` from registry.npmjs.org,
    checks its sha512 against the integrity the registry published, and only
    then runs the ``install-cloud.sh`` inside it. Nothing unverified executes.

``install-cloud.sh`` -> ``cloud_kit.py install``
    Copies the package to ``~/.cache/token-optimizer-cloud/<version>``, writes
    ``~/.cache/token-optimizer-cloud/token-optimizer-cloud-run.sh`` (sets CLAUDE_PLUGIN_ROOT and execs
    the real hooks/python-launcher.sh -> hooks/run.py), and merges into
    ``~/.claude/settings.json`` a hook map GENERATED from the package's own
    hooks/hooks.json: same events, matchers, timeouts, async flags and runner
    arguments, only the launcher prefix differs. Commands carry absolute paths
    and never the literal ``${CLAUDE_PLUGIN_ROOT}`` (repo/user hooks containing
    it never run in cloud sessions).

``measure.py cloud-doctor``
    Checks the install from inside a VM (or any machine).

Rules this module keeps:
    * stdlib only, no sibling imports (``python3 -I`` from the tarball); the one
      exception, hook_provenance.py, is loaded by explicit path to record hooks.
    * nothing is written into any repository working tree.
    * settings are merged, never overwritten; only entries whose command runs
      our run.sh are ever replaced or removed; JSONC / duplicate keys refused.
    * ``main`` never raises (any error is one line on stderr, which
      install-cloud.sh appends to install.log, and exit status 1), and the
      shell wrapper always exits 0, so a cloud session's setup never fails.
"""
from __future__ import annotations
import base64
import compileall
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
PACKAGE = "@alexgreensh/token-optimizer"
REGISTRY = "https://registry.npmjs.org"
CACHE_DIRNAME = "token-optimizer-cloud"
RUNNER_NAME = "token-optimizer-cloud-run.sh"
# Every hook command we write contains this (whatever the cache dir is); it is
# how we, and measure.py's self-heal, recognize our entries and only ours.
MARKER = "/" + RUNNER_NAME
SCRIPTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS_DIR.parents[2]
# Semver with an optional prerelease tag (5.13.50-rc.1). The charset stays
# shell-safe because the version is rendered into the setup script.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$")
def _version_key(v: str):
    """Sort key: releases after their prereleases, numeric parts numerically."""
    core, _, pre = v.partition("-")
    nums = tuple(int(x) for x in core.split("."))
    if not pre:
        return nums + ((1,),)
    parts = tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))
    return nums + ((0,) + parts,)
_ARG_RE = re.compile(r"^[A-Za-z0-9_./=:-]+$")
# The canonical hooks.json command ends with:
#   exec "$b" "$D/hooks/python-launcher.sh" "$D/hooks/run.py" <runner args>; done; exit 0
# ($D = the plugin root, or the newest sibling version when an upgrade deleted
# it). The older "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" spelling is still accepted.
_CANON_RE = re.compile(r'"(?:\$\{CLAUDE_PLUGIN_ROOT\}|\$D)/hooks/run\.py" ([^;"\'`$]+?); done; exit 0$')


class KitError(Exception):
    """A refusal with a message meant for the user."""


# --------------------------------------------------------------------- paths


def cache_dir() -> Path:
    override = os.environ.get("TOKEN_OPTIMIZER_CLOUD_HOME")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".cache" / CACHE_DIRNAME


def claude_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".claude"


# Managed-settings drop-in (opt-in fallback, see write_managed_fallback). Our
# own file in Claude Code's documented drop-in dir; managed-settings.json and
# every other file there are never read for writing, changed or removed.
MANAGED_DIR = "/etc/claude-code/managed-settings.d"
MANAGED_FILE = "50-token-optimizer-cloud.json"
MANAGED_ENV = "TOKEN_OPTIMIZER_CLOUD_MANAGED"
ROUTE_LOG = "route.log"


def managed_dir() -> Path:
    return Path(os.environ.get("TOKEN_OPTIMIZER_CLOUD_MANAGED_DIR") or MANAGED_DIR)


def managed_fallback_requested() -> bool:
    return os.environ.get(MANAGED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def package_version(root: Path) -> str:
    for rel in ("package.json", ".claude-plugin/plugin.json"):
        try:
            v = json.loads((root / rel).read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(v, str) and _VERSION_RE.match(v):
            return v
    raise KitError("cannot read a version from %s" % root)


# ------------------------------------------------------------------ hook map


def runner_args(command: str) -> list:
    """Runner arguments of one canonical hooks.json command (after run.py)."""
    m = _CANON_RE.search(command or "")
    if not m:
        raise KitError("hooks.json command has an unexpected shape: %r" % (command or "")[:120])
    args = m.group(1).split()
    for a in args:
        if not _ARG_RE.match(a):
            raise KitError("unexpected runner argument %r in hooks.json" % a)
    return args


def _sh_path(path) -> str:
    """A path as the POSIX shell that runs the kit sees it: forward slashes on
    every host. The kit's commands and run.sh only ever run under sh in a
    Linux VM, and MARKER ("/" + RUNNER_NAME) is how our entries are found
    again, so a host-native backslash path (rendering on Windows) would write
    entries that neither sh nor the strip/doctor/self-heal match."""
    return Path(path).as_posix()


def hook_command(runner: Path, args: list) -> str:
    # /bin/sh by absolute path: the host runs this line with whatever PATH it
    # has, and a stripped PATH turned a bare `sh` into "exec: sh: not found",
    # exit 127 on every hook (the #80 class). POSIX guarantees /bin/sh.
    r = shlex.quote(_sh_path(runner))
    return "[ -r %s ] || exit 0; exec /bin/sh %s %s" % (r, r, " ".join(args))


def generate_hook_map(hooks_json: dict, runner: Path) -> dict:
    """event -> groups, mirroring hooks.json exactly except each command."""
    src = hooks_json.get("hooks") if isinstance(hooks_json, dict) else None
    if not isinstance(src, dict) or not src:
        raise KitError("hooks.json has no hooks")
    out = {}
    for event, groups in src.items():
        new_groups = []
        for group in groups:
            g = {}
            for key, value in group.items():
                if key != "hooks":
                    g[key] = value
                    continue
                hooks = []
                for hook in value:
                    h = dict(hook)
                    if h.get("type") != "command":
                        raise KitError("non-command hook in hooks.json (%s)" % event)
                    h["command"] = hook_command(runner, runner_args(h.get("command", "")))
                    hooks.append(h)
                g["hooks"] = hooks
            new_groups.append(g)
        out[event] = new_groups
    return out


def load_hooks_json(root: Path) -> dict:
    return json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ settings


def _no_dup_pairs(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise KitError("duplicate key %r" % k)
        seen[k] = v
    return seen


def load_settings(path: Path):
    """Return (data, raw_text). {} for a missing or blank file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    text = raw.decode("utf-8-sig")
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text, object_pairs_hook=_no_dup_pairs)
    except KitError as exc:
        raise KitError("%s has a %s; nothing was changed" % (path, exc)) from None
    except ValueError as exc:
        raise KitError("%s is not plain JSON (comments or trailing commas?); nothing was "
                       "changed: %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise KitError("%s is not a JSON object; nothing was changed" % path)
    return data, text


def _is_our_hook(hook) -> bool:
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def _is_ours(group) -> bool:
    """True when a group carries at least one of our hook commands."""
    return isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(
        _is_our_hook(h) for h in group["hooks"])


def _without_ours(groups: list) -> list:
    """Our hook commands taken out, hook by hook. A group that also holds the user's own
    hooks keeps them and its other keys; only a group left empty is dropped; others untouched."""
    kept = []
    for group in groups:
        rest = [h for h in group["hooks"] if not _is_our_hook(h)] if _is_ours(group) else None
        if rest is None or rest:
            kept.append(group if rest is None else dict(group, hooks=rest))
    return kept


def _only_ours(groups: list) -> list:
    """The inverse view, for the doctor: each group cut down to our hooks."""
    return [dict(g, hooks=[h for h in g["hooks"] if _is_our_hook(h)]) for g in groups if _is_ours(g)]


def merge_settings(settings: dict, hook_map: dict) -> dict:
    out = dict(settings)
    hooks = out.get("hooks", {})
    if not isinstance(hooks, dict):
        raise KitError("'hooks' in settings.json is not an object; nothing was changed")
    hooks = dict(hooks)
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise KitError("hooks.%s in settings.json is not a list; nothing was changed" % event)
        kept = _without_ours(groups)
        if kept or event in hook_map or not groups:
            hooks[event] = kept
        else:
            del hooks[event]
    for event, groups in hook_map.items():
        hooks[event] = hooks.get(event, []) + groups
    out["hooks"] = hooks
    return out


def strip_settings(settings: dict) -> dict:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return dict(settings)
    out = dict(settings)
    new_hooks = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept = _without_ours(groups)
        if kept or not groups:
            new_hooks[event] = kept
    if new_hooks or not hooks:
        out["hooks"] = new_hooks
    else:
        del out["hooks"]
    return out


def _indent_of(text) -> object:
    if not text:
        return 2
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            ws = line[: len(line) - len(stripped)]
            return "\t" if ws.startswith("\t") else len(ws)
    return 2


def dump_settings(data: dict, like_text) -> bytes:
    return (json.dumps(data, indent=_indent_of(like_text), ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes, mode=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_settings_hooks(settings_path: Path, hook_map) -> bool:
    """Merge (hook_map given) or strip (None). Returns True when the file changed."""
    data, text = load_settings(settings_path)
    new = merge_settings(data, hook_map) if hook_map is not None else strip_settings(data)
    if new == data:
        return False
    # A file that held only our hooks is left as {} rather than deleted: we
    # cannot know whether something else created it.
    _atomic_write(settings_path, dump_settings(new, text))
    _record_provenance(settings_path, data, new, hook_map or {})
    return True


def _record_provenance(path: Path, before: dict, after: dict, hook_map: dict) -> None:
    """Record the hook entries this write added (hook_provenance.py, by path)."""
    try:
        spec = importlib.util.spec_from_file_location("hook_provenance", SCRIPTS_DIR / "hook_provenance.py")
        hp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hp)
        hp.record_cloud_write(path, before, after, hook_map)
    except Exception:  # noqa: BLE001 - provenance never blocks an install
        pass


def write_managed_fallback(hook_map: dict, directory=None) -> str:
    """Write the same hook map into our own managed-settings drop-in.

    WHY, AND WHY OPT-IN. The primary route is the VM's ~/.claude/settings.json:
    proven live (probe P7: hooks written there fire), absent at session start,
    and separate from the launcher's own hooks (~/.claude/launcher-settings.json).
    If a future launcher rewrote that file after the setup script, our hooks
    would vanish silently. The only other documented layer a root setup script
    can write without touching the repository is the managed tier:
    /etc/claude-code/managed-settings.d/*.json on Linux (code.claude.com
    managed-settings: drop-ins merge with managed-settings.json, one file per
    owner). Hooks there merge with user hooks, and a handler defined in more
    than one settings file runs once (hooks docs, "Deduplication of identical
    handlers"), so the drop-in carries byte-identical commands.

    But the docs only promise that file to SELF-HOSTED environments ("also
    reads the managed settings file in its runner image ... only when neither
    server-managed settings nor an MDM policy supplies the managed tier"); for
    Anthropic-hosted VMs they say policy comes from server-managed settings.
    And a managed file that fails to parse stops Claude Code from starting. So
    this is off unless asked for (TOKEN_OPTIMIZER_CLOUD_MANAGED=1, printed by
    `cloud-setup-script --managed-fallback`), never touches another file in the
    dir, and is written atomically. Returns the path written, '' when skipped.
    """
    d = Path(directory) if directory else managed_dir()
    if not managed_fallback_requested() and directory is None:
        return ""
    target = d / MANAGED_FILE
    data = (json.dumps({"hooks": hook_map}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        if target.read_bytes() == data:
            return str(target)
    except OSError:
        pass
    _atomic_write(target, data, 0o644)
    return str(target)


def remove_managed_fallback(directory=None) -> bool:
    """Delete our drop-in, only if it is ours (carries our runner marker)."""
    target = (Path(directory) if directory else managed_dir()) / MANAGED_FILE
    try:
        if MARKER not in target.read_text(encoding="utf-8", errors="replace"):
            return False
        target.unlink()
        return True
    except OSError:
        return False


def managed_fallback_hooks(directory=None) -> dict:
    """Our hooks as the managed drop-in carries them ({} when absent)."""
    target = (Path(directory) if directory else managed_dir()) / MANAGED_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {}
    return {e: _only_ours(gs) for e, gs in hooks.items() if isinstance(gs, list) and _only_ours(gs)}


# --------------------------------------------------------------------- run.sh


RUN_SH = """#!/bin/sh
# Token Optimizer for Claude Code cloud sessions. Written by install-cloud.sh
# (version {version}); hooks in ~/.claude/settings.json call it. It runs one
# real Token Optimizer hook and stays silent itself.
# Off outside Claude Code cloud VMs; TOKEN_OPTIMIZER_CLOUD_KIT=off disables it,
# =force runs it anyway (skips both checks below).
R={root}
L="$R/hooks/python-launcher.sh"
[ -r "$L" ] || exit 0
case "${{TOKEN_OPTIMIZER_CLOUD_KIT:-}}" in
  off|0|false) exit 0 ;;
  force) ;;
  *)
    [ "$CLAUDE_CODE_REMOTE" = true ] || exit 0
    # Stand down when the real plugin is loaded too (an enabled plugin install
    # that applies here, or an account-synced copy), so no hook runs twice.
    # The grep is a cheap pre-filter; cloud_kit.py decides on the parsed file
    # (scope, project path, enabledPlugins).
    P="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/plugins"
    if [ -f "$P/installed_plugins.json" ] && grep -Eq '"token-optimizer[^"@]*@' "$P/installed_plugins.json" 2>/dev/null; then
      python3 -I "$R/skills/token-optimizer/scripts/cloud_kit.py" plugin-active >/dev/null 2>&1 && exit 0
    fi
    for f in "$P"/synced/*/token-optimizer*/hooks/hooks.json; do [ -f "$f" ] && exit 0; done
    ;;
esac
# Route check, once per session: if our hooks are not in the user settings
# file, something rewrote it after the setup script and this hook was wired by
# another layer (the managed fallback). cloud-doctor reports the last line.
if [ "$1" = hooks/sessionstart_runner.py ]; then
  S="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/settings.json"
  if ! grep -qF "/{runner_name}" "$S" 2>/dev/null; then
    date -u "+%Y-%m-%dT%H:%M:%SZ session-start: hooks missing from $S; wired by another layer" \\
      >> "$(dirname "$0")/{route_log}" 2>/dev/null
  fi
fi
CLAUDE_PLUGIN_ROOT="$R"
PYTHONPYCACHEPREFIX={pycache}
export CLAUDE_PLUGIN_ROOT PYTHONPYCACHEPREFIX
# The kit version doubles as Token Optimizer's "this is a cloud VM" evidence
# (lean profile), so it is exported only inside one: =force on a desktop must
# not switch the desktop to the cloud profile.
if [ "$CLAUDE_CODE_REMOTE" = true ]; then
  TOKEN_OPTIMIZER_CLOUD_KIT_VERSION={version}
  export TOKEN_OPTIMIZER_CLOUD_KIT_VERSION
fi
# bash by absolute path too (a stripped PATH must not exit 127, #80), and a
# clean exit when no bash exists at all: a hook never fails the session.
for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash; do
  command -v "$b" >/dev/null 2>&1 || continue
  exec "$b" "$L" "$R/hooks/run.py" "$@"
done
exit 0
"""


def render_run_sh(root: Path, pycache: Path, version: str) -> str:
    return RUN_SH.format(root=shlex.quote(_sh_path(root)), pycache=shlex.quote(_sh_path(pycache)),
                         version=version, runner_name=RUNNER_NAME, route_log=ROUTE_LOG)


# -------------------------------------------------------------------- install

# What the package carries and the install copies. Everything else in the
# extracted tarball (package.json, README, LICENSE files) is copied too; this
# list is what must exist for a usable install.
_REQUIRED = ("hooks/hooks.json", "hooks/python-launcher.sh", "hooks/run.py",
             "skills/token-optimizer/scripts/measure.py") + tuple(
    "skills/token-optimizer/scripts/_measure/" + name for name in (
        '__init__.py', '_compat.py', 'runtime.py', 'pricing.py', 'audit.py',
        'dashboard.py', 'lifecycle.py', 'sessions.py', 'trends.py', 'cache_policy.py',
        'daemon.py', 'quality_archive.py', 'continuity.py', 'savings.py', 'cli.py', 'dashboard_history.py', 'weekly_activity.py', 'headline.py', 'counted_position.py', 'counted_pricing_v5.py', 'transcript_backfill.py', 'counted_subagents.py', 'lean_resume_threshold.py', 'output_reread.py', 'session_effect.py', 'structural_ledger.py', 'structural_listing.py', 'structural_report.py', 'structural_seed.py', 'structural_value.py', 'rework_metrics.py', 'session_best.py', 'savings_story.py', 'counted_orphans.py', 'counted_cap.py', 'archive_void_evidence.py', 'compaction_survival.py', 'archive_void_codex.py', 'compression_fixture_void.py', 'archive_health.py', 'cost_per_message_trend.py', 'lean_resume_receivers.py', 'routing_period.py', 'fixture_context.py', 'transcript_product.py',
    )
)


# Written into the cache dir on install. Uninstall only removes the shared
# names (pycache, .stage-*, the runner, install.log) where it finds this, and
# prune/uninstall only remove version dirs that hold our package: the cache
# dir can be user-set (TOKEN_OPTIMIZER_CLOUD_HOME) and shared with other tools.
CACHE_MARKER = ".token-optimizer-cloud-kit"


def _is_our_version_dir(child: Path) -> bool:
    if not child.is_dir() or child.is_symlink() or not _VERSION_RE.match(child.name):
        return False
    if not (child / "hooks" / "run.py").is_file():
        return False
    for rel, want in (("package.json", PACKAGE), (".claude-plugin/plugin.json", "token-optimizer")):
        try:
            if json.loads((child / rel).read_text(encoding="utf-8")).get("name") == want:
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def _validate_tree(root: Path) -> None:
    missing = [r for r in _REQUIRED if not (root / r).is_file()]
    if missing:
        raise KitError("package at %s is incomplete, missing: %s" % (root, ", ".join(missing)))


def _precompile(root: Path, pycache: Path) -> None:
    """Byte-compile into the pycache prefix so the first hooks start warm.

    Keyed by absolute source path, so it must run on the final location.
    """
    old = sys.pycache_prefix
    sys.pycache_prefix = str(pycache)
    try:
        for sub in ("hooks", "skills/token-optimizer/scripts"):
            compileall.compile_dir(str(root / sub), quiet=1, workers=1)
    finally:
        sys.pycache_prefix = old


def install(source: Path, cache=None, claude=None, precompile=True) -> dict:
    source = Path(source).resolve()
    cache = Path(cache) if cache else cache_dir()
    claude = Path(claude) if claude else claude_dir()
    _validate_tree(source)
    version = package_version(source)
    target = cache / version
    cache.mkdir(parents=True, exist_ok=True)

    if source != target.resolve() and not (target / "hooks" / "run.py").is_file():
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(cache)))
        try:
            shutil.copytree(str(source), str(stage / "pkg"), symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            _validate_tree(stage / "pkg")
            try:
                os.rename(str(stage / "pkg"), str(target))
            except OSError:
                if not (target / "hooks" / "run.py").is_file():
                    raise
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)
    _validate_tree(target)

    pycache = cache / "pycache"
    runner = cache / RUNNER_NAME
    _atomic_write(cache / CACHE_MARKER, b"Token Optimizer cloud kit cache. Safe to delete with the dir.\n", 0o644)
    _atomic_write(runner, render_run_sh(target, pycache, version).encode("utf-8"), 0o755)

    hook_map = generate_hook_map(load_hooks_json(target), runner)
    settings_path = claude / "settings.json"
    changed = write_settings_hooks(settings_path, hook_map)
    managed = ""
    if managed_fallback_requested():
        try:
            managed = write_managed_fallback(hook_map)
        except OSError as exc:  # not root, read-only /etc: the primary route stands
            managed = "skipped: %s" % exc

    if precompile:
        try:
            _precompile(target, pycache)
        except Exception:  # noqa: BLE001 - warm start is an optimization only
            pass

    pruned = []
    for child in sorted(cache.iterdir()):
        if child.name != version and _is_our_version_dir(child):
            shutil.rmtree(str(child), ignore_errors=True)
            pruned.append(child.name)

    return {"version": version, "root": str(target), "runner": str(runner),
            "settings": str(settings_path), "settings_changed": changed,
            "managed_fallback": managed, "events": sorted(hook_map), "pruned": pruned}


def uninstall(cache=None, claude=None) -> dict:
    cache = Path(cache) if cache else cache_dir()
    claude = Path(claude) if claude else claude_dir()
    changed = write_settings_hooks(claude / "settings.json", None)
    removed = []
    if remove_managed_fallback():
        removed.append(MANAGED_FILE)
    if cache.is_dir():
        ours = (cache / CACHE_MARKER).is_file()
        for child in cache.iterdir():
            if _is_our_version_dir(child) or (ours and child.is_dir() and not child.is_symlink() and (
                    child.name == "pycache" or child.name.startswith(".stage-"))):
                shutil.rmtree(str(child), ignore_errors=True)
                removed.append(child.name)
        for name in ((RUNNER_NAME, "install.log", ROUTE_LOG, CACHE_MARKER) if ours else (RUNNER_NAME,)):
            p = cache / name
            if p.is_file():
                p.unlink()
                removed.append(name)
        try:
            cache.rmdir()
        except OSError:
            pass
    return {"settings_changed": changed, "removed": sorted(removed)}


# --------------------------------------------------------------- setup script


def tarball_url(version: str, registry: str = REGISTRY) -> str:
    return "%s/%s/-/token-optimizer-%s.tgz" % (registry.rstrip("/"), PACKAGE, version)


def integrity_of_file(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return "sha512-" + base64.b64encode(h.digest()).decode("ascii")


def registry_dist(version: str, registry: str = REGISTRY, timeout: float = 15.0) -> dict:
    url = "%s/%s/%s" % (registry.rstrip("/"), urllib.parse.quote(PACKAGE, safe="@"), version)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            meta = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise KitError("could not read %s@%s from %s (%s). Is this version published on npm?"
                       % (PACKAGE, version, registry, exc)) from None
    dist = meta.get("dist") or {}
    integrity = " ".join(i for i in str(dist.get("integrity", "")).split() if i.startswith("sha512-"))
    if not integrity:
        raise KitError("registry has no sha512 integrity for %s@%s" % (PACKAGE, version))
    return {"tarball": dist.get("tarball") or tarball_url(version, registry),
            "integrity": integrity.split()[0]}


# The verify-then-run core shared by both channels. $U = tarball URL, $I = the
# sha512 integrity string. install-cloud.sh only runs after the hash matched.
_VERIFY_RUN = (
    'curl -fsSL --retry 2 --max-time 120 -o t.tgz "$U" && '
    '[ "sha512-$(openssl dgst -sha512 -binary t.tgz | base64 | tr -d \'\\n\')" = "$I" ] && '
    'tar -xzf t.tgz && sh package/install-cloud.sh'
)


def _install_cmd(managed: bool) -> str:
    return ("%s=1 sh package/install-cloud.sh" % MANAGED_ENV) if managed else "sh package/install-cloud.sh"


def render_setup_script(version: str, integrity: str, url: str, managed: bool = False) -> str:
    """Pinned: the paste carries the hash, so the code that runs is fixed."""
    for v, label in ((version, "version"), (integrity, "integrity")):
        if "'" in v or "\n" in v:
            raise KitError("bad %s" % label)
    if not _VERSION_RE.match(version) or not integrity.startswith("sha512-"):
        raise KitError("bad version or integrity")
    return (
        "# Token Optimizer %s for Claude Code cloud sessions: fetches the npm tarball, checks its\n"
        "# sha512, runs its install-cloud.sh. Quiet and never fails the setup (log: ~/.cache/%s/install.log).\n"
        "( U=%s I=%s T=\"$(mktemp -d)\" && cd \"$T\" && %s; cd /; rm -rf \"$T\" ) >/dev/null 2>&1 || true\n"
        % (version, CACHE_DIRNAME, shlex.quote(url), shlex.quote(integrity),
           _VERIFY_RUN.replace("sh package/install-cloud.sh", _install_cmd(managed)))
    )


def render_setup_script_latest(registry: str = REGISTRY, managed: bool = False) -> str:
    """Follows the `latest` dist-tag; the hash is the registry's own integrity.

    That hash proves the bytes arrived intact, not who published them: this
    script trusts whatever is published under the package name."""
    meta = "%s/%s/latest" % (registry.rstrip("/"), urllib.parse.quote(PACKAGE, safe="@"))
    prefix = "%s/%s/-/" % (registry.rstrip("/"), PACKAGE)
    py = ('import json,sys;d=json.load(open(sys.argv[1]))["dist"];'
          'print(d["tarball"],[i for i in d["integrity"].split() if i.startswith("sha512-")][0])')
    return (
        "# Token Optimizer (latest on npm) for Claude Code cloud sessions: checks the tarball against\n"
        "# the sha512 the registry publishes. Quiet and never fails the setup (log: ~/.cache/%s/install.log).\n"
        "( T=\"$(mktemp -d)\" && cd \"$T\" && curl -fsSL --max-time 30 -o m.json %s && "
        "set -- $(python3 -I -c %s m.json) && U=\"$1\" I=\"$2\" && [ \"${U#%s}\" != \"$U\" ] && %s; "
        "cd /; rm -rf \"$T\" ) >/dev/null 2>&1 || true\n"
        % (CACHE_DIRNAME, shlex.quote(meta), shlex.quote(py), prefix,
           _VERIFY_RUN.replace("sh package/install-cloud.sh", _install_cmd(managed)))
    )


# --------------------------------------------------------------------- doctor


_TO_KEY_RE = re.compile(r"^token-optimizer[^@]*@")


def _same_dir(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def _enabled_flag(path: Path, key: str):
    """enabledPlugins[key] from one settings file: True / False / None (unset)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    flags = data.get("enabledPlugins") if isinstance(data, dict) else None
    value = flags.get(key) if isinstance(flags, dict) else None
    return value if isinstance(value, bool) else None


def _registry_entry_active(key: str, records, claude: Path, project) -> bool:
    """True when one installed_plugins.json entry is a Token Optimizer install
    that Claude Code loads for THIS session: a user-scope install, or a
    project/local-scope one whose projectPath is this project, and not turned
    off in enabledPlugins (project settings beat user settings)."""
    applies = False
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        scope = rec.get("scope") or "user"
        if scope in ("user", "managed"):
            applies = True
        elif scope in ("project", "local") and project and _same_dir(rec.get("projectPath") or "", project):
            applies = True
    if not applies:
        return False
    layers = []
    if project:
        layers += [Path(project) / ".claude" / "settings.local.json", Path(project) / ".claude" / "settings.json"]
    layers.append(claude / "settings.json")
    for layer in layers:
        flag = _enabled_flag(layer, key)
        if flag is not None:
            return flag
    return True


def plugin_loaded(claude=None, project=None) -> str:
    """Mirror of run.sh's stand-down check. Returns the reason, '' when clear.

    Stands down only for a Token Optimizer plugin Claude Code actually loads
    here: an enabled install whose scope covers this project, or an
    account-synced copy (when account plugin sync reaches cloud VMs, the synced
    plugin runs and the kit must step aside). A disabled entry, or one scoped
    to another project, no longer silences the kit. An unreadable registry that
    names Token Optimizer still stands down: two copies of every hook is the
    worse failure."""
    claude = Path(claude) if claude else claude_dir()
    if project is None:
        project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    plugins = claude / "plugins"
    reg = plugins / "installed_plugins.json"
    try:
        text = reg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if text and re.search(r'"token-optimizer[^"@]*@', text):
        try:
            data = json.loads(text)
            entries = data.get("plugins") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("no plugins map")
        except ValueError:
            return "plugin named in an unreadable %s" % reg
        for key, records in sorted(entries.items()):
            if isinstance(key, str) and _TO_KEY_RE.match(key) and \
                    _registry_entry_active(key, records, claude, project):
                return "plugin %s listed in %s" % (key, reg)
    for f in sorted(plugins.glob("synced/*/token-optimizer*/hooks/hooks.json")):
        if f.is_file():
            return "account-synced plugin at %s" % f.parents[1]
    return ""


def _host_detail() -> str:
    keys = ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE", "CLAUDE_CODE_REMOTE_SESSION_ID")
    return ", ".join("%s=%s" % (k, os.environ.get(k) or "(unset)") for k in keys)


def doctor(cache=None, claude=None, managed=None) -> dict:
    """Check the install and say which route runs the hooks in THIS session.

    Routes, in the order Claude Code would honour them for Token Optimizer:
    a loaded plugin (installed or account-synced: the kit stands down), the
    VM's ~/.claude/settings.json (the setup script's primary target), the
    opt-in managed-settings drop-in, or none. "Hooks missing from
    ~/.claude/settings.json" while the runner is installed means something
    rewrote that file after the setup script ran: called out on its own."""
    cache = Path(cache) if cache else cache_dir()
    claude = Path(claude) if claude else claude_dir()
    checks = []

    def add(ok, name, detail):
        checks.append({"ok": bool(ok), "check": name, "detail": detail})

    in_cloud = os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() == "true"
    add(True, "Cloud session", _host_detail())
    runner = cache / RUNNER_NAME
    add(runner.is_file(), "Runner", str(runner) if runner.is_file() else "missing: run the setup script")
    versions = sorted((c.name for c in cache.iterdir() if _is_our_version_dir(c)),
                      key=_version_key) if cache.is_dir() else []
    root = cache / versions[-1] if versions else None
    add(bool(versions), "Installed version", ", ".join(versions) or "none")
    expected = None
    if root is not None:
        try:
            expected = generate_hook_map(load_hooks_json(root), runner)
        except (OSError, ValueError, KitError) as exc:
            add(False, "Hook map", "cannot build from %s: %s" % (root, exc))

    settings_path = claude / "settings.json"
    ours = {}
    settings_error = ""
    try:
        data, _ = load_settings(settings_path)
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        ours = {e: _only_ours(gs) for e, gs in hooks.items() if isinstance(gs, list)}
        ours = {e: gs for e, gs in ours.items() if gs}
    except KitError as exc:
        settings_error = str(exc)
    managed_hooks = managed_fallback_hooks(managed)
    managed_path = (Path(managed) if managed else managed_dir()) / MANAGED_FILE
    reason = plugin_loaded(claude)

    if reason:
        route = "plugin"
    elif ours:
        route = "settings"
    elif managed_hooks:
        route = "managed"
    else:
        route = "none"

    if settings_error:
        add(False, "Settings hooks", settings_error)
    elif not ours:
        if runner.is_file() and in_cloud:
            add(route in ("managed", "plugin"), "Settings hooks",
                "MISSING from %s at session time: the setup script wrote them, so something "
                "rewrote the file after it ran. %s" % (
                    settings_path,
                    "Hooks still run through the managed fallback (%s)." % managed_path
                    if route == "managed" else
                    "No other route wires them: Token Optimizer is NOT running in this session. "
                    "Re-run the setup script, or add --managed-fallback to it."
                    if route == "none" else "The plugin runs the hooks instead."))
        else:
            add(route in ("managed", "plugin"), "Settings hooks", "none in %s" % settings_path)
    elif expected is None:
        add(True, "Settings hooks", "%d events wired" % len(ours))
    else:
        add(ours == expected, "Settings hooks",
            "match hooks.json (%d events)" % len(expected) if ours == expected
            else "drift from hooks.json: re-run the setup script")

    if managed_hooks:
        same = expected is None or managed_hooks == expected
        add(same, "Managed fallback", "%s (%s)" % (
            managed_path, "identical to the settings hooks, deduplicated by Claude Code" if same
            else "drift from hooks.json: re-run the setup script"))
    else:
        add(True, "Managed fallback", "not installed (opt-in: cloud-setup-script --managed-fallback)")
    add(True, "Plugin also loaded", ("yes, the cloud hooks stand down (%s)" % reason) if reason else "no")

    labels = {"plugin": "the Token Optimizer plugin (%s)" % reason,
              "settings": "%s (setup script)" % settings_path,
              "managed": "%s (managed fallback)" % managed_path,
              "none": "none: no hooks are wired"}
    add(route != "none", "Live route", labels[route])
    try:
        lines = (cache / ROUTE_LOG).read_text(encoding="utf-8", errors="replace").splitlines()
        if lines:
            add(True, "Route log", "%d session start(s) without settings hooks; last: %s"
                % (len(lines), lines[-1]))
    except OSError:
        pass
    return {"ok": all(c["ok"] for c in checks), "route": route, "checks": checks}


# ------------------------------------------------------------------------ CLI

_SETUP_USAGE = """usage: measure.py cloud-setup-script [--latest] [--managed-fallback] [--version V]
                                     [--integrity sha512-...] [--from-tarball FILE] [--registry URL]

Prints a setup script for Claude Code cloud environments (claude.ai/code ->
environment settings -> Setup script). Paste it once per environment: every
session in that environment, any repo, then runs the real Token Optimizer
hooks. Default pins this version and its registry sha512.

--latest            follow npm's latest tag instead. Each download is checked
                    against the sha512 the registry publishes, which proves the
                    bytes are intact, NOT who published them: it trusts whatever
                    is published under @alexgreensh/token-optimizer.
--managed-fallback  also write the hooks to /etc/claude-code/managed-settings.d/
                    (our own drop-in file), in case something rewrites
                    ~/.claude/settings.json after the setup script. Documented
                    for self-hosted environments; see the docs."""


def main_setup_script(argv) -> int:
    if "-h" in argv or "--help" in argv:
        print(_SETUP_USAGE)
        return 0
    opts = {"--version": None, "--integrity": None, "--from-tarball": None, "--registry": None,
            "--tarball-url": None}
    latest = False
    managed = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--latest":
            latest = True
        elif a == "--managed-fallback":
            managed = True
        elif a in opts and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 1
        else:
            print("cloud-setup-script: unknown argument %r\n\n%s" % (a, _SETUP_USAGE), file=sys.stderr)
            return 2
        i += 1
    registry = opts["--registry"] or REGISTRY
    try:
        if latest:
            sys.stdout.write(render_setup_script_latest(registry, managed=managed))
            return 0
        version = opts["--version"] or package_version(PLUGIN_ROOT)
        url = opts["--tarball-url"] or tarball_url(version, registry)
        if opts["--integrity"]:
            integrity = opts["--integrity"]
        elif opts["--from-tarball"]:
            integrity = integrity_of_file(Path(opts["--from-tarball"]))
        else:
            dist = registry_dist(version, registry)
            integrity, url = dist["integrity"], opts["--tarball-url"] or dist["tarball"]
        sys.stdout.write(render_setup_script(version, integrity, url, managed=managed))
        return 0
    except (KitError, OSError) as exc:
        print("cloud-setup-script: %s" % exc, file=sys.stderr)
        return 1


def main_doctor(argv) -> int:
    report = doctor()
    if "--json" in argv:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("Token Optimizer cloud doctor")
        for c in report["checks"]:
            print("  [%s] %s: %s" % ("OK" if c["ok"] else "!!", c["check"], c["detail"]))
    return 0 if report["ok"] else 1


def main(argv) -> int:
    """Entry for install-cloud.sh: ``cloud_kit.py install --source DIR [--cache DIR]``."""
    cmd = argv[0] if argv else ""
    args = argv[1:]
    opts = {}
    i = 0
    while i < len(args):
        if args[i] in ("--source", "--cache", "--claude-dir") and i + 1 < len(args):
            opts[args[i]] = args[i + 1]
            i += 2
        elif args[i] == "--no-precompile":
            opts["--no-precompile"] = True
            i += 1
        else:
            print("cloud_kit: unknown argument %r" % args[i], file=sys.stderr)
            return 2
    try:
        if cmd == "install":
            report = install(Path(opts.get("--source") or PLUGIN_ROOT), opts.get("--cache"),
                             opts.get("--claude-dir"), precompile="--no-precompile" not in opts)
        elif cmd == "uninstall":
            report = uninstall(opts.get("--cache"), opts.get("--claude-dir"))
        elif cmd == "doctor":
            return main_doctor(args)
        elif cmd == "plugin-active":
            # run.sh's stand-down: exit 0 = a loaded plugin owns the hooks.
            return 0 if plugin_loaded() else 1
        else:
            print("usage: cloud_kit.py install|uninstall|doctor|plugin-active [--source DIR] [--cache DIR] "
                  "[--claude-dir DIR]", file=sys.stderr)
            return 2
    except (KitError, OSError) as exc:
        print("cloud_kit %s: %s" % (cmd, exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - never a traceback out of the setup script
        # A settings.json that is not UTF-8, an odd value type, a bug: report
        # it in install.log and fail this command, never raise.
        print("cloud_kit %s: unexpected %s: %s" % (cmd, type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
