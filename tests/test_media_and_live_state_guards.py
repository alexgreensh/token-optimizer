"""Screenshots and other live reads must reach the model intact.

Three bugs, found driving a browser with Claude in Chrome:
1. archive_result serialized an image content block to JSON, so a screenshot's
   base64 crossed the 4KB archive threshold and the model got a text pointer
   instead of the image (and a fake "tokens saved" row: images are billed by
   pixels, not base64 length).
2. The PreToolUse re-fetch guard treated an identical call as a re-fetch for
   48h, so a second screenshot (same args, new page) was denied, and live reads
   like an inbox or chat query were redirected to stale archives.
3. With two Token Optimizer installs firing on one Bash call, cross-turn dedup
   saw its twin's record and said "identical to your previous output" about
   output the model had never seen.

These run the real hook scripts as subprocesses, the way the host does.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SID = "media-guard-session"
BIG_B64 = "iVBORw0KGgo" + "A" * 20000


def _env(tmp_path, **extra):
    env = {**os.environ, "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap")}
    env.pop("TOKEN_OPTIMIZER_REFETCH_GUARD_WINDOW_SECONDS", None)
    env.pop("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", None)
    env.update(extra)
    return env


def _hook(script, payload, tmp_path, **extra):
    p = subprocess.run([sys.executable, str(SCRIPTS / script), "--quiet"],
                       input=json.dumps(payload), capture_output=True, text=True,
                       env=_env(tmp_path, **extra), timeout=60)
    return p.stdout.strip()


def _archive_dir(tmp_path):
    return tmp_path / "snap" / "tool-archive" / SID


def _post(tool_name, tool_response, tool_use_id, tool_input=None):
    return {"hook_event_name": "PostToolUse", "session_id": SID,
            "tool_name": tool_name, "tool_use_id": tool_use_id,
            "tool_input": tool_input or {"tabId": 1, "action": "screenshot"},
            "tool_response": tool_response}


SCREENSHOT_SHAPES = {
    "anthropic": [
        {"type": "text", "text": "Successfully captured screenshot"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": BIG_B64}},
    ],
    "mcp": {"content": [{"type": "image", "data": BIG_B64, "mimeType": "image/png"}]},
    "mcp_resource_blob": [{"type": "resource", "resource": {"uri": "x://s", "mimeType": "image/png", "blob": BIG_B64}}],
}


# --- 1: media is never archived or replaced ------------------------------------

@pytest.mark.parametrize("shape", sorted(SCREENSHOT_SHAPES))
def test_screenshot_passes_through_untouched(tmp_path, shape):
    out = _hook("archive_result.py",
                _post("mcp__claude-in-chrome__computer", SCREENSHOT_SHAPES[shape], f"toolu_img_{shape}"),
                tmp_path)
    assert out == "", f"hook replaced a screenshot result:\n{out[:300]}"
    assert not (_archive_dir(tmp_path) / f"toolu_img_{shape}.json").exists()


def test_image_in_any_mcp_tool_passes_through(tmp_path):
    # Not only browser tools: any MCP returning an image must stay visible.
    out = _hook("archive_result.py",
                _post("mcp__somechart__render", SCREENSHOT_SHAPES["anthropic"], "toolu_chart"),
                tmp_path)
    assert out == ""


def test_large_text_mcp_result_is_still_archived(tmp_path):
    # Regression guard: the media skip must not switch off normal archiving.
    out = _hook("archive_result.py",
                _post("mcp__somechatty__list_issues", "issue line\n" * 2000, "toolu_text",
                      {"q": "x"}),
                tmp_path)
    assert "updatedMCPToolOutput" in out
    assert (_archive_dir(tmp_path) / "toolu_text.json").exists()


def test_live_state_archive_footer_does_not_forbid_recalling(tmp_path):
    out = _hook("archive_result.py",
                _post("mcp__claude-in-chrome__get_page_text", "page text\n" * 2000, "toolu_page"),
                tmp_path)
    replacement = json.loads(out)["hookSpecificOutput"]["updatedMCPToolOutput"]
    assert "Do NOT call" not in replacement
    assert "call the tool again for current state" in replacement


# --- 2: the re-fetch guard ------------------------------------------------------

def _seed_manifest(tmp_path, tool_name, tool_input, tool_use_id, age_seconds):
    from refetch_fingerprint import ARGS_HASH_KEY, tool_fingerprint
    d = _archive_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tool_use_id}.json").write_text(json.dumps({"response": "archived body"}))
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    entry = {"tool_name": tool_name, "tool_use_id": tool_use_id, "tokens_est": 5000,
             ARGS_HASH_KEY: tool_fingerprint(tool_name, tool_input), "timestamp": ts}
    with (d / "manifest.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _guard(tmp_path, tool_name, tool_input, **extra):
    out = _hook("refetch_guard.py", {"hook_event_name": "PreToolUse", "session_id": SID,
                                      "tool_name": tool_name, "tool_input": tool_input},
                tmp_path, **extra)
    return json.loads(out)["hookSpecificOutput"].get("permissionDecision")


def test_guard_never_blocks_live_state_tools_even_with_old_manifest_entry(tmp_path):
    # Manifests written by older versions carry fingerprints for browser tools;
    # the guard itself must still let the call through.
    args = {"tabId": 1, "action": "screenshot"}
    _seed_manifest(tmp_path, "mcp__claude-in-chrome__computer", args, "toolu_old", age_seconds=5)
    assert _guard(tmp_path, "mcp__claude-in-chrome__computer", args) is None


def test_guard_still_breaks_an_immediate_loop(tmp_path):
    args = {"q": "big"}
    _seed_manifest(tmp_path, "mcp__somechatty__list_issues", args, "toolu_fresh", age_seconds=10)
    assert _guard(tmp_path, "mcp__somechatty__list_issues", args) == "deny"


def test_guard_allows_a_later_recheck_of_live_data(tmp_path):
    # An hour later the same inbox/chat query is a deliberate re-check, not a loop.
    args = {"chat": "family", "limit": 50}
    _seed_manifest(tmp_path, "mcp__whatsapp__list_messages", args, "toolu_hour", age_seconds=3600)
    assert _guard(tmp_path, "mcp__whatsapp__list_messages", args) is None


def test_guard_window_is_configurable(tmp_path):
    args = {"q": "big"}
    _seed_manifest(tmp_path, "mcp__somechatty__list_issues", args, "toolu_w", age_seconds=120)
    assert _guard(tmp_path, "mcp__somechatty__list_issues", args,
                  TOKEN_OPTIMIZER_REFETCH_GUARD_WINDOW_SECONDS="60") is None


def test_guard_allows_entries_without_a_timestamp(tmp_path):
    from refetch_fingerprint import ARGS_HASH_KEY, tool_fingerprint
    args = {"q": "big"}
    d = _archive_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "toolu_nots.json").write_text(json.dumps({"response": "body"}))
    (d / "manifest.jsonl").write_text(json.dumps({
        "tool_name": "mcp__somechatty__list_issues", "tool_use_id": "toolu_nots",
        ARGS_HASH_KEY: tool_fingerprint("mcp__somechatty__list_issues", args)}) + "\n")
    assert _guard(tmp_path, "mcp__somechatty__list_issues", args) is None


def test_live_state_patterns():
    from refetch_fingerprint import is_live_state_tool
    for name in ("mcp__claude-in-chrome__computer", "mcp__Claude_in_Chrome__get_page_text",
                 "mcp__plugin_claude-in-chrome_claude-in-chrome__read_page", "mcp__chrome__find",
                 "mcp__plugin_playwright_playwright__browser_snapshot",
                 "mcp__puppeteer__screenshot", "mcp__Chrome-DevTools__take_snapshot"):
        assert is_live_state_tool(name), name
    for name in ("mcp__somechatty__list_issues", "mcp__whatsapp__list_messages", "Bash", ""):
        assert not is_live_state_tool(name), name


# --- 3: twin hook dispatch in cross-turn dedup ----------------------------------

@pytest.fixture()
def dedup(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "twin-session")
    for m in ("bash_compress_hook", "session_store", "delta_diff", "compression_log"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("bash_compress_hook")
    yield mod
    sys.modules.pop("bash_compress_hook", None)


OUT = "\n".join(f"line {i} of real output" for i in range(40))


def test_twin_dispatch_is_not_called_identical(dedup):
    assert dedup._crossturn_dedup("cat log", OUT, "toolu_same") is None  # install A
    assert dedup._crossturn_dedup("cat log", OUT, "toolu_same") is None  # install B, same call

def test_old_store_without_the_column_migrates(dedup, tmp_path, capsys):
    # Upgrade path for a user whose session DB predates last_tool_use_id and
    # still holds rows from the removed cross-turn dedupe (DECISION row 12).
    # Opening it must add the column, keep the old row intact (no crash, no
    # data loss), never serve a "same as before" reference from that row, and
    # /clear must still wipe it.
    import json
    import sqlite3
    from session_store import SessionStore
    store = SessionStore("twin-session")
    path = Path(store.db_path)
    store.close()
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE command_outputs (
        command_hash TEXT PRIMARY KEY, command_text TEXT NOT NULL,
        output_hash TEXT NOT NULL, output_chars INTEGER NOT NULL,
        compressed_output TEXT, timestamp REAL NOT NULL)""")
    conn.execute("INSERT INTO command_outputs VALUES ('h1', 'ls -la', 'o1', 900, NULL, 1.0)")
    conn.commit()
    conn.close()

    store = SessionStore("twin-session")
    store._connect()
    store.close()
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(command_outputs)")}
    rows = conn.execute("SELECT command_hash, command_text, last_tool_use_id FROM command_outputs").fetchall()
    conn.close()
    assert "last_tool_use_id" in cols
    assert rows == [("h1", "ls -la", "")]

    # The old row can never surface as a dedup reference any more.
    assert dedup._crossturn_dedup("ls -la", OUT, "toolu_a") is None
    assert dedup._crossturn_dedup("ls -la", OUT, "toolu_b") is None
    listing = "\n".join(f"-rw-r--r--  1 user staff {1000 + i:5d} Nov 23 10:{i % 60:02d} file_{i:03d}.txt"
                        for i in range(60)) + "\n"
    for tid in ("toolu_c", "toolu_d"):
        dedup._run({"tool_name": "Bash", "session_id": "twin-session", "tool_use_id": tid,
                    "tool_input": {"command": "ls -la"},
                    "tool_response": {"stdout": listing, "stderr": "", "exit_code": 0}})
        out = capsys.readouterr().out
        delivered = json.loads(out)["hookSpecificOutput"]["updatedToolOutput"]["stdout"] if out.strip() else listing
        assert "identical" not in delivered.lower() and "unchanged since" not in delivered.lower()

    store = SessionStore("twin-session")
    store.reset_context_state()
    store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM command_outputs").fetchone()[0] == 0
    conn.close()


