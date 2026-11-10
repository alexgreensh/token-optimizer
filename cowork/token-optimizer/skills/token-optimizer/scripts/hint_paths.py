"""Canonical form for hinted file paths.

The Read hook matches files on ``Path.resolve()``; hint serves used to be stored
exactly as written, so a symlinked, relative or ``~`` path could never be
credited as followed. Both sides go through ``normalize_hint_path`` so they
compare equal. Stdlib only.
"""
from __future__ import annotations

import os


def normalize_hint_path(path) -> str:
    """~ expanded, absolute, symlinks resolved. Empty input gives ''."""
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        return text
