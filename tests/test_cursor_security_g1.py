#!/usr/bin/env python3
"""G1 security regressions: path confinement, secret persistence, perms.

Pinned contracts (all reproduced by independent review before the fix):
1. _read_checkpoint_bounded only reads paths resolving under
   <cursor_home>/token-optimizer - a forged tally can never pull an
   arbitrary local file into model context.
2. The observed-events ledger appends with O_NOFOLLOW and 0o600; the data
   dir is 0o700.
3. postToolUseFailure error text is redacted BEFORE truncation/persist,
   including token-only URL userinfo (https://TOKEN@host), and the field is
   DROPPED when the redactor fails (fail closed).
4. The read-cache fingerprint store never persists raw file content; a
   redactor failure degrades the entry to hash-only.

Run: python3 -m pytest tests/test_cursor_security_g1.py -v
"""

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_hook_bridge as bridge  # noqa: E402
import cursor_read_cache  # noqa: E402


@pytest.fixture()
def home(monkeypatch, tmp_path):
    cur = tmp_path / ".cursor"
    cur.mkdir()
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    return cur


class TestCheckpointConfinement:
    def test_arbitrary_local_file_refused(self, home, tmp_path):
        victim = tmp_path / "id_rsa_fake"
        victim.write_text("FAKE-PRIVATE-KEY-MATERIAL")
        assert bridge._read_checkpoint_bounded(str(victim)) is None

    def test_checkpoint_under_data_dir_accepted(self, home):
        cp = home / "token-optimizer" / "snapshot" / "checkpoints" / "cp.md"
        cp.parent.mkdir(parents=True)
        cp.write_text("real checkpoint body")
        out = bridge._read_checkpoint_bounded(str(cp))
        assert out is not None and "real checkpoint body" in out

    def test_parent_symlink_escape_refused(self, home, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "cp.md").write_text("escaped body")
        link = home / "token-optimizer" / "snapshot"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        assert bridge._read_checkpoint_bounded(str(link / "cp.md")) is None

    def test_dotdot_escape_refused(self, home, tmp_path):
        victim = tmp_path / "victim.md"
        victim.write_text("dotdot body")
        sneaky = str(home / "token-optimizer" / ".." / ".." / "victim.md")
        assert bridge._read_checkpoint_bounded(sneaky) is None


class TestTallySymlinkSafety:
    def test_symlinked_tally_never_clobbers_victim(self, home, tmp_path):
        """G1-F2 byte-level: a planted tally symlink must not redirect the
        write. Non-JSON victim so no merge can mask the clobber."""
        victim = tmp_path / "victim-notes"
        victim.write_text("PRECIOUS ORIGINAL BYTES - my notes")
        sdir = home / "token-optimizer" / "sessions"
        sdir.mkdir(parents=True)
        link = sdir / "convtally9.json"
        link.symlink_to(victim)
        fields = bridge.decode_payload({
            "conversation_id": "convtally9", "hook_event_name": "stop",
        })
        bridge._update_tally(fields, increment_turns=True)
        assert victim.read_text() == "PRECIOUS ORIGINAL BYTES - my notes"
        # The link itself is replaced by a real tally file (or removed):
        # either way the write never went through it.
        assert not link.is_symlink() or not link.exists()

    def test_symlinked_tally_reads_as_absent(self, home, tmp_path):
        victim = tmp_path / "evil.json"
        victim.write_text(json.dumps({"conversation_id": "convtally8", "tool_calls": 9999}))
        sdir = home / "token-optimizer" / "sessions"
        sdir.mkdir(parents=True)
        link = sdir / "convtally8.json"
        link.symlink_to(victim)
        assert bridge._load_tally(link) is None


