#!/usr/bin/env python3
"""GitHub #127 — session continuity is blind to non-English prompts.

The topic tokenizer extracted words with ``[a-zA-Z0-9_./:-]+``, which matches
nothing above U+007F. Korean/Chinese/Japanese prompts tokenized to ``[]`` and
scored a hard ``0.0`` before any threshold was consulted; accented Latin split
at the accent (``módulo`` -> ``dulo``). The fix is a two-branch tokenizer at the
three topic-scoring sites (``_resume_topic_score`` x2, ``keyword_relevance_score``'s
``content_words``): an ASCII/accented-Latin run, OR a whole non-ASCII run as its
own token, so a token never mixes ASCII and non-ASCII.

The ``_RECOVER_TOKEN_RE`` recover/keep tokenizer is deliberately NOT changed:
the issue reasoned that widening it makes non-Latin items produce 3+ tokens that
then fail the ASCII-only keep-set overlap test and drop needed lines.

The parity fixture (``tests/fixtures/i18n_topic_score_parity.json``) is the same
JSON asserted by the OpenClaw and OpenCode TS suites, so all three runtimes score
the SAME non-English inputs identically, including the two documented known limits.
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
    """_resume_topic_score's match/no-match must equal the shared fixture for
    every runtime. Positive rows prove #127 is fixed; the two limit rows pin
    the documented boundaries so a future change to them is a conscious one."""
    mod = _measure()
    tmp = Path(tempfile.mkdtemp(prefix="to-127-"))
    for row in I18N_FIXTURE:
        cp = tmp / f"{row['name']}.md"
        cp.write_text(row["checkpoint"], encoding="utf-8")
        score = mod._resume_topic_score(row["prompt"], cp)
        matched = score > 0.0
        assert matched is row["expect_match"], (
            f"{row['name']}: expected match={row['expect_match']} "
            f"got score={score} ({row['why']})"
        )


def test_hangul_prompt_no_longer_empties_to_zero():
    """The headline #127 symptom: a Korean prompt whose 4+char words appear
    verbatim in the checkpoint must not score 0.0."""
    mod = _measure()
    tmp = Path(tempfile.mkdtemp(prefix="to-127-"))
    cp = tmp / "cp.md"
    cp.write_text("리팩터링 데이터베이스 마이그레이션", encoding="utf-8")
    assert mod._resume_topic_score("데이터베이스 마이그레이션 리팩터링 이어서 해줘", cp) > 0.0


def test_tokenizer_keeps_ascii_identifier_clean_next_to_hangul():
    """'measure.py를 분석' must yield the clean identifier 'measure.py', not the
    glued 'measure.py를' (which would no longer match a checkpoint's 'measure.py').
    This is why the fix uses two branches rather than a wholesale-Unicode class."""
    import re
    pattern = r"[a-zA-Z0-9_.:À-ÖØ-öø-ÿĀ-ɏ/-]+|[^\x00-\x7F]+"
    toks = re.findall(pattern, "measure.py를 분석해줘".lower())
    assert "measure.py" in toks
    assert "measure.py를" not in toks


def test_tokenizer_splits_math_symbols_not_letters():
    """U+00D7 (x) and U+00F7 (÷) sit inside Latin-1 but are symbols: they must
    split a token, not merge '0.35xaudio' into one lump."""
    import re
    pattern = r"[a-zA-Z0-9_.:À-ÖØ-öø-ÿĀ-ɏ/-]+|[^\x00-\x7F]+"
    assert re.findall(pattern, "0.35×audio") == ["0.35", "×", "audio"]
    # accented Latin, by contrast, stays whole
    assert "café" in re.findall(pattern, "café au lait")


def test_recover_token_re_left_unchanged():
    """Regression guard: the recover/keep tokenizer must stay ASCII-only, per the
    issue's reasoning that widening it drops needed non-Latin lines."""
    mod = _measure()
    assert mod._RECOVER_TOKEN_RE.pattern == r"[a-zA-Z0-9_./:-]+"
