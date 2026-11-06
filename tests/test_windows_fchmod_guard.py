"""os.fchmod() does not exist on Windows (POSIX-only; unlike os.chmod
there is no Windows implementation, so a bare call raises AttributeError, which is
NOT an OSError subclass and so slips past `except OSError` guards). Two atomic-write
sites in measure.py were unguarded -- the dashboard generator and the keep-warm
scheduler marker writer -- crashing every dashboard build and the marker write on
Windows.

Two layers of protection here:

1. A source guard over the WHOLE bug class: every `os.fchmod(` in measure.py must sit
   under a `hasattr(os, "fchmod")` check. This is the regression tripwire -- it fails
   the instant any future edit adds an unguarded site, regardless of platform.

2. A functional Windows simulation: with `os.fchmod` removed (as on real Windows),
   the dashboard generator and the keep-warm marker writer must complete without an
   AttributeError. mkstemp already creates the temp file 0600, so dropping the fchmod
   loses nothing on Windows.
"""
from __future__ import annotations

import importlib
import re
import sys
import tempfile
from pathlib import Path

import pytest
from helpers.measure_split import measure_source

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"


# --- Layer 1: source guard over every fchmod site (bug-class tripwire) ---

def test_every_fchmod_call_is_guarded():
    # Scan EVERY shipped copy of measure.py, not just the canonical one: the repo
    # keeps three byte-identical mirrors (skills/, plugins/, cowork/), and an
    # unguarded os.fchmod in any of them crashes Windows just the same.
    measures = [REPO / prefix / 'skills/token-optimizer/scripts/measure.py'
                for prefix in ('', 'plugins/token-optimizer', 'cowork/token-optimizer')]
    assert measures, "no measure.py copies found to scan"
    unguarded = []
    for mp in measures:
        lines = measure_source(mp).splitlines()
        for i, line in enumerate(lines):
            if re.search(r"\bos\.fchmod\s*\(", line):
                # The guard is `if hasattr(os, "fchmod"):` on one of the few lines
                # immediately above (allowing for an intervening comment line).
                window = "\n".join(lines[max(0, i - 3):i])
                if 'hasattr(os, "fchmod")' not in window and "hasattr(os, 'fchmod')" not in window:
                    unguarded.append(f"{mp.relative_to(REPO)}:{i + 1}")
    assert not unguarded, (
        f"unguarded os.fchmod() call(s) at {unguarded} -- os.fchmod does not exist on "
        "Windows; wrap with `if hasattr(os, \"fchmod\"):`"
    )


# --- Layer 2: functional Windows simulation ---

@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-fchmod-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _simulate_windows_no_fchmod(monkeypatch, m):
    """Make hasattr(m.os, 'fchmod') False, as it is on real Windows."""
    if hasattr(m.os, "fchmod"):
        monkeypatch.delattr(m.os, "fchmod")
    assert not hasattr(m.os, "fchmod"), "simulation failed: os.fchmod still present"


def test_dashboard_generation_survives_missing_fchmod(m, monkeypatch):
    """generate_standalone_dashboard must not AttributeError on Windows (site 1)."""
    _simulate_windows_no_fchmod(monkeypatch, m)
    # force=True bypasses the 60s throttle so the write path actually runs.
    try:
        m.generate_standalone_dashboard(quiet=True, force=True)
    except AttributeError as e:
        if "fchmod" in str(e):
            pytest.fail(f"dashboard generation hit the unguarded os.fchmod: {e}")
        raise
    assert m.DASHBOARD_PATH.exists(), "dashboard file was not written under the Windows sim"


def test_keepwarm_marker_write_survives_missing_fchmod(m, monkeypatch):
    """The keep-warm scheduler marker writer must not AttributeError on Windows (site 2).

    It is wrapped in `except OSError`, but AttributeError is not an OSError, so before
    the guard the crash propagated instead of the intended graceful return.
    """
    _simulate_windows_no_fchmod(monkeypatch, m)
    # bootstrap_rc defaults to a sentinel, so a bare call reaches the fchmod line.
    result = m._keepwarm_write_scheduler_marker()
    assert result is True, (
        "keep-warm marker write should succeed under the Windows sim; "
        f"got {result!r} -- an unguarded os.fchmod AttributeError would propagate here "
        "since it is not an OSError caught by the writer's except clause"
    )
