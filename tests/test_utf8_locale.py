#!/usr/bin/env python3
"""Regression tests for non-UTF-8-locale session detection (Hebrew / CJK paths).

Reproduces the beta-tester bug: under a non-UTF-8 active encoding (Windows ANSI
codepages, an explicit non-UTF-8 LANG/LC_ALL, or PYTHONUTF8=0), Python's std
streams and default open() encoding fall back to that charset, so non-ASCII
session paths/content crash a hook with UnicodeDecode/EncodeError and the tool
"doesn't identify the claude session."

Run directly:  python3 tests/test_utf8_locale.py
Exits non-zero on first failure.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "token-optimizer" / "skills" / "token-optimizer" / "scripts"
RUN_PY = REPO / "plugins" / "token-optimizer" / "hooks" / "run.py"

HEB = "/Users/x/פרויקט-שלי"  # "my project" in Hebrew
# A locale env that forces a non-UTF-8 stdio + default encoding on every platform.
ASCII_ENV = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii", "LC_ALL": "C"}


def _run(code, env, stdin=None):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SCRIPTS), env=env, input=stdin,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )


def test_enforce_utf8_io_allows_non_ascii_print():
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "from utf8_io import enforce_utf8_io; enforce_utf8_io()\n"
        f"print('project: {HEB}')\n"
    )
    r = _run(code, ASCII_ENV)
    assert r.returncode == 0, f"non-ascii print crashed: {r.stderr}"
    assert "פרויקט" in r.stdout, f"hebrew missing from stdout: {r.stdout!r}"


def test_enforce_utf8_io_is_idempotent():
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "from utf8_io import enforce_utf8_io\n"
        "enforce_utf8_io(); enforce_utf8_io(); enforce_utf8_io()\n"
        f"print('{HEB}')\n"
    )
    r = _run(code, ASCII_ENV)
    assert r.returncode == 0, f"idempotent calls crashed: {r.stderr}"


def test_hook_io_parses_non_ascii_stdin():
    payload = '{"cwd":"%s","tool_name":"Bash"}' % HEB
    # The child asserts equality itself, then prints ascii-only OK, so the result
    # never has to round-trip non-ASCII through the (deliberately ascii) stdout.
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "from hook_io import read_stdin_hook_input\n"
        "d = read_stdin_hook_input()\n"
        f"assert d.get('cwd') == {HEB!r}, repr(d)\n"
        "assert d.get('tool_name') == 'Bash', repr(d)\n"
        "print('OK')\n"
    )
    r = _run(code, ASCII_ENV, stdin=payload)
    assert r.returncode == 0, f"hook_io crashed/mismatched on hebrew stdin: {r.stderr}"
    assert "OK" in r.stdout, f"hebrew cwd not parsed: {r.stdout!r} {r.stderr!r}"


def test_open_reads_non_ascii_jsonl_with_explicit_encoding():
    # The pattern the code now uses everywhere: explicit encoding survives bad locale.
    code = (
        "import tempfile, os\n"
        "p = tempfile.mktemp()\n"
        f"open(p, 'w', encoding='utf-8').write('{HEB}')\n"
        "assert open(p, encoding='utf-8').read() == '" + HEB + "'\n"
        "os.unlink(p); print('OK')\n"
    )
    r = _run(code, ASCII_ENV)
    assert r.returncode == 0, f"explicit-utf8 open crashed: {r.stderr}"


def test_reexec_flips_non_utf8_locale_to_utf8_mode():
    # A real script file (not -c) so sys.argv[0] is a path, like measure.py.
    probe = Path(__file__).resolve().parent / "_reexec_probe_tmp.py"
    probe.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from utf8_io import reexec_in_utf8_mode\n"
        "reexec_in_utf8_mode()\n"
        "import locale\n"
        "ok = sys.flags.utf8_mode == 1 and (locale.getpreferredencoding(False) or '').lower().replace('-','') == 'utf8'\n"
        f"p = {str(probe.parent / '_heb_probe_tmp.txt')!r}\n"
        f"open(p, 'w', encoding='utf-8').write({'פרויקט'!r})\n"
        "ok = ok and open(p).read() == 'פרויקט'\n"  # default open(), no encoding=
        "os.unlink(p)\n"
        "print('REEXEC_OK' if ok else 'REEXEC_FAIL')\n",
        encoding="utf-8",
    )
    try:
        r = subprocess.run(
            [sys.executable, str(probe)], env=ASCII_ENV,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        assert r.returncode == 0, f"re-exec probe crashed: {r.stderr}"
        assert "REEXEC_OK" in r.stdout, f"re-exec did not flip to utf-8: {r.stdout!r} {r.stderr!r}"
    finally:
        probe.unlink(missing_ok=True)


def test_reexec_noop_under_utf8_locale_does_not_loop():
    probe = Path(__file__).resolve().parent / "_reexec_noloop_tmp.py"
    probe.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from utf8_io import reexec_in_utf8_mode\n"
        "reexec_in_utf8_mode()\n"
        "print('NO_REEXEC' if os.environ.get('TOKEN_OPTIMIZER_UTF8_REEXEC') is None else 'REEXECED')\n",
        encoding="utf-8",
    )
    try:
        utf8_env = {**os.environ}
        utf8_env.pop("PYTHONUTF8", None)
        utf8_env.pop("TOKEN_OPTIMIZER_UTF8_REEXEC", None)
        utf8_env["LC_ALL"] = "en_US.UTF-8"
        utf8_env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, str(probe)], env=utf8_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        assert r.returncode == 0, f"utf-8 probe crashed: {r.stderr}"
        assert "NO_REEXEC" in r.stdout, f"unexpected re-exec under utf-8 locale: {r.stdout!r}"
    finally:
        probe.unlink(missing_ok=True)


def test_reexec_treats_utf8_codepages_as_utf8():
    # cp65001 (Windows UTF-8 code page) and utf_8 alias must NOT trigger a re-exec,
    # else Windows-UTF-8 users re-exec on every hook invocation. Non-UTF-8 charsets
    # (ascii, cp1255 Hebrew-Windows) must still trigger it.
    src = (SCRIPTS / "utf8_io.py").read_text(encoding="utf-8")
    assert '"cp65001"' in src and '"65001"' in src, "cp65001 must be treated as UTF-8"
    assert '.replace("_", "")' in src, "must normalize utf_8 alias"

    def would_reexec(enc):
        n = enc.lower().replace("-", "").replace("_", "")
        return n not in ("utf8", "cp65001", "65001", "")
    for safe in ("UTF-8", "utf8", "utf_8", "cp65001", "CP65001", "65001"):
        assert not would_reexec(safe), f"{safe} should NOT re-exec"
    for needs in ("ascii", "ANSI_X3.4-1968", "cp1255"):
        assert would_reexec(needs), f"{needs} should re-exec"


def test_run_py_injects_utf8_env():
    src = RUN_PY.read_text(encoding="utf-8")
    assert '"PYTHONUTF8": "1"' in src, "run.py must force PYTHONUTF8=1 in child env"
    assert '"PYTHONIOENCODING": "utf-8"' in src, "run.py must force PYTHONIOENCODING in child env"
    # env=child_env must be passed to Popen; tolerate extra kwargs such as
    # start_new_session=True (added by the stop-hook orphan-process fix).
    assert re.search(r"subprocess\.Popen\(cmd, env=child_env\b", src), "run.py must pass child_env to Popen"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
