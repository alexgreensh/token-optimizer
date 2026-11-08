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
