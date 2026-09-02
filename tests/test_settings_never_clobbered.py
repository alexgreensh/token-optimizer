#!/usr/bin/env python3
"""settings.json must never lose a top-level key by accident.

THE BUG THIS PINS
-----------------
For three days a user's ``~/.claude/settings.json`` repeatedly collapsed to
exactly 230 bytes holding exactly two keys::

    {
      "cleanupPeriodDays": 99999,
      "statusLine": {"type": "command", "command": "node '.../statusline.js'"}
    }

Every artifact was byte-identical (md5 ``208f833588d58d4c56f675fb6a99951b``),
which rules out a race between two stale in-memory copies (that varies) and
points at a deterministic reconstruct-from-a-degraded-read.

Root cause: ``setup_quality_bar`` opened with the LOSSY ``_read_settings_json()``,
which collapses a missing / malformed / unreadable file to ``{}``, then wrote
that dict back. Under a PLUGIN install the UserPromptSubmit cache hook is
deliberately skipped (it comes from hooks.json, GitHub #7), so the only key the
rebuild wrote was ``statusLine`` -- which is exactly why the wipe artifact has
no ``hooks`` key. ``run_ensure_health`` calls ``setup_quality_bar(quiet=True)``
on EVERY SessionStart for a plugin user whose settings.json has no cache hook,
which matches the observed every-4-8-minutes clustering during session spawns.

#106 added ``_read_settings_json_checked()`` for exactly this class but
converted only 2 of 20 ``_write_settings_atomic`` call sites, so the class
stayed live.

THE FIX THIS PINS
-----------------
1. ``_settings_write_guard`` -- ONE choke point inside
   ``_write_settings_atomic_locked``. It re-reads the real on-disk file and
   REFUSES any write that drops a top-level key, unless the caller declares the
   removal via ``allow_removing_keys=``. Refusal is loud on stderr.
2. ``_read_settings_for_write`` -- a write-oriented reader where a MISSING file
   is ``ok=False``, because settings.json existing is the normal state.
3. Every unsafe call site converted to a checked read that branches on ``ok``.

Run: python3 -m pytest tests/test_settings_never_clobbered.py -q
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

# The degraded two-key residue a clobbering write leaves behind, reconstructed.
WIPE_MD5 = "0cb2ca08f8a3bcc9d5bb23250cd77fcc"
WIPE_STATUSLINE_CMD = (
    "node '/home/example/project/skills/token-optimizer/scripts/statusline.js'"
)

# A realistic full settings.json: every top-level key class a clobber can drop.
FULL_SETTINGS = {
    "cleanupPeriodDays": 99999,
    "statusLine": {"type": "command", "command": WIPE_STATUSLINE_CMD},
    "hooks": {
        "SessionEnd": [{"hooks": [{"type": "command", "command": "tr-extract.sh"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "proactive-recall.sh"}]}],
    },
    "enabledPlugins": {"token-optimizer@alexgreensh-token-optimizer": True},
    "env": {"MY_KEY": "keep-me"},
    "permissions": {"allow": ["Bash(ls:*)"]},
    "mcpServers": {"tavily": {"command": "npx", "args": ["tavily-mcp"]}},
    "model": "opus",
    "effortLevel": "high",
    "extraKnownMarketplaces": {"10x-company": {"source": "x"}},
    "compactInstructions": "keep the plan",
    "tui": {"theme": "dark"},
    "voice": "alloy",
    "voiceEnabled": True,
    "agentPushNotifEnabled": True,
    "showClearContextOnPlanAccept": False,
    "skipDangerousModePermissionPrompt": True,
}


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    """Load measure.py against a throwaway CLAUDE_DIR. Never touches ~/.claude."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)

    home = tmp_path / "claude"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    # Make _is_plugin_installed() true: this is the user's actual topology, and
    # it is what makes the wipe write ONLY statusLine.
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@alexgreensh-token-optimizer": {}}}),
        encoding="utf-8",
    )
    settings = home / "settings.json"
    settings.write_text(json.dumps(FULL_SETTINGS, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    yield mod, settings
    sys.modules.pop("measure", None)


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

def test_guard_refuses_a_write_that_drops_keys(measure, capsys):
    mod, settings = measure
    assert mod._write_settings_atomic({"statusLine": FULL_SETTINGS["statusLine"]}) is False
    err = capsys.readouterr().err
    assert "REFUSED settings.json write" in err
    assert "would DROP top-level key(s)" in err
    assert "mcpServers" in err and "permissions" in err
    assert _read(settings) == FULL_SETTINGS, "the on-disk file was modified by a refused write"


def test_guard_allows_a_pure_addition(measure):
    mod, settings = measure
    payload = dict(FULL_SETTINGS)
    payload["newThing"] = 1
    assert mod._write_settings_atomic(payload) is True
    assert _read(settings)["newThing"] == 1
    assert set(_read(settings)) == set(FULL_SETTINGS) | {"newThing"}


def test_guard_allows_a_value_change(measure):
    mod, settings = measure
    payload = dict(FULL_SETTINGS, model="sonnet")
    assert mod._write_settings_atomic(payload) is True
    assert _read(settings)["model"] == "sonnet"


def test_guard_allows_a_declared_removal(measure):
    """A deliberate key removal must still work when it is declared."""
    mod, settings = measure
    payload = {k: v for k, v in FULL_SETTINGS.items() if k != "statusLine"}
    assert mod._write_settings_atomic(payload, allow_removing_keys={"statusLine"}) is True
    on_disk = _read(settings)
    assert "statusLine" not in on_disk
    assert set(on_disk) == set(FULL_SETTINGS) - {"statusLine"}


def test_declared_removal_does_not_license_other_removals(measure, capsys):
    """The opt-in is per-key: declaring statusLine must not permit dropping env."""
    mod, settings = measure
    payload = {k: v for k, v in FULL_SETTINGS.items() if k not in ("statusLine", "env")}
    assert mod._write_settings_atomic(payload, allow_removing_keys={"statusLine"}) is False
    err = capsys.readouterr().err
    assert "env" in err
    assert _read(settings) == FULL_SETTINGS


def test_guard_refuses_when_on_disk_file_is_malformed(measure, capsys):
    mod, settings = measure
    settings.write_text('{"model": "opus", ', encoding="utf-8")  # truncated
    assert mod._write_settings_atomic({"statusLine": FULL_SETTINGS["statusLine"]}) is False
    err = capsys.readouterr().err
    assert "malformed" in err
    assert settings.read_text(encoding="utf-8") == '{"model": "opus", ', "malformed file was overwritten"


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX mode bits; root ignores file permissions",
)
def test_guard_refuses_when_on_disk_file_is_unreadable(measure, capsys):
    mod, settings = measure
    os.chmod(settings, 0o000)
    try:
        assert mod._write_settings_atomic({"statusLine": FULL_SETTINGS["statusLine"]}) is False
        err = capsys.readouterr().err
        assert "unreadable" in err
    finally:
        os.chmod(settings, 0o644)
    assert _read(settings) == FULL_SETTINGS


def test_guard_allows_creating_a_genuinely_missing_file(measure):
    """A first-ever install must still be able to create settings.json."""
    mod, settings = measure
    settings.unlink()
    assert mod._write_settings_atomic({"statusLine": FULL_SETTINGS["statusLine"]}) is True
    assert _read(settings) == {"statusLine": FULL_SETTINGS["statusLine"]}


def test_guard_refuses_a_non_dict_payload(measure, capsys):
    mod, settings = measure
    assert mod._write_settings_atomic(["not", "a", "dict"]) is False
    assert "not a dict" in capsys.readouterr().err
    assert _read(settings) == FULL_SETTINGS


def test_guard_checks_through_a_symlink(measure, tmp_path):
    """SETTINGS_PATH may be a dotfiles symlink; the guard must compare against
    the REAL file os.replace lands on, not the link path."""
    mod, settings = measure
    real = tmp_path / "real-settings.json"
    real.write_text(json.dumps(FULL_SETTINGS, indent=2) + "\n", encoding="utf-8")
    settings.unlink()
    settings.symlink_to(real)
    assert mod._write_settings_atomic({"statusLine": FULL_SETTINGS["statusLine"]}) is False
    assert _read(real) == FULL_SETTINGS
    assert settings.is_symlink(), "the refused write detached the symlink"


# ---------------------------------------------------------------------------
# The write-oriented reader
# ---------------------------------------------------------------------------

def test_read_for_write_rejects_missing_file_by_default(measure):
    mod, settings = measure
    settings.unlink()
    data, ok = mod._read_settings_for_write()
    assert (data, ok) == ({}, False)
    # ...while the plain checked reader still calls a missing file "ok" (read-only use).
    assert mod._read_settings_json_checked()[2] is True


def test_read_for_write_allows_missing_file_when_opted_in(measure):
    mod, settings = measure
    settings.unlink()
    assert mod._read_settings_for_write(allow_missing=True) == ({}, True)


def test_read_for_write_rejects_malformed(measure):
    mod, settings = measure
    settings.write_text("{oops", encoding="utf-8")
    assert mod._read_settings_for_write() == ({}, False)


# ---------------------------------------------------------------------------
# The actual regression: reconstruct the exact 230-byte artifact
# ---------------------------------------------------------------------------

def test_exact_230_byte_wipe_signature_is_rejected(measure, capsys):
    """Direct regression on the observed artifact.

    Reconstruct the exact degraded residue a clobbering write leaves behind, and
    assert the guard refuses to let it land over a healthy settings.json.
    """
    mod, settings = measure
    wipe = {
        "cleanupPeriodDays": 99999,
        "statusLine": {"type": "command", "command": WIPE_STATUSLINE_CMD},
    }
    wipe_bytes = (json.dumps(wipe, indent=2, ensure_ascii=False) + "\n").encode()
    # Sanity: the residue really is just the two machine-written keys.
    assert set(wipe) == {"cleanupPeriodDays", "statusLine"}, sorted(wipe)
    assert hashlib.md5(wipe_bytes).hexdigest() == WIPE_MD5

    assert mod._write_settings_atomic(wipe) is False
    err = capsys.readouterr().err
    assert "REFUSED settings.json write" in err
    for lost in ("hooks", "enabledPlugins", "env", "permissions", "mcpServers",
                 "model", "effortLevel", "tui", "voiceEnabled"):
        assert lost in err, f"{lost} not named in the refusal"
    assert _read(settings) == FULL_SETTINGS
    assert settings.read_bytes() != wipe_bytes


def test_setup_quality_bar_does_not_wipe_on_unreadable_settings(measure, capsys):
    """The proven live path.

    BEFORE the fix this call left a 200-byte file containing only `statusLine`,
    destroying 16 of 17 top-level keys.
    """
    mod, settings = measure
    os.chmod(settings, 0o000)
    try:
        mod.setup_quality_bar(quiet=True)
    finally:
        os.chmod(settings, 0o644)
    on_disk = _read(settings)
    assert on_disk == FULL_SETTINGS, (
        "setup_quality_bar rebuilt settings.json from a degraded read; "
        f"survivors={sorted(on_disk)}"
    )


def test_setup_quality_bar_does_not_wipe_on_malformed_settings(measure):
    mod, settings = measure
    raw = '{"model": "opus", "env": {"A"'
    settings.write_text(raw, encoding="utf-8")
    mod.setup_quality_bar(quiet=True)
    assert settings.read_text(encoding="utf-8") == raw, "malformed settings.json was rebuilt"


def test_setup_quality_bar_does_not_invent_a_file_when_missing(measure):
    """A transient missing-file window must not become a fresh 1-key install."""
    mod, settings = measure
    settings.unlink()
    mod.setup_quality_bar(quiet=True)
    assert not settings.exists(), (
        "an automated self-heal re-created settings.json from scratch during a "
        "missing-file window; that is how a full file becomes a two-key stub"
    )


def test_setup_quality_bar_retries_stale_read_as_a_merge(measure, monkeypatch, capsys):
    """Quality Bar must install after a concurrent top-level settings addition."""
    mod, settings = measure
    settings.write_text(json.dumps({"cleanupPeriodDays": 99999}, indent=2) + "\n", encoding="utf-8")
    real_read = mod._read_settings_for_write
    injected = {"done": False}

    def stale_read(*args, **kwargs):
        data, ok = real_read(*args, **kwargs)
        if ok and not injected["done"]:
            injected["done"] = True
            concurrent = {"cleanupPeriodDays": 99999, "permissions": {"allow": ["Bash(ls:*)"]}}
            settings.write_text(json.dumps(concurrent, indent=2) + "\n", encoding="utf-8")
        return data, ok

    monkeypatch.setattr(mod, "_read_settings_for_write", stale_read)
    mod.setup_quality_bar(quiet=True)
    on_disk = _read(settings)
    assert "statusLine" in on_disk
    assert on_disk["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert "REFUSED settings.json write" not in capsys.readouterr().err


def test_ensure_health_cleanup_period_does_not_rebuild_from_empty(measure):
    mod, settings = measure
    settings.write_text("{ not json", encoding="utf-8")
    data, ok = mod._read_settings_for_write()
    assert ok is False and data == {}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_writers_never_drop_a_key(measure):
    """N threads each add their own key. Whoever loses the lease no-ops; nobody
    may ever land a file that is missing a key that was on disk."""
    mod, settings = measure
    errors: list[str] = []

    def worker(i: int) -> None:
        for _ in range(6):
            try:
                cur, ok = mod._read_settings_for_write()
                if not ok:
                    continue
                cur = dict(cur)
                cur[f"worker{i}"] = i
                mod._write_settings_atomic(cur)
                on_disk = _read(settings)
                missing = set(FULL_SETTINGS) - set(on_disk)
                if missing:
                    errors.append(f"worker{i} observed missing keys: {sorted(missing)}")
            except Exception as e:  # pragma: no cover
                errors.append(f"worker{i}: {e!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    on_disk = _read(settings)
    assert set(FULL_SETTINGS) <= set(on_disk), (
        f"keys lost under concurrency: {sorted(set(FULL_SETTINGS) - set(on_disk))}"
    )


def test_stale_payload_retries_as_a_fresh_merge(measure):
    """A caller's pre-lease read must not turn a concurrent addition into refusal."""
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok

    concurrent = dict(FULL_SETTINGS)
    concurrent["newConcurrentKey"] = {"keep": True}
    settings.write_text(json.dumps(concurrent, indent=2) + "\n", encoding="utf-8")

    assert mod._write_settings_atomic(stale) is True
    on_disk = _read(settings)
    assert on_disk["newConcurrentKey"] == {"keep": True}
    assert on_disk["permissions"] == FULL_SETTINGS["permissions"]


def test_cleanup_period_and_statusline_writers_merge_stale_reads(measure):
    """Concurrent writers must both land their keys despite pre-lease reads."""
    mod, settings = measure
    settings.write_text("{}\n", encoding="utf-8")
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    writers = {
        "cleanupPeriodDays": 99999,
        "statusLine": {"type": "command", "command": "node statusline.js"},
    }

    def writer(key, value):
        try:
            stale, ok = mod._read_settings_for_write()
            assert ok
            stale = dict(stale)
            stale[key] = value
            barrier.wait(timeout=3)
            results[key] = mod._write_settings_atomic(stale)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(f"{key}: {exc!r}")

    threads = [
        threading.Thread(target=writer, args=(key, value))
        for key, value in writers.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    assert results == {"cleanupPeriodDays": True, "statusLine": True}
    on_disk = _read(settings)
    assert on_disk == writers


def test_every_atomic_settings_write_consumes_its_result():
    """No caller may report or assume success after a refused write."""
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_write_settings_atomic":
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Expr):
            offenders.append(f"line {node.lineno}")
    assert not offenders, "ignored _write_settings_atomic result at " + ", ".join(offenders)


def test_no_write_site_uses_the_lossy_reader(measure):
    """Static guard: keep the class fixed.

    Every ``_write_settings_atomic`` call site must have a CHECKED or
    write-oriented read nearby, never the lossy ``_read_settings_json()``.
    """
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8").split("\n")
    offenders = []
    for i, line in enumerate(src, 1):
        if "_write_settings_atomic" not in line:
            continue
        if line.strip().startswith("#") or "def _write_settings_atomic" in line:
            continue
        nearest = None
        for j in range(max(0, i - 70), i):
            s = src[j]
            if "_read_settings_for_write(" in s:
                nearest = "safe"
            elif "_read_settings_json_checked()" in s:
                nearest = "safe"
            elif "_read_settings_json()" in s:
                nearest = "lossy"
        if nearest == "lossy":
            offenders.append(f"line {i}: {line.strip()}")
    assert not offenders, "lossy read feeding a settings write:\n" + "\n".join(offenders)
