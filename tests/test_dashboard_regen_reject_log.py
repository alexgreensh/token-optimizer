"""A rejected regenerate click must leave a trace.

Root cause: the M-4 token check in
the dashboard daemon's do_POST runs BEFORE the api/regenerate handler. A wrong/
empty X-TO-Token was rejected with 403 before the handler ever ran, so
_log_regen was never called and daemon-regen.log stayed empty for weeks while
the user reported a dead button. The click was indistinguishable from nothing.

The fix adds a rate-limited `_log_reject_regen(path)` call on the token-reject
branch so a rejected api/* POST always writes one trace line (capped to one per
~30s so a looping client cannot spam the log).

These tests extract the shipped function out of the generated daemon source
(the same pattern test_daemon_measure_py_resolution.py uses for the resolver)
and assert both the logging behaviour and the do_POST wiring. They fail at edit
time if the reject trace is removed or moved out of the token-mismatch branch.
"""
from helpers.measure_split import measure_source as _split_measure_source

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _generated_src() -> str:
    import measure

    return measure._generate_daemon_script()


def _load_reject_logger(tmp_log: Path):
    """Exec the reject logger out of the generated daemon source.

    The logger lives inside a generated template, so it cannot be imported
    directly. Extracting it keeps the test bound to the shipped text rather than
    a copy that could drift away from what users actually run.
    """
    src = _generated_src()
    # _log_reject_regen now guards _REJECT_LOG_LAST_TS with
    # _STATE_LOCK; give the extracted function a real lock so it runs in-isolation.
    ns = {"time": time, "os": __import__("os"), "_STATE_LOCK": threading.Lock()}
    # _REJECT_LOG_LAST_TS is now a per-path dict with a type annotation
    # (`_REJECT_LOG_LAST_TS: dict = {}`), so allow an optional `: <type>`.
    for const in ("_REJECT_LOG_LAST_TS", "_REJECT_LOG_MIN_GAP", "_REJECT_LOG_MAX_KEYS", "LOG_CAP_BYTES"):
        m = re.search(r"^%s(?::[^=]+)? = .*$" % re.escape(const), src, re.M)
        assert m, f"{const} missing from generated daemon"
        exec(m.group(0), ns)
    ns["REGEN_LOG"] = str(tmp_log)
    # _log_reject_regen caps the log before appending.
    mc = re.search(r"^def _cap_log\(.*?\n(?=^\S)", src, re.M | re.S)
    assert mc, "_cap_log missing from generated daemon"
    exec(mc.group(0), ns)
    # _log_reject_regen now calls _sanitize_log_path — extract it into the ns too.
    ms = re.search(r"^def _sanitize_log_path\(.*?\n(?=^\S)", src, re.M | re.S)
    assert ms, "_sanitize_log_path missing from generated daemon"
    exec(ms.group(0), ns)
    m = re.search(r"^def _log_reject_regen\(.*?\n(?=^\S)", src, re.M | re.S)
    assert m, "_log_reject_regen missing from generated daemon"
    exec(m.group(0), ns)
    return ns


def test_reject_logger_writes_exactly_one_line(tmp_path):
    log = tmp_path / "daemon-regen.log"
    ns = _load_reject_logger(log)
    ns["_log_reject_regen"]("api/regenerate")
    text = log.read_text(encoding="utf-8")
    assert "REJECT api/* POST token-mismatch" in text
    assert "path=api/regenerate" in text
    assert text.count("\n") == 1, "first reject must write exactly one line"


def test_reject_logger_is_rate_limited(tmp_path):
    """A second call for the SAME path inside the min-gap window must not write
    another line. The throttle is now per-path, so a hammering client on one
    endpoint still gets exactly one line (a DIFFERENT path logging separately is
    the intended per-path behavior, covered by test_reject_log_throttle)."""
    log = tmp_path / "daemon-regen.log"
    ns = _load_reject_logger(log)
    ns["_log_reject_regen"]("api/regenerate")
    ns["_log_reject_regen"]("api/regenerate")  # same path, immediately after -> throttled
    text = log.read_text(encoding="utf-8")
    assert text.count("\n") == 1, (
        "per-path rate limit failed: a hammering client on one path wrote "
        f"{text.count(chr(10))} lines, expected 1"
    )


def test_reject_logger_never_raises_on_dead_log(tmp_path):
    """The daemon must survive an unwritable log path."""
    ns = _load_reject_logger(tmp_path / "no-such-dir" / "daemon-regen.log")
    ns["_log_reject_regen"]("api/regenerate")  # must not raise


def test_do_POST_calls_reject_logger_on_token_mismatch():
    """The reject trace must fire on the token-mismatch branch, before the 403."""
    src = _generated_src()
    # Locate the do_POST token check block.
    m = re.search(
        r"expected_tok = _read_token\(\).*?if not expected_tok or not hmac\.compare_digest\([^)]*\):\s*"
        r"(.*?)\n\s*self\.send_error\(403, \"Forbidden: invalid token\"\)",
        src, re.S,
    )
    assert m, "token-mismatch branch not found in generated do_POST"
    branch = m.group(1)
    assert "_log_reject_regen(" in branch, (
        "token-mismatch branch does not call _log_reject_regen before send_error(403)"
    )


def test_do_POST_does_not_log_reject_on_valid_token_path():
    """The reject logger must NOT be called outside the token-mismatch branch.

    A correct-token POST reaches the route handlers; it must not write a reject
    line. Assert the only call site is inside the mismatch guard.
    """
    src = _generated_src()
    call_sites = src.count("_log_reject_regen(")
    # One definition (def _log_reject_regen(path):) + one call in the mismatch
    # branch. Any more means the reject trace fires on the happy path.
    assert call_sites == 2, (
        f"expected exactly one call site (plus the def), found {call_sites - 1} "
        "calls -- a reject log on the valid-token path would be a false alarm"
    )


def test_reject_logger_mirrored_to_plugin_tree():
    """Both measure.py trees must carry the reject logger."""
    a = _split_measure_source(SCRIPTS / "measure.py")
    b = _split_measure_source(ROOT
        / "plugins" / "token-optimizer" / "skills" / "token-optimizer" / "scripts"
        / "measure.py")
    assert a == b, "measure.py drifted between the two install trees"
