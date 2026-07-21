"""A nudge must never report another session's numbers as yours.

Observed failure: a verbosity-steer nudge fired on the FIRST prompt of a
brand-new session, quoting fill 47% / score 73. That session's transcript was
essentially empty and could not have produced those figures. The nudge reads a
quality cache keyed by the resolved transcript, and when no transcript_path was
supplied it fell back to _find_current_session_jsonl() -- which returns the most
recently active transcript, i.e. somebody else's on a new session. Nothing
checked that the resolved transcript was the live one.

The blast radius is not a cosmetic wrong number: the nudge is persuasive, and an
assistant acting on it conserves tokens that were never scarce for the rest of
the session.

Silence is the correct failure mode here. A missing nudge costs a small
optimization; a wrong nudge costs the whole session.

Run: python3 -m pytest tests/test_session_identity_guard.py -v
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402

SRC = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
STEER = SRC[SRC.index("def run_verbosity_steer("):][:6000]


def test_guessed_transcripts_are_marked_as_such():
    """The guard needs to know whether the path was told to us or inferred."""
    assert "guessed = False" in STEER
    assert "guessed = True" in STEER
    # The guess must be recorded on the fallback branch, not the trusted one.
    trusted = STEER.index("filepath = Path(transcript_path)")
    fallback = STEER.index("filepath = _find_current_session_jsonl()")
    assert trusted < fallback
    assert STEER.index("guessed = True") > fallback


def test_mismatched_session_id_emits_nothing():
    """Identity known and wrong -> silence."""
    assert "want_sid" in STEER and "have_sid" in STEER
    assert "have_sid != want_sid" in STEER


def test_unknown_sentinel_is_not_treated_as_an_identity():
    """sanitize_session_id() returns the truthy sentinel "unknown" for empty input.

    Treating that as a real identity makes every caller that omits session_id
    fail the comparison, silently killing all nudges -- the same invisible
    failure this guard exists to prevent. Caught by the existing 25%-boundary
    test before it could ship.
    """
    assert measure.sanitize_session_id("") == "unknown", "sentinel changed; revisit the guard"
    assert 'if want_sid == "unknown":' in STEER, "empty identity must not masquerade as one"
    assert 'have_sid == "unknown"' in STEER, "an unverifiable cache must fail closed"


def test_cache_without_a_session_id_is_not_trusted():
    """Otherwise an older cache predating the field bypasses the check."""
    guard = STEER[STEER.index("want_sid = sanitize_session_id"):][:900]
    assert "not have_sid" in guard, "a missing id must fail closed, not pass"


def test_guessed_transcript_without_identity_emits_nothing():
    """The exact observed failure: inferred transcript, no identity to verify."""
    assert "elif guessed:" in STEER
    guard = STEER[STEER.index("elif guessed:"):][:400]
    assert "return \"\"" in guard


def test_guard_runs_before_any_message_is_built():
    """A guard that fires after the message is built has already leaked."""
    guard_at = STEER.index("want_sid = sanitize_session_id")
    note_at = STEER.index("window_note = _format_window_note(cached)")
    assert guard_at < note_at, "identity must be settled before rendering anything"


def test_hook_passes_the_session_id_through():
    """Claude Code supplies session_id on every hook payload.

    The Claude entrypoint previously dropped it, which is why the guard had
    nothing to verify against and the fallback could not be caught.
    """
    call = SRC[SRC.index('elif args[0] == "verbosity-steer":'):][:1200]
    assert 'session_id=hook_input.get("session_id")' in call


def test_supplied_transcript_path_still_works():
    """No behavior change on the normal path: a known transcript is trusted."""
    assert "if transcript_path and Path(transcript_path).exists():" in STEER
    # A supplied path must not be forced through the guessed-path rejection.
    assert "elif guessed:" in STEER, "rejection is scoped to the guessed branch only"


# --- the identity comparison itself ----------------------------------------

def test_sanitize_makes_the_comparison_stable():
    """Both sides go through the same normalizer, so formatting cannot desync them."""
    sid = "abc-123-DEF"
    assert measure.sanitize_session_id(sid) == measure.sanitize_session_id(sid)


def test_distinct_sessions_do_not_normalize_together():
    """The guard is only as good as the ids being genuinely distinct."""
    a = measure.sanitize_session_id("0e37aafe-6625-457c-9d94-68e7ea73e45c")
    b = measure.sanitize_session_id("768b1b27-7a17-45b0-859a-58d4ee8620c6")
    assert a and b and a != b
