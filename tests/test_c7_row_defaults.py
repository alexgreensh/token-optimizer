"""Tests for DECISION rows 9, 11, 12 (Step 6):
- Row 9: big_read_preview default OFF (opt-in via TOKEN_OPTIMIZER_BIG_NONCODE_READ=1)
- Row 11: verbosity-steer default OFF (opt-in via TOKEN_OPTIMIZER_VERBOSITY_STEER=1)
- Row 12: rerun dedupe (bash_rerun_dedupe) and cross-output dedupe removed.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bash_compress_hook as bch  # noqa: E402
import big_noncode_read as bnr  # noqa: E402
import measure  # noqa: E402


def test_row9_big_read_preview_default_off(monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_BIG_NONCODE_READ", raising=False)
    assert bnr.is_big_noncode_read_enabled() is False
    monkeypatch.setenv("TOKEN_OPTIMIZER_BIG_NONCODE_READ", "1")
    assert bnr.is_big_noncode_read_enabled() is True


def test_row11_verbosity_steer_default_off(monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_VERBOSITY_STEER", raising=False)
    assert measure.run_verbosity_steer(quiet=True) == ""


def test_row12_rerun_and_crossturn_dedupe_removed():
    assert not (SCRIPTS / "bash_rerun_dedupe.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bash_rerun_dedupe")
    sample = "\n".join(f"line {i}" for i in range(50))
    assert bch._crossturn_dedup("git status", sample, "tu-1") is None
    assert bch._crossturn_dedup("git status", sample, "tu-2") is None
