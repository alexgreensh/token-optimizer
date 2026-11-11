import subprocess
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/assets/dashboard.html'


def test_file_probe_online_and_offline():
    html = HTML.read_text()
    start = html.rindex("  if (typeof fetch === 'function') {")
    probe = html[start:html.index('\n});\n</script>', start)]
    assert "window.location.protocol === 'file:' && !window.__TOKEN_REGEN_LIVE" in html
    assert 'measure.py setup-daemon</code> to see live numbers.' in html
    live_start = html.index('  function _setApiLive(')
    set_live = html[live_start:html.index('  window.__TOKEN_API_POST', live_start)]
    for online, token_ok in ((True, True), (True, False), (False, False)):
        script = '''
const window = {location:{protocol:'file:'}};
const DAEMON_URL = 'http://localhost:24842';
let started = 0, tokenFetch = 0;
function startApp(){ started++; }
function _fetchToken(){ tokenFetch++; if (TOKEN_OK) window.__TOKEN_API_TOKEN = 'token'; return Promise.resolve(); }
function fetch(url){ if (url !== DAEMON_URL + '/api/health') throw Error('wrong URL');
  return ONLINE ? Promise.resolve({json: () => Promise.resolve({ok:true})}) : Promise.reject(Error('offline')); }
'''.replace('ONLINE', 'true' if online else 'false').replace('TOKEN_OK', 'true' if token_ok else 'false') + set_live + '''
function boot(){
''' + probe + '''
}
boot();
setImmediate(() => {
  if (started !== 1 || Boolean(window.__TOKEN_REGEN_LIVE) !== (ONLINE && TOKEN_OK) || window.__TOKEN_API_LIVE || tokenFetch !== (ONLINE ? 1 : 0)) process.exit(1);
});
'''.replace('ONLINE', 'true' if online else 'false').replace('TOKEN_OK', 'true' if token_ok else 'false')
        subprocess.run(['node', '-e', script], check=True, timeout=10)
