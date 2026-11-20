#!/usr/bin/env python3
"""Release artefacts that are kept in step by hand or by memory must agree.

#201 #227 and the version-constant gate cover hand-maintained version strings.
These cover the drifts that were still open on 2026-10-19:

  * npm vs git. @alexgreensh/token-optimizer@5.13.50 was published from commit
    41759b8e; later shipped-file fixes then went into the git release while the
    Sign-release npm gate only asked "is that version on npm?", which it was.
    ``check_npm_published --match-tree`` compares the tarball with the tree
    file for file, and the release workflow now runs it.
  * the checksum manifest roots. sign-release.sh decides which files get a
    checksum; install.sh refuses an install whose manifest misses a file under
    ITS list. The two lists are copies; if they part, every verified install
    aborts (or a root ships unverified). They must stay the same set.
  * marketplace.json carries the plugin version in the token-optimizer entry,
    not merely somewhere in the file.

Run: python3 -m pytest tests/test_release_drift.py -q
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import check_npm_published as gate  # noqa: E402


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in sorted(files.items()):
            info = tarfile.TarInfo("package/" + rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(scope="module")
def shipped(tmp_path_factory):
    """relpath -> bytes, exactly what build.py --pack stages from this checkout."""
    sys.path.insert(0, str(REPO / "packaging" / "npm"))
    import build
    pkg = build.stage(tmp_path_factory.mktemp("npm"), include_untracked=False)
    return {p.relative_to(pkg).as_posix(): p.read_bytes() for p in pkg.rglob("*") if p.is_file()}


def _gate(files: dict):
    return gate.match_tree("5.13.51", REPO, fetch=lambda v: {"tarball": "npm://t.tgz", "integrity": "sha512-x"},
                           download=lambda url: _tgz(files))


def test_tarball_built_from_this_tree_matches(shipped):
    ok, message = _gate(shipped)
    assert ok, message


def test_tarball_from_an_older_commit_is_caught(shipped):
    """The 5.13.50 shape: same version, one shipped script from before a fix."""
    rel = "skills/token-optimizer/scripts/measure.py"
    assert rel in shipped
    older = dict(shipped)
    older[rel] = shipped[rel].replace(b"def ", b"def  ", 1)
    ok, message = _gate(older)
    assert not ok
    assert "different bytes:  " + rel in message, message


def test_missing_and_extra_files_are_caught(shipped):
    files = dict(shipped)
    gone = "hooks/run.py"
    del files[gone]
    files["skills/token-optimizer/scripts/leftover_debug.py"] = b"print('x')\n"
    ok, message = _gate(files)
    assert not ok
    assert "missing from npm: " + gone in message
    assert "only on npm:      skills/token-optimizer/scripts/leftover_debug.py" in message


def test_tree_comparison_names_every_kind_of_drift():
    """Git-free core of the gate (runs in a planted-bug copy with no .git)."""
    tree = {"hooks/run.py": b"run\n", "skills/x/measure.py": b"def f(): pass\n", "package.json": b"{}\n"}
    expected = {rel: hashlib.sha256(b).hexdigest() for rel, b in tree.items()}
    assert gate.tree_mismatches(_tgz(tree), expected) == []
    npm = dict(tree)
    npm["skills/x/measure.py"] = b"def f():  pass\n"   # built from the commit before a fix
    del npm["hooks/run.py"]
    npm["leftover.py"] = b"x\n"
    assert gate.tree_mismatches(_tgz(npm), expected) == [
        "missing from npm: hooks/run.py",
        "only on npm:      leftover.py",
        "different bytes:  skills/x/measure.py",
    ]


def test_a_download_failure_fails_the_gate():
    ok, message = gate.match_tree("5.13.51", REPO, fetch=lambda v: {"tarball": "u", "integrity": "sha512-x"},
                                  download=lambda url: (_ for _ in ()).throw(OSError("403")))
    assert not ok and "403" in message


def test_expected_side_is_the_staged_tree(shipped):
    """The gate's expected side is build.py's staging, not a second hand-kept list."""
    staged = gate._staged_files(REPO)
    assert staged == {rel: hashlib.sha256(b).hexdigest() for rel, b in shipped.items()}


def test_sign_release_workflow_runs_the_tree_match():
    wf = (REPO / ".github" / "workflows" / "sign-release.yml").read_text(encoding="utf-8")
    step = wf[wf.index("Check the cloud npm package"):]
    calls = [line for line in step.splitlines() if line.strip().startswith("python3 scripts/check_npm_published.py")]
    assert len(calls) == 1 and calls[0].rstrip().endswith("$match"), calls
    assert 'match="--match-tree"' in step


# --------------------------------------------------------------------------- #
# Checksum manifest roots: signer and installer must list the same files.
# --------------------------------------------------------------------------- #

def _ls_files_roots(path: Path) -> set:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"ls-files \\\n((?:\s+[^\s|]+ \\\n)+)\s+\|", text)
    assert m, f"no `git ls-files` root list found in {path}"
    return {line.strip().rstrip("\\").strip() for line in m.group(1).splitlines() if line.strip()}


def test_signer_and_installer_checksum_the_same_roots():
    signer = _ls_files_roots(REPO / "scripts" / "sign-release.sh")
    installer = _ls_files_roots(REPO / "install.sh")
    assert signer == installer, (
        f"sign-release.sh checksums {sorted(signer)} but install.sh requires {sorted(installer)}: "
        "a root only the installer lists aborts every verified install; a root only the signer "
        "lists is fine; a root neither lists ships unverified.")
    assert {"hooks/", "skills/", "install.sh"} <= signer


def test_marketplace_entry_carries_the_plugin_version():
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = [p for p in market["plugins"] if p["name"] == plugin["name"]]
    assert len(entry) == 1 and entry[0]["version"] == plugin["version"], entry


def _flaky_download(files: dict, failures: int, calls: list):
    """npm right after a publish: the metadata resolves, the tarball 404s for a while."""
    def download(url):
        calls.append(url)
        if len(calls) <= failures:
            raise OSError("HTTP Error 404: Not Found")
        return _tgz(files)
    return download


def test_a_tarball_that_404s_right_after_publish_is_retried(shipped):
    """5.13.75: the registry document was up, the tarball URL 404'd once, and the gate failed a good release."""
    calls, naps = [], []
    ok, message = gate.match_tree("5.13.51", REPO, fetch=lambda v: {"tarball": "u", "integrity": "sha512-x"},
                                  download=_flaky_download(shipped, 2, calls),
                                  attempts=5, wait_seconds=15, sleep=naps.append)
    assert ok, message
    assert len(calls) == 3 and naps == [15, 15]


