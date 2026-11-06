"""detect_runtime() caches its answer for the whole process. A test that forces
a foreign runtime must not leave that answer for the tests after it: in CI one
leftover "codex" made four unrelated tests in the same slice fail. The two
tests below run in file order; the second fails if the first one's answer leaks.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"))
import importlib  # noqa: E402


def _env():
    # Resolve through sys.modules at call time: earlier files that pop and
    # re-import runtime_env leave a duplicate instance, and the bound name
    # would keep a stale cache the conftest cleanup never sees (CI afab5255).
    return importlib.import_module("runtime_env")


def test_a_forces_codex_and_leaves_without_cleaning(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "codex")
    _env().detect_runtime.cache_clear()
    assert _env().detect_runtime() == "codex"


def test_b_next_test_does_not_inherit_codex(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    assert _env().detect_runtime() == "claude"
