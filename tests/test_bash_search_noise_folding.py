"""Tests for grep/rg noise folding in Bash search output (bash_search_results.py)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bash_compress
import bash_search_results


def _build_mixed_rg_output() -> str:
    lines = [
        "src/auth/session.ts:14:export function createSession(userId: string) {",
        "src/auth/session.ts:42:  return createSession(req.user.id);",
        "src/api/login.ts:88:const s = createSession(account.id);",
    ]
    for i in range(12):
        lines.append(f"node_modules/pkg-{i:02d}/index.js:{i + 1}:exports.createSession = fn;")
    for i in range(6):
        lines.append(f"dist/chunk_{i}.js:{i + 10}:function createSession() {{}}")
    for i in range(4):
        lines.append(f"build/out_{i}.js:{i + 5}:createSession();")
    lines.append(".git/logs/HEAD:12:commit: update createSession")
    lines.append("vendor/lib/session.go:19:func createSession() error { return nil }")
    lines.append("vendor/lib/session.go:44:err := createSession()")
    lines.append('package-lock.json:1024:  "name": "createSession-helper"')
    lines.append('yarn.lock:410:createSession-helper@^1.0.0:')
    lines.append("public/assets/app.min.js:1:var a=function createSession(e){return e};")
    lines.append("public/assets/vendor.min.js:1:function createSession(){return!0}")
    return "\n".join(lines) + "\n"


def test_folds_noisy_folders_lockfiles_and_minified_with_folder_names():
    raw = _build_mixed_rg_output()
    out = bash_compress.compress("rg -n 'createSession' .", raw)

    # Non-noisy source matches untouched
    assert "src/auth/session.ts:14:export function createSession(userId: string) {" in out
    assert "src/auth/session.ts:42:  return createSession(req.user.id);" in out
    assert "src/api/login.ts:88:const s = createSession(account.id);" in out

    # Every noisy folder is listed with its count
    assert "node_modules/: 12 matches across 12 files" in out
    assert "dist/: 6 matches across 6 files" in out
    assert "build/: 4 matches across 4 files" in out
    assert ".git/: 1 match across 1 file" in out
    assert "vendor/: 2 matches across 1 file" in out
    assert "package-lock.json" in out and "yarn.lock" in out
    assert "public/assets/ (*.min.js): 2 matches across 2 files" in out

    # Individual noisy match lines are folded away
    assert "node_modules/pkg-00/index.js:1:" not in out
    assert "dist/chunk_0.js:10:" not in out
    assert "public/assets/app.min.js:1:" not in out
    # Savings are meaningful
    assert len(out) < len(raw) * 0.70


def test_nested_noisy_folder_preserves_full_folder_path():
    lines = [f"src/app.py:{i}:match_{i}" for i in range(1, 5)]
    for i in range(15):
        lines.append(f"packages/web/node_modules/lib-{i}/index.js:1:match_{i}")
    for i in range(15):
        lines.append(f"apps/api/dist/file_{i}.js:1:match_{i}")
    raw = "\n".join(lines) + "\n"

    out = bash_compress.compress("rg -n match .", raw)
    assert "packages/web/node_modules/: 15 matches across 15 files" in out
    assert "apps/api/dist/: 15 matches across 15 files" in out
    assert "src/app.py:1:match_1" in out


def test_explicitly_targeted_noisy_folder_is_not_folded():
    lines = [f"node_modules/pkg-{i:02d}/index.js:1:createSession()" for i in range(35)]
    raw = "\n".join(lines) + "\n"

    # Targeting node_modules explicitly -> do NOT fold node_modules
    out = bash_compress.compress("rg -n 'createSession' node_modules/", raw)
    assert "node_modules/pkg-00/index.js:1:createSession()" in out
    assert "[Folded noisy matches:" not in out


def test_explicitly_targeted_lockfile_or_minified_is_not_folded():
    lines = [f"package-lock.json:{i}:  \"integrity\": \"sha512-{i}\"" for i in range(35)]
    raw = "\n".join(lines) + "\n"
    out = bash_compress.compress("grep -n integrity package-lock.json", raw)
    assert "package-lock.json:0:" in out
    assert "[Folded noisy matches:" not in out

    min_lines = [f"public/app.min.js:{i}:var x{i}=1;" for i in range(35)]
    raw_min = "\n".join(min_lines) + "\n"
    out_min = bash_compress.compress("rg -n 'var x' -g '*.min.js' public/", raw_min)
    assert "public/app.min.js:0:" in out_min
    assert "[Folded noisy matches:" not in out_min


def test_credentials_in_noisy_matches_are_still_preserved():
    lines = [f"node_modules/pkg-{i:02d}/index.js:1:const x = {i};" for i in range(32)]
    lines.append("node_modules/leaky/config.js:9:const aws = 'AKIAIOSFODNN7EXAMPLE';")
    raw = "\n".join(lines) + "\n"

    out = bash_compress.compress("rg -n 'const' .", raw)
    # bash_compress pre-scan + reinject must keep credentials intact
    assert "AKIAIOSFODNN7EXAMPLE" in out
    assert "node_modules/:" in out


def test_short_search_output_untouched():
    short = "\n".join(f"node_modules/pkg/a.js:{i}:foo" for i in range(12)) + "\n"
    assert bash_compress.compress("rg -n foo .", short) == short


def test_benchmark_fixture_passes():
    import json
    import benchmark

    fixture_path = SCRIPTS / "fixtures" / "b5_more_savings.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixtures
    for fixture in fixtures:
        passed, result = benchmark.run_single_fixture(
            fixture, compressor=bash_compress.compress
        )
        assert passed, f"Fixture {fixture['name']} failed: {result}"
