"""Native Windows: the installer must refuse with guidance rather than persist
a hook command cmd.exe cannot parse."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.mark.skipif(os.name != "nt", reason="the refusal is only observable on native Windows")
def test_native_windows_install_is_refused_with_guidance(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    ai = importlib.import_module("antigravity_install")
    with pytest.raises(RuntimeError, match="native Windows"):
        ai.install(home=tmp_path)
    assert not (tmp_path / "hooks.json").exists()
