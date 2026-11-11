import json
import subprocess
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/dashboard.html'


def test_missing_token_breakdown_is_not_rendered():
    html = HTML.read_text()
    start = html.index('  function tokensSavedCardHtml(')
    end = html.index('\n  function renderTrends()', start)
    script = html[start:end] + '''
      console.log(JSON.stringify(tokensSavedCardHtml({token_headline: {total_tokens: 5}})));
    '''
    prelude = "var data={trends:{}}; function fn(n){return String(n)};\n"
    rendered = json.loads(subprocess.check_output(['node', '-e', prelude + script], text=True))
    assert 'Tokens Saved' in rendered
    assert 'Tokens by type' not in rendered
    assert 'Measuring' not in rendered


def test_dashboard_has_no_measuring_placeholder():
    assert 'Measuring' not in HTML.read_text()


def test_available_token_classes_render_without_missing_ones():
    html = HTML.read_text()
    start = html.index('  function tokensSavedCardHtml(')
    end = html.index('\n  function renderTrends()', start)
    prelude = 'var data={trends:{}}; function fn(n){return String(n)};\n'
    call = "console.log(tokensSavedCardHtml({token_headline:{total_tokens:100},token_type_breakdown:{cache_write:42}}));"
    rendered = subprocess.check_output(['node', '-e', prelude + html[start:end] + call], text=True)
    assert 'Modeled cache writes avoided' in rendered and '>42<' in rendered
    assert 'Modeled cache re-reads avoided' not in rendered
    assert 'Fresh input and output avoided' not in rendered


def test_missing_memory_has_no_placeholder_bar():
    html = HTML.read_text()
    start = html.index('    var barsHtml = vals.map(')
    end = html.index("    document.getElementById('chart-bars')", start)
    setup = "var vals=[10,0,20], barLabels=['A','Memory','B'], memoryExists=false, ctxWindow=100; function logBar(x){return x}; function fn(x){return String(x)};\n"
    rendered = subprocess.check_output(['node', '-e', setup + html[start:end] + 'console.log(barsHtml);'], text=True)
    assert 'Memory' not in rendered and '—' not in rendered
    assert 'A' in rendered and 'B' in rendered


def test_unpriced_repeat_read_value_is_omitted():
    from test_savings_render import fixture, render
    savings = fixture()
    savings['action_savings'] = {'reread_available': False, 'reread_prices_available': False}
    savings['counted_period'] = {'available': False, 'reread_prices_available': False}
    savings['counted_cumulative'] = {'prices_available': False}
    html = render(savings)
    assert 'Repeat reads avoided <strong>' not in html
