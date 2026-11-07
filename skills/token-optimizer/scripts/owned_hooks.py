"""Token Optimizer's own SessionEnd, Stop and smart-compact entries in
settings.json: which ones the fossil reconcile, setup_hook's upgrade and
uninstall, and setup_smart_compact --uninstall may rewrite or remove.

These used to act on any command that merely mentioned measure.py with one
of our subcommands, so a user's own /home/me/mytool/measure.py collect was
rewritten or deleted. An entry is now theirs to touch only when it is
proven ours (a shipped context or a ledger record, exactly as the healer
proves), or it is byte-identical to one of the exact commands setup_hook /
setup_smart_compact ever generated, rebuilt forward with a measure.py path
that is a Token Optimizer install's.

Command tokens only propose candidates here; byte equality of a forward
rebuild decides. Lives beside measure.py and must stay importable on Python
3.9 with the standard library only.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import hook_heal
import hook_provenance as hp


_PLACEHOLDER_SCRIPT = "${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py"
_SCRIPT_TAIL = ("skills", "token-optimizer", "scripts", "measure.py")


def _sq(path):
    return "'%s'" % path


def _sh(text):
    return shlex.quote(str(text).replace("\\", "/"))


def _l2c(*args):
    return subprocess.list2cmdline(list(args))


# setup_hook's HOOK_COMMAND, every shape it shipped (git history of measure.py).
SESSION_END_SHAPES = (
    lambda py, mp: "python3 %s collect --quiet" % mp,
    lambda py, mp: "python3 %s collect --quiet" % _sq(mp),
    lambda py, mp: "python3 {0} collect --quiet && python3 {0} dashboard --quiet".format(_sq(mp)),
    lambda py, mp: "%s && %s >NUL 2>&1" % (_l2c(py, mp, "collect", "--quiet"), _l2c(py, mp, "dashboard", "--quiet")),
    lambda py, mp: "{0} {1} collect --quiet && {0} {1} dashboard --quiet >/dev/null 2>&1".format(_sh(py), _sh(mp)),
    lambda py, mp: "python3 %s session-end-flush --trigger end" % _sq(mp),
    lambda py, mp: "%s %s session-end-flush --trigger end >/dev/null 2>&1" % (_sh(py), _sh(mp)),
)

# setup_smart_compact's commands, every shape it shipped.
_COMPACT_SUBCOMMANDS = ("compact-capture --trigger auto", "compact-restore",
                        "compact-capture --trigger stop", "compact-capture --trigger end")
COMPACT_SHAPES = tuple(
    shape for sub in _COMPACT_SUBCOMMANDS for shape in (
        lambda py, mp, sub=sub: "python3 %s %s" % (_sq(mp), sub),
        lambda py, mp, sub=sub: "[ -r {0} ] || exit 0; python3 {0} {1}".format(_sq(mp), sub),
        lambda py, mp, sub=sub: "[ -r {0} ] || exit 0; {1} {0} {2}".format(_sh(mp), _sh(py), sub),
    ))


# setup_quality_bar's UserPromptSubmit command, every shape it shipped.
QUALITY_SHAPES = (
    lambda py, mp: "python3 %s quality-cache --quiet" % _sq(mp),
    lambda py, mp: "%s %s quality-cache --quiet" % (_sh(py), _sh(mp)),
)


def _path_parts(path):
    text = str(path).replace("\\", "/")
    if os.name == "nt":
        text = text.lower()
    return tuple(part for part in text.split("/") if part)


def _install_roots(facade):
    """Fixed roots R whose R/skills/token-optimizer/scripts/measure.py is ours."""
    roots = [Path(facade.CLAUDE_DIR),  # script install, skill link: ~/.claude/skills/token-optimizer
             Path(facade.CLAUDE_DIR) / "token-optimizer"]  # install.sh's clone that link points into
    for get in (lambda: Path(facade.MEASURE_PY_PATH).parents[3],
                lambda: Path(facade.__file__).resolve().parents[3],
                lambda: Path(facade._find_plugin_hooks_json()).parent.parent):
        try:
            roots.append(get())
        except Exception:
            pass
    return roots


def is_install_script(facade, path):
    """True when ``path`` is a Token Optimizer install's measure.py: this copy,
    the script install (skill link or its clone), the plugin root hooks.json names, any plugin-cache
    version (one since removed included), or the placeholder spelling."""
    if path == _PLACEHOLDER_SCRIPT:
        return True
    text = str(path).replace("\\", "/")
    if not _absolute(text):
        return False
    parts = _path_parts(text)
    if parts[-4:] != _path_parts("/".join(_SCRIPT_TAIL)) or "." in parts or ".." in parts:
        return False
    head = parts[:-4]
    if any(head == _path_parts(root) for root in _install_roots(facade)):
        return True
    # Any marketplace: Claude Code names the cache dir after the plugin, our
    # marketplace has been renamed and mirrored, and this path only proposes a
    # candidate; the whole command must still equal one we generated.
    cache = _path_parts(Path(facade.CLAUDE_DIR) / "plugins" / "cache")
    return len(head) == len(cache) + 3 and head[:len(cache)] == cache and head[-2] == "token-optimizer"


def _absolute(text):
    text = str(text).replace("\\", "/")
    return text.startswith("/") or (len(text) > 2 and text[1] == ":" and text[2] == "/")


def matches_shape(facade, command, shapes):
    """``command`` is exactly one of ``shapes`` rebuilt from its own measure.py
    and interpreter tokens, with that measure.py an install's. The tokens only
    propose candidates; byte equality of the rebuilt command decides."""
    for tokens in _token_views(command):
        for i, token in enumerate(tokens):
            if not token.endswith("measure.py") or not is_install_script(facade, token):
                continue
            for py in {"python3", tokens[i - 1] if i else "python3"}:
                if not _our_interpreter(py):
                    continue  # a wrapper (sudo, env, nice) is not a command we wrote
                for shape in shapes:
                    try:
                        if shape(py, token) == command:
                            return True
                    except Exception:
                        continue
    return False


def _our_interpreter(token):
    """An interpreter token our generators wrote: bare python3 (POSIX shapes)
    or sys.executable, an absolute path to a python / python3 / python3.N
    (``w``, ``.exe``) binary (Windows shapes). Never a ``py`` launcher: no
    generator emitted one."""
    if token == "python3":
        return True
    if not _absolute(token):
        return False
    name = _path_parts(token)[-1] if _path_parts(token) else ""
    if os.name != "nt":
        name = name.lower()
    for suffix in (".exe", "w"):
        if name[-len(suffix):] == suffix:
            name = name[:-len(suffix)]
    version = name[len("python"):]
    return name[:len("python")] == "python" and all(c.isdigit() or c == "." for c in version) \
        and ".." not in version and version[:1] != "."


def _token_views(command):
    """POSIX shell tokens, plus cmd-style tokens (list2cmdline output, where a
    backslash is a plain path separator) with one pair of quotes removed."""
    views = []
    for posix in (True, False):
        try:
            tokens = shlex.split(command, posix=posix)
        except ValueError:
            continue
        if not posix:
            tokens = [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in tokens]
        views.append(tokens)
    return views


def owner(facade, shapes):
    """``owns(event, group, hook)`` for one pass: proven ours in its context,
    or an exact generated shape. Proof is loaded on first use only."""
    cache = []

    def owns(event, group, hook):
        context = hp.entry_context(event, group, hook)
        if context is None:
            return False
        if not cache:
            dest = hook_heal._resolved(facade.SETTINGS_PATH)
            cache.append(hp.Provenance.from_facade(facade, settings_path=dest or facade.SETTINGS_PATH))
        return bool(cache[0].proof(context[3], context[:3])) or matches_shape(facade, context[3], shapes)
    return owns


def _entries(groups):
    """``(group, hook)`` for every hook dict under a list of groups."""
    for group in groups if isinstance(groups, list) else ():
        entries = group.get("hooks") if isinstance(group, dict) else None
        for hook in entries if isinstance(entries, list) else ():
            if isinstance(hook, dict):
                yield group, hook


def _map_groups(hooks, event, fn):
    """Apply ``fn(group, hook)`` to each hook dict in ``hooks[event]``: it
    returns the hook (kept), a replacement, or None (dropped). A group emptied
    by drops goes; an untouched group stays the same object. Returns
    ``(dropped, replaced)``."""
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return 0, 0
    dropped = replaced = 0
    new_groups = []
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            new_groups.append(group)
            continue
        out = []
        for hook in entries:
            new = fn(group, hook) if isinstance(hook, dict) else hook
            if new is None:
                dropped += 1
            else:
                replaced += new is not hook
                out.append(new)
        if len(out) == len(entries) and all(x is y for x, y in zip(out, entries)):
            new_groups.append(group)
        elif out:
            new_groups.append(dict(group, hooks=out))
    if dropped or replaced:
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)
    return dropped, replaced


def _filter_groups(hooks, event, drop):
    """Drop the hooks ``drop(group, hook)`` selects; returns how many."""
    return _map_groups(hooks, event, lambda g, h: None if drop(g, h) else h)[0]


def _plugin_provides_stop_flush(facade):
    try:
        if not facade._is_plugin_installed():
            return False
        path = facade._find_plugin_hooks_json()
        plugin = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        return any(facade._sessionend_cmd_is_flush(hook.get("command", ""))
                   for _, hook in _entries((plugin.get("hooks") or {}).get("Stop")))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def apply_fossil_reconcile(facade, settings, result):
    """lifecycle._apply_sessionend_fossil_reconcile, on our entries only.

    SessionEnd: each collect/dashboard fossil of ours is rewritten in place to
    HOOK_COMMAND, or removed when a current flush of ours is already there.
    Stop: our collect fossils go, and so does every flush of ours after the
    first (all of them when the plugin's hooks.json provides the Stop flush).
    Anything not ours is left byte for byte. Returns True if anything changed;
    ``result`` counts this call's changes."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    owns = owner(facade, SESSION_END_SHAPES)
    flush, fossil = facade._sessionend_cmd_is_flush, facade._sessionend_cmd_is_collect_fossil

    def ours(event, group, hook, shape):
        return shape(hook.get("command", "")) and owns(event, group, hook)

    has_current = [any(ours("SessionEnd", g, h, flush) for g, h in _entries(hooks.get("SessionEnd")))]

    def heal_session_end(group, hook):
        if not ours("SessionEnd", group, hook, fossil):
            return hook
        if has_current[0]:
            result["removed"] += 1
            return None
        result["rewritten"] += 1
        has_current[0] = True
        return facade._rewrite_collect_fossil_hook(hook)

    changed = any(_map_groups(hooks, "SessionEnd", heal_session_end))
    provides, seen = _plugin_provides_stop_flush(facade), [False]

    def drop_stop(group, hook):
        if ours("Stop", group, hook, fossil):
            return True
        if ours("Stop", group, hook, flush):
            if provides or seen[0]:
                return True
            seen[0] = True
        return False

    stop_removed = _filter_groups(hooks, "Stop", drop_stop)
    result["stop_removed"] += stop_removed
    return changed or stop_removed > 0


def remove_session_end(facade, settings):
    """setup_hook --uninstall / cleanup: drop our SessionEnd hooks only."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return 0
    owns = owner(facade, SESSION_END_SHAPES)
    return _filter_groups(hooks, "SessionEnd", lambda group, hook: (
        facade._is_token_optimizer_session_end_hook(hook) and owns("SessionEnd", group, hook)))


def session_end_view(facade, settings):
    """``settings`` reduced to our own SessionEnd hooks, for setup_hook's
    installed / up-to-date check: a user's own measure.py session-end-flush
    must never read as ours and make setup_hook skip installing it."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get("SessionEnd") if isinstance(hooks, dict) else None
    owns = owner(facade, SESSION_END_SHAPES)
    mine = [hook for group, hook in _entries(groups) if owns("SessionEnd", group, hook)]
    return {"hooks": {"SessionEnd": [{"hooks": mine}]}}


def replace_session_end_fossil(facade, hooks, new_hook):
    """setup_hook's upgrade: in each SessionEnd group, our first fossil
    becomes ``new_hook``. True when one was replaced; with none of ours,
    the caller installs alongside instead."""
    owns, replaced = owner(facade, SESSION_END_SHAPES), False
    groups = hooks.get("SessionEnd") if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else ():
        entries = group.get("hooks") if isinstance(group, dict) else None
        for i, hook in enumerate(entries if isinstance(entries, list) else ()):
            if isinstance(hook, dict) and facade._sessionend_cmd_is_collect_fossil(hook.get("command", "")) \
                    and owns("SessionEnd", group, hook):
                entries[i] = new_hook
                replaced = True
                break
    return replaced


def remove_smart_compact(facade, settings):
    """setup_smart_compact --uninstall: drop our compact-capture /
    compact-restore entries from its four events. Returns hooks removed."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return 0
    owns = owner(facade, COMPACT_SHAPES)
    removed = 0
    for event in ("PreCompact", "SessionStart", "Stop", "SessionEnd"):
        removed += _filter_groups(hooks, event, lambda group, hook, event=event: (
            isinstance(hook.get("command"), str)
            and ("compact-capture" in hook["command"] or "compact-restore" in hook["command"])
            and owns(event, group, hook)))
    return removed


def _cache_roots_named(facade, command):
    """Plugin-cache version roots (.../plugins/cache/<m>/token-optimizer/<v>)
    a command names, removed versions included. Candidates only."""
    roots = []
    for cache in {str(Path(facade.CLAUDE_DIR) / "plugins" / "cache"),
                  str(Path(facade.CLAUDE_DIR) / "plugins" / "cache").replace("\\", "/")}:
        start = 0
        while True:
            at = command.find(cache, start)
            if at < 0:
                break
            start = at + len(cache)
            rest = command[start:].replace("\\", "/").split("/")
            if len(rest) > 3 and rest[0] == "" and rest[2] == "token-optimizer":
                version = rest[3]
                for stop in ("'", '"', " ", ";", ")"):
                    version = version.split(stop)[0]
                if version and version not in (".", ".."):
                    roots.append(str(Path(cache) / rest[1] / "token-optimizer" / version))
    return roots


def cleanup_owner(facade):
    """``owns(event, group, hook)`` for measure.py cleanup: proven ours only.

    Ours when the entry is proven in its context (ledger, or a shipped
    hooks.json entry for this install), or is a shipped hooks.json entry
    instantiated for an install root the command itself names (a removed
    plugin version, the script-install clone, the ${CLAUDE_PLUGIN_ROOT}
    spelling), or is byte-identical to a SessionEnd, smart-compact or
    quality-bar command we generated for an install's measure.py. Naming our
    subcommands or scripts proves nothing."""
    base = owner(facade, SESSION_END_SHAPES + COMPACT_SHAPES + QUALITY_SHAPES)
    fixed = [str(r) for r in _install_roots(facade)] + ["${CLAUDE_PLUGIN_ROOT}"]
    cache = {}

    def instantiated(roots):
        key = tuple(sorted(set(roots)))
        if key not in cache:
            cache[key] = hp.instantiate_contexts(hp.SHIPPED_HOOK_CONTEXTS, list(key),
                                                 getattr(facade, "_resolve_hook_command", None))
        return cache[key]

    def owns(event, group, hook):
        if base(event, group, hook):
            return True
        context = hp.entry_context(event, group, hook)
        if context is None:
            return False
        return context in instantiated(fixed + _cache_roots_named(facade, context[3]))
    return owns
