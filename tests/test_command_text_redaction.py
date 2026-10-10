"""Credentials typed into a Bash command must not reach the session store (review r2, F1).

Both writers of command text (the async PostToolUse archive and the sync
cross-turn dedup) redact the command with the one engine in credential_patterns
before the 500-char cut and the insert. Every secret is invented and assembled at
runtime. The store is read back table by table, FTS included.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

CASES = [
    ("curl -u deploy:" + "CurlPwFAKE9" + " https://example.com/x", "CurlPwFAKE9"),
    ("export DB_PASSWORD='correct " + "horse FAKE" + "' && ./migrate", "horse FAKE"),
    ("PASSWORD=" + "hunter2FAKE" + " ./deploy.sh", "hunter2FAKE"),
    ("API_KEY=" + "FAKEenvApiKeyQw7Lm3" + " make deploy", "FAKEenvApiKeyQw7Lm3"),
    ("git clone deploy:" + "GitFAKEpw9" + "@git.example.com:team/repo", "GitFAKEpw9"),
    ("mysql -u root -p" + "Sup3rS3cretFAKE" + " db", "Sup3rS3cretFAKE"),
    ("echo " + "PipedFAKEsecret77" + " | docker login --password-stdin", "PipedFAKEsecret77"),
    ("tool --token " + "FlagFAKEtoken5" + " run", "FlagFAKEtoken5"),
]
# A secret that straddles the 500-char cut must not survive as a fragment.
LONG = ("echo start; " + "x" * 470 + " API_KEY=" + "STRADDLEfakeKeyValue12345" + " end", "STRADDLEfake")


def _db_blobs(snap: Path):
    for dbp in (snap / "session-store").glob("*.db"):
        con = sqlite3.connect(dbp)
        try:
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
            for t in tables:
                try:
                    for row in con.execute(f"select * from {t}"):
                        yield t, " ".join(str(v) for v in row if v is not None)
                except sqlite3.DatabaseError:
                    continue
        finally:
            con.close()


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    snap = tmp_path / "snap"
    (home / ".claude").mkdir(parents=True)
    snap.mkdir()
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap)})
    env.pop("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", None)
    return env, snap


@pytest.mark.parametrize("command,secret", CASES + [LONG], ids=[c[1][:14] for c in CASES + [LONG]])
def test_archive_hook_stores_redacted_command(sandbox, command, secret):
    env, snap = sandbox
    payload = {
        "session_id": "sess-cmd",
        "tool_name": "Bash",
        "tool_use_id": "tu_cmd_1",
        "tool_input": {"command": command},
        "tool_response": "".join(f"row-{i:04d}: ok\n" for i in range(400)) + "END",
    }
    proc = subprocess.run([sys.executable, str(SCRIPTS / "archive_result.py")],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    blobs = list(_db_blobs(snap))
    assert any("tool_outputs" in t for t, _ in blobs), "hook stored nothing; test would pass vacuously"
    leaked = sorted({t for t, b in blobs if secret in b})
    assert not leaked, f"{secret!r} stored in {leaked}"


@pytest.mark.parametrize("command,secret", CASES + [LONG], ids=[c[1][:14] for c in CASES + [LONG]])
def test_sync_dedup_stores_redacted_command(sandbox, command, secret):
    env, snap = sandbox
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import bash_compress_hook as b\n"
        "b._crossturn_dedup(%r, 'row ok\\n' * 80, 'tu-1')\n" % (str(SCRIPTS), command)
    )
    env = dict(env, CLAUDE_SESSION_ID="sess-dedup")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    blobs = list(_db_blobs(snap))
    assert any("command_outputs" in t for t, _ in blobs), "dedup stored nothing; test would pass vacuously"
    leaked = sorted({t for t, b in blobs if secret in b})
    assert not leaked, f"{secret!r} stored in {leaked}"


def test_engine_command_mode_keeps_structure():
    sys.path.insert(0, str(SCRIPTS))
    import credential_patterns as cp
    out = cp.redact_credentials("curl -u deploy:" + "CurlPwFAKE9" + " https://example.com/x", command=True)
    assert out.startswith("curl -u ") and out.endswith(" https://example.com/x")
    assert "CurlPwFAKE9" not in out and "deploy" not in out
    # Plain output text is not command text: no flag/pipe rewriting without command=True.
    text = "usage: echo hi | wc -l  (see -u user:pass docs)"
    assert cp.redact_credentials(text) == text


@pytest.mark.parametrize("command,secret", CASES + [LONG], ids=[c[1][:14] for c in CASES + [LONG]])
def test_thrash_guard_streak_store_redacts_command(sandbox, command, secret):
    """The third command-text writer (streak store) shares the same engine."""
    env, snap = sandbox
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import thrash_guard as g\n"
        "for _ in range(3):\n"
        "    g.check(%r, 'row ok\\n' * 80)\n" % (str(SCRIPTS), command)
    )
    env = dict(env, CLAUDE_SESSION_ID="sess-thrash")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    blobs = list(_db_blobs(snap))
    assert any(t == "command_run_streaks" for t, _ in blobs), \
        "thrash guard stored nothing; test would pass vacuously"
    leaked = sorted({t for t, b in blobs if secret in b})
    assert not leaked, f"{secret!r} stored in {leaked}"
