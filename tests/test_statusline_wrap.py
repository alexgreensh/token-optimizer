#!/usr/bin/env python3
"""Statusline responsive wrap: narrowing the terminal must reflow
segments onto new physical rows instead of the host clipping the overflow. Width
comes from the COLUMNS env var (Claude Code v2.1.153+ exports it, live on resize).
When COLUMNS is unset (older Claude Code), output stays byte-identical to the prior
two-row form -- no regression.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATUSLINE = REPO / "skills" / "token-optimizer" / "scripts" / "statusline.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not STATUSLINE.exists(), reason="node or statusline.js unavailable"
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _vlen(s):
    return len(_ANSI.sub("", s))


def _run(payload, cols=None, extra_env=None):
    env = dict(os.environ)
    env.pop("COLUMNS", None)
    # A real slim setting would shrink every run here to one line.
    env.pop("TOKEN_OPTIMIZER_STATUS_BAR_SIZE", None)
    if extra_env:
        env.update(extra_env)
    if cols is not None:
        env["COLUMNS"] = str(cols)
    p = subprocess.run(
        [NODE, str(STATUSLINE)], input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    # Normalize CRLF: node on Windows emits \r\n, which would leave a stray \r
    # on every line after split("\n") and skew the width/line-count assertions.
    return p.stdout.replace("\r\n", "\n")


PAYLOAD = {
    "model": {"display_name": "Opus 4.8"},
    "workspace": {"current_dir": str(REPO)},
    "session_id": "abc123",
    "context_window": {"used_percentage": 42},
}


def test_narrow_window_reflows_and_no_packed_line_over_budget():
    """At 40 cols the two logical rows reflow to >=3 physical rows, and no line that
    packed multiple segments exceeds the budget. A lone unsplittable segment (no
    SEP inside) may exceed -- the host clips that one, nothing can wrap inside it."""
    lines = _run(PAYLOAD, cols=40).split("\n")
    assert len(lines) >= 3, f"expected reflow to >=3 rows at 40 cols, got {len(lines)}: {lines!r}"
    for ln in lines:
        if _vlen(ln) > 40:
            # allowed only if it is a single segment (no ' | ' separator inside)
            assert " | " not in _ANSI.sub("", ln), f"packed line over 40 cols: {ln!r}"


def test_narrow_wraps_more_than_wide():
    narrow = _run(PAYLOAD, cols=40).split("\n")
    wide = _run(PAYLOAD, cols=200).split("\n")
    assert len(narrow) > len(wide)


def test_wide_window_stays_two_rows():
    assert len(_run(PAYLOAD, cols=200).split("\n")) == 2


def test_columns_unset_is_backcompat_two_rows():
    """COLUMNS unset -> exactly two rows, and byte-identical to the wide-fallback
    shape (row1Segs.join(SEP) + '\\n' + row2Parts.join(SEP))."""
    unset = _run(PAYLOAD, cols=None)
    assert len(unset.split("\n")) == 2
    # a very wide window packs each row to a single line, i.e. the same join the
    # unset fallback produces -> they must match byte-for-byte.
    assert unset == _run(PAYLOAD, cols=500)


def test_every_line_ends_reset_no_color_bleed():
    """Each emitted physical row must terminate its own color so nothing bleeds
    into the host shell after the status line."""
    for cols in (40, 200, None):
        out = _run(PAYLOAD, cols=cols)
        for ln in out.split("\n"):
            if _ANSI.search(ln):  # only lines that opened a color must close it
                assert ln.endswith("\x1b[0m") or "\x1b[0m" in ln, f"unreset line: {ln!r}"


SLIM = {"TOKEN_OPTIMIZER_STATUS_BAR_SIZE": "slim"}


def test_slim_is_one_line_with_the_important_fields():
    """TOKEN_OPTIMIZER_STATUS_BAR_SIZE=slim collapses the status line to row 1:
    model, project, context bar and ContextQ, on a single line."""
    out = _run(PAYLOAD, extra_env=SLIM)
    assert "\n" not in out, f"expected one line, got: {out!r}"
    plain = _ANSI.sub("", out)
    assert "Opus 4.8" in plain
    assert "42%" in plain
    assert "ContextQ:" in plain
    # Row-2-only fields stay off the slim line.
    assert "Eff:" not in plain
    assert "Compacts:" not in plain


def test_slim_stays_one_line_when_narrow():
    """Slim is a single line by choice: COLUMNS does not reflow it."""
    out = _run(PAYLOAD, cols=40, extra_env=SLIM)
    assert "\n" not in out, f"expected one line at 40 cols, got: {out!r}"


def test_slim_other_values_keep_two_rows():
    """Anything but 'slim' is the full line (same rule as the desktop band)."""
    for value in ("full", "wide", "0"):
        out = _run(PAYLOAD, extra_env={"TOKEN_OPTIMIZER_STATUS_BAR_SIZE": value})
        assert len(out.split("\n")) == 2, f"SIZE={value!r} should keep two rows: {out!r}"
