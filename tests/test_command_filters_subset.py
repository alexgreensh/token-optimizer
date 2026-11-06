"""command-filters.toml must load on Python versions without tomllib (3.9, 3.10).

The fallback reader accepts a strict subset of TOML. Where tomllib exists, every
file the fallback accepts must parse to the identical value, and a file tomllib
rejects must never be accepted by the fallback.
"""
import random
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

ACCEPTED = [
    'filters.add.my-tests = { command = "cargo test", handler = "pytest" }\n[filters.exclude]\ncommands = ["git status"]\n',
    "a.b.c = 1\n[a.d]\nx = 1\n[a.b.e]\ny = 2\n",
    "[a.b.c]\n[a.b]\n[a]\n",
    "a.b = 1\na.c.d = 2\n[x]\ny = 1\n",
    README_EXAMPLE,
    "",
    "# only a comment\n",
    "[a]\nk = 'lit\\eral'\n",
    '[a.b]\nk = "esc \\" \\\\ \\n \\t \\u00e9 \\U0001F600"\n',
    '["quoted key"."x y"]\n"k k" = 1\n',
    "[a]\nx = [\n  'one', # c\n  \"two\",\n]\n",
    "[a]\nx = []\ny = {}\nz = { p = 1, q.r = true }\n",
    "a.b = 1\na.c = false\n",
    "[a]\nn = -12\nm = +7\n",
    "[a]\nk = 1 # trailing\r\nj = 2\r\n",
    "[a.b]\nx = 1\n[a]\ny = 2\n",
    "[b.b.b]\n[b]\nb.a.b = 1\n",
    "[a.b.c]\nx = 1\n[a]\nb.d = 1\n",
    "[a.b.c]\n[a]\nb.d = 1\nb.e.f = 2\n",
]

REJECTED = [
    "a.b.c = 1\n[a.b]\nx = 1\n",
    "a.b = 1\n[a]\nx = 1\n",
    "a = {b = 1}\n[a.c]\nx = 1\n",
    "a = " + "[" * 5000 + "]" * 5000 + "\n",
    "a = " + "{b=" * 3000 + "1" + "}" * 3000 + "\n",
    "a.bx = 1\n[a]\ny = 2\n",
    "a = {x = 1}\n[a]\ny = 2\n",
    "a = {x = 1}\na.y = 2\n",
    "[a.b]\nx = 1\n[a]\nb.c = 1\n",
    "[a.b]\n[a]\nb.c = 1\n",
    "[a.b.c]\n[a]\nb.c.x = 1\n",
    "[a.b.c]\n[a]\nb.d = 1\n[a.b]\n",
    "[a]\nb = {x = 1}\n[a.b.c]\n",
    "a.b = 1\n[a.b]\n",
    "\ufeff[a]\nk = 1\n",
    '[a]\nk = """multi\nline"""\n',
    "[a]\nk = '''multi\nline'''\n",
    "[[a]]\nk = 1\n",
    "[a]\nk = 1.5\n",
    "[a]\nk = 1979-05-27\n",
    "[a]\nk = 0x10\n",
    "[a]\nk = 01\n",
    "[a]\nk = 1\nk = 2\n",
    "[a]\n[a]\n",
    "a = 1\n[a]\nk = 1\n",
    "[a]\nk = \"unterminated\n",
    "[a]\nk = 'unterminated\n",
    '[a]\nk = "bad \\q"\n',
    '[a]\nk = "\\ud800"\n',
    "[a]\nk = [1, 2\n",
    "[a]\nk = {x = 1,}\n",
    "[a]\nk = {x = 1\n}\n",
    "[a]\nk = tru\n",
    "[a]\nk = 1 2\n",
    "[a\nk = 1\n",
    "k\n",
    "= 1\n",
    "[a]\nk = \"tab\x01ctrl\"\n",
]


@pytest.mark.parametrize("text", ACCEPTED)
def test_accepted_text_matches_stdlib(text):
    got = cf._loads_subset(text)
    if tomllib is not None:
        assert got == tomllib.loads(text)


@pytest.mark.parametrize("text", REJECTED)
def test_unsupported_or_invalid_text_is_rejected(text):
    with pytest.raises(ValueError):
        cf._loads_subset(text)


def test_readme_example_value():
    assert cf._loads_subset(README_EXAMPLE) == {"filters": {
        "add": {"my-tests": {"command": "cargo test", "handler": "pytest"},
                "my-lint": {"command": "ruff check", "handler": "lint"}},
        "exclude": {"commands": ["git status", "ls -la"]}}}


@pytest.mark.skipif(tomllib is None, reason="needs tomllib to compare against")
def test_mutations_never_accept_what_stdlib_rejects_or_read_differently():
    rng = random.Random(20261106)
    alphabet = list('[]{}=,."\'#\\ \n\t01aZ-_+xu')
    seeds = ACCEPTED + REJECTED
    for _ in range(4000):
        text = list(rng.choice(seeds))
        for _ in range(rng.randint(1, 4)):
            op = rng.choice("ids")
            pos = rng.randrange(len(text) + 1)
            if op == "i":
                text.insert(pos, rng.choice(alphabet))
            elif op == "d" and text:
                del text[min(pos, len(text) - 1)]
            elif text:
                text[min(pos, len(text) - 1)] = rng.choice(alphabet)
        text = "".join(text)
        try:
            expected = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError, RecursionError):
            expected = None
        try:
            got = cf._loads_subset(text)
        except ValueError:
            continue
        assert expected is not None, f"subset accepted invalid TOML: {text!r}"
        assert got == expected, f"subset read {text!r} differently"


@pytest.mark.parametrize("stdlib", [True, False])
def test_loader_reads_readme_example_without_tomllib(tmp_path, monkeypatch, stdlib):
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


def test_unsupported_file_fails_open_and_keeps_safety_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "tomllib", None)
    path = tmp_path / "command-filters.toml"
    path.write_text('[filters.add]\nx = { command = "cargo test", handler = "pytest" }\n[[more]]\n', encoding="utf-8")
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