def test_a_tarball_that_never_arrives_fails_after_the_last_attempt(shipped):
    calls, naps = [], []
    ok, message = gate.match_tree("5.13.51", REPO, fetch=lambda v: {"tarball": "u", "integrity": "sha512-x"},
                                  download=_flaky_download(shipped, 99, calls),
                                  attempts=3, wait_seconds=7, sleep=naps.append)
    assert not ok and "404" in message and "could not download" in message
    assert len(calls) == 3 and naps == [7, 7]


def test_a_single_attempt_never_sleeps(shipped):
    calls, naps = [], []
    ok, _ = gate.match_tree("5.13.51", REPO, fetch=lambda v: {"tarball": "u", "integrity": "sha512-x"},
                            download=_flaky_download(shipped, 1, calls), sleep=naps.append)
    assert not ok and len(calls) == 1 and naps == []


def test_a_retried_download_is_still_compared_with_the_tree(shipped):
    """Retrying must not weaken the gate: the bytes that finally arrive are checked."""
    rel = "skills/token-optimizer/scripts/measure.py"
    older = dict(shipped)
    older[rel] = shipped[rel].replace(b"def ", b"def  ", 1)
    calls = []
    ok, message = gate.match_tree("5.13.51", REPO, fetch=lambda v: {"tarball": "u", "integrity": "sha512-x"},
                                  download=_flaky_download(older, 1, calls),
                                  attempts=3, wait_seconds=0, sleep=lambda s: None)
    assert not ok and "different bytes:  " + rel in message


def test_main_passes_the_retry_budget_to_the_tree_match(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(gate, "check", lambda *a, **k: (True, "published"))
    def fake_match(version, repo, fetch, **kwargs):
        seen.update(kwargs)
        return True, "matches"
    monkeypatch.setattr(gate, "match_tree", fake_match)
    assert gate.main(["--version", "5.13.51", "--match-tree", "--attempts", "60", "--wait", "15"]) == 0
    assert seen == {"attempts": 60, "wait_seconds": 15.0}