def test_pre_fix_twin_without_tool_use_id_is_recognised(dedup, monkeypatch):
    # The stuck Cowork copy runs old code: its row has no last_tool_use_id.
    # A wide window keeps a slow runner's gap between write and check inside it.
    monkeypatch.setattr(dedup, "_LEGACY_TWIN_SECONDS", 60.0)
    from session_store import SessionStore
    from delta_diff import content_hash
    store = SessionStore("twin-session")
    store.insert_command_output(content_hash("cat log"), "cat log", content_hash(OUT), len(OUT), OUT)
    store.close()
    assert dedup._crossturn_dedup("cat log", OUT, "toolu_new") is None


def test_structured_data_mentioning_images_still_archives(tmp_path):
    # A Notion/Slack-style payload that DESCRIBES an image node is data, not media.
    payload = {"results": [{"type": "image", "image": {"url": "https://x/y.png"}, "id": i}
                           for i in range(300)]}
    out = _hook("archive_result.py",
                _post("mcp__notion__search", payload, "toolu_notion", {"q": "x"}), tmp_path)
    assert "updatedMCPToolOutput" in out


def test_read_tool_image_shape_is_media(tmp_path):
    from archive_result import _contains_media_block
    assert _contains_media_block({"type": "image", "file": {"base64": "AAA", "type": "image/png"}})
    assert not _contains_media_block({"type": "text", "file": {"content": "x"}})
