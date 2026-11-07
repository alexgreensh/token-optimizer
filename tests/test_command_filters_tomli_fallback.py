"""command-filters.toml fallback parser on Python versions without tomllib.

Python 3.9/3.10 have no stdlib tomllib, so the loader falls back to the
vendored flat Tomli module (_tomli). It is a full TOML 1.0 parser, so files
outside the old hand-written subset (floats, multi-line strings, arrays of
tables, dates, hex) now load instead of being skipped with a warning.
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import command_filters as cf  # noqa: E402

try:
    import tomllib
except ImportError:  # Python 3.9 / 3.10
    tomllib = None

README_EXAMPLE = '''# Add a read-only command to an existing compression handler.
[filters.add]
my-tests = { command = "cargo test", handler = "pytest" }
my-lint  = { command = "ruff check", handler = "lint" }

# Exclude commands from compression (glob patterns supported).
[filters.exclude]
commands = ["git status", "ls -la"]
'''

# TOML the retired subset reader rejected: float, multi-line string, array of
# tables, date/time and hex integer. Full Tomli accepts all of it.
EXOTIC = '''[filters.add]
my-tests = { command = "cargo test", handler = "pytest" }

[filters.exclude]
commands = [
  "git status",
  """multi
line""",
]
pi = 3.14
when = 1979-05-27T07:32:00Z
hex = 0x10

[[filters.meta]]
note = "ignored"
'''


@pytest.mark.parametrize("stdlib", [True, False])
def test_loader_reads_readme_example(tmp_path, monkeypatch, stdlib, capsys):
    if stdlib and tomllib is None:
        pytest.skip("no tomllib on this interpreter")
    if not stdlib:
        monkeypatch.setattr(cf, "tomllib", None)
    path = tmp_path / "command-filters.toml"
    path.write_text(README_EXAMPLE, encoding="utf-8")
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(path))
    cf.reset_cache()
    config = cf.load_filters()
    assert {a.name: (a.command, a.handler) for a in config.adds} == {
        "my-tests": ("cargo test", "pytest"), "my-lint": ("ruff check", "lint")}
    assert config.excludes == ("git status", "ls -la")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("stdlib", [True, False])
def test_full_toml_outside_old_subset_loads_without_warning(
        tmp_path, monkeypatch, stdlib, capsys):
    """Syntax the retired subset reader rejected now parses cleanly."""
    if stdlib and tomllib is None:
        pytest.skip("no tomllib on this interpreter")
    if not stdlib:
        monkeypatch.setattr(cf, "tomllib", None)
        assert cf._load_tomli_fallback() is not None
    path = tmp_path / "command-filters.toml"
    path.write_text(EXOTIC, encoding="utf-8")
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(path))
    cf.reset_cache()
    config = cf.load_filters()
    assert [a.name for a in config.adds] == ["my-tests"]
    assert config.excludes == ("git status", "multi\nline")
    assert capsys.readouterr().err == ""


def test_fallback_parser_is_vendored_tomli(tmp_path, monkeypatch):
    """The 3.9/3.10 fallback is the shipped _tomli module, not a subset."""
    monkeypatch.setattr(cf, "tomllib", None)
    parser = cf._load_tomli_fallback()
    assert parser is not None
    assert parser.loads("a = 1.5\nb = [1, 2]\n") == {"a": 1.5, "b": [1, 2]}
    with pytest.raises(parser.TOMLDecodeError):
        parser.loads("x = [1,")


def test_malformed_file_fails_open_and_keeps_safety_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "tomllib", None)
    path = tmp_path / "command-filters.toml"
    path.write_text('[filters.add]\nx = { command = "cargo test", handler = "pytest" }\n[[broken\n',
                    encoding="utf-8")
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(path))
    cf.reset_cache()
    assert cf.load_filters() == cf.CommandFilterConfig()
    path.write_text('[filters.add]\nbad = { command = "sudo ls", handler = "ls" }\n'
                    'ok = { command = "cargo test", handler = "pytest" }\n', encoding="utf-8")
    cf.reset_cache()
    assert [a.name for a in cf.load_filters().adds] == ["ok"]


def test_non_utf8_file_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "tomllib", None)
    path = tmp_path / "command-filters.toml"
    path.write_bytes(b"[a]\nk = '\xff\xfe'\n")
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(path))
    cf.reset_cache()
    assert cf.load_filters() == cf.CommandFilterConfig()


@pytest.mark.parametrize("stdlib", [True, False])
def test_deeply_nested_file_fails_open(tmp_path, monkeypatch, stdlib):
    if stdlib and tomllib is None:
        pytest.skip("no tomllib on this interpreter")
    if not stdlib:
        monkeypatch.setattr(cf, "tomllib", None)
    path = tmp_path / "command-filters.toml"
    path.write_text("a = " + "[" * 100000 + "]" * 100000 + "\n", encoding="utf-8")
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(path))
    cf.reset_cache()
    assert cf.load_filters() == cf.CommandFilterConfig()
