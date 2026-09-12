"""The three script trees must stay byte-identical.

`skills/token-optimizer/scripts/`,
`plugins/token-optimizer/skills/token-optimizer/scripts/` (the Codex
marketplace mirror), and
`cowork/token-optimizer/skills/token-optimizer/scripts/` (the Cowork mirror)
ship the same files to different install paths. Nothing in the build copies
one to the others, so a fix applied to one copy and not the rest is invisible
until a user on the other install path reports the bug a second time.
measure.py alone is ~35k lines; that drift is silent under a manual diff.

This converts the invariant from discipline into a red test.

Intentional one-sided files are listed in ONE_SIDED. Adding a file to only
some trees is a deliberate act, so it must be a deliberate edit here too --
otherwise a file silently missing from an install path reads as "not
duplicated yet" rather than as a bug.
"""

import hashlib
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREES = {
    "skills": os.path.join(REPO_ROOT, "skills", "token-optimizer", "scripts"),
    "plugins": os.path.join(
        REPO_ROOT, "plugins", "token-optimizer", "skills", "token-optimizer", "scripts"
    ),
    "cowork": os.path.join(
        REPO_ROOT, "cowork", "token-optimizer", "skills", "token-optimizer", "scripts"
    ),
}

# Files that legitimately live in only one tree, relative to that tree's root.
# benchmark.py is a development harness, not shipped to the plugin install path.
ONE_SIDED = {"benchmark.py"}

# Never compared: build artifacts and caches are regenerated per-machine and
# carry no source meaning.
IGNORED_DIR_PARTS = {"__pycache__", ".pytest_cache"}


def _relative_files(root):
    """Every real file under root, relative to it, minus regenerable artifacts."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIR_PARTS]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def test_shared_scripts_are_byte_identical():
    """A file present in more than one tree must be the same file everywhere."""
    files_by_tree = {name: _relative_files(root) for name, root in TREES.items()}
    all_files = set().union(*files_by_tree.values())
    shared = {
        rel for rel in all_files if sum(rel in f for f in files_by_tree.values()) > 1
    }
    assert shared, "found no shared files -- the tree paths are probably wrong"

    drifted = []
    for rel in sorted(shared):
        digests = {
            name: _digest(os.path.join(root, rel))
            for name, root in TREES.items()
            if rel in files_by_tree[name]
        }
        if len(set(digests.values())) > 1:
            drifted.append(f"{rel} (differs across: {', '.join(sorted(digests))})")

    tree_list = "\n".join(
        f"  {os.path.relpath(root, REPO_ROOT)}/<file>" for root in TREES.values()
    )
    assert not drifted, (
        "These files differ between the script trees:\n  "
        + "\n  ".join(drifted)
        + "\n\nApply the change to ALL copies:\n"
        + tree_list
    )


def test_one_sided_files_are_declared():
    """A file not present in every tree must be an explicitly declared exception.

    Catches the other half of the drift class: not a changed file, but a NEW
    file added to one install path and forgotten in the others.
    """
    files_by_tree = {name: _relative_files(root) for name, root in TREES.items()}
    all_files = set().union(*files_by_tree.values())
    partial = sorted(
        rel for rel in all_files
        if sum(rel in f for f in files_by_tree.values()) < len(TREES)
    )
    undeclared = [rel for rel in partial if rel not in ONE_SIDED]

    assert not undeclared, (
        "These files are missing from at least one script tree:\n  "
        + "\n  ".join(undeclared)
        + "\n\nEither copy them to the other trees, or add them to ONE_SIDED "
        "in this test to record that the asymmetry is deliberate."
    )
