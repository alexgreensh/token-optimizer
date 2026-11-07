"""Cross-turn output dedup: a session-stateful compression a per-command tool
(e.g. Boost) structurally cannot do.

When the same read-only command is re-run in a session, Token Optimizer collapses
the repeat: identical output -> a one-line note, a small change -> just the diff.
Reuses the (previously dormant) command_outputs store + delta_diff. Fail-open and
self-sufficient (the caller attaches the progressive-disclosure pointer).
"""
import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def hook(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-xturn-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-xturn-session")
    sys.path.insert(0, str(SCRIPTS))
    for m in ("bash_compress_hook", "session_store", "delta_diff",
              "compression_log", "credential_patterns"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("bash_compress_hook")
    importlib.reload(mod)
    yield mod
    sys.modules.pop("bash_compress_hook", None)


def _trends_db_path(hook) -> Path:
    """Resolve the trends.db path for the current snapshot dir."""
    from compression_log import TRENDS_DB
    return Path(TRENDS_DB)


def _session_db_path(hook) -> Path:
    """Resolve the per-session command_outputs db path."""
    from session_store import SessionStore
    store = SessionStore("test-xturn-session")
    p = store.db_path
    store.close()
    return Path(p)


def _git_status(nfiles, branch="main"):
    base = [f"On branch {branch}", "Your branch is up to date.", "",
            "Changes not staged for commit:"]
    files = [f"\tmodified:   src/module_{i:02d}.py" for i in range(nfiles)]
    return "\n".join(base + files + ["", "no changes added, only modified files here"]) + "\n"

def test_first_run_is_not_deduped(hook):
    assert hook._crossturn_dedup("git status", _git_status(12)) is None

def test_first_run_returns_caller_compressed_preview_when_asked(hook):
    # Baseline-building call with a caller whose single-pass compression helped:
    # `output` is already the compressed preview, so hand it back rather than
    # dropping it and emitting raw.
    preview = _git_status(12)[:400]
    assert hook._crossturn_dedup(
        "git status", preview, prefer_compressed_on_first_run=True) == preview

def test_first_run_fallback_keeps_the_recorded_baseline_raw(hook):
    # The fallback must not change what later turns diff against: the store
    # keeps the raw (redacted) output as the baseline.
    raw = _git_status(12)
    preview = raw[:400]
    hook._crossturn_dedup("git status", preview, prefer_compressed_on_first_run=True)
    ref = hook._crossturn_dedup("git status", raw)
    assert ref is not None and "same as your previous" in ref.lower()

def test_identical_rerun_is_collapsed(hook):
    out = _git_status(12)
    assert hook._crossturn_dedup("git status", out) is None       # records
    ref = hook._crossturn_dedup("git status", out)                # identical
    assert ref is not None
    assert "identical" in ref.lower()
    assert len(ref) < len(out) * 0.5                              # big saving

def test_small_change_becomes_a_delta(hook):
    hook._crossturn_dedup("git status", _git_status(12))          # records
    ref = hook._crossturn_dedup("git status", _git_status(14))    # +2 files
    assert ref is not None
    assert "except" in ref.lower()
    assert len(ref) < len(_git_status(14)) * 0.85

def test_different_command_never_dedups(hook):
    hook._crossturn_dedup("git status", _git_status(12))
    other = "\n".join(f"-rw-r--r-- 1 u s {1000+i} module_{i:02d}.py" for i in range(30)) + "\n"
    assert hook._crossturn_dedup("ls -la", other) is None

def test_tiny_output_is_ignored(hook):
    # Below the 200-char floor -> not worth a reference.
    small = "On branch main\n"
    assert hook._crossturn_dedup("git status", small) is None
    assert hook._crossturn_dedup("git status", small) is None

def test_never_raises_without_session(hook, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert hook._crossturn_dedup("git status", _git_status(12)) is None


# ---------------------------------------------------------------------------
# C-1 / C-2 / C-4: credential redaction regression tests.
#
# PR #164 redacted the dedup STORE path but left three adjacent leaks in the
# same main() flow: the dedup `label` (embedded in the ref the model sees and
# logged to trends.db as compressed_text), the `command_pattern` column in
# trends.db's compression_events table, and zero tests pinning any of it. A
# regression that removes a `_redact_credentials` call would have passed the
# entire suite green. These tests pin all three surfaces so a secret-bearing
# command can never reach the model context or disk in cleartext.
# ---------------------------------------------------------------------------

# A realistic secret-bearing command. The Bearer token and the AWS access key
# are the two shapes the report called out explicitly.
_BEARER_SECRET = "sk-ant-BearerSecretTOKEN1234567890abcdefXYZ"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # well-known AWS docs example key (not live)
_CMD_WITH_BEARER = f'curl -H "Authorization: Bearer {_BEARER_SECRET}" https://api.example.com/v1/data'
_OUTPUT_WITH_AWS_KEY = (
    f"{{\n  \"status\": \"ok\",\n  \"key\": \"{_AWS_KEY}\",\n"
    f"  \"rows\": [\n" + "\n".join(f"    {{\"id\": {i}}}" for i in range(40)) + "\n  ]\n}\n"
)

def test_crossturn_dedup_command_text_redacted_in_store(hook):
    """C-4: a credential-bearing command must not survive in command_outputs.command_text."""
    hook._crossturn_dedup(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY)  # records
    db = _session_db_path(hook)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT command_text FROM command_outputs LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "dedup should have recorded the command"
    command_text = row[0] or ""
    assert _BEARER_SECRET not in command_text
    assert "Bearer " + _BEARER_SECRET not in command_text
    assert "CREDENTIAL REDACTED" in command_text

def test_crossturn_dedup_compressed_output_redacted_in_store(hook):
    """C-4: a credential-bearing output must not survive in command_outputs.compressed_output."""
    hook._crossturn_dedup(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY)  # records
    db = _session_db_path(hook)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT compressed_output FROM command_outputs LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    compressed_output = row[0] or ""
    assert _AWS_KEY not in compressed_output
    assert "CREDENTIAL REDACTED" in compressed_output

def test_crossturn_dedup_ref_label_does_not_leak_secret(hook):
    """C-1: the ref string returned to the model must not contain the raw secret.

    The label is embedded in the ref (``[Token Optimizer: identical to your
    previous `<label>` output ...]``) which becomes updatedToolOutput.stdout.
    A raw `label = command.strip()[:60]` would surface the Bearer token to the
    model AND to trends.db via compressed_text.
    """
    hook._crossturn_dedup(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY)  # records
    ref = hook._crossturn_dedup(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY)  # identical
    assert ref is not None
    assert _BEARER_SECRET not in ref
    assert "Bearer " + _BEARER_SECRET not in ref
    # The redacted label should carry the placeholder, proving safe_command was used.
    assert "CREDENTIAL REDACTED" in ref

def test_crossturn_dedup_delta_label_does_not_leak_secret(hook):
    """C-1 (delta path): the delta ref must also use the redacted label."""
    hook._crossturn_dedup(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY)  # records
    changed = _OUTPUT_WITH_AWS_KEY.replace("\"ok\"", "\"changed\"")
    ref = hook._crossturn_dedup(_CMD_WITH_BEARER, changed)  # delta
    assert ref is not None
    assert _BEARER_SECRET not in ref
    assert "Bearer " + _BEARER_SECRET not in ref
    assert "CREDENTIAL REDACTED" in ref

def test_log_event_command_pattern_redacted_in_trends_db(hook):
    """C-2: _log_event must redact command_pattern before it reaches trends.db.

    compression_events.command_pattern is persisted to disk in cleartext. A
    raw `command[:100]` would store the Bearer token and any other inline
    secret indefinitely.
    """
    hook._log_event(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY, "compressed-preview",
                    feature="crossturn_dedup")
    db = _trends_db_path(hook)
    assert db.exists(), "trends.db should have been created by _log_event"
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT command_pattern FROM compression_events "
            "WHERE feature = 'crossturn_dedup' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    command_pattern = row[0] or ""
    assert _BEARER_SECRET not in command_pattern
    assert "Bearer " + _BEARER_SECRET not in command_pattern
    assert "CREDENTIAL REDACTED" in command_pattern

def test_log_event_does_not_persist_raw_secret_in_any_column(hook):
    """C-2 (whole-row sweep): no column in compression_events may carry the raw
    secret. The table stores command_pattern (text) plus token counts and
    metadata; command_pattern is the only free-text column that could leak.
    This sweeps every TEXT column as a defense-in-depth guard against a future
    schema change adding another free-text column.
    """
    hook._log_event(_CMD_WITH_BEARER, _OUTPUT_WITH_AWS_KEY, "compressed-preview",
                    feature="crossturn_dedup")
    db = _trends_db_path(hook)
    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(compression_events)").fetchall()]
        row = conn.execute(
            "SELECT * FROM compression_events "
            "WHERE feature = 'crossturn_dedup' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    for col, val in zip(cols, row):
        if isinstance(val, str):
            assert _BEARER_SECRET not in val, f"raw secret leaked in column {col!r}"
            assert "Bearer " + _BEARER_SECRET not in val, f"Bearer leaked in column {col!r}"

def test_sibling_agents_do_not_share_dedup_history(hook, monkeypatch):
    """A reference is valid only inside the agent context that saw the output."""
    out = _git_status(12)
    monkeypatch.setenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", "agent-alpha")
    assert hook._crossturn_dedup("git status", out) is None

    monkeypatch.setenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", "agent-beta")
    assert hook._crossturn_dedup("git status", out) is None

    # Beta's own repeat still dedups, proving isolation did not disable the feature.
    ref = hook._crossturn_dedup("git status", out)
    assert ref is not None
    assert "identical" in ref.lower()

def test_same_agent_keeps_dedup_history(hook, monkeypatch):
    out = _git_status(12)
    monkeypatch.setenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", "agent-alpha")
    assert hook._crossturn_dedup("git status", out) is None
    assert hook._crossturn_dedup("git status", out) is not None


def test_main_and_agent_store_ids_are_stable_and_distinct(hook):
    session = "test-xturn-session"
    assert hook._dedup_store_id(session, "") == session
    assert hook._dedup_store_id(session, "agent-alpha") == hook._dedup_store_id(
        session, "agent-alpha")
    assert hook._dedup_store_id(session, "agent-alpha") != hook._dedup_store_id(
        session, "agent-beta")
    assert hook._dedup_store_id(session, "agent-alpha") != session

@pytest.mark.parametrize("previous", [None, "agent-stale"])
@pytest.mark.parametrize("raises", [False, True])
def test_hook_payload_overrides_and_restores_agent_context(
        hook, monkeypatch, previous, raises):
    """The current payload wins, and both success/exception restore prior state."""
    payload = {"session_id": "shared-session", "agent_id": "agent-new"}
    if previous is None:
        monkeypatch.delenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", raising=False)
    else:
        monkeypatch.setenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", previous)
    monkeypatch.setitem(sys.modules, "hook_io", SimpleNamespace(
        read_stdin_hook_input=lambda max_bytes: payload))
    seen = []

    def run(value):
        seen.append(__import__("os").environ["TOKEN_OPTIMIZER_DEDUP_AGENT_ID"])
        if raises:
            raise RuntimeError("test failure")

    monkeypatch.setattr(hook, "_run", run)
    if raises:
        with pytest.raises(RuntimeError, match="test failure"):
            hook.main()
    else:
        hook.main()

    assert seen == ["agent-new"]
    env = __import__("os").environ
    if previous is None:
        assert "TOKEN_OPTIMIZER_DEDUP_AGENT_ID" not in env
    else:
        assert env["TOKEN_OPTIMIZER_DEDUP_AGENT_ID"] == previous


def test_hook_payload_without_agent_uses_main_sentinel(hook, monkeypatch):
    seen = []
    payload = {"session_id": "shared-session"}
    monkeypatch.delenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", raising=False)
    monkeypatch.setitem(sys.modules, "hook_io", SimpleNamespace(
        read_stdin_hook_input=lambda max_bytes: payload))
    monkeypatch.setattr(hook, "_run", lambda value: seen.append(
        __import__("os").environ.get("TOKEN_OPTIMIZER_DEDUP_AGENT_ID")))

    hook.main()

    assert seen == [hook._MAIN_AGENT_SENTINEL]
    assert hook._dedup_store_id("shared-session", seen[0]) == "shared-session"


def test_whitespace_agent_id_falls_back_to_session_identity(hook):
    """A whitespace-only agent_id is not a real identity: it strips to nothing
    and must resolve to the session-only store, so a host that sends "  " cannot
    silently fork the main agent off its own dedup/streak history."""
    session = "shared-session"
    assert hook._dedup_store_id(session, "   ") == session
    assert hook._dedup_store_id(session, "\t\n ") == session
    # A real, non-whitespace id still forks off its own store.
    assert hook._dedup_store_id(session, "agent-alpha") != session


def test_similar_agent_ids_do_not_merge_stores(hook):
    """Opacity: a supplied agent_id is an OPAQUE identity, never normalized.

    ``.strip()`` is consulted only to decide emptiness. Two distinct-but-similar
    ids that differ ONLY in surrounding whitespace must therefore map to distinct
    stores -- collapsing them would silently merge two supplied identities, the
    exact over-reach this fix removes. All three forms are also distinct from the
    bare session (they are real, non-empty ids)."""
    session = "test-xturn-session"
    padded_both = hook._dedup_store_id(session, " agent-A ")
    bare = hook._dedup_store_id(session, "agent-A")
    trailing = hook._dedup_store_id(session, "agent-A ")
    # All three are pairwise DISTINCT (no accidental merge).
    assert len({padded_both, bare, trailing}) == 3
    # None collapses onto the bare session identity.
    assert padded_both != session
    assert bare != session
    assert trailing != session
    # Same id repeated is still stable (opacity is deterministic, not random).
    assert hook._dedup_store_id(session, " agent-A ") == padded_both


def test_hook_payload_preserves_unstripped_agent_identity(hook, monkeypatch):
    """main() must carry a non-empty agent_id through UN-stripped, so " agent-A"
    (padded) and "agent-A" (bare) resolve to DISTINCT stores end-to-end."""
    session = "shared-session"

    def context_for(agent_id):
        seen = []
        payload = {"session_id": session, "agent_id": agent_id}
        monkeypatch.delenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", raising=False)
        monkeypatch.setitem(sys.modules, "hook_io", SimpleNamespace(
            read_stdin_hook_input=lambda max_bytes: payload))
        monkeypatch.setattr(hook, "_run", lambda value: seen.append(
            __import__("os").environ.get("TOKEN_OPTIMIZER_DEDUP_AGENT_ID")))
        hook.main()
        return seen[0]

    padded_ctx = context_for(" agent-A")
    bare_ctx = context_for("agent-A")
    # The context env value is the original, un-stripped id.
    assert padded_ctx == " agent-A"
    assert bare_ctx == "agent-A"
    # And they resolve to distinct stores, neither equal to the bare session.
    assert hook._dedup_store_id(session, padded_ctx) != hook._dedup_store_id(
        session, bare_ctx)
    assert hook._dedup_store_id(session, padded_ctx) != session
    assert hook._dedup_store_id(session, bare_ctx) != session


@pytest.mark.parametrize("agent_id", [None, "", "   "])
def test_hook_payload_blank_agent_falls_back_to_main_sentinel(
        hook, monkeypatch, agent_id):
    """agent_id of None, "", or whitespace resolves to the main sentinel, whose
    store id is the bare session id (no cross-agent forking)."""
    seen = []
    payload = {"session_id": "shared-session"}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    else:
        payload["agent_id"] = None
    monkeypatch.delenv("TOKEN_OPTIMIZER_DEDUP_AGENT_ID", raising=False)
    monkeypatch.setitem(sys.modules, "hook_io", SimpleNamespace(
        read_stdin_hook_input=lambda max_bytes: payload))
    monkeypatch.setattr(hook, "_run", lambda value: seen.append(
        __import__("os").environ.get("TOKEN_OPTIMIZER_DEDUP_AGENT_ID")))

    hook.main()

    assert seen == [hook._MAIN_AGENT_SENTINEL]
    assert hook._dedup_store_id("shared-session", seen[0]) == "shared-session"
