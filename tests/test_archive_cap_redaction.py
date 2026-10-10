"""Archive size caps redact BEFORE cutting.

The PostToolUse archive cuts a response at 5MB; the Codex backfill archive cuts
output at the same width. Redaction ran on the cut text, so a secret straddling
the cap survived as a prefix no pattern recognises (``ghp_`` + 18 chars) and was
written to the archive entry. Redact first, then cut.

Secrets are assembled at runtime (push protection scans test files).
Run: python3 -m pytest tests/test_archive_cap_redaction.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

CAP = 5_242_880
TOKEN = "ghp_" + "Q7r8T9u0V1w2X3y4Z5a6B7c8D9e0F1g2H3i4"
FRAG = "Q7r8T9u0"
SID = "22222222-2222-2222-2222-222222222222"


def _tree_text(root: Path) -> str:
    chunks = []
    for p in root.rglob("*"):
        if p.is_file() and p.stat().st_size < 20_000_000:
            chunks.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def test_posttooluse_archive_redacts_secret_straddling_the_5mb_cap(tmp_path):
    body = "a" * (CAP - 18) + TOKEN + " tail\n" + "b" * 1000
    payload = {
        "tool_name": "mcp__demo__dump",
        "tool_use_id": "toolu_cap1",
        "session_id": SID,
        "tool_input": {},
        "tool_response": body,
    }
    env = dict(os.environ)
    env.update({"TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
                "HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude")})
    env.pop("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", None)
    out = subprocess.run([sys.executable, str(SCRIPTS / "archive_result.py")],
                         input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    blob = _tree_text(tmp_path)
    assert "TRUNCATED at 5MB" in blob, "the response was not archived (fixture did not reach the cap path)"
    assert FRAG not in blob, "a partial token straddling the 5MB cap was archived"
    assert "ghp_" not in blob
