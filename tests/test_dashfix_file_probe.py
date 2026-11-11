import subprocess
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/dashboard.html'


def test_file_probe_online_and_offline():
    html = HTML.read_text()
    start = html.rindex("  if (typeof fetch === 'function') {")
    probe = html[start:html.index('\n});\n</script>', start)]
    assert "window.location.protocol === 'file:' ?" in html
    assert 'Open ' in html and '/token-optimizer for live numbers.' in html
    assert 'measure.py setup-daemon</code> to see live numbers.' in html
    live_start = html.index('  function _setApiLive(')
    set_live = html[live_start:html.index('  window.__TOKEN_API_POST', live_start)]
    for online in (True, False):
        script = '''
const window = {location:{protocol:'file:'}};
const DAEMON_URL = 'http://localhost:24842';
let started = 0, tokenFetch = 0;
function startApp(){ started++; }
function _fetchToken(){ tokenFetch++; return Promise.resolve(); }
function fetch(url, options){ if (url !== DAEMON_URL + '/api/health' || options.mode !== 'no-cors') throw Error('wrong probe');
  return ONLINE ? Promise.resolve({type:'opaque'}) : Promise.reject(Error('offline')); }
'''.replace('ONLINE', 'true' if online else 'false') + set_live + '''
function boot(){
''' + probe + '''
}
boot();
setImmediate(() => {
  if (started !== 1 || Boolean(window.__TOKEN_FILE_DAEMON_LIVE) !== ONLINE || window.__TOKEN_API_LIVE || tokenFetch !== 0) process.exit(1);
});
'''.replace('ONLINE', 'true' if online else 'false')
        subprocess.run(['node', '-e', script], check=True, timeout=10)


def test_file_page_shows_the_right_instruction_and_no_button():
    html = HTML.read_text()
    start = html.index("(window.location.protocol === 'file:' ?")
    end = html.index(" +\n            '<span id=\"to-regen-msg\"", start)
    expression = html[start:end]
    for online in (True, False):
        script = ("const window={location:{protocol:'file:'},__TOKEN_FILE_DAEMON_LIVE:"
                  + ('true' if online else 'false') + "}; const DAEMON_URL='http://localhost:24842';\n"
                  + 'console.log(' + expression + ');')
        output = subprocess.check_output(['node', '-e', script], text=True)
        assert 'Regenerate savings</button>' not in output
        assert ('Open http://localhost:24842/token-optimizer for live numbers.' in output) == online
        assert ('measure.py setup-daemon' in output) != online


def test_file_page_post_is_rejected_without_fetching():
    html = HTML.read_text()
    start = html.index('  window.__TOKEN_API_POST = function(path, body) {')
    end = html.index('\n  function waitForRegen(', start)
    script = '''
const window = {location:{protocol:'file:'}};
function fetch(){throw Error('file page must not send a POST');}
''' + html[start:end] + '''
window.__TOKEN_API_POST('/api/regenerate', {}).then(() => process.exit(1), () => process.exit(0));
'''
    subprocess.run(['node', '-e', script], check=True, timeout=10)
