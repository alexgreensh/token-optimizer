"""Provenance for Token Optimizer's own Claude Code hook entries.

The malformed-hook healer may delete a settings.json hook only when it can
prove Token Optimizer wrote that exact entry. An entry is its full context,
``(event, matcher, hook type, command)``; proof is byte identity of that whole
tuple, nothing looser:

* the context is in the provenance ledger under this settings file (every
  writer records the entries it adds, see ``record_written``), or
* it is byte-identical to a shipped hooks.json entry
  (``shipped_hook_templates``) with the command instantiated for the running
  install root, in one of the exact spellings setup_all_hooks writes.

The same command under another event or matcher is a different entry and is
not proven by either. Ledger records from before contexts existed (command
digests under "files") prove nothing for settings.json: the fail-safe choice.
Their in-context cases are exactly the shipped-template cases, which still
prove by template. Codex hooks.json keeps command-keyed records ("files").

There is deliberately no regex, shlex, quote stripping, separator rewriting,
case folding or prefix test anywhere on the removal path. PR #323 rounds
v6-v8c showed every text heuristic either deletes a foreign hook or misses
ours; an unproven entry is left alone and reported once instead.

Ledger: ``<CONFIG_DIR>/settings_heal/hook_provenance.json``. Writes take an
OS file lock (``flock`` / ``msvcrt.locking``) that the kernel holds exactly as
long as the owning process lives, re-read under it, union-merge and land with
tempfile + os.replace. A stalled but live writer keeps the lock, so a second
writer can never be told it succeeded and then lose its record to the first
one's older snapshot; it fails instead. A dead writer's lock is gone at once. Missing ledger: empty.
Unreadable: template-only and never overwritten. Corrupt: template-only, and
the next write moves it aside and starts fresh.

Lives beside measure.py (not in _measure/, whose file set is frozen) and must
stay importable on Python 3.9 with the standard library only.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

try:
    from shipped_hook_templates import SHIPPED_HOOK_CONTEXTS, SHIPPED_HOOK_TEMPLATES
except ImportError:  # loaded by path under python -I (cloud_kit): the sibling, by path too
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "shipped_hook_templates", str(Path(__file__).with_name("shipped_hook_templates.py")))
    _shipped = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_shipped)
    SHIPPED_HOOK_CONTEXTS, SHIPPED_HOOK_TEMPLATES = (_shipped.SHIPPED_HOOK_CONTEXTS,
                                                    _shipped.SHIPPED_HOOK_TEMPLATES)

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


def context_digest(context):
    """sha256 of one entry's full context ``(event, matcher, type, command)``."""
    return hashlib.sha256(json.dumps(list(context)).encode("ascii")).hexdigest()


def entry_context(event, group, hook):
    """The exact context of one settings.json hook entry, or None when it has
    none we could ever have written (non-string matcher, type or command).
    An absent matcher is "": setup_all_hooks' own group-selection rule."""
    if not isinstance(group, dict) or not isinstance(hook, dict) or not isinstance(event, str):
        return None
    matcher, hook_type, command = group.get("matcher", ""), hook.get("type"), hook.get("command")
    if not isinstance(matcher, str) or not isinstance(command, str) or not command:
        return None
    if hook_type is not None and not isinstance(hook_type, str):
        return None
    return (event, matcher, hook_type, command)


