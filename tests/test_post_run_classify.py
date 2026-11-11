"""W2: classify a Bash command after it ran (post_run_classify) + its gate.

Synthetic inputs only. The full pre-registered gate run lives in
tests/fixtures/post_run_classify/gate_harness.py; these tests pin the
classifier table, the three modes, and verify the verifier: a classifier that
strips `rm -rf x &&` and a compressor that drops an error line must each turn
the gate red on items the real code passes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
GATE_DIR = REPO / "tests" / "fixtures" / "post_run_classify"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(GATE_DIR))

import post_run_classify as prc  # noqa: E402
import gate_harness  # noqa: E402
import make_fixture  # noqa: E402


@pytest.mark.parametrize("command,expected", [
    ("cd /p && git diff", "git diff"),
    ("cd /p; git status", "git status"),
    ("export X=1; pytest -q", "pytest -q"),
    ("X=1 git diff", "git diff"),
    ("cd /p\ngit diff HEAD~1", "git diff HEAD~1"),
    ("git -C /p status", "git status"),
    ('git -C "/a b" -C /c diff --stat', "git diff --stat"),
    ('cd "/a b" && X=1 du -sh .', "du -sh ."),
    ("set -euo pipefail; cd /p; ls -la", "ls -la"),
    ("cd /p && grep -rn foo . | head -50", "grep -rn foo . | head -50"),
    ("cd /p && git diff > o.txt", "git diff > o.txt"),  # redirection kept verbatim
])
def test_classify_returns_effective_command(command, expected):
    assert prc.classify(command) == expected


@pytest.mark.parametrize("command", [
    "rm -rf x && git status",
    "git diff; git status",
    "for f in *; do cat $f; done",
    "cd /p && cat <<EOF\nx\nEOF",
    "cd /p && ls $(pwd)",
    "cd /p && wc -l `ls`",
    "cd /p && git diff || true",
    "cd /p && (git status)",
    "cd /p & git status",
    "cd - && git status",
    "set; git status",
    "",
])
def test_classify_gives_up_on_compound_or_unsafe(command):
    assert prc.classify(command) is None


@pytest.mark.parametrize("command", [
    "cd /p && git diff > o.txt",        # writes a file: not read-only
    "cd /p && git show HEAD:src/a.py",  # file content print
    "cd /p && sed -n 1,50p f",          # generic fallback, not allowed
    "cd /p && cat f.py",
    "cd /p && make",
    "cd /p && sqlite3 app.db \"delete from t\"",
    "git status",                       # already bare: the normal path owns it
])
def test_eligible_effective_refuses(command):
    assert prc.eligible_effective(command) is None


def test_eligible_effective_allows_pattern_specific():
    assert prc.eligible_effective("cd /p && git diff") == "git diff"
    assert prc.eligible_effective("git -C /p status") == "git status"


@pytest.mark.parametrize("raw,expected", [
    ("", "shadow"), ("shadow", "shadow"), ("1", "on"), ("on", "on"),
    ("0", "off"), ("off", "off"), ("bogus", "shadow"),
])
def test_mode_env(monkeypatch, raw, expected):
    monkeypatch.setenv(prc.ENV_VAR, raw)
    assert prc.mode() == expected


def _item(iid):
    return next(i for i in make_fixture.build(2)["items"] if i["id"] == iid)


def test_fixture_hash_is_pinned():
    assert gate_harness.canon_hash(make_fixture.build(1)) == gate_harness.PINNED_HASHES[1]
    assert gate_harness.canon_hash(make_fixture.build(2)) == gate_harness.PINNED_HASHES[2]


def test_shadow_changes_nothing_and_logs_opportunity(tmp_path):
    it = _item("P00-cd_semi")  # `cd /work/project; git diff`
    shadow = gate_harness.run_hook(SCRIPTS, tmp_path, "sh", it["command"], it["stdout"])
    off = gate_harness.run_hook(SCRIPTS, tmp_path, "off", it["command"], it["stdout"], mode="0")
    assert shadow["rc"] == 0 and off["rc"] == 0
    assert shadow["emitted"] is None and off["emitted"] is None
    assert gate_harness.opportunity_rows(shadow["snap"]) == (1, 0)
    assert gate_harness.opportunity_rows(off["snap"]) == (0, 0)


def test_on_mode_matches_bare_command(tmp_path):
    it, bare = _item("P00-cd_semi"), _item("P00-bare")
    on = gate_harness.run_hook(SCRIPTS, tmp_path, "on", it["command"], it["stdout"], mode="1")
    ref = gate_harness.run_hook(SCRIPTS, tmp_path, "bare", bare["command"], bare["stdout"], mode="1")
    assert on["emitted"] is not None
    assert gate_harness.norm(on["emitted"]) == gate_harness.norm(ref["emitted"])
    assert len(on["emitted"]) < len(it["stdout"])


_VERIFY_IDS = "D-00,E-02-bare,E-02-cd,P00-bare,P00-cd_semi"


def _gate(tmp_path, bad):
    return gate_harness.main(["--hook-dir", str(SCRIPTS), "--workdir", str(tmp_path / f"g-{bad or 'good'}"),
                              "--ids", _VERIFY_IDS, "--json-out", str(tmp_path / f"{bad or 'good'}.json"),
                              "--jobs", "4"] + (["--bad", bad] if bad else []))


def _fails(tmp_path, bad):
    import json
    rep = json.loads((tmp_path / f"{bad or 'good'}.json").read_text())["report"]
    return {r["id"]: r["fails"] for r in rep}


def test_gate_green_on_real_code(tmp_path):
    assert _gate(tmp_path, "") == 0
    assert not any(v for k, v in _fails(tmp_path, "").items() if k != gate_harness.NC_ID)


def test_verifier_bad_classifier_turns_gate_red(tmp_path):
    assert _gate(tmp_path, "classifier") == 1
    assert "7-negative-space" in _fails(tmp_path, "classifier")["D-00"]  # rm -rf build && git status


def test_verifier_bad_compressor_turns_gate_red(tmp_path):
    assert _gate(tmp_path, "compressor") == 1
    fails = _fails(tmp_path, "compressor")
    assert "4-preserved" in fails["E-02-bare"] and "4-preserved" in fails["E-02-cd"]
