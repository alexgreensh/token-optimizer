#!/usr/bin/env python3
"""GitHub #127 — session continuity is blind to non-English prompts.

The topic tokenizer extracted words with ``[a-zA-Z0-9_./:-]+``, which matches
nothing above U+007F. Korean/Chinese/Japanese prompts tokenized to ``[]`` and
scored a hard ``0.0`` before any threshold was consulted; accented Latin split
at the accent (``módulo`` -> ``dulo``). The fix is a single shared tokenizer
(``_topic_tokens`` = ``_TOPIC_TOKEN_RE`` + a script-aware length floor) used at
all three topic-scoring sites (``_resume_topic_score`` x2, ``keyword_relevance_score``).

Two design points the gauntlet pinned:
  * The floor is script-aware: CJK (>= U+3000) is kept at len>=2 because a 2-char
    Hangul/Han word (결제, 모듈) carries a full topic; ASCII/accented Latin keep len>3.
  * ``_RECOVER_TOKEN_RE`` is deliberately NOT widened — doing so makes non-Latin items
    produce 3+ tokens that fail the ASCII-only keep-set overlap test and drop needed lines.

Tests assert against the PRODUCTION symbols (``mod._TOPIC_TOKEN_RE`` / ``mod._topic_tokens``),
never a re-declared copy, so a drift in the shipped regex turns them red. The parity fixture
(``tests/fixtures/i18n_topic_score_parity.json``) is the same JSON asserted by the OpenClaw
and OpenCode suites.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

with open(REPO / "tests" / "fixtures" / "i18n_topic_score_parity.json", encoding="utf-8") as _f:
    I18N_FIXTURE = json.load(_f)


def _measure():
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    return importlib.import_module("measure")


def test_resume_topic_score_i18n_parity_fixture():
    """_resume_topic_score's match/no-match must equal the shared fixture for every row —
    the same rows the OpenClaw and OpenCode suites assert, so all three runtimes agree."""
    mod = _measure()
    tmp = Path(tempfile.mkdtemp(prefix="to-127-"))
    for row in I18N_FIXTURE:
        cp = tmp / f"{row['name']}.md"
        cp.write_text(row["checkpoint"], encoding="utf-8")
        score = mod._resume_topic_score(row["prompt"], cp)
        assert (score > 0.0) is row["expect_match"], (
            f"{row['name']}: expected match={row['expect_match']} "
            f"got score={score} ({row['why']})"
        )


def test_keyword_relevance_score_i18n_parity_fixture():
    """keyword_relevance_score (the 3rd changed site, live in OpenClaw's cross-session
    scorer) must agree with the fixture too — closes the coverage gap the gauntlet found."""
    mod = _measure()
    tmp = Path(tempfile.mkdtemp(prefix="to-127-kr-"))
    for row in I18N_FIXTURE:
        cp = tmp / f"{row['name']}.md"
        cp.write_text(row["checkpoint"], encoding="utf-8")
        score = mod.keyword_relevance_score(row["prompt"], cp)
        assert (score > 0.0) is row["expect_match"], (
            f"{row['name']}: keyword_relevance expected match={row['expect_match']} got {score}"
        )


def test_script_aware_floor_via_production_helper():
    """CJK tokens are kept at len>=2 (결제, 모듈); ASCII/accented Latin keep len>3.
    Asserts the real mod._topic_tokens, not a copy."""
    mod = _measure()
    assert mod._topic_tokens("결제 모듈") == {"결제", "모듈"}          # 2-char Hangul kept
    assert mod._topic_tokens("ré tú de la") == set()                  # 2-char accented Latin dropped
    assert "api" not in mod._topic_tokens("use the api key")          # 3-char ASCII dropped
    assert "keepwarm" in mod._topic_tokens("the keepwarm daemon")     # 4+ ASCII kept


def test_tokenizer_keeps_ascii_identifier_clean_next_to_hangul():
    """'measure.py를 분석' must yield the clean identifier 'measure.py', not the glued
    'measure.py를'. Asserts the PRODUCTION regex (mod._TOPIC_TOKEN_RE), not a copy."""
    mod = _measure()
    toks = mod._TOPIC_TOKEN_RE.findall("measure.py를 분석해줘".lower())
    assert "measure.py" in toks
    assert "measure.py를" not in toks


def test_tokenizer_splits_math_symbols_not_letters():
    """U+00D7 (×) and U+00F7 (÷) sit inside Latin-1 but are symbols: they must split a
    token. Accented letters, by contrast, stay whole. Asserts the production regex."""
    mod = _measure()
    assert mod._TOPIC_TOKEN_RE.findall("0.35×audio") == ["0.35", "×", "audio"]
    assert "café" in mod._TOPIC_TOKEN_RE.findall("café au lait")


def test_latin_extended_range_is_symbol_free():
    """The Ā-ɏ (U+0100–U+024F) range must contain only letters — a symbol there would
    silently merge into a token. Every codepoint tokenizes as a single 1-char match."""
    mod = _measure()
    for cp in range(0x0100, 0x0250):
        ch = chr(cp)
        assert mod._TOPIC_TOKEN_RE.findall(ch) == [ch], f"U+{cp:04X} did not tokenize cleanly"


def test_non_utf8_checkpoint_scores_zero_not_crash():
    """A non-UTF-8 checkpoint must score 0.0 for itself, never raise UnicodeDecodeError
    and abort scoring for the whole candidate loop (errors='replace')."""
    mod = _measure()
    tmp = Path(tempfile.mkdtemp(prefix="to-127-bad-"))
    bad = tmp / "bad.md"
    bad.write_bytes(b"\xff\xfe some cp1252 \x92 junk")
    # No exception is the assertion; a float result proves the guard held.
    assert isinstance(mod._resume_topic_score("some topic here", bad), float)
    assert isinstance(mod.keyword_relevance_score("some topic here", bad), float)


def test_resume_topic_score_strips_all_intent_phrases():
    """Locks the Python behavior the TS ports must mirror: re.sub strips EVERY intent phrase,
    so a second phrase's content word ("conversation") never leaks into the topic set.
    A non-global JS .replace stripped only the first phrase and diverged (1.0 -> 0.5)."""
    mod = _measure()
    tmp = Path(tempfile.mkdtemp(prefix="to-127-strip-"))
    cp = tmp / "cp.md"
    cp.write_text("parser module notes", encoding="utf-8")
    assert mod._resume_topic_score(
        "continue on the parser, resume the conversation", cp
    ) == 1.0


def test_recover_token_re_left_unchanged():
    """Regression guard: the recover/keep tokenizer stays ASCII-only and is NOT the wider
    topic tokenizer, per the issue's data-loss reasoning."""
    mod = _measure()
    assert mod._RECOVER_TOKEN_RE.pattern == r"[a-zA-Z0-9_./:-]+"
    assert mod._RECOVER_TOKEN_RE.pattern != mod._TOPIC_TOKEN_RE.pattern
