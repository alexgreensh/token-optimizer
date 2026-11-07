"""The command-filters loader only reads regular files, so a FIFO or a
directory at the filters path can never hang or break a hook."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import command_filters  # noqa: E402


def _load_with(path, monkeypatch):
    monkeypatch.setenv(command_filters.ENV_FILTERS_PATH, str(path))
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("cfg", command_filters.load_filters()), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "load_filters blocked on a special file"
    return result["cfg"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_at_the_filters_path_is_ignored_without_blocking(tmp_path, monkeypatch):
    fifo = tmp_path / "command-filters.toml"
    os.mkfifo(fifo)
    cfg = _load_with(fifo, monkeypatch)
    assert cfg == command_filters.CommandFilterConfig()


def test_directory_at_the_filters_path_is_ignored(tmp_path, monkeypatch):
    folder = tmp_path / "command-filters.toml"
    folder.mkdir()
    cfg = _load_with(folder, monkeypatch)
    assert cfg == command_filters.CommandFilterConfig()


def test_regular_filters_file_still_loads(tmp_path, monkeypatch):
    path = tmp_path / "command-filters.toml"
    path.write_text('[filters.exclude]\ncommands = ["git log"]\n')
    cfg = _load_with(path, monkeypatch)
    assert cfg != command_filters.CommandFilterConfig()
