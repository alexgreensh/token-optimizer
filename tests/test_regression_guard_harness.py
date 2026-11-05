"""The planted-bug harness (scripts/check_regression_guards.py) must never pass
by planting nothing: a stale `old`, a malformed entry, a broken mutation file or
a pytest run that collected nothing all have to fail the check loudly."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_regression_guards",
                                               REPO / "scripts" / "check_regression_guards.py")
guards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guards)

TARGET = "skills/token-optimizer/scripts/cloud_kit.py"
TEST = "tests/test_cloud_kit.py::test_prerelease_versions_are_accepted_and_sorted"


@pytest.fixture
def mutations_dir(tmp_path, monkeypatch):
    d = tmp_path / "mutations"
    d.mkdir()
    monkeypatch.setattr(guards, "MUTATIONS_DIR", d)
    monkeypatch.setattr(guards, "MUTATIONS", [])
    return d


def test_every_registered_mutation_is_well_formed_and_still_applies():
    muts = guards.collect()
    assert len(muts) >= 12
    for name, _tests, relpath, edits, origin in muts:
        try:
            guards.plant((REPO / relpath).read_text(encoding="utf-8"), edits)
        except guards.MutationError as exc:
            pytest.fail(f"{origin}: {name}: {exc}")


def test_stale_mutation_fails_loudly(mutations_dir, capsys):
    (mutations_dir / "area.py").write_text(
        "MUTATIONS = [('gone', [%r], %r, [('text that is not in the file', 'x')])]\n" % (TEST, TARGET))
    assert guards.main([]) == 1
    captured = capsys.readouterr()
    assert "STALE gone" in captured.out and "stale" in captured.err


def test_one_stale_edit_among_several_is_still_stale():
    text = "alpha beta"
    with pytest.raises(guards.MutationError):
        guards.plant(text, [("alpha", "A"), ("alpha", "again")])  # 2nd edit no longer matches
    assert guards.plant(text, [("alpha", "A"), ("beta", "B")]) == "A B"


@pytest.mark.parametrize("body,needle", [
    ("MUTATIONS = None\n", "must export a MUTATIONS list"),
    ("raise RuntimeError('boom')\n", "failed to import"),
    ("MUTATIONS = [('n', [%r], %r, [('a', 'b')], 'extra')]\n" % (TEST, TARGET), "entry must be"),
    ("MUTATIONS = [('n', [], %r, [('a', 'b')])]\n" % TARGET, "non-empty list of pytest node ids"),
    ("MUTATIONS = [('n', ['tests/nope.py'], %r, [('a', 'b')])]\n" % TARGET, "does not exist"),
    ("MUTATIONS = [('n', [%r], 'skills/nope.py', [('a', 'b')])]\n" % TEST, "does not exist"),
    ("MUTATIONS = [('n', [%r], '../x.py', [('a', 'b')])]\n" % TEST, "repo-relative"),
    ("MUTATIONS = [('n', [%r], %r, [('same', 'same')])]\n" % (TEST, TARGET), "plants nothing"),
    ("MUTATIONS = [('n', [%r], %r, [])]\n" % (TEST, TARGET), "at least one"),
])
def test_malformed_mutation_files_fail_the_check(mutations_dir, capsys, body, needle):
    (mutations_dir / "area.py").write_text(body)
    assert guards.main(["--list"]) == 1
    assert needle in capsys.readouterr().err


def test_duplicate_names_across_files_fail(mutations_dir, capsys):
    line = "MUTATIONS = [('dup', [%r], %r, [('def _version_key', 'def _vk')])]\n" % (TEST, TARGET)
    (mutations_dir / "a.py").write_text(line)
    (mutations_dir / "b.py").write_text(line)
    assert guards.main(["--list"]) == 1
    assert "duplicate" in capsys.readouterr().err


def test_underscore_files_are_helpers_not_mutation_lists(mutations_dir):
    (mutations_dir / "_shared.py").write_text("X = 1\n")
    assert guards.collect() == []


def test_only_a_real_test_failure_counts_as_caught(monkeypatch, tmp_path):
    """pytest exit 1 = caught. 0 = guard passed on broken code. 2/4/5 (collection
    error, usage error, no tests collected) prove nothing and must fail too."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "f.py").write_text("X = 1\n")
    mut = ("m", ["t"], "pkg/f.py", [("X = 1", "X = 2")], "here")
    for code, status in ((1, "ok"), (0, "fail"), (2, "fail"), (4, "fail"), (5, "fail")):
        monkeypatch.setattr(guards, "_pytest", lambda *_a, _c=code, **_k: (_c, ""))
        assert guards._run_one(mut, root)[0] == status, code
        assert (root / "pkg" / "f.py").read_text() == "X = 1\n", "copy restored after each mutation"


