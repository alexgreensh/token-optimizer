# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Taneli Hukkinen
# Licensed to PSF under a Contributor Agreement.

# Compatibility shim: the canonical flat copy ships as the _tomli payload
# module so Devin's closed set can use it without _measure imports. This
# package exists only so `from _measure._vendor import tomli` keeps working;
# _vendor's lazy loader still defers the real parser to first TOML access.
from _tomli import TOMLDecodeError, __version__, load, loads

__all__ = ("loads", "load", "TOMLDecodeError")
