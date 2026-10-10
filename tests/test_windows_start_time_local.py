"""Finding 10f (sweep r3d): Windows start times print in local time, as POSIX does."""

import importlib.util
import sys
import time
from datetime import datetime, timezone
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




# --- Finding 10f -------------------------------------------------------------


def _local_expected(utc_dt):
    return utc_dt.astimezone().strftime("%a %b %d %H:%M:%S %Y")


def test_iso_start_time_prints_in_local_time(measure):
    utc = datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc)
    parsed = measure._parse_iso_process_datetime("2026-10-10T09:00:00Z")
    assert parsed["started"] == _local_expected(utc)


def test_iso_start_time_with_offset_prints_in_local_time(measure):
    utc = datetime(2026, 10, 10, 7, 0, 0, tzinfo=timezone.utc)
    parsed = measure._parse_iso_process_datetime("2026-10-10T09:00:00+02:00")
    assert parsed["started"] == _local_expected(utc)


def test_wmi_start_time_prints_in_local_time(measure):
    utc = datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc)
    parsed = measure._parse_wmi_datetime("20261010090000.000000+000")
    assert parsed["started"] == _local_expected(utc)


def test_wmi_start_time_with_offset_prints_in_local_time(measure):
    utc = datetime(2026, 10, 10, 7, 0, 0, tzinfo=timezone.utc)
    parsed = measure._parse_wmi_datetime("20261010090000.000000+120")
    assert parsed["started"] == _local_expected(utc)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset to move the local zone")
def test_start_time_moves_with_the_local_zone(measure, monkeypatch):
    monkeypatch.setenv("TZ", "Etc/GMT-2")  # UTC+2
    time.tzset()
    try:
        parsed = measure._parse_iso_process_datetime("2026-10-10T09:00:00Z")
        assert parsed["started"] == "Sat Oct 10 11:00:00 2026"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_naive_start_time_is_unchanged(measure):
    parsed = measure._parse_iso_process_datetime("2026-10-10T09:00:00")
    assert parsed["started"] == "Sat Oct 10 09:00:00 2026"


def test_elapsed_seconds_unchanged_by_local_conversion(measure):
    parsed = measure._parse_iso_process_datetime("2026-10-10T09:00:00Z")
    expected = (datetime.now(timezone.utc) - datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc)).total_seconds()
    assert abs(parsed["elapsed_seconds"] - max(0, int(expected))) <= 2