# ------------------------------------------------- guard tests that skip here
# Windows CI 5.13.51: POSIX-only guard tests skip on Windows, the mutated run
# then "passes" by skipping, and ~45 mutations were reported as misses. A
# mutation whose listed tests ALL skip on this OS is n/a (neither caught nor a
# miss); one with some tests running is judged on those; --require-all makes
# n/a a failure so the Linux job still proves every mutation.

OUT = {
    "tests/test_a.py::test_posix_only": "skipped",
    "tests/test_a.py::test_param[x]": "skipped",
    "tests/test_a.py::test_param[y]": "skipped",
    "tests/test_b.py::test_runs": "passed",
    "tests/test_b.py::TestK::test_m": "skipped",
}


@pytest.mark.parametrize("tests,expected", [
    (["tests/test_a.py::test_posix_only"], "n/a"),
    (["tests/test_a.py::test_param"], "n/a"),             # every param skipped
    (["tests/test_a.py"], "n/a"),                         # whole file skipped
    (["tests/test_a.py", "tests/test_b.py::TestK"], "n/a"),
    (["tests/test_a.py", "tests/test_b.py::test_runs"], "run"),  # partial: judged on what ran
    (["tests/test_b.py"], "run"),
    (["tests/test_a.py::test_para"], "missing"),          # prefix of a name is not a match
    (["tests/test_c.py::test_gone"], "missing"),
])
def test_coverage_reads_real_outcomes(tests, expected):
    assert guards.coverage(tests, OUT) == expected


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A tiny repo with one target, a guard that skips on every OS, a guard
    that runs and catches, and a guard that runs and does NOT catch."""
    root = tmp_path / "fake"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "pkg" / "f.py").write_text("X = 1\nY = 1\n")
    load = ("import pathlib, runpy\n"
            "F = runpy.run_path(str(pathlib.Path(__file__).parent.parent / 'pkg' / 'f.py'))\n")
    (root / "tests" / "test_skips.py").write_text(
        "import pytest\n" + load + "@pytest.mark.skip(reason='not on this OS')\n"
        "def test_x():\n    assert F['X'] == 1\n")
    (root / "tests" / "test_runs.py").write_text(load + "def test_x():\n    assert F['X'] == 1\n")
    (root / "tests" / "test_blind.py").write_text(load + "def test_y():\n    assert F['Y'] == 1\n")
    monkeypatch.setattr(guards, "REPO", root)
    monkeypatch.setattr(guards, "MUTATIONS_DIR", root / "none")
    monkeypatch.setenv("REGRESSION_GUARDS_TMP", str(tmp_path / "rg"))
    return root


def _set(monkeypatch, *muts):
    monkeypatch.setattr(guards, "MUTATIONS", [
        (name, tests, "pkg/f.py", [("X = 1", "X = 2")]) for name, tests in muts])


def test_all_skipped_guard_is_na_not_caught_not_missed(fake_repo, monkeypatch, capsys):
    _set(monkeypatch, ("only-skips", ["tests/test_skips.py"]),
         ("partly", ["tests/test_skips.py", "tests/test_runs.py"]))
    assert guards.main([]) == 0
    out = capsys.readouterr().out
    assert "n/a   only-skips (guard tests skip on " in out
    assert "ok    partly" in out
    assert "ok    only-skips" not in out and "FAIL  only-skips" not in out
    assert "1 of 2 known regressions are caught; 1 n/a" in out


def test_require_all_fails_on_na(fake_repo, monkeypatch, capsys):
    _set(monkeypatch, ("only-skips", ["tests/test_skips.py"]))
    assert guards.main(["--require-all"]) == 1
    assert "--require-all" in capsys.readouterr().err


def test_partly_skipped_guard_that_misses_is_still_a_miss(fake_repo, monkeypatch, capsys):
    """Some tests ran: judged on those. Here the one that ran never looks at X,
    so the mutation is a real miss, with or without --require-all."""
    _set(monkeypatch, ("blind", ["tests/test_skips.py", "tests/test_blind.py"]))
    assert guards.main([]) == 1
    captured = capsys.readouterr()
    assert "FAIL  blind" in captured.out and "n/a" not in captured.out
    assert "PASSED on the broken code" in captured.err


def test_clean_run_chunks_cover_every_test_once_and_never_split_a_file():
    tests = [f"tests/test_{f}.py::t{i}" for f in "abcde" for i in range(ord(f) - 96)]
    chunks = guards.clean_run_chunks(tests, 3)
    assert len(chunks) == 3
    assert sorted(t for c in chunks for t in c) == sorted(tests)
    for f in "abcde":
        assert sum(any(t.startswith(f"tests/test_{f}.py") for t in c) for c in chunks) == 1
    # More workers than files: one group per file, none empty.
    assert len(guards.clean_run_chunks(tests[:3], 8)) == 2
