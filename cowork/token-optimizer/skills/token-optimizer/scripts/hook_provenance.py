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
import stat
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


def _read_regular(path):
    """Bytes of a regular file, without ever blocking. open() on a FIFO waits
    for a writer forever and a device can read forever, so anything that is
    not a regular file raises OSError (the caller's "unreadable"). The stat
    covers platforms without O_NONBLOCK; the fstat closes the swap-after-stat
    race on the descriptor actually read."""
    if not stat.S_ISREG(os.stat(str(path)).st_mode):
        raise OSError("ledger is not a regular file")
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("ledger is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def load_ledger(path):
    """Return ``(ledger, status)``; status is ok, missing, unreadable or corrupt.

    Anything but ok yields an empty ledger, so the caller proves by template
    only (invariant: no ledger, no extra removals).
    """
    try:
        raw = _read_regular(path)
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
        self.settings_path = str(settings_path)
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
    def from_facade(cls, facade, settings_path=None):
        """``settings_path`` is the resolved file this pass read; the ledger
        key comes from it, never from a second resolution of SETTINGS_PATH."""
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
        # Only the frozen shipped set proves a template. The live hooks.json
        # is mutable (and may belong to another install): it can only keep
        # the command setup_all_hooks writes now from churning, never add
        # deletion authority. A newly installed command is proven by the ledger.
        proven = instantiate(SHIPPED_HOOK_TEMPLATES, sorted(set(roots)), resolve)
        current = set()
        if setup_root is not None:
            for template in live:
                try:
                    current.add(resolve(template, setup_root))
                except Exception:
                    pass
        return cls(ledger_path_for(facade),
                   facade.SETTINGS_PATH if settings_path is None else settings_path,
                   proven, current, system == "Darwin", getattr(facade, "_TO_SCRIPT_NAMES", ()))

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

    def reportable(self, command):
        # Diagnostics only: the entries the old heuristic would have weighed.
        # Never consulted for a removal.
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
                if digest in self._reported or digest in seen or not self.reportable(command):
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
                        settings=self.settings_path,
                        reason="not recorded by Token Optimizer and not a shipped template; left in place")
        except Exception:
            return


# --- the heal transaction ----------------------------------------------------

def _resolved(path):
    try:
        return Path(path).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None


def _read_settings_at(dest):
    """The settings object stored at exactly ``dest``, or None when it is
    missing, not a regular file, unreadable, not JSON or not an object."""
    try:
        data = json.loads(_read_regular(dest).decode("utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def plan_removals(settings, prov):
    """``(new_hooks, removed, kept_commands, unproven)`` for one settings
    object. Pure: decides per hook entry, mutates nothing."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return None, 0, [], []
    removed, new_hooks, kept_commands, unproven = 0, {}, [], []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        new_groups = []
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else None
            if not isinstance(entries, list):
                new_groups.append(group)
                continue
            kept = []
            for hook in entries:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                command = hook.get("command", "")
                verdict = prov.verdict(command)
                if verdict == REMOVE:
                    removed += 1
                    continue
                if verdict == UNPROVEN:
                    unproven.append((event, command))
                kept_commands.append(command)
                kept.append(hook)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
        if new_groups:
            new_hooks[event] = new_groups
    return new_hooks, removed, kept_commands, unproven


def _write_bound(facade, dest, prov):
    """Under the settings lock: confirm SETTINGS_PATH still resolves to
    ``dest``, fresh-read ``dest``, re-plan with the proof keyed to ``dest``
    and write only if the file is still the one read (identity check right
    before os.replace, which re-resolves). Any drift writes nothing.
    Returns ``(removed, plan)``; ``plan`` is None when nothing was decided."""
    with facade._settings_lock() as acquired:
        if not acquired or _resolved(facade.SETTINGS_PATH) != dest:
            return 0, None
        identity = facade._settings_file_identity(dest)
        fresh = _read_settings_at(dest)
        if fresh is None:
            return 0, None
        plan = plan_removals(fresh, prov)
        if not plan[1] or _resolved(facade.SETTINGS_PATH) != dest:
            return 0, plan
        new_settings = dict(fresh)
        new_settings["hooks"] = plan[0]
        if not facade._write_settings_atomic_locked(new_settings, expect_identity=identity):
            return 0, plan
        return plan[1], plan


def heal_settings(facade):
    """The malformed-hook heal pass (lifecycle._fix_malformed_hook_commands).

    SETTINGS_PATH is resolved ONCE; that destination is what is read, what
    keys the proof, and what is written. A symlink retargeted mid-pass can
    never get one file judged with another file's proof, or an A-derived
    payload written into B. Returns the number of removed entries.
    """
    dest = _resolved(facade.SETTINGS_PATH)
    current = _read_settings_at(dest) if dest is not None else None
    if current is None:
        return 0
    prov = Provenance.from_facade(facade, settings_path=dest)
    plan = plan_removals(current, prov)
    if plan[0] is None:
        return 0
    removed = 0
    if plan[1]:
        removed, fresh_plan = _write_bound(facade, dest, prov)
        plan = fresh_plan or plan
    # Seed on first run, report unproven entries once; never a removal.
    prov.finish(plan[2], plan[3], log=facade._settings_heal_log)
    if not removed:
        return 0
    try:  # next session re-runs the full hook heal cycle
        facade._write_config_flag("last_hook_heal_check", 0)
    except Exception:
        pass
    # Script installs need the current hooks back now; plugin installs get
    # them from hooks.json.
    if not facade._is_plugin_installed():
        warn = "  [Token Optimizer] Warning: hook re-provisioning failed after cleanup: %s"
        try:
            result = facade.setup_all_hooks(dry_run=False, verbose=False)
            if result.get("error"):
                print(warn % result["error"], file=facade.sys.stderr)
        except Exception as exc:
            print(warn % exc, file=facade.sys.stderr)
    return removed


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