def iter_contexts(settings):
    """Every entry context in a settings object (its "hooks" map)."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for event, groups in hooks.items() if isinstance(hooks, dict) else ():
        for group in groups if isinstance(groups, list) else ():
            entries = group.get("hooks") if isinstance(group, dict) else None
            for hook in entries if isinstance(entries, list) else ():
                context = entry_context(event, group, hook)
                if context is not None:
                    yield context


def contexts_of(settings):
    """Multiset of entry contexts: take it from the read a writer derives its
    payload from, and hand it to ``record_written`` after the write."""
    counts = {}
    for context in iter_contexts(settings):
        counts[context] = counts.get(context, 0) + 1
    return counts


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


def instantiate_contexts(contexts, roots, resolve=None):
    """``instantiate`` for shipped contexts: same event, matcher and type,
    command instantiated for each root spelling."""
    out = set()
    for event, matcher, hook_type, template in contexts:
        for command in instantiate((template,), roots, resolve):
            out.add((event, matcher, hook_type, command))
    return frozenset(out)


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
    return {"version": LEDGER_VERSION, "files": {}, "seeded": {}, "reported": {},
            "contexts": {}, "contexts_seeded": {}}


def _records_ok(section):
    if not isinstance(section, dict):
        return False
    for key, entries in section.items():
        if not isinstance(key, str) or not isinstance(entries, dict):
            return False
        if not all(isinstance(d, str) and isinstance(v, dict) for d, v in entries.items()):
            return False
    return True


def _valid(data):
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return False
    if not _records_ok(data.get("files")) or not _records_ok(data.get("contexts", {})):
        return False
    if not isinstance(data.get("contexts_seeded", {}), dict):
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
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version != LEDGER_VERSION:
        return _empty(), "unreadable"  # another version's ledger: never ours to move or rewrite
    if not _valid(data):
        return _empty(), "corrupt"
    for section in ("seeded", "reported", "contexts", "contexts_seeded"):
        data.setdefault(section, {})
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


# Errors that mean another process holds the lock: worth waiting for. Any
# other error (a filesystem without locks, a bad descriptor) never clears.
_BUSY = (frozenset((errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR)) if fcntl is not None
         else frozenset((errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK))))


def _try_lock(fd):
    """True: locked. False: held by another process. None: unusable here."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError as exc:
        return False if exc.errno in _BUSY else None


def _unlock(fd):
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def _exclusive(lock_path, timeout=2.0):
    """Yield True while this process holds the OS lock on ``lock_path``.

    The kernel owns it: it stays exclusive for as long as the holder lives,
    however long it stalls, and is released the moment it exits or dies. No
    lease to expire, so no second owner while the first is still writing.
    A lock file that is not a regular file (FIFO, device) is never waited on.
    Exactly one yield on every path, so an exception from the body always
    propagates as itself.
    """
    fd = _acquire(lock_path, timeout)
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        _unlock(fd)
        os.close(fd)


def _acquire(lock_path, timeout):
    """A descriptor holding the lock on ``lock_path``, or None."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        if os.path.lexists(str(lock_path)) and not stat.S_ISREG(os.stat(str(lock_path)).st_mode):
            return None
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError:
        return None
    try:
        usable = stat.S_ISREG(os.fstat(fd).st_mode) and (fcntl is not None or msvcrt is not None)
        deadline = time.monotonic() + timeout
        while usable:
            got = _try_lock(fd)
            if got:
                return fd
            if got is None or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
    except OSError:
        pass
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


def _update(path, mutate):
    """Locked read-merge-write. ``mutate(data)`` returns True when it changed
    something. Returns ``(written, reset)``: reset is True when a corrupt
    ledger was moved aside. Never raises; no lock, no write."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, False
    lock = path.with_name(path.name + ".flock")
    try:
        with _exclusive(lock) as acquired:
            if not acquired:
                return False, False
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
            changed = mutate(data)
            pending = _merge_pending(path, data)
            if not (changed or reset or pending):
                return True, False
            written = _write_atomic(path, data)
            for item in pending if written else ():
                try:
                    os.unlink(str(item))
                except OSError:
                    pass
            return written, reset
    except Exception:
        return False, False


# --- pending records ------------------------------------------------------------
# A record whose locked write fails (lock held past the timeout, no lock
# support, unreadable ledger) is queued as one small file beside the ledger
# and merged by the next locked write that lands: late, never lost. Same
# directory and trust as the ledger. Only what a writer added is ever queued.

def _pending_dir(path):
    path = Path(path)
    return path.with_name(path.name + ".pending")


