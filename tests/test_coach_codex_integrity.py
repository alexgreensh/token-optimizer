"""Coaching must use consistent inventories and evidence-backed estimates.

Codex-runtime integrity for `measure.py coach` and the session detectors:
the Codex headline must count the real advertised inventory, savings claims must
come from per-skill measurements (never an inventory-wide upper bound or a flat
per-skill guess), Claude-specific guidance must not leak into the Codex runtime,
elapsed duration alone must not dock the health score, the fill->quality curve
must match the session's model family, and long Codex descriptions must stay
visible instead of silently disappearing behind a Claude-only truncation claim.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
from detectors import cache_instability, output_waste, respond_to_bash  # noqa: E402


def _runtime(monkeypatch, name):
    monkeypatch.setattr(measure, "detect_runtime", lambda: name)


def _coach_env(monkeypatch, runtime):
    _runtime(monkeypatch, runtime)
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: [])
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})


def _skills_detail(names, tokens=10):
    return {n: {"name": n, "frontmatter_tokens": tokens} for n in names}


# --- Ported from Astra's partial patch ---------------------------------------


def test_foreign_runtime_does_not_read_claude_settings(monkeypatch):
    monkeypatch.setattr(respond_to_bash, 'detect_runtime', lambda: 'codex', raising=False)
    monkeypatch.setattr(respond_to_bash, '_load_settings', lambda p: (_ for _ in ()).throw(AssertionError('foreign read')))
    assert respond_to_bash.detect_respond_to_bash({}) == []


def test_coach_codex_inventory_and_projections(monkeypatch):
    _coach_env(monkeypatch, 'codex')
    names = [f'skill-{i}' for i in range(30)]
    components = {
        'skills': {'count': 30, 'tokens': 300, 'names': names},
        'skills_detail': _skills_detail(names, tokens=10),
        'plugin_skills': {'count': 10, 'tokens': 200, 'names': [f'plugin-{i}' for i in range(10)]},
        'hooks': {'configured': True, 'names': ['Stop']},
    }
    trends = {
        'skills': {'never_used': names + ['token-coach', 'missing-skill']},
        'daily': [{'session_details': [{'duration_minutes': 500}]}] * 7 + [{'session_details': [{'duration_minutes': 70}]}],
        'total_cost_usd': 100, 'session_count': 10,
    }
    result = measure.generate_coach_data(components=components, trends=trends)
    assert result['snapshot']['skill_count'] == 40
    assert result['snapshot']['skill_tokens'] == 500
    patterns = {p['name']: p for p in result['patterns_bad']}
    unused = patterns['Many Unused Skills']
    assert '30 of 40' in unused['detail']
    # Own measurement skills and names that are not installed are filtered out;
    # savings reflect measured descriptions, not a count x constant guess.
    assert 'token-coach' not in unused['detail']
    assert 'upper bound' not in unused['savings'].lower()
    assert 'not estimated' in patterns['Session Duration Creep']['savings'].lower()
    cost = patterns['High Cost Per Session']
    assert 'estimated' in cost['detail'].lower()
    assert 'Sonnet' not in cost['fix'] and 'Haiku' not in cost['fix']
    assert '$3.00' not in cost['savings']


def test_claude_setting_warning_still_works(monkeypatch):
    monkeypatch.setattr(respond_to_bash, 'detect_runtime', lambda: 'claude')
    monkeypatch.setattr(respond_to_bash, '_load_settings', lambda p: {})
    assert respond_to_bash.detect_respond_to_bash({})[0]['name'] == 'respond_to_bash_commands'
    monkeypatch.setattr(respond_to_bash, '_load_settings', lambda p: {'respondToBashCommands': False})
    assert respond_to_bash.detect_respond_to_bash({}) == []


# --- W1: unused-skill savings = measured subset, or "unknown" ----------------


def test_codex_unused_savings_uses_measured_subset(monkeypatch):
    """Savings sum the unused candidates' own measured frontmatter tokens —
    not the whole inventory and not count x a flat constant."""
    _coach_env(monkeypatch, 'codex')
    names = [f'skill-{i}' for i in range(30)]
    detail = _skills_detail(names, tokens=10)
    detail['skill-0']['frontmatter_tokens'] = 500  # one heavy skill
    components = {
        'skills': {'count': 30, 'tokens': 6000, 'names': names},
        'skills_detail': detail,
        'plugin_skills': {'count': 0, 'tokens': 0, 'names': []},
        'hooks': {'configured': True, 'names': ['Stop']},
    }
    trends = {'skills': {'never_used': list(names)}}
    result = measure.generate_coach_data(components=components, trends=trends)
    bad = {p['name']: p for p in result['patterns_bad']}
    unused = bad.get('Many Unused Skills') or bad['Unused Skill Overhead']
    # measured: 29 x 10 + 500 = 790. NOT the 6,000-token inventory total and
    # NOT 30 x 100.
    assert '790' in unused['savings']
    assert '6,000' not in unused['savings']
    assert '3,000' not in unused['savings']
    assert 'upper bound' not in unused['savings'].lower()


def test_unused_savings_unknown_when_unmeasured(monkeypatch):
    """No skills_detail -> honest 'unknown', never an inflated bound."""
    _coach_env(monkeypatch, 'claude')
    names = [f'skill-{i}' for i in range(30)]
    components = {
        'skills': {'count': 30, 'tokens': 6000, 'names': names},
        'hooks': {'configured': True, 'names': ['SessionEnd']},
    }
    trends = {'skills': {'never_used': list(names)}}
    result = measure.generate_coach_data(components=components, trends=trends)
    bad = {p['name']: p for p in result['patterns_bad']}
    unused = bad.get('Many Unused Skills') or bad['Unused Skill Overhead']
    assert 'unknown' in unused['savings'].lower()
    assert '6,000' not in unused['savings']
    assert '3,000' not in unused['savings']


# --- W2: runtime isolation — no Claude guidance leaks into Codex -------------

_CLAUDE_ONLY_TOKENS = ('Sonnet', 'Haiku', 'Opus', 'CLAUDE.md', '/model',
                       'respondToBash', '.claude/settings', '~/.claude',
                       'claude.ai')


def _multi_model_sessions(n, *, model_count=2, chr_=0.2, dur=500, inp=100_000):
    return [{'duration_minutes': dur, 'model_count': model_count,
             'cache_hit_rate': chr_, 'input_tokens': inp} for _ in range(n)]


def test_codex_coach_output_has_no_claude_guidance(monkeypatch):
    """Every pattern the coach emits under Codex must speak Codex."""
    _coach_env(monkeypatch, 'codex')
    components = {
        'skills': {'count': 5, 'tokens': 50, 'names': [f's{i}' for i in range(5)]},
        'plugin_skills': {'count': 0, 'tokens': 0, 'names': []},
        'hooks': {'configured': True, 'names': ['Stop']},
    }
    daily = (
        [{'session_details': _multi_model_sessions(1)} for _ in range(7)]
        + [{'session_details': [{'duration_minutes': 70, 'model_count': 1,
                                 'cache_hit_rate': 0.6, 'input_tokens': 20_000}]}]
    )
    # Run A: model-switch-heavy recent sessions trigger the model-aware cache
    # branch, frequent-switching, duration and cost patterns.
    trends_a = {'skills': {'never_used': []}, 'daily': daily,
                'total_cost_usd': 100, 'session_count': 10}
    result_a = measure.generate_coach_data(components=components, trends=trends_a)
    names_a = {p['name'] for p in result_a['patterns_bad']}
    assert 'Cache Hit Rate Dropping (Model Switches)' in names_a
    assert 'Frequent Model Switching' in names_a
    assert 'High Cost Per Session' in names_a

    # Run B: single-model recent sessions trigger the plain cache branch.
    daily_b = (
        [{'session_details': _multi_model_sessions(1, model_count=1)} for _ in range(7)]
        + [{'session_details': [{'duration_minutes': 70, 'model_count': 1,
                                 'cache_hit_rate': 0.6, 'input_tokens': 20_000}]}]
    )
    trends_b = {'skills': {'never_used': []}, 'daily': daily_b,
                'total_cost_usd': 100, 'session_count': 10}
    result_b = measure.generate_coach_data(components=components, trends=trends_b)
    names_b = {p['name'] for p in result_b['patterns_bad']}
    assert 'Cache Hit Rate Dropping' in names_b

    for result in (result_a, result_b):
        for p in result['patterns_bad']:
            blob = f"{p.get('detail', '')} {p.get('fix', '')} {p.get('savings', '')}"
            for token in _CLAUDE_ONLY_TOKENS:
                assert token not in blob, f"{p['name']} leaks {token!r} under Codex: {blob}"
        for q in result.get('questions', []):
            for token in _CLAUDE_ONLY_TOKENS:
                assert token not in q, f"question leaks {token!r} under Codex: {q}"


def test_cache_instability_is_claude_only(monkeypatch):
    """The detector only inspects Anthropic prompt-cache surfaces (CLAUDE.md,
    .mcp.json, .claude.json, .claude/processes) — it must not fire under Codex."""
    monkeypatch.setattr(cache_instability, 'detect_runtime', lambda: 'codex', raising=False)
    assert cache_instability.detect_cache_instability({}) == []
    monkeypatch.setattr(cache_instability, 'detect_runtime', lambda: 'claude')
    findings = cache_instability.detect_cache_instability(
        {'claude_md_content': 'x' * 300 + '\nLast updated: 2026-01-01 rest of line here padded out'})
    assert isinstance(findings, list)  # runs; may or may not find, must not raise


def test_output_waste_suggests_agents_md_under_codex(monkeypatch):
    monkeypatch.setattr(output_waste, 'detect_runtime', lambda: 'codex', raising=False)
    turns = [{'tools_used': ['Read'], 'input_tokens': 100, 'output_tokens': 5000}
             for _ in range(6)]
    findings = output_waste.detect_output_waste({'turns': turns, 'total_input_tokens': 600,
                                                 'total_output_tokens': 30_000})
    text = ' '.join(f.get('suggestion', '') + f.get('evidence', '') for f in findings)
    assert 'CLAUDE.md' not in text


# --- W3: elapsed duration alone must not dock health --------------------------


def _duration_trends(*, recent_dur, older_dur, recent_inp=None, older_inp=None):
    def _s(dur, inp):
        s = {'duration_minutes': dur}
        if inp is not None:
            s['input_tokens'] = inp
        return s
    daily = ([{'session_details': [_s(recent_dur, recent_inp)]}] * 7
             + [{'session_details': [_s(older_dur, older_inp)]}])
    return {'skills': {'never_used': []}, 'daily': daily}


def _base_components():
    return {
        'skills': {'count': 5, 'tokens': 50,
                   'names': [f's{i}' for i in range(5)]},
        'hooks': {'configured': True, 'names': ['Stop']},
    }


def test_duration_without_token_growth_does_not_dock_health(monkeypatch):
    """Idle-time-driven duration inflation is informational, not a score dock."""
    _coach_env(monkeypatch, 'codex')
    creep = measure.generate_coach_data(
        components=_base_components(),
        trends=_duration_trends(recent_dur=500, older_dur=70))
    flat = measure.generate_coach_data(
        components=_base_components(),
        trends=_duration_trends(recent_dur=70, older_dur=70))
    assert creep['health_score'] == flat['health_score']
    creep_names = {p['name']: p for p in creep['patterns_bad']}
    if 'Session Duration Creep' in creep_names:
        assert creep_names['Session Duration Creep']['severity'] == 'low'


def test_duration_with_token_growth_docks_health(monkeypatch):
    """Duration creep PLUS real per-session token growth is evidence — dock."""
    _coach_env(monkeypatch, 'codex')
    creep = measure.generate_coach_data(
        components=_base_components(),
        trends=_duration_trends(recent_dur=500, older_dur=70,
                                recent_inp=100_000, older_inp=50_000))
    flat = measure.generate_coach_data(
        components=_base_components(),
        trends=_duration_trends(recent_dur=70, older_dur=70,
                                recent_inp=50_000, older_inp=50_000))
    creep_names = {p['name']: p for p in creep['patterns_bad']}
    assert 'Session Duration Creep' in creep_names
    assert creep['health_score'] < flat['health_score']


# --- W4: Codex quality estimate uses a Codex-appropriate basis ----------------


def _quick_scan(monkeypatch, codex_cfg):
    _runtime(monkeypatch, 'codex')
    monkeypatch.delenv('CODEX_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.setattr(measure, 'measure_components', lambda: {})
    monkeypatch.setattr(measure, 'detect_context_window', lambda: (258400, 'test'))
    monkeypatch.setattr(measure, '_collect_trends_data', lambda **kw: None)
    monkeypatch.setattr(measure, '_auto_snapshot', lambda *a, **kw: None)
    monkeypatch.setattr(measure, '_read_codex_config', lambda: codex_cfg)
    return measure.quick_scan(as_json=True)


def test_codex_quality_estimate_uses_openai_curve(monkeypatch):
    result = _quick_scan(monkeypatch, {'model': 'gpt-5.5-codex'})
    assert result['quality_curve'] == 'openai-gpt-5.5'
    # Fill->quality is always an estimate, and the label says so.
    assert 'heuristic' in result['quality_basis'].lower()


def test_codex_quality_estimate_labels_heuristic_when_model_unknown(monkeypatch):
    """Unresolvable Codex model -> the Codex-appropriate gpt-5 curve is used
    (Codex sessions run gpt-5.x-codex variants) and the result is labeled as a
    heuristic, never presented as Anthropic-measured Codex evidence."""
    result = _quick_scan(monkeypatch, {})
    assert result['quality_curve'] == 'openai-gpt-5'
    assert 'anthropic' not in result['quality_curve']
    assert 'heuristic' in result['quality_basis'].lower()


# --- W5: inventory matches what Codex actually loads --------------------------


def _write_skill(path: Path, name: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: a test skill\n---\nbody\n",
                    encoding="utf-8")


def test_disabled_plugin_skills_not_counted_active(tmp_path, monkeypatch):
    """Skills under ~/.codex/plugins/cache belong to a plugin; when config.toml
    disables that plugin the skills are not loaded — the inventory must not
    report them as active."""
    monkeypatch.setattr(measure, 'RUNTIME_DIR', tmp_path)
    cache = tmp_path / 'plugins' / 'cache'
    _write_skill(cache / 'mkt' / 'plug-off' / '1.0' / 'skills' / 'off-skill' / 'SKILL.md', 'off-skill')
    _write_skill(cache / 'mkt' / 'plug-on' / '1.0' / 'skills' / 'on-skill' / 'SKILL.md', 'on-skill')
    _write_skill(tmp_path / 'skills' / 'user-skill' / 'SKILL.md', 'user-skill')
    cfg = {'plugins': {'plug-off@mkt': {'enabled': False},
                       'plug-on@mkt': {'enabled': True}}}
    inv = measure._collect_codex_skill_inventory(cfg, project=tmp_path / 'proj')
    active = {i['name'] for i in inv['active']}
    disabled = {i['name'] for i in inv['disabled']}
    assert 'user-skill' in active
    assert 'on-skill' in active
    assert 'off-skill' not in active
    assert 'off-skill' in disabled


def test_codex_snapshot_labels_inventory_basis(monkeypatch):
    _coach_env(monkeypatch, 'codex')
    result = measure.generate_coach_data(
        components=_base_components(), trends={'skills': {'never_used': []}})
    basis = result['snapshot'].get('skills_basis', '')
    assert basis, "Codex snapshot must label what the skill count actually measures"
    assert 'not directly observed' in basis or 'advertised' in basis


# --- W6: long Codex descriptions must not silently disappear ------------------


def test_codex_long_descriptions_still_surface(monkeypatch):
    """A >1,536-char description is flagged 'truncated' by the shared scan, but
    that truncation is a Claude Code behavior. Under Codex the entry must still
    surface as a long description, not vanish from every warning."""
    _coach_env(monkeypatch, 'codex')
    components = _base_components()
    components['skill_frontmatter_quality'] = {'verbose_skills': [
        {'name': 'mega-skill', 'description_chars': 2000, 'truncated': True},
        {'name': 'long-a', 'description_chars': 300, 'truncated': False},
        {'name': 'long-b', 'description_chars': 250, 'truncated': False},
    ]}
    result = measure.generate_coach_data(components=components, trends={'skills': {'never_used': []}})
    names = {p['name'] for p in result['patterns_bad']}
    assert 'Truncated Skill Descriptions' not in names
    verbose = {p['name']: p for p in result['patterns_bad']}.get('Verbose Skill Descriptions')
    assert verbose is not None, 'long Codex descriptions vanished from all warnings'
    assert '3' in verbose['detail']  # the >1536 entry still counted
    assert 'truncat' not in (verbose['detail'] + verbose['fix']).lower()


def test_claude_truncated_warning_preserved(monkeypatch):
    """Claude path unchanged: >1,536-char descriptions still get the truncation
    warning (Claude Code really does silently cut them)."""
    _coach_env(monkeypatch, 'claude')
    components = _base_components()
    components['skill_frontmatter_quality'] = {'verbose_skills': [
        {'name': 'mega-skill', 'description_chars': 2000, 'truncated': True},
        {'name': 'long-a', 'description_chars': 300, 'truncated': False},
        {'name': 'long-b', 'description_chars': 250, 'truncated': False},
        {'name': 'long-c', 'description_chars': 220, 'truncated': False},
    ]}
    result = measure.generate_coach_data(components=components, trends={'skills': {'never_used': []}})
    names = {p['name'] for p in result['patterns_bad']}
    assert 'Truncated Skill Descriptions' in names
    assert 'Verbose Skill Descriptions' in names


# --- Claude-runtime regression: correct Claude text is preserved --------------


def test_claude_coach_text_preserved(monkeypatch):
    """Where the existing guidance was already correct for Claude, it stays."""
    _coach_env(monkeypatch, 'claude')
    components = _base_components()
    trends = {
        'skills': {'never_used': []},
        'daily': (
            [{'session_details': _multi_model_sessions(1)} for _ in range(7)]
            + [{'session_details': [{'duration_minutes': 70, 'model_count': 1,
                                     'cache_hit_rate': 0.6, 'input_tokens': 20_000}]}]
        ),
        'total_cost_usd': 100, 'session_count': 10,
    }
    result = measure.generate_coach_data(components=components, trends=trends)
    patterns = {p['name']: p for p in result['patterns_bad']}
    assert 'Sonnet' in patterns['High Cost Per Session']['fix']
    assert '/model' in patterns['Frequent Model Switching']['fix']
    assert '/model' in patterns['Cache Hit Rate Dropping (Model Switches)']['fix']


def test_codex_config_reads_full_toml_on_supported_python(monkeypatch, tmp_path):
    """Python 3.9/3.10 must not silently discard real Codex configuration."""
    monkeypatch.setattr(measure, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(measure, "_codex_config_cache", None)
    (tmp_path / "config.toml").write_text('''
model = "gpt-5.4"
model_context_window = 1_000_000
tool_output_token_limit = 8_192
notes = """first line
[not.a.table]
last line"""
when = 2026-11-06T12:00:00Z
[mcp_servers."service.with.dots"]
command = 'example-server'
args = ["--one", "--two",]
env = { MODE = "test", "key.with.dots" = "yes" }
[[skills.config]]
path = '/tmp/a skill/SKILL.md'
enabled = false
''', encoding="utf-8")
    config = measure._read_codex_config()
    assert config["model"] == "gpt-5.4"
    assert config["model_context_window"] == 1_000_000
    assert config["tool_output_token_limit"] == 8192
    assert "[not.a.table]" in config["notes"]
    assert config["when"].isoformat() == "2026-11-06T12:00:00+00:00"
    assert config["mcp_servers"]["service.with.dots"]["env"]["key.with.dots"] == "yes"
    assert config["skills"]["config"][0]["enabled"] is False
    assert measure._read_codex_config() is config
    assert measure._codex_config_model() == "gpt-5.4"
    assert measure._codex_config_int("model_context_window") == 1_000_000
    assert measure._codex_config_int("tool_output_token_limit") == 8192


def test_codex_config_invalid_or_missing_still_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(measure, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(measure, "_codex_config_cache", None)
    assert measure._read_codex_config() == {}
    (tmp_path / "config.toml").write_text('model = "one"\nmodel = "two"\n', encoding="utf-8")
    assert measure._read_codex_config() == {}


@pytest.mark.parametrize("text", [
    'x=1_000\ny=0xff\nz=0o755\nw=0b1010\n',
    'x=1.2e3\ny=-0.0\n',
    '"a.b" = "v"\na.b.c = 4\n',
    '[[products]]\nname="one"\n[[products]]\nname="two"\n',
    "x='''literal\n[a]\nend'''\n",
    'x = [[1,2], [3,4], ]\ny={a="b",c=[1,2]}\n',
    'x=1979-05-27T07:32:00Z\ny=1979-05-27\nz=07:32:00\n',
    'x="\\u03B4 \\U0001F600"\n',
    '[a]\n[a.b]\nx=true\n',
])
def test_bundled_toml_reader_matches_runtime_parser(text):
    from _measure._vendor import tomli
    assert tomli.loads(text) == measure.tomllib.loads(text)


@pytest.mark.parametrize("text", [
    'x=1\nx=2', 'x=01', 'x=[1,', '[a]\n[a]',
    'x="\\q"', 'x={a=1,a=2}', 'x = "\x00"',
])
def test_bundled_toml_reader_rejects_invalid_documents(text):
    from _measure._vendor import tomli
    with pytest.raises(tomli.TOMLDecodeError):
        tomli.loads(text)


_LAZY_TOMLI_PROBE = r'''
import sys, importlib.abc
class _NoStdlibToml(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "tomllib":
            raise ImportError("simulated Python 3.9/3.10")
sys.meta_path.insert(0, _NoStdlibToml())
sys.path.insert(0, sys.argv[1])
import measure
parser = "_measure._vendor.tomli._parser"
before = parser in sys.modules
data = measure.tomllib.loads("a = 1\n[b]\nc = [1.5]\n")
after = parser in sys.modules
from _measure._vendor import tomli
print(before, after, data == {"a": 1, "b": {"c": [1.5]}}, tomli is measure.tomllib,
      issubclass(measure.tomllib.TOMLDecodeError, ValueError))
'''


def test_vendored_tomli_fallback_runs_only_on_first_parse(tmp_path):
    """3.9/3.10 facade import must not execute Tomli; the first parse does."""
    import os
    import subprocess
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    out = subprocess.run(
        [sys.executable, "-c", _LAZY_TOMLI_PROBE, str(SCRIPTS)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "True", "True", "True", "True"], out.stdout


_LAZY_TOMLI_THREADS_PROBE = r'''
import sys, threading, importlib.abc
class _NoStdlibToml(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "tomllib":
            raise ImportError("simulated Python 3.9/3.10")
sys.meta_path.insert(0, _NoStdlibToml())
sys.path.insert(0, sys.argv[1])
import measure
barrier = threading.Barrier(8)
errors = []
def worker():
    try:
        barrier.wait(timeout=10)
        for _ in range(10):
            assert measure.tomllib.loads("a = 1\n[b]\nc = [1.5]\n") == {"a": 1, "b": {"c": [1.5]}}
    except Exception as exc:
        errors.append(repr(exc))
threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("errors", len(errors), errors[:1])
'''


def test_vendored_tomli_first_parse_is_thread_safe(tmp_path):
    """Concurrent first parses must not observe a half-loaded lazy module.

    Python 3.9/3.10's importlib LazyLoader is lock-free; eight threads racing
    the first attribute access used to raise AttributeError on the lazy
    module. The locked lazy loader must serialize the first exec instead.
    """
    import os
    import subprocess
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    out = subprocess.run(
        [sys.executable, "-c", _LAZY_TOMLI_THREADS_PROBE, str(SCRIPTS)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[:2] == ["errors", "0"], out.stdout
