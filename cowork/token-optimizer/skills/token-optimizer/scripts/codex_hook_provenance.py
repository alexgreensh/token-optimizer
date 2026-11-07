"""Exact-bytes ownership for Token Optimizer's Codex hooks.

Same rule as the Claude healer (hook_provenance.py): codex_install may replace
or remove a hook in ~/.codex/hooks.json (or a project .codex/hooks.json) only
when that hook's exact command is

* recorded in the ledger beside that hooks.json (every Codex write records
  what it wrote, and every Codex run records the provable commands it finds,
  so a later interpreter upgrade cannot orphan them), or
* the current generator's output for this install, or
* byte-identical to a command some shipped codex_install.py generated
  (shipped_codex_hook_templates.json, harvested from git history with
  placeholder paths), instantiated with this machine's real install root, its
  parent, the running interpreter and each shipped version number.

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
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if isinstance(hooks, list):
        return [h.get("command") for h in hooks if isinstance(h, dict)]
    return [group.get("command")] if "command" in group else []


class CodexProof:
    def __init__(self, hooks_path, root, current_commands=(), windows=None, executable=None):
        self.windows = (sys.platform == "win32") if windows is None else bool(windows)
        self.executable = sys.executable if executable is None else str(executable)
        self.current = frozenset(c for c in current_commands if isinstance(c, str))
        self.hooks_path = Path(hooks_path) if hooks_path else None
        self.ledger_path = (self.hooks_path.parent / hp.CODEX_LEDGER_NAME) if self.hooks_path else None
        self.recorded = frozenset()
        if self.ledger_path is not None:
            ledger, status = hp.load_ledger(self.ledger_path)
            if status == "ok":
                self.recorded = frozenset(ledger["files"].get(hp.settings_key(self.hooks_path), {}))
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
            inst = template
            for placeholder, real in subs:
                inst = inst.replace(placeholder, real)
            if _MARK not in inst:
                out.add(inst)
        self._cache[version] = frozenset(out)
        return self._cache[version]

    def owns(self, command):
        if not isinstance(command, str) or not command or self.root is None:
            return False
        if command in self.current or hp.command_digest(command) in self.recorded:
            return True
        if not self.semver:
            return command in self._instances(None)
        return any(command in self._instances(v) for v in self.versions if v in command)

    def group_is_ours(self, group):
        commands = hook_commands(group)
        return bool(commands) and all(self.owns(c) for c in commands)

    def strip(self, group):
        """``group`` without our hooks; None when nothing of it is left."""
        if not isinstance(group, dict):
            return group
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            return None if self.group_is_ours(group) else group
        kept = [h for h in hooks if not (isinstance(h, dict) and self.owns(h.get("command")))]
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
        for groups in (data.get("hooks") or {}).values() if isinstance(data, dict) else ():
            for group in groups if isinstance(groups, list) else ():
                for command in hook_commands(group):
                    if isinstance(command, str) and not self.owns(command) \
                            and any(n in command for n in names):
                        found.append(command)
        return found

    def record_present(self, data, extra=(), source="codex_seen"):
        """Record every proven command in ``data`` (plus ``extra``, commands
        this run just wrote). Idempotent; writes only when something is new."""
        if self.ledger_path is None:
            return False
        commands = [c for g in _all_groups(data) for c in hook_commands(g) if self.owns(c)]
        commands.extend(c for c in extra if isinstance(c, str))
        return hp.record(self.ledger_path, self.hooks_path, commands, source)


def _all_groups(data):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for groups in hooks.values() if isinstance(hooks, dict) else ():
        for group in groups if isinstance(groups, list) else ():
            yield group


def for_codex(hooks_path, root, current_commands):
    return CodexProof(hooks_path, root, current_commands)


def current_commands(managed_hooks):
    """Every command the current generator writes, all optional hooks on."""
    import inspect
    kw = {}
    try:
        for name, param in inspect.signature(managed_hooks).parameters.items():
            if param.kind == param.KEYWORD_ONLY:
                kw[name] = True
        data = managed_hooks(**kw)
    except Exception:
        return []
    return [c for groups in (data or {}).values() for g in groups for c in hook_commands(g)]
