"""Finding 10e (sweep r3d): the per-pid Windows start-time fallback uses ToString('o'),
which has seven fractional digits. datetime.fromisoformat rejects that on Python 3.9/3.10.
"""

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_r3d_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def measure():
    return _load_measure()




# --- Finding 10e -------------------------------------------------------------


SEVEN_DIGIT = "2026-10-10T09:00:00.1234567+02:00"


def test_normalize_trims_fraction_to_six_digits(measure):
    out = measure._normalize_ps_iso(SEVEN_DIGIT)
    frac = re.search(r"\.(\d+)", out).group(1)
    assert len(frac) <= 6, out
    assert out.startswith("2026-10-10T09:00:00.123456")
    assert out.endswith("+02:00")


@pytest.mark.parametrize(
    "raw, expected_frac",
    [
        ("2026-10-10T09:00:00.1234567Z", "123456"),
        ("2026-10-10T09:00:00.12-05:00", "12"),
        ("2026-10-10T09:00:00.1234567", "123456"),
        ("2026-10-10T09:00:00.123456789+00:00", "123456"),
    ],
)
def test_normalize_handles_every_fraction_length(measure, raw, expected_frac):
    out = measure._normalize_ps_iso(raw)
    assert re.search(r"\.(\d+)", out).group(1).startswith(expected_frac)
    assert len(re.search(r"\.(\d+)", out).group(1)) <= 6


def test_normalize_leaves_fractionless_times_alone(measure):
    assert measure._normalize_ps_iso("2026-10-10T09:00:00Z") == "2026-10-10T09:00:00+00:00"


def test_seven_digit_timestamp_parses_with_a_strict_py39_style_parser(measure, monkeypatch):
    """Python 3.9/3.10 fromisoformat takes only 3 or 6 fractional digits. Emulate that."""

    real = datetime

    class Strict39(datetime):
        @classmethod
        def fromisoformat(cls, s):
            m = re.search(r"\.(\d+)", s)
            if m and len(m.group(1)) not in (3, 6):
                raise ValueError(f"Invalid isoformat string: {s!r}")
            return real.fromisoformat(s)

    monkeypatch.setattr(measure, "datetime", Strict39)
    parsed = measure._parse_iso_process_datetime(SEVEN_DIGIT)
    assert parsed is not None
    assert parsed["elapsed_seconds"] > 0


def test_pad_two_digit_fraction_for_py39(measure):
    """3.9 also rejects 1, 2, 4 and 5 fractional digits; the normalizer pads to 6."""
    out = measure._normalize_ps_iso("2026-10-10T09:00:00.12-05:00")
    assert re.search(r"\.(\d+)", out).group(1) == "120000"