def has_pending(path):
    try:
        return bool(os.listdir(str(_pending_dir(path))))
    except OSError:
        return False


def _queue(path, section, key, entries):
    """Queue ``entries`` (digest -> meta) for ``section``/``key``. True when
    the file landed. Named by its content, so a retry adds no second file;
    written beside the ledger and renamed in, so the queue only ever holds
    complete files."""
    tmp = None
    try:
        name = hashlib.sha256(json.dumps([section, key, sorted(entries)]).encode("utf-8")).hexdigest()
        qdir = _pending_dir(path)
        qdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(qdir.parent), prefix=".hook_provenance-q-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"section": section, "key": key, "entries": entries}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(qdir / name))
        tmp = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _merge_pending(path, data):
    """Fold every well-formed queued record into ``data``; return their files."""
    qdir, merged = _pending_dir(path), []
    try:
        names = sorted(os.listdir(str(qdir)))
    except OSError:
        return merged
    for name in names:
        try:
            body = json.loads(_read_regular(qdir / name).decode("utf-8"))
            section, key, entries = body["section"], body["key"], body["entries"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if section not in ("files", "contexts") or not isinstance(key, str) \
                or not _records_ok({key: entries}):
            continue
        target = data[section].setdefault(key, {})
        for digest, meta in entries.items():
            target.setdefault(digest, meta)
        merged.append(qdir / name)
    return merged


def _record_entries(ledger_path, section, key, entries):
    """"ok" (in the ledger), "queued" (lands with the next write) or "failed"."""
    def mutate(data):
        target = data[section].setdefault(key, {})
        new = {d: v for d, v in entries.items() if d not in target}
        target.update(new)
        return bool(new)

    if _update(ledger_path, mutate)[0]:
        return "ok"
    return "queued" if _queue(ledger_path, section, key, entries) else "failed"


def record(ledger_path, settings_path, commands, source):
    """Record that Token Optimizer wrote these exact commands into
    ``settings_path``. Call after the settings write landed. Never raises."""
    try:
        wanted = [c for c in commands if isinstance(c, str) and c]
        if not wanted:
            return True
        now = int(time.time())
        entries = {command_digest(c): {"source": str(source), "ts": now} for c in wanted}
        return _record_entries(ledger_path, "files", settings_key(settings_path), entries) == "ok"
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


def record_contexts(ledger_path, settings_path, contexts, source):
    """Record that Token Optimizer wrote these exact entries (full contexts)
    into ``settings_path``. Call after the settings write landed. Never raises."""
    return _contexts_status(ledger_path, settings_path, contexts, source) == "ok"


def _contexts_status(ledger_path, settings_path, contexts, source):
    try:
        wanted = [c for c in contexts if isinstance(c, tuple) and len(c) == 4]
        if not wanted:
            return "ok"
        now = int(time.time())
        entries = {context_digest(c): {"source": str(source), "ts": now, "cmd": command_digest(c[3])}
                   for c in wanted}
        return _record_entries(ledger_path, "contexts", settings_key(settings_path), entries)
    except Exception:
        return "failed"


def _record_failed(log, source, count, status, settings):
    """One heal-log line for a record that did not land in the ledger."""
    try:
        log("hook_provenance_record_failed", source=str(source), entries=count,
            queued=status == "queued", settings=str(settings),
            reason="queued beside the ledger; lands with the next ledger write" if status == "queued"
            else "not recorded; a later malformed copy will be kept, never removed")
    except Exception:
        pass


def added_contexts(before, after, commands):
    """Entries in ``after`` (a settings object) that are not in ``before``
    (``contexts_of`` the read it was derived from), counted as a multiset and
    limited to ``commands``, the exact strings this writer generated. So a
    preserved entry, foreign or ours, is never claimed: only what this write
    added."""
    wanted = set(c for c in commands if isinstance(c, str))
    remaining = dict(before or {})
    added = []
    for context in iter_contexts(after):
        if context[3] not in wanted:
            continue
        if remaining.get(context, 0) > 0:
            remaining[context] -= 1
            continue
        added.append(context)
    return added


def record_written(facade, before, after, commands, source):
    """Installer-side hook: record the entries a successful SETTINGS_PATH
    write added. ``before`` is ``contexts_of`` the read the payload came from,
    ``after`` the payload written."""
    try:
        contexts = added_contexts(before, after, commands)
        if not contexts:
            if has_pending(ledger_path_for(facade)):  # an earlier run's queued record lands now
                _update(ledger_path_for(facade), lambda data: False)
            return True
        status = "failed"
        if facade._settings_heal_ensure_dir() is not None:
            status = _contexts_status(ledger_path_for(facade), facade.SETTINGS_PATH, contexts, source)
        if status != "ok":
            _record_failed(facade._settings_heal_log, source, len(contexts), status, facade.SETTINGS_PATH)
        return status == "ok"
    except Exception:
        return False


def record_rewrites(facade, before, after, source):
    """For writers that substitute text INSIDE existing commands
    (_fix_stale_settings_paths): such output can be part foreign, so a
    rewritten entry is recorded only when the entry it replaced was already
    proven ours, in its context, under the same settings file. ``before`` and
    ``after`` must have the same shape (only command strings changed)."""
    try:
        prov = Provenance.from_facade(facade)
        contexts = []
        for old, new in zip(_hook_slots(before), _hook_slots(after)):
            if old[0] != new[0] or old[1] == new[1] or None in (old[1], new[1]):
                continue
            if new[1][:3] == old[1][:3] and prov.proof(old[1][3], old[1][:3]):
                contexts.append(new[1])
        if not contexts:
            return True
        status = "failed"
        if facade._settings_heal_ensure_dir() is not None:
            status = _contexts_status(prov.ledger_path, prov.settings_path, contexts, source)
        if status != "ok":
            _record_failed(facade._settings_heal_log, source, len(contexts), status, prov.settings_path)
        return status == "ok"
    except Exception:
        return False


def _hook_slots(settings):
    """``((event, group index, hook index), context or None)`` per hook slot."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for event, groups in hooks.items() if isinstance(hooks, dict) else ():
        for gi, group in enumerate(groups if isinstance(groups, list) else ()):
            entries = group.get("hooks") if isinstance(group, dict) else None
            for hi, hook in enumerate(entries if isinstance(entries, list) else ()):
                yield (event, gi, hi), entry_context(event, group, hook)


def record_cloud_write(settings_path, before, after, hook_map):
    """cloud_kit (python -I, no facade): record the entries its settings
    write added, in the ledger measure.py's healer reads for that profile:
    ``<claude dir>/token-optimizer/settings_heal``."""
    try:
        commands = [context[3] for context in iter_contexts({"hooks": hook_map})]
        contexts = added_contexts(contexts_of(before), after, commands)
        if not contexts:
            return True
        heal_dir = Path(settings_path).parent / "token-optimizer" / "settings_heal"
        heal_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(str(heal_dir), 0o700)
        status = _contexts_status(heal_dir / LEDGER_NAME, settings_path, contexts, "cloud_kit")
        if status != "ok":
            _record_failed(lambda action, **fields: _append_log(heal_dir, action, fields),
                           "cloud_kit", len(contexts), status, settings_path)
        return status == "ok"
    except Exception:
        return False


def _append_log(heal_dir, action, fields):
    """measure.py's heal_log.jsonl line format, for callers without a facade."""
    entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action}
    entry.update(fields)
    with open(str(Path(heal_dir) / "heal_log.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Provenance:
    """One heal pass's view: proven sets, ledger snapshot, verdicts."""

    def __init__(self, ledger_path, settings_path, proven_contexts, current_commands,
                 darwin, script_names=()):
        self.ledger_path = Path(ledger_path)
        self.settings_path = str(settings_path)
        self.key = settings_key(settings_path)
        self.proven_contexts = frozenset(proven_contexts)
        self.proven_templates = frozenset(c[3] for c in self.proven_contexts)
        self.current_commands = frozenset(current_commands)
        self.darwin = bool(darwin)
        self.script_names = tuple(script_names)
        ledger, self.status = load_ledger(self.ledger_path)
        ok = self.status == "ok"
        records = ledger["contexts"].get(self.key, {}) if ok else {}
        self._recorded = frozenset(records)
        self._recorded_commands = frozenset(v.get("cmd") for v in records.values())
        self._seeded = ok and self.key in ledger["contexts_seeded"]
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
        proven = instantiate_contexts(SHIPPED_HOOK_CONTEXTS, sorted(set(roots)), resolve)
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

    def proof(self, command, context=None):
        """"template", "ledger" or None for ``command`` in ``context``
        (``(event, matcher, type)``). Exact bytes of the whole tuple only.
        Without a context: proven in SOME context (diagnostics and
        ``_is_malformed_to_hook``; the healer always passes the context)."""
        if context is None:
            if command in self.proven_templates:
                return "template"
            return "ledger" if command_digest(command) in self._recorded_commands else None
        full = tuple(context) + (command,)
        if full in self.proven_contexts:
            return "template"
        if context_digest(full) in self._recorded:
            return "ledger"
        return None

    def verdict(self, command, context=None):
        if not isinstance(command, str) or not command:
            return KEEP
        if not looks_malformed(command, self.darwin):
            return KEEP
        if command in self.current_commands:
            return KEEP  # what setup_all_hooks writes now: never churn it
        if self.proof(command, context):
            return REMOVE
        return UNPROVEN

    def verdict_entry(self, event, group, hook):
        """The healer's verdict for one settings.json entry, in its context.
        An entry with no writable context is never proven."""
        command = hook.get("command", "") if isinstance(hook, dict) else ""
        context = entry_context(event, group, hook)
        if context is None:
            if not isinstance(command, str) or not command or command in self.current_commands \
                    or not looks_malformed(command, self.darwin):
                return KEEP
            return UNPROVEN
        return self.verdict(command, context[:3])

    def reportable(self, command):
        # Diagnostics only: the entries the old heuristic would have weighed.
        # Never consulted for a removal.
        return any(name in command for name in self.script_names)

    def finish(self, present, unproven, log=None):
        """After a heal pass: seed the ledger on first run (exact template
        matches only) and report each unproven entry once.

        ``present`` is every entry context left in settings; ``unproven`` is a
        list of ``(event, command)``. ``log(action, **fields)`` is the heal log.
        """
        try:
            if self.status == "unreadable":
                return
            seeds = [] if self._seeded else sorted(
                {c for c in present if c in self.proven_contexts}, key=repr)
            reports, seen = [], set()
            for event, command in unproven:
                digest = command_digest(command)
                if digest in self._reported or digest in seen or not self.reportable(command):
                    continue
                seen.add(digest)
                reports.append((event, command, digest))
            if self._seeded and not reports and self.status == "ok" \
                    and not has_pending(self.ledger_path):
                return
            key, now, logged = self.key, int(time.time()), []

            def mutate(data):
                changed = False
                if key not in data["contexts_seeded"]:
                    entries = data["contexts"].setdefault(key, {})
                    for context in seeds:
                        entries.setdefault(context_digest(context),
                                           {"source": "migration-seed", "ts": now,
                                            "cmd": command_digest(context[3])})
                    data["contexts_seeded"][key] = now
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


def write_codex_hooks(path, data, proof, writer, extra=(), source="codex_install"):
    """Codex installer: write hooks.json via ``writer`` (raises on failure, as
    before), then record in the ledger beside it every entry ``proof``
    (codex_hook_provenance.CodexProof) owns plus ``extra``, the entries
    (contexts) this run just rewrote."""
    writer(path, data)
    try:
        proof.record_present(data, extra, source)
    except Exception:
        pass
