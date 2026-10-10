"""Archive preview must cut on a grapheme-cluster boundary.

A raw codepoint slice ends mid-cluster: a ZWJ emoji sequence renders as a
different emoji, a combining mark at the tail attaches to the footer, an odd
regional indicator shows half a flag. The preview backs the cut off to the last
complete cluster — no external dependency.

Run: python3 -m pytest tests/test_archive_preview_clusters.py -q
"""
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import archive_result as ar  # noqa: E402

FAMILY = "👨\u200d👩\u200d👧"          # ZWJ-joined family emoji, 5 codepoints
MARKED = "e" + "\u0301" * 4            # e + 4 combining marks
FLAGS = "🇮🇱🇮🇱🇮🇱"                 # 3 flag pairs = 6 regional indicators


def _preview(text):
    return ar._compress_mcp_preview(text, "text")


def test_zwj_sequence_is_never_split():
    out = _preview("A" * 1498 + FAMILY + "TAIL")
    assert not out.endswith("\u200d"), repr(out[-8:])
    assert "👨" not in out[-6:], f"partial cluster leaked into preview: {out[-8:]!r}"
    assert len(out) <= ar._ARCHIVE_PREVIEW_SIZE


def test_combining_marks_are_never_left_at_the_tail():
    out = _preview("B" * 1497 + MARKED + "TAIL")
    assert not unicodedata.combining(out[-1]), repr(out[-8:])
    # The incomplete cluster is dropped whole rather than shown altered.
    assert out.endswith("B"), repr(out[-8:])


def test_flag_pairs_are_never_split():
    out = _preview("C" * 1497 + FLAGS + "TAIL")
    ri_tail = 0
    for ch in reversed(out):
        if "\U0001f1e6" <= ch <= "\U0001f1ff":
            ri_tail += 1
        else:
            break
    assert ri_tail % 2 == 0, f"preview ends on half a flag pair: {out[-8:]!r}"


def test_emoji_modifier_is_never_left_at_the_tail():
    out = _preview("D" * 1497 + "👋🏽" + "TAIL")  # wave + skin-tone modifier
    assert not out.endswith("👋"), repr(out[-8:])


def test_variation_selector_is_never_left_at_the_tail():
    out = _preview("E" * 1498 + "✈️" + "TAIL")  # plane + VS16
    assert not out.endswith("✈"), repr(out[-8:])


def test_clean_text_is_untouched():
    text = "F" * 5000
    out = _preview(text)
    assert out == text[:ar._ARCHIVE_PREVIEW_SIZE]


def test_short_text_is_untouched():
    assert _preview("tiny 👨\u200d👩\u200d👧 text") == "tiny 👨\u200d👩\u200d👧 text"


def test_json_preview_cut_is_cluster_safe():
    # A path-listing preview takes the same boundary helper.
    text = "dir/" + "file\n" * 400 + "file👨\u200d👩\u200d👧\n" + "more\n" * 400
    out = ar._compress_mcp_preview(text, "paths")
    assert not out.endswith("\u200d") and "👨" not in out[-6:]
