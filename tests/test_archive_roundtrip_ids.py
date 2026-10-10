"""Archive id and round-trip fidelity (sweep findings F7, F9a, F9b, F9c).

F7  The Codex backfill keyed entries with a lossy sanitizer, so ``a b``, ``a.b``
    and ``a_b`` all landed on ``a_b`` and the later outputs were silently dropped.
F9a ``expand`` printed the stored text plus a newline, so it was not byte-identical.
F9b ``$`` in the key regex also matches before a trailing newline, so ``abc\\n``
    was treated as a safe key.
F9c A safe id longer than the filename limit could not be archived at all.

All values are made up. Run: python3 -m pytest tests/test_archive_roundtrip_ids.py -q
"""
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

SID = "22222222-2222-2222-2222-222222222222"
CODEX_SID = "sess-codex"


def _env(tmp_path):
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path / "home"),
                "USERPROFILE": str(tmp_path / "home"),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
                "TOKEN_OPTIMIZER_RUNTIME": "claude",
                "PYTHONIOENCODING": "utf-8"})
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def _archive(tmp_path, tool_use_id, body):
    payload = {"tool_name": "mcp__demo__dump", "session_id": SID,
               "tool_use_id": tool_use_id, "tool_input": {},
               "tool_response": body}
    p = subprocess.run([sys.executable, str(SCRIPTS / "archive_result.py")],
                       input=json.dumps(payload), text=True, capture_output=True,
                       env=_env(tmp_path), timeout=120)
    assert p.returncode == 0, p.stderr
    return p


def _expand_bytes(tmp_path, key, sid=SID):
    p = subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "expand", key,
                        "--session", sid],
                       capture_output=True, env=_env(tmp_path), timeout=120)
    return p


def _key_from(p):
    replacement = json.loads(p.stdout)["hookSpecificOutput"]["updatedMCPToolOutput"]
    return re.search(r"expand ([a-zA-Z0-9_-]+)", replacement).group(1), replacement


# --------------------------------------------------------------------- F7

_DRIVER = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, os.environ["SCRIPTS"])
    import codex_session, measure
    measure._use_codex_session_adapter = lambda filepath=None: True
    items = json.loads(os.environ["ITEMS"])
    codex_session.iter_tool_outputs = lambda path, *, min_chars=4096, max_outputs=20: list(items)
    path = os.path.join(os.environ["HOME"], "rollout-codex.jsonl")
    open(path, "w").write("{}\\n")
    n = measure._codex_backfill_tool_archive(filepath=path, session_id=os.environ["SID"], max_outputs=20)
    print(json.dumps({"written": n}))
