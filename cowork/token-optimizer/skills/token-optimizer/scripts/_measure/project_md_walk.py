"""Which directories the project CLAUDE.md walk must skip.

The walk climbs from cwd looking for project CLAUDE.md files. The home
directory is skipped because ~/CLAUDE.md and the global CLAUDE.md are counted
separately. When HOME points somewhere else (a test sandbox, a scratch run),
the account's real home must be skipped too, or the walk reads the user's
own instruction files into a run that was meant to be isolated.
"""
from __future__ import annotations


def homes(facade):
    """HOME as this process sees it, plus the OS account's own home."""
    found = [facade.HOME]
    try:
        real = facade._real_account_home()
    except Exception:
        real = None
    if real is not None:
        found.append(real)
    return found


def is_home(facade, parent, known=None):
    """True when ``parent`` is HOME or the account's real home."""
    for home in (known if known is not None else homes(facade)):
        if parent == home or facade._same_path(parent, home):
            return True
    return False
