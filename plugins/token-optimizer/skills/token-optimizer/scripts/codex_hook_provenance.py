"""Exact-bytes ownership for Token Optimizer's Codex hooks.

Same rule as the Claude healer (hook_provenance.py): codex_install may replace
or remove a hook in ~/.codex/hooks.json (or a project .codex/hooks.json) only
when that hook's whole entry, ``(event, matcher, hook type, command)``, is

* recorded in the ledger for that hooks.json (every Codex write records
  the entries it wrote, and every Codex run records the provable entries it
  finds, so a later interpreter upgrade cannot orphan them), or
* an entry the current generator writes for this install, or
* byte-identical to an entry some shipped codex_install.py generated
  (shipped_codex_hook_templates.json, harvested from git history with
  placeholder paths), its command instantiated with this machine's real
  install root, its parent, the running interpreter and each shipped version
  number.

Our command copied into another event or under another matcher is a
different entry and is not ours.

The ledger, its lock and its queue live in Token Optimizer's own Codex state
dir (``<CODEX_HOME>/token-optimizer/``), keyed by each hooks.json's resolved
path, never beside a project's .codex/hooks.json in the user's repo. Command-only records from before entries
were recorded prove nothing (the fail-safe choice).

No text markers, regexes, separator rewriting or signature normalization
decide ownership. A shipped version number appearing in a command only picks
which exact candidates to build; equality alone decides. Unproven hooks are
kept, whatever they mention.

Ownership is per hook command, not per group: a group mixing ours and a
foreign hook loses only ours.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import hook_provenance as hp

_MARK = "TOSENT"
_TEMPLATES_FILE = Path(__file__).resolve().parent / "shipped_codex_hook_templates.json"
_SHIPPED = []


def shipped():
    """The frozen harvest (loaded once). Unreadable => nothing proves by template."""
    if not _SHIPPED:
        try:
            _SHIPPED.append(json.loads(_TEMPLATES_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _SHIPPED.append({"placeholders": {}, "templates": {}, "versions": []})
    return _SHIPPED[0]


def _is_semver(name):
    parts = str(name).split(".")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def hook_commands(group):
    """The command strings a hooks.json group carries (any shape)."""
    return [hook.get("command") for hook in _handlers(group)]


def _handlers(group):
    """The dicts in a group that carry a command: its hooks, or the group
    itself in the older one-command-per-group shape."""
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if isinstance(hooks, list):
        return [h for h in hooks if isinstance(h, dict)]
    return [group] if "command" in group else []


def entry_context(event, group, handler):
    """``(event, matcher, type, command)`` of one handler, or None when it has
    none a generator could have written. An absent matcher is ""."""
    if not isinstance(event, str) or not isinstance(group, dict) or not isinstance(handler, dict):
        return None
    matcher, hook_type, command = group.get("matcher", ""), handler.get("type"), handler.get("command")
    if not isinstance(matcher, str) or not isinstance(command, str) or not command:
        return None
    if hook_type is not None and not isinstance(hook_type, str):
        return None
    return (event, matcher, hook_type, command)


def contexts_in(event, group):
    return [entry_context(event, group, handler) for handler in _handlers(group)]


class CodexProof:
    def __init__(self, hooks_path, root, current_contexts=(), windows=None, executable=None,
                 ledger_dir=None):
        self.windows = (sys.platform == "win32") if windows is None else bool(windows)
        self.executable = sys.executable if executable is None else str(executable)
        self.current = frozenset(c for c in current_contexts if isinstance(c, tuple) and len(c) == 4)
        self.hooks_path = Path(hooks_path) if hooks_path else None
        self.ledger_path = (Path(ledger_dir) / hp.CODEX_LEDGER_NAME
                            if self.hooks_path and ledger_dir is not None else None)
        self.recorded = frozenset()
        if self.ledger_path is not None:
            ledger, status = hp.load_ledger(self.ledger_path)
            if status == "ok":
                self.recorded = frozenset(ledger["contexts"].get(hp.settings_key(self.hooks_path), {}))
        self.root = root
        self.root_str = str(root) if root is not None else ""
        name = getattr(root, "name", "")
        self.semver = root is not None and _is_semver(name)
        self.parent_str = str(root.parent) if self.semver else ""
        self.versions = frozenset(shipped()["versions"]) | ({name} if self.semver else frozenset())
        # Placeholder variant: spaced placeholders only where the real path has
        # a space, so the generator's quoting matches byte for byte.
        plat = "windows" if self.windows else "posix"
        variant = ("S" if " " in self.root_str else "P") + ("S" if " " in self.executable else "P")
        self._placeholders = shipped()["placeholders"].get(plat, {}).get(variant, {})
        self._templates = shipped()["templates"].get(plat + "/" + variant, ())
        self._cache = {}

    def _instances(self, version):
        if version in self._cache:
            return self._cache[version]
        ph, templates = self._placeholders, self._templates
        if not ph:
            return frozenset()
        if version is None:
            subs = [(ph["dev"], self.root_str)]
        else:
            # The generator joined with the install's own path class.
            subs = [(ph["root"], str(self.root.parent / version)), (ph["parent"], self.parent_str)]
        subs.append((ph["python"], self.executable))
        out = set()
        for template in templates:
            if not (isinstance(template, list) and len(template) == 4):
                continue  # a command-only template proves nothing
            inst = template[3]
            for placeholder, real in subs:
                inst = inst.replace(placeholder, real)
            if _MARK not in inst:
                out.add((template[0], template[1], template[2], inst))
        self._cache[version] = frozenset(out)
        return self._cache[version]

    def owns(self, context):
        """True when this exact entry ``(event, matcher, type, command)`` is ours."""
        if not (isinstance(context, tuple) and len(context) == 4) or self.root is None:
            return False
        if context in self.current or hp.context_digest(context) in self.recorded:
            return True
        if not self.semver:
            return context in self._instances(None)
        return any(context in self._instances(v) for v in self.versions if v in context[3])

    def group_is_ours(self, event, group):
        contexts = contexts_in(event, group)
        return bool(contexts) and all(self.owns(c) for c in contexts)

    def strip(self, event, group):
        """``group`` under ``event`` without our hooks; None when nothing of it is left."""
        if not isinstance(group, dict):
            return group
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            return None if self.group_is_ours(event, group) else group
        kept = [h for h in hooks if not self.owns(entry_context(event, group, h))]
        if len(kept) == len(hooks):
            return group
        if not kept:
            return None
        trimmed = dict(group)
        trimmed["hooks"] = kept
        return trimmed

    def unproven(self, data, names):
        """Commands present that mention one of ``names`` but are not proven.
        Diagnostics only (doctor); never used to decide a removal."""
        found = []
        for event, group in _all_groups(data):
            for handler in _handlers(group):
                command = handler.get("command")
                if isinstance(command, str) and not self.owns(entry_context(event, group, handler)) \
                        and any(n in command for n in names):
                    found.append(command)
        return found

    def record_present(self, data, extra=(), source="codex_seen"):
        """Record every proven entry in ``data`` plus ``extra``, the entries
        (contexts) this run just wrote. Idempotent; writes only when new."""
        if self.ledger_path is None:
            return False
        contexts = [c for event, g in _all_groups(data) for c in contexts_in(event, g) if self.owns(c)]
        contexts.extend(c for c in extra if isinstance(c, tuple) and len(c) == 4)
        return hp.record_contexts(self.ledger_path, self.hooks_path, contexts, source)


def _all_groups(data):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for event, groups in hooks.items() if isinstance(hooks, dict) else ():
        for group in groups if isinstance(groups, list) else ():
            yield event, group


def for_codex(hooks_path, root, current_contexts, ledger_dir):
    return CodexProof(hooks_path, root, current_contexts, ledger_dir=ledger_dir)


def _managed(managed_hooks):
    import inspect
    kw = {}
    try:
        for name, param in inspect.signature(managed_hooks).parameters.items():
            if param.kind == param.KEYWORD_ONLY:
                kw[name] = True
        return managed_hooks(**kw) or {}
    except Exception:
        return {}


def current_contexts(managed_hooks):
    """Every entry the current generator writes, all optional hooks on."""
    return [c for event, groups in _managed(managed_hooks).items() for g in groups
            for c in contexts_in(event, g) if c is not None]


def current_commands(managed_hooks):
    """Every command the current generator writes, all optional hooks on."""
    return [c[3] for c in current_contexts(managed_hooks)]
