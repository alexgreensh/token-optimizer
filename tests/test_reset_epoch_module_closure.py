"""Install closure for the reset-epoch module (K2).

session_store.py and read_cache.py import `reset_epoch` as a plain sibling
module. Every runtime tree that ships those two files must ship the sibling
too, byte-identical, or the read-cache hook fails to import at runtime.
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREES = (
    ROOT / "skills/token-optimizer/scripts",
    ROOT / "plugins/token-optimizer/skills/token-optimizer/scripts",
    ROOT / "cowork/token-optimizer/skills/token-optimizer/scripts",
)
MODULE = "reset_epoch"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reset_epoch_shipped_in_every_tree_byte_identical():
    digests = {t: _digest(t / f"{MODULE}.py") for t in TREES}
    assert len(set(digests.values())) == 1, digests


def test_every_tree_that_imports_it_ships_it():
    pat = re.compile(rf"^\s*from {MODULE} import ", re.M)
    for tree in TREES:
        importers = [p.name for p in tree.glob("*.py") if pat.search(p.read_text(encoding="utf-8"))]
        assert sorted(importers) == ["read_cache.py", "session_store.py"], (tree, importers)
        assert (tree / f"{MODULE}.py").is_file(), tree


def test_devin_loader_classifies_it_as_repo_module():
    # Devin's closed-set loader must refuse (not silently resolve) repo modules
    # outside its payload; reset_epoch is one, like its importers.
    src = (TREES[0] / "devin_source.py").read_text(encoding="utf-8")
    assert f"'{MODULE}'" in src
