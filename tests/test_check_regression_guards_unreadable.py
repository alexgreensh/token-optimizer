"""an unreadable file in the tree must not abort the guard check."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_regression_guards.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_regression_guards", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_copy_skips_unreadable_file_and_unreadable_dir_with_warning(tmp_path, capsys):
    mod = _load()
    src = tmp_path / "src"
    (src / "locked_dir").mkdir(parents=True)
    (src / "ok.txt").write_text("ok")
    (src / "secret.txt").write_text("x")
    (src / "locked_dir" / "inner.txt").write_text("y")
    (src / "secret.txt").chmod(0)
    (src / "locked_dir").chmod(0)
    dst = tmp_path / "dst"
    try:
        mod._copy_repo(src, dst)
    finally:
        (src / "secret.txt").chmod(0o644)
        (src / "locked_dir").chmod(0o755)
    assert (dst / "ok.txt").read_text() == "ok"
    err = capsys.readouterr().err
    assert "secret.txt" in err and "skipp" in err.lower()
