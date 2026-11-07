"""Provenance for Token Optimizer's own Claude Code hook entries.

The malformed-hook healer may delete a settings.json hook only when it can
prove Token Optimizer wrote that exact entry. Proof is byte identity, nothing
looser:

* the command's sha256 is in the provenance ledger under this settings file
  (every installer path records what it writes, see ``record``), or
* the command is byte-identical to a shipped hooks.json template
  (``shipped_hook_templates``) instantiated for the running install root, in
  one of the exact spellings setup_all_hooks writes.

There is deliberately no regex, shlex, quote stripping, separator rewriting,
case folding or prefix test anywhere on the removal path. PR #323 rounds
v6-v8c showed every text heuristic either deletes a foreign hook or misses
ours; an unproven entry is left alone and reported once instead.

Ledger: ``<CONFIG_DIR>/settings_heal/hook_provenance.json``. Writes take
``hook_runtime.lease_lock``, re-read under the lock, union-merge and land
with tempfile + os.replace, so concurrent sessions never lose each other's
records and a reader never sees a half-written file. Missing ledger: empty.
Unreadable: template-only and never overwritten. Corrupt: template-only, and
the next write moves it aside and starts fresh.

Lives beside measure.py (not in _measure/, whose file set is frozen) and must
stay importable on Python 3.9 with the standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from shipped_hook_templates import SHIPPED_HOOK_TEMPLATES

try:
    from hook_runtime import lease_lock
except Exception:  # pragma: no cover - shipped together; without it, never write
    lease_lock = None

LEDGER_NAME = "hook_provenance.json"
LEDGER_VERSION = 1
CODEX_LEDGER_NAME = ".token-optimizer-hook-provenance.json"

# The exact version resolver every current installer-written command carries
# (hooks/hooks.json). Removing ONE exact copy before the subshell test keeps
# installer plumbing from reading as malformation; any other "$(" or backtick
# still counts. tests/test_malformed_detector_installer_resolver.py pins it to
# hooks.json byte for byte.
CURRENT_RESOLVER = 'D="$(ls -d "${D%[/\\\\]*}"/*/ 2>/dev/null | grep -E "/[0-9]+[.][0-9]+[.][0-9]+/$" | sort -V | tail -n1)"'

_DOUBLE_HOME = ("$HOME/Users/", "$HOME/home/", "${HOME}/Users/", "${HOME}/home/")
_PLACEHOLDERS = ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT")

KEEP = "keep"
REMOVE = "remove"
UNPROVEN = "unproven"


# --- identity -----------------------------------------------------------------

def command_digest(command):
    """sha256 of the command's exact UTF-8 bytes (lone surrogates kept)."""
    return hashlib.sha256(command.encode("utf-8", "surrogatepass")).hexdigest()


def settings_key(settings_path):
    """Stable per-file ledger key: absolute, symlink-resolved, OS-normcased.

    Writer and healer both derive it from the same SETTINGS_PATH, so this only
    has to be deterministic; it is never compared against a hook command.
    """
    try:
        path = Path(settings_path).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        path = Path(os.path.abspath(str(settings_path)))
    return os.path.normcase(str(path))


def looks_malformed(command, darwin):
    """Main's syntactic malformation test, unchanged: a subshell or backtick
    left after removing one exact resolver, or a doubled $HOME on macOS."""
    probe = command.replace(CURRENT_RESOLVER, 'D=""', 1)
    if "$(" in probe or "`" in probe:
        return True
    return bool(darwin) and any(marker in command for marker in _DOUBLE_HOME)


def instantiate(templates, roots, resolve=None):
    """Exact strings the given templates become for the given root spellings.

    ``resolve`` is setup_all_hooks' own resolver (current output: forward
    slashes on Windows). The plain substitution is the legacy spelling older
    installers wrote with the native root. Both are forward instantiations of
    shipped bytes; nothing is ever parsed back out of a settings command.
    """
    out = set()
    for template in templates:
        for root in roots:
            plain = template
            for placeholder in _PLACEHOLDERS:
                plain = plain.replace(placeholder, root)
            out.add(plain)
            if resolve is not None:
                try:
                    out.add(resolve(template, root))
                except Exception:
                    pass
    return frozenset(out)


# --- ledger -------------------------------------------------------------------

def _empty():
    return {"version": LEDGER_VERSION, "files": {}, "seeded": {}, "reported": {}}