def _run(fn, payload):
    """Call a bridge handler, capturing its stdout JSON envelope."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(payload)
    return json.loads(buf.getvalue() or "{}")


def _stage_rewrite(home, monkeypatch, conv, tuid, command):
    """Drive preToolUse as Cursor would so a real rewrite is staged."""
    monkeypatch.setattr(bridge, "_bash_hook", SimpleNamespace(
        _is_whitelisted=lambda c: True,
        _has_dangerous_chars=lambda c: False,
        _is_auto_approvable=lambda c: True,
    ))
    monkeypatch.setattr(bridge, "_COMPRESS_AVAILABLE", True)
    monkeypatch.setattr(bridge, "_COMPRESS_PATH", SCRIPTS / "bash_compress.py")
    out = _run(bridge.handle_pre_tool_use, {
        "conversation_id": conv,
        "tool_name": "Shell",
        "tool_use_id": tuid,
        "tool_input": {"command": command},
    })
    assert "bash_compress.py" in out["updated_input"]["command"]
    return out


class TestPendingRewriteSecretFree:
    def test_staged_rewrite_stores_hash_only(self, home, monkeypatch):
        """The rewritten command text never touches disk: a whitelisted
        command can still embed a credential in its arguments."""
        _stage_rewrite(home, monkeypatch, "convpendsec", "tool-use-secret-1",
                       "git status # S3CR3T-MARKER-9f8e7d")
        d = home / "token-optimizer" / "pending-rewrites"
        staged = list(d.glob("*.json"))
        assert len(staged) == 1
        raw = staged[0].read_bytes()
        assert b"S3CR3T-MARKER-9f8e7d" not in raw
        payload = json.loads(raw)
        assert set(payload) == {"sha256", "ts"}
        if os.name == "posix":
            assert stat.S_IMODE(staged[0].stat().st_mode) == 0o600

    def test_pending_rewrite_paired_by_session_and_tool_use_id(self, home, monkeypatch):
        """Two sessions sharing a tool_use_id never cross-pair."""
        _stage_rewrite(home, monkeypatch, "convpairea", "shared-tool-use-id-1", "git status")
        _stage_rewrite(home, monkeypatch, "convpaireb", "shared-tool-use-id-1", "git status")
        staged = list((home / "token-optimizer" / "pending-rewrites").glob("*.json"))
        assert len(staged) == 2
        names = {p.name for p in staged}
        assert any(n.startswith("convpairea--") for n in names)
        assert any(n.startswith("convpaireb--") for n in names)


class TestRewriteClassificationUnknown:
    def test_missing_executed_command_is_unknown_not_ignored(self, home, monkeypatch):
        """A staged rewrite plus absent/invalid postToolUse command data says
        nothing about what Cursor ran - never classify it as ignored."""
        _stage_rewrite(home, monkeypatch, "convunk1", "tool-use-unk-1", "git status")
        out = _run(bridge.handle_post_tool_use, {
            "conversation_id": "convunk1",
            "tool_name": "Shell",
            "tool_use_id": "tool-use-unk-1",
            "tool_input": {"working_directory": "/tmp"},  # no command key
        })
        ledger = (home / "token-optimizer" / "observed-events.jsonl")
        events = [json.loads(l) for l in ledger.read_text().splitlines()]
        posts = [e for e in events if e.get("event") == "postToolUse"]
        assert posts, "expected a postToolUse ledger event"
        assert all("rewrite" not in e for e in posts)


class TestLedgerHardening:
    def test_ledger_created_0600_and_dir_0700(self, home):
        bridge._record_observed("stop", conversation_id="conv-perms")
        ledger = home / "token-optimizer" / "observed-events.jsonl"
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
        assert stat.S_IMODE((home / "token-optimizer").stat().st_mode) == 0o700

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
    def test_symlinked_ledger_not_followed(self, home, tmp_path):
        victim = tmp_path / "victim.jsonl"
        victim.write_text("")
        (home / "token-optimizer").mkdir(parents=True, exist_ok=True)
        (home / "token-optimizer" / "observed-events.jsonl").symlink_to(victim)
        bridge._record_observed("stop", conversation_id="conv-link")
        assert victim.read_text() == ""  # append refused, victim untouched


class TestFailureErrorRedaction:
    def test_url_token_userinfo_redacted_before_persist(self, home):
        bridge.handle_post_tool_use_failure({
            "conversation_id": "conv-redact", "tool_name": "Shell",
            "error": "fatal: Authentication failed for "
                     "'https://ghp_SECRETTOKENLEAK123456@github.com/x/repo'",
        })
        obs = (home / "token-optimizer" / "observed-events.jsonl").read_text()
        assert "ghp_SECRETTOKENLEAK123456" not in obs

    def test_redactor_failure_drops_error_field(self, home, monkeypatch):
        import credential_patterns

        def boom(_text):
            raise RuntimeError("custom pattern blew up")

        monkeypatch.setattr(credential_patterns, "redact_credentials", boom)
        bridge.handle_post_tool_use_failure({
            "conversation_id": "conv-boom", "tool_name": "Shell",
            "error": "token ghp_SECRETTOKENLEAK123456 here",
        })
        obs = (home / "token-optimizer" / "observed-events.jsonl").read_text()
        assert "ghp_SECRETTOKENLEAK123456" not in obs
        entry = json.loads(obs.strip().splitlines()[-1])
        assert "error" not in entry


class TestReadCacheStoreRedaction:
    def test_store_never_persists_raw_content(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(cursor_read_cache, "_home", lambda: home)
        monkeypatch.setenv("HOME", str(tmp_path))
        secret_py = tmp_path / "module_with_secret.py"
        secret_py.write_text('API_KEY = "AKIAZZZZTOPSECRETKEY00"\n')
        fields = {
            "conversation_id": "conv-store",
            "tool_name": "Read",
            "tool_args": {"file_path": str(secret_py)},
        }
        cursor_read_cache.evaluate(fields)
        store = home / "token-optimizer" / "read-fingerprints" / "conv-store.json"
        assert store.exists()
        assert "AKIAZZZZTOPSECRETKEY00" not in store.read_text()

    def test_redactor_failure_degrades_to_hash_only(self, home, tmp_path, monkeypatch):
        import credential_patterns

        monkeypatch.setattr(credential_patterns, "redact_credentials",
                            lambda _t: (_ for _ in ()).throw(RuntimeError("boom")))
        text = cursor_read_cache._redact_for_storage("API_KEY = 'AKIAZZZZTOPSECRETKEY00'")
        assert text is None


class TestTokenUserinfoPattern:
    def test_token_only_userinfo_redacted(self):
        from credential_patterns import redact_credentials

        out = redact_credentials("see https://ghp_ABCDEFGHIJKLMNOP@github.com/o/r")
        assert "ghp_ABCDEFGHIJKLMNOP" not in out

    def test_short_username_and_plain_url_untouched(self):
        from credential_patterns import redact_credentials

        assert redact_credentials("https://user@example.com/x") == "https://user@example.com/x"
        assert redact_credentials("https://example.com/x") == "https://example.com/x"
