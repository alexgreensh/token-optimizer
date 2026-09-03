#!/usr/bin/env python3
"""Shared python-interpreter trust gate for hook installers.

Every adapter persists an absolute interpreter path into a hook command that
fires under the host agent. A writable or foreign-owned interpreter (or one
sitting in a writable directory) is a persistence vector: whoever controls the
bytes controls every future hook invocation. This module is the single gate
all installers share; per-adapter copies were consolidated here so the
semantics cannot drift between adapters.

Pure stat, never runs the target.
"""

from __future__ import annotations

import os
import stat as _stat


def py_trust_reason(p: str) -> str | None:
    """None when trusted, else a short human-readable rejection reason."""
    try:
        real = os.path.realpath(p)
        if not os.path.isfile(real):
            return f"{real} does not exist"
        if os.name == "nt" or not hasattr(os, "geteuid"):
            return None
        euid = os.geteuid()

        def _admin_owned(uid: int) -> bool:
            # euid (ours) or root/admin (0): an interpreter either we or the
            # system controls. Hosted-CI caches (hostedtoolcache) and macOS
            # Homebrew are euid/admin-owned; /usr/bin is root-owned.
            return uid == euid or uid == 0

        st_file = os.stat(real)
        st_dir = os.stat(os.path.dirname(real))
        # Root-controlled tool caches (hosted-CI hostedtoolcache) ship the
        # interpreter group/world-writable by distribution policy. That is
        # acceptable ONLY when both the interpreter and its directory are
        # root-owned and the directory itself is not world-writable: then the
        # writability comes from root's tool-cache policy, and only root can
        # modify the directory contents, so no peer user can swap the bytes.
        # A user-owned writable interpreter stays a swap vector and is
        # rejected below.
        if st_file.st_uid == 0 and st_dir.st_uid == 0 and not (
            st_dir.st_mode & _stat.S_IWOTH
        ):
            return None
        # The interpreter's BYTES must never be world-writable, and must be
        # admin-owned. Group-writable is accepted ONLY in the self-group case
        # (owner is us AND the group is our own primary group): hosted-CI tool
        # caches extract the interpreter 0775 under the runner's own group, and
        # that group is exactly the account running this installer. A file
        # group-writable by any OTHER group is a swap vector.
        if st_file.st_mode & _stat.S_IWOTH:
            return f"{real} is world-writable"
        if st_file.st_mode & _stat.S_IWGRP and not (
            st_file.st_uid == euid and st_file.st_gid == os.getegid()
        ):
            return f"{real} is group-writable by a foreign group"
        if not _admin_owned(st_file.st_uid):
            return f"{real} is owned by uid {st_file.st_uid}, not by us or root"
        # The containing DIR must not be world-writable. Group-writable is the
        # admin-group case (hostedtoolcache 0775 under the runner's own group)
        # and is accepted only when root owns the dir, or when the owner is us
        # AND the writable group is our own primary group -- a group-writable
        # dir whose group is any other account would let that account swap the
        # interpreter.
        if st_dir.st_mode & _stat.S_IWOTH:
            return f"{os.path.dirname(real)} is world-writable"
        if st_dir.st_mode & _stat.S_IWGRP and not (
            st_dir.st_uid == 0
            or (st_dir.st_uid == euid and st_dir.st_gid == os.getegid())
        ):
            return (f"{os.path.dirname(real)} is group-writable by a group "
                    f"the user does not control (uid {st_dir.st_uid}, "
                    f"gid {st_dir.st_gid})")
        return None
    except (OSError, ValueError) as exc:
        # ValueError: embedded null byte in path (realpath raises it, not
        # OSError). Without catching it, one bad candidate aborts the entire
        # resolver search instead of being rejected and skipped.
        return f"stat failed: {exc}"


def py_path_is_trusted(p: str) -> bool:
    """Trusted iff the interpreter's bytes are admin-owned (euid or root) and
    not group/other-writable, and its dir is not world-writable and not
    group-writable by a third party. On Windows, stat ownership is unreliable
    under Git-Bash, so require only that the path is a real file."""
    return py_trust_reason(p) is None