def _valid(data):
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return False
    files = data.get("files")
    if not isinstance(files, dict):
        return False
    for key, entries in files.items():
        if not isinstance(key, str) or not isinstance(entries, dict):
            return False
        if not all(isinstance(d, str) and isinstance(v, dict) for d, v in entries.items()):
            return False
    seeded = data.get("seeded", {})
    reported = data.get("reported", {})
    if not isinstance(seeded, dict) or not isinstance(reported, dict):
        return False
    return all(isinstance(v, list) and all(isinstance(d, str) for d in v)
               for v in reported.values())


def load_ledger(path):
    """Return ``(ledger, status)``; status is ok, missing, unreadable or corrupt.

    Anything but ok yields an empty ledger, so the caller proves by template
    only (invariant: no ledger, no extra removals).
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return _empty(), "missing"
    except OSError:
        return _empty(), "unreadable"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _empty(), "corrupt"
    if not _valid(data):
        return _empty(), "corrupt"
    data.setdefault("seeded", {})
    data.setdefault("reported", {})
    return data, "ok"


def _write_atomic(path, data):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hook_provenance-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, indent=1)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, str(path))
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _update(path, mutate):
    """Locked read-merge-write. ``mutate(data)`` returns True when it changed
    something. Returns ``(written, reset)``: reset is True when a corrupt
    ledger was moved aside. Never raises; a lost lock writes nothing."""
    if lease_lock is None:
        return False, False
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, False
    lock = path.with_name(path.name + ".lock")
    for _attempt in range(3):
        try:
            # Each writer persists a DISTINCT mutation: opt out of the herd
            # throttle so a waiting writer takes a released lease at once.
            with lease_lock(lock, acquire_timeout=2.0, cohort_throttle=False,
                            reclaim_released=True) as acquired:
                if not acquired:
                    continue
                data, status = load_ledger(path)
                if status == "unreadable":
                    return False, False
                reset = False
                if status == "corrupt":
                    try:
                        os.replace(str(path), str(path.with_name(
                            "%s.corrupt-%d" % (path.name, int(time.time())))))
                        reset = True
                    except OSError:
                        return False, False
                if not mutate(data) and not reset:
                    return True, False
                return _write_atomic(path, data), reset
        except Exception:
            return False, False
    return False, False


def record(ledger_path, settings_path, commands, source):
    """Record that Token Optimizer wrote these exact commands into
    ``settings_path``. Call after the settings write landed. Never raises."""
    try:
        wanted = [c for c in commands if isinstance(c, str) and c]
        if not wanted:
            return True
        key = settings_key(settings_path)
        now = int(time.time())

        def mutate(data):
            entries = data["files"].setdefault(key, {})
            changed = False
            for command in wanted:
                digest = command_digest(command)
                if digest not in entries:
                    entries[digest] = {"source": str(source), "ts": now}
                    changed = True
            return changed

        return _update(ledger_path, mutate)[0]
    except Exception:
        return False


# --- facade glue ----------------------------------------------------------------

def _same_path(a, b):
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError, RuntimeError):
        return False


def _hook_commands(hooks_json):
    try:
        data = json.loads(Path(hooks_json).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    found = set()
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for groups in (hooks or {}).values() if isinstance(hooks, dict) else ():
        for group in groups if isinstance(groups, list) else ():
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else ():
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str) and command:
                    found.add(command)
    return found


def ledger_path_for(facade):
    return Path(facade._settings_heal_dir()) / LEDGER_NAME


def record_from_facade(facade, commands, source):
    """Installer-side hook: record commands just written to SETTINGS_PATH."""
    try:
        if facade._settings_heal_ensure_dir() is None:
            return False
        return record(ledger_path_for(facade), facade.SETTINGS_PATH, commands, source)
    except Exception:
        return False


class Provenance:
    """One heal pass's view: proven sets, ledger snapshot, verdicts."""

    def __init__(self, ledger_path, settings_path, proven_templates, current_commands,
                 darwin, script_names=()):
        self.ledger_path = Path(ledger_path)
        self.key = settings_key(settings_path)
        self.proven_templates = frozenset(proven_templates)
        self.current_commands = frozenset(current_commands)
        self.darwin = bool(darwin)
        self.script_names = tuple(script_names)
        ledger, self.status = load_ledger(self.ledger_path)
        ok = self.status == "ok"
        self._recorded = frozenset(ledger["files"].get(self.key, {})) if ok else frozenset()
        self._seeded = ok and self.key in ledger["seeded"]
        self._reported = frozenset(ledger["reported"].get(self.key, [])) if ok else frozenset()

    @classmethod
    def from_facade(cls, facade):
        system = facade.platform.system()
        install_root = None
        try:
            # facade = <root>/skills/token-optimizer/scripts/measure.py
            install_root = Path(facade.__file__).resolve().parents[3]
        except Exception:
            pass
        roots, setup_root, live = [], None, set()
        try:
            hooks_json = facade._find_plugin_hooks_json()
        except Exception:
            hooks_json = None
        if hooks_json:
            setup_root = Path(hooks_json).parent.parent
            live = _hook_commands(hooks_json)
        if install_root is not None:
            roots.append(str(install_root))
            # setup_all_hooks writes its own root spelling; it proves only when
            # it is this running install (main's ownership boundary).
            if setup_root is not None and _same_path(setup_root, install_root):
                roots.append(str(setup_root))
        resolve = facade._resolve_hook_command
        proven = instantiate(SHIPPED_HOOK_TEMPLATES | live, sorted(set(roots)), resolve)
        current = set()
        if setup_root is not None:
            for template in live:
                try:
                    current.add(resolve(template, setup_root))
                except Exception:
                    pass
        return cls(ledger_path_for(facade), facade.SETTINGS_PATH, proven, current,
                   system == "Darwin", getattr(facade, "_TO_SCRIPT_NAMES", ()))

    def proof(self, command):
        """"template", "ledger" or None. Exact bytes only."""
        if command in self.proven_templates:
            return "template"
        if command_digest(command) in self._recorded:
            return "ledger"
        return None

    def verdict(self, command):
        if not isinstance(command, str) or not command:
            return KEEP
        if not looks_malformed(command, self.darwin):
            return KEEP
        if command in self.current_commands:
            return KEEP  # what setup_all_hooks writes now: never churn it
        if self.proof(command):
            return REMOVE
        return UNPROVEN

    def _worth_reporting(self, command):
        # Diagnostics only: the entries the old heuristic would have weighed.
        return any(name in command for name in self.script_names)

    def finish(self, present, unproven, log=None):
        """After a heal pass: seed the ledger on first run (exact template
        matches only) and report each unproven entry once.

        ``present`` is every command left in settings; ``unproven`` is a list
        of ``(event, command)``. ``log(action, **fields)`` is the heal log.
        """
        try:
            if self.status == "unreadable":
                return
            seeds = [] if self._seeded else sorted(
                {c for c in present if isinstance(c, str) and c in self.proven_templates})
            reports, seen = [], set()
            for event, command in unproven:
                digest = command_digest(command)
                if digest in self._reported or digest in seen or not self._worth_reporting(command):
                    continue
                seen.add(digest)
                reports.append((event, command, digest))
            if self._seeded and not reports and self.status == "ok":
                return
            key, now, logged = self.key, int(time.time()), []

            def mutate(data):
                changed = False
                if key not in data["seeded"]:
                    entries = data["files"].setdefault(key, {})
                    for command in seeds:
                        entries.setdefault(command_digest(command),
                                           {"source": "migration-seed", "ts": now})
                    data["seeded"][key] = now
                    changed = True
                done = data["reported"].setdefault(key, [])
                for event, command, digest in reports:
                    if digest not in done:
                        done.append(digest)
                        logged.append((event, command, digest))
                        changed = True
                return changed

            written, reset = _update(self.ledger_path, mutate)
            if log is None:
                return
            if reset:
                log("hook_provenance_reset", reason="corrupt ledger moved aside; template-only proof")
            if written:
                for event, command, digest in logged:
                    log("hook_unproven", event=event, sha256=digest, command=command[:200],
                        reason="not recorded by Token Optimizer and not a shipped template; left in place")
        except Exception:
            return


def write_codex_hooks(path, data, proof, writer, extra=(), source="codex_install"):
    """Codex installer: write hooks.json via ``writer`` (raises on failure, as
    before), then record in the ledger beside it every command ``proof``
    (codex_hook_provenance.CodexProof) owns plus ``extra``, the commands this
    run just generated or rewrote."""
    writer(path, data)
    try:
        proof.record_present(data, extra, source)
    except Exception:
        pass
