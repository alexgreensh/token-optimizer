#!/usr/bin/env python3
"""Run bash_compress_hook.main() from W2_HOOK_DIR, optionally with a known-bad patch.

W2_BAD=classifier  -> post_run_classify also treats `rm ...` as a harmless prefix
W2_BAD=compressor  -> bash_compress.compress drops warning/error lines
Used only by gate_harness.py to verify that the gate goes red on bad code.
"""
import os
import re
import sys

hook_dir = os.environ["W2_HOOK_DIR"]
sys.path.insert(0, hook_dir)
bad = os.environ.get("W2_BAD", "")

if bad == "classifier":
    import post_run_classify as _prc
    _orig_harmless = _prc._is_harmless

    def _bad_harmless(tokens):
        if tokens and tokens[0] == "rm":
            return True
        return _orig_harmless(tokens)
    _prc._is_harmless = _bad_harmless

if bad == "compressor":
    import bash_compress as _bc
    _orig_compress = _bc.compress
    _drop = re.compile(r"(?i)\b(warning|error)\b")

    def _bad_compress(command_str, raw_output, returncode=0, stderr=""):
        out = _orig_compress(command_str, raw_output, returncode=returncode, stderr=stderr)
        return "\n".join(ln for ln in out.splitlines() if not _drop.search(ln))
    _bc.compress = _bad_compress

import bash_compress_hook  # noqa: E402

bash_compress_hook.main()
