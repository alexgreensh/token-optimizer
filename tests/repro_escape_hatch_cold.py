#!/usr/bin/env python3
"""K4-candidate repro: the escape hatch is cold in hook-per-process hosts.

`consecutive_denials` has no `file_reads` column, so it lives only in the
in-memory entry dict. Each PreToolUse hook is a fresh process, so the counter
reloads as 0 (via _ensure_entry_defaults) on every event and never reaches
MAX_CONSECUTIVE_DENIALS (3): the hatch that exists to bound denial loops can
never fire.

This spawns THREE fresh python processes, each running handle_read against the
same session with the same redundant-judged file, and reports the decisions.
Expected current behavior: 3 denials, 0 escapes. If a future fix persists the
counter, the THIRD process escapes and this repro flips.

Not collected by pytest (no test_ prefix); run directly:
  python3 tests/repro_escape_hatch_cold.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
SESSION = "k4-repro-session"

CHILD = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import read_cache, session_store
store = session_store.SessionStore(sys.argv[2])
entry = store.get_file_entry(sys.argv[1])
denials = int((entry or {}).get("consecutive_denials", 0) or 0)
hook = {
    "session_id": sys.argv[2],
    "transcript_path": sys.argv[4],
    "tool_name": "Read",
    "tool_input": {"file_path": sys.argv[1]},
}
read_cache.handle_read(hook, "block", True)
entry_after = store.get_file_entry(sys.argv[1])
print(json.dumps({
    "denials_at_judgment": denials,
    "row_present_after": entry_after is not None,
}))
"""

SEED = r"""
import os, sys, time
sys.path.insert(0, sys.argv[3])
import session_store
st = os.stat(sys.argv[1])
store = session_store.SessionStore(sys.argv[2])
store.upsert_file_entry(sys.argv[1], {
    "mtime_ns": st.st_mtime_ns, "size_bytes": st.st_size,
    "ranges_seen": [[0, 0]], "tokens_est": 4000, "read_count": 3,
    "last_access": time.time(),
})
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="k4-repro-"))
    env = dict(
        os.environ,
        TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(tmp),
        CLAUDE_CONFIG_DIR=str(tmp / "claude"),
        HOME=str(tmp),
        TOKEN_OPTIMIZER_RUNTIME="claude",
        TOKEN_OPTIMIZER_READ_CACHE_MODE="block",
    )
    (tmp / "claude").mkdir()
    target = tmp / "big.py"
    target.write_text("# comment\n" + "x = 1\n" * 4000, encoding="utf-8")
    transcript = tmp / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-11-06T10:00:00Z",
            "cwd": str(tmp),
            "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 1}},
        }) + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, "-c", SEED, str(target), SESSION, str(SCRIPTS)],
        env=env, check=True, capture_output=True,
    )

    decisions = []
    for i in range(3):
        proc = subprocess.run(
            [sys.executable, "-c", CHILD, str(target), SESSION, str(SCRIPTS), str(transcript)],
            env=env, capture_output=True, text=True,
        )
        escaped = "Escape hatch" in proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        decisions.append({
            "process": i + 1,
            "denials_at_judgment": result["denials_at_judgment"],
            "escaped": escaped,
        })

    print(json.dumps(decisions, indent=2))
    escapes = sum(1 for d in decisions if d["escaped"])
    zero_loads = sum(1 for d in decisions if d["denials_at_judgment"] == 0)
    print(
        f"RESULT: {len(decisions)} fresh processes, {zero_loads} judged with "
        f"counter=0, {escapes} escapes. "
        + ("HATCH COLD (counter never persists across processes)."
           if escapes == 0 and zero_loads == 3
           else "hatch fired - counter persistence has changed; re-triage K4.")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
