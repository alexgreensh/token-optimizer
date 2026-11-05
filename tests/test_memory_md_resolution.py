#!/usr/bin/env python3
"""Regression tests for MEMORY.md resolution in measure_components() (#301).

Two Windows-driven defects, both reported against v5.13.63:

1. The HOME-project slug was built as `"-" + str(HOME).replace("/", "-")`.
   On Windows str(HOME) is `C:\\Users\\<name>`, so the colon and backslashes
   survived and the candidate never matched Claude Code's real project dir
   (`C--Users-<name>`). The fix builds the slug with the same
   _encode_project_dir_name() Claude Code's project dirs are named by.

2. When neither the HOME project nor the cwd-matched project had a
   memory/MEMORY.md, a scan-all fallback charged the NEWEST MEMORY.md found in
   ANY project -- a file Claude Code never injects into this session. The
   report must say 0 instead of billing an unrelated project.

Run directly:  python3 -m pytest tests/test_memory_md_resolution.py -q
"""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = Path(os.environ.get("TOKOPT_MEASURE_SCRIPTS") or (REPO / "skills" / "token-optimizer" / "scripts"))
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """A fake $HOME + ~/.claude with a projects/ tree, and an isolated cwd."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    projects = claude_dir / "projects"
    projects.mkdir(parents=True)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude_dir)
    monkeypatch.chdir(cwd)
    return {"home": home, "claude_dir": claude_dir, "projects": projects, "cwd": cwd}


def _write_memory(projects: Path, slug: str, text: str) -> Path:
    mem = projects / slug / "memory" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(text, encoding="utf-8")
    return mem


def test_windows_home_project_slug_matches(rig, monkeypatch):
    """A Windows HOME (drive colon + backslashes) must resolve to its own
    project dir (C--Users-<name>), not fall through to another project."""
    win_home = Path("C:\\Users\\tester")  # POSIX Path whose str keeps the backslashes
    monkeypatch.setattr(measure, "HOME", win_home)
    own = _write_memory(rig["projects"], "C--Users-tester", "home memory\n" * 5)
    _write_memory(rig["projects"], "-tmp-newer-project", "other memory\n" * 50)

    components = measure.measure_components()
    mem = components["memory_md"]
    assert mem["exists"] is True
    assert Path(mem["path"]) == own
    assert mem["lines"] == 5


def test_no_cross_project_fallback(rig, monkeypatch):
    """No HOME memory + no cwd-project memory => report 0, never another
    project's MEMORY.md (the pre-#301 scan-all fallback charged it)."""
    monkeypatch.setattr(measure, "HOME", rig["home"])
    _write_memory(rig["projects"], "-some-unrelated-project", "other memory\n" * 40)

    components = measure.measure_components()
    mem = components["memory_md"]
    assert mem["exists"] is False
    assert mem["tokens"] == 0
    assert mem["path"] == ""


def test_cwd_project_memory_still_resolves(rig, monkeypatch):
    """A cwd-matched project's memory is still picked up when the HOME
    project has none (resolution order step 2 is preserved)."""
    monkeypatch.setattr(measure, "HOME", rig["home"])
    cwd_slug = measure._encode_project_dir_name(str(rig["cwd"]))
    own = _write_memory(rig["projects"], cwd_slug, "project memory\n" * 7)

    components = measure.measure_components()
    mem = components["memory_md"]
    assert mem["exists"] is True
    assert Path(mem["path"]) == own
    assert mem["lines"] == 7


def test_posix_home_project_unchanged(rig, monkeypatch):
    """POSIX HOME resolution is byte-for-byte what it was before the fix."""
    monkeypatch.setattr(measure, "HOME", rig["home"])
    slug = measure._encode_project_dir_name(str(rig["home"]))
    own = _write_memory(rig["projects"], slug, "posix home memory\n" * 3)

    components = measure.measure_components()
    mem = components["memory_md"]
    assert mem["exists"] is True
    assert Path(mem["path"]) == own
    assert mem["lines"] == 3
