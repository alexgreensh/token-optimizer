#!/usr/bin/env python3
"""Statusline responsive wrap (Alex request): narrowing the terminal must reflow
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


def _run(payload, cols=None):
    env = dict(os.environ)
    env.pop("COLUMNS", None)
    if cols is not None:
        env["COLUMNS"] = str(cols)
    p = subprocess.run(
        [NODE, str(STATUSLINE)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=15,
    )
    return p.stdout


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