''')


def _backfill(tmp_path, specs):
    items = [{"tool_use_id": tid, "output": (marker + "\n") * 400, "tool_name": "exec_command",
              "tool_type": "codex", "command_or_path": "cat big.log"}
             for tid, marker in specs]
    drv = tmp_path / "driver.py"
    drv.write_text(_DRIVER, encoding="utf-8")
    env = _env(tmp_path)
    env.update({"SCRIPTS": str(SCRIPTS), "ITEMS": json.dumps(items), "SID": CODEX_SID})
    p = subprocess.run([sys.executable, str(drv)], capture_output=True, text=True,
                       env=env, timeout=300)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])["written"]


def _entries(tmp_path):
    arch = tmp_path / "snap" / "tool-archive" / CODEX_SID
    return {f.name: json.loads(f.read_text(encoding="utf-8"))
            for f in arch.glob("*.json")} if arch.is_dir() else {}


def _markers(tmp_path):
    return sorted(d["response"].split("\n", 1)[0] for d in _entries(tmp_path).values())


def test_codex_backfill_keeps_ids_that_sanitize_alike(tmp_path):
    specs = [("a b", "FROM-A"), ("a.b", "FROM-B"), ("a_b", "FROM-C"), ("it's", "FROM-D"),
             ("x" * 100 + "1", "FROM-E"), ("x" * 100 + "2", "FROM-F")]
    assert _backfill(tmp_path, specs) == 6
    assert _markers(tmp_path) == ["FROM-A", "FROM-B", "FROM-C", "FROM-D", "FROM-E", "FROM-F"]
    for name in _entries(tmp_path):
        assert re.fullmatch(r"[a-zA-Z0-9_-]+\.json", name)
    # Every one is retrievable through the key that was written.
    for name, data in _entries(tmp_path).items():
        key = name[:-5]
        p = _expand_bytes(tmp_path, key, sid=CODEX_SID)
        assert p.returncode == 0, p.stderr
        assert p.stdout.decode("utf-8").startswith(data["response"].split("\n", 1)[0])


def test_codex_backfill_safe_id_keeps_its_own_name(tmp_path):
    assert _backfill(tmp_path, [("call_abc-1", "FROM-SAFE")]) == 1
    assert list(_entries(tmp_path)) == ["call_abc-1.json"]


def test_codex_backfill_is_idempotent_for_identical_payloads(tmp_path):
    specs = [("a b", "FROM-A"), ("call_1", "FROM-B")]
    assert _backfill(tmp_path, specs) == 2
    assert _backfill(tmp_path, specs) == 0
    assert len(_entries(tmp_path)) == 2


def test_codex_backfill_never_drops_a_different_payload_for_the_same_id(tmp_path):
    assert _backfill(tmp_path, [("call_1", "FIRST")]) == 1
    assert _backfill(tmp_path, [("call_1", "SECOND")]) == 1
    assert _markers(tmp_path) == ["FIRST", "SECOND"]
    for name in _entries(tmp_path):
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}\.json", name)
# --------------------------------------------------------------------- F9a

def test_expand_returns_stored_text_byte_for_byte(tmp_path):
    body = ("line one\nline two\n" * 400) + "no trailing newline"
    key, _ = _key_from(_archive(tmp_path, "tu_exact", body))
    p = _expand_bytes(tmp_path, key)
    assert p.returncode == 0, p.stderr
    out = p.stdout
    if os.linesep != "\n":  # Windows text mode turns \n into \r\n on the way out
        out = out.replace(os.linesep.encode(), b"\n")
    assert out == body.encode("utf-8")


# --------------------------------------------------------------------- F9b

def test_key_regex_rejects_trailing_newline():
    import archive_result
    assert archive_result._ARCHIVE_KEY_RE.match("abc\n") is None
    assert archive_result._safe_archive_key("abc\n") != "abc\n"
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", archive_result._safe_archive_key("abc\n"))


def test_archive_with_newline_id_prints_a_runnable_pointer(tmp_path):
    p = _archive(tmp_path, "abc\n", "payload\n" * 1000)
    key, replacement = _key_from(p)
    assert key != "abc"
    assert "\n" not in key
    assert (tmp_path / "snap" / "tool-archive" / SID / f"{key}.json").is_file()
    assert _expand_bytes(tmp_path, key).returncode == 0


def test_expand_rejects_trailing_newline_key(tmp_path):
    _archive(tmp_path, "abc", "payload\n" * 1000)
    # "abc\n" is not a valid key; it must not resolve to the "abc" entry.
    p = _expand_bytes(tmp_path, "abc\n")
    assert p.returncode != 0
    assert b"Invalid tool_use_id" in p.stderr


# --------------------------------------------------------------------- F9c

@pytest.mark.parametrize("n", [129, 300])
def test_overlong_safe_id_is_archived_under_a_digest(tmp_path, n):
    tid = "a" * n
    p = _archive(tmp_path, tid, "payload\n" * 1000)
    key, replacement = _key_from(p)
    assert key != tid and len(key) <= 128
    assert (tmp_path / "snap" / "tool-archive" / SID / f"{key}.json").is_file()
    assert "Failed to archive" not in p.stderr
    assert _expand_bytes(tmp_path, key).returncode == 0


def test_id_at_the_limit_keeps_its_own_name(tmp_path):
    import archive_result
    assert archive_result._safe_archive_key("a" * 128) == "a" * 128
    assert archive_result._safe_archive_key("a" * 129) != "a" * 129


def test_distinct_overlong_ids_do_not_collide():
    import archive_result
    a = archive_result._safe_archive_key("x" * 200 + "1")
    b = archive_result._safe_archive_key("x" * 200 + "2")
    assert a != b
