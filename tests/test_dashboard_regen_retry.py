"""The regenerate button must not dead-click on a stale/empty token.

Root cause: the page's load-time
`_fetchToken` is fire-and-forget, so on a daemon that warmed slowly
`window.__TOKEN_API_TOKEN` could still be empty when the reader clicked
Regenerate. Every click then sent an empty `X-TO-Token` and the server silently
403'd BEFORE the `api/regenerate` handler -- the button looked dead and
`daemon-regen.log` stayed empty for weeks. A stale (but non-empty) page token
hit the same wall.

The fix is client-first: on a 403, auto-refetch the token and retry the
POST exactly once; if it still fails, surface a prominent inline error instead
of a tiny span. If the token is empty at click time, lazy-fetch it before the
first POST (that is a prerequisite, not a retry).

Source-contract checks cover the wiring; Node executes the shipped handler to
verify success, retries, duplicate clicks, and recoverable failures.
"""

import re
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "skills" / "token-optimizer" / "assets"


def _regen_body(html: str) -> str:
    start = html.index("window.__TOKEN_REGENERATE = function()")
    # The assignment is `window.__TOKEN_REGENERATE = function() { ... };`. Walk
    # braces from the function body's opening `{` to its matching close, then
    # include the trailing `};`. A naive `index("};", start)` stops at the first
    # inner callback's `};` and truncates the contract under test.
    open_brace = html.index("{", start)
    depth = 0
    i = open_brace
    while i < len(html):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                # Include the closing `};` of the assignment.
                return html[start:i + 2]
        i += 1
    raise AssertionError("unterminated __TOKEN_REGENERATE function")


def test_regen_handler_exists():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    assert "window.__TOKEN_REGENERATE = function()" in html


def test_regen_lazy_fetches_an_empty_token_before_first_post():
    """An empty page token must be fetched before POSTing, not retried against.

    The lazy-fetch is the prerequisite for the first POST. Without it the empty-
    token case still 403s silently.
    """
    body = _regen_body((ASSETS / "dashboard.html").read_text(encoding="utf-8"))
    assert "window.__TOKEN_API_TOKEN" in body, "handler no longer reads the token"
    assert "_fetchToken(" in body, "handler no longer calls _fetchToken"
    # The empty-token branch must gate the first attempt on a fetch, and that
    # fetch must NOT count as a retry (attempt(false), not attempt(true)).
    assert re.search(r"if\s*\(\s*window\.__TOKEN_API_TOKEN\s*\)\s*\{[^}]*attempt\(false\)", body), (
        "empty-token branch must POST via attempt(false), not skip the fetch"
    )
    # The else branch lazy-fetches then POSTs via attempt(false). The branch
    # contains an inner callback `}` so a single [^}]* can't span it; assert the
    # else block body carries both the fetch and the non-retry first attempt.
    # Target the BOTTOM if/else (the dispatch), not earlier `else` keywords
    # inside fail()/attempt() definitions.
    dispatch_idx = body.index("if (window.__TOKEN_API_TOKEN)")
    dispatch = body[dispatch_idx:]
    assert "attempt(false)" in dispatch, "dispatch must POST via attempt(false)"
    assert "_fetchToken(" in dispatch, "dispatch does not lazy-fetch the token"
    # The lazy-fetch else branch must not itself retry on the first POST.
    else_idx = dispatch.index("else")
    else_block = dispatch[else_idx:]
    assert "attempt(false)" in else_block, (
        "else branch must start at attempt(false), not attempt(true) (the fetch "
        "is a prerequisite, not a retry)"
    )
    assert "attempt(true)" not in else_block, (
        "else branch must not retry on the first POST after a lazy fetch"
    )


def test_regen_retries_exactly_once_on_403():
    """A 403 on the POST must refetch the token and retry exactly once.

    The `haveRetried` flag is the single guard against both an infinite loop
    (persistent bad token) and a missing retry (stale token never refreshed).
    """
    body = _regen_body((ASSETS / "dashboard.html").read_text(encoding="utf-8"))
    # The retry is triggered only by an authError (403) and only when not yet retried.
    assert "e.authError" in body, "handler no longer branches on authError (403)"
    assert "haveRetried" in body, "handler lost the single-retry guard"
    # Exactly one call site flips the flag to true (the retry); the first
    # attempt always passes false. Two `attempt(true)` would mean a loop.
    assert body.count("attempt(true)") == 1, (
        "expected exactly one retry entry point (attempt(true)), got "
        f"{body.count('attempt(true)')}"
    )
    # The retry path must refetch the token before re-POSTing.
    retry_block = body[body.index("attempt(true)"):]
    assert "_fetchToken(" in retry_block, "retry does not refetch the token before re-POSTing"


def test_regen_surfaces_a_prominent_persistent_failure():
    """A persistent failure must show a prominent block, not the tiny status span.

    The old `to-regen-msg` span is 12-13px and dim -- a dead click left no
    visible trace. The fix adds a dedicated `to-regen-error` block shown only on
    failure, and the handler must surface failures there.
    """
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="to-regen-error"' in html, "prominent error block missing from the template"
    body = _regen_body(html)
    assert "to-regen-error" in body, "handler does not touch the prominent error block"
    # The block must be hidden at the start of a click (so a prior failure
    # clears on retry) and shown on failure.
    assert re.search(r"errBox\.hidden\s*=\s*true", body), (
        "handler does not clear the error block at the start of a click"
    )
    assert re.search(r"errBox\.hidden\s*=\s*false", body), (
        "handler does not reveal the error block on failure"
    )
    # The button must be re-enabled on failure (no stuck "Regenerating...").
    assert "btn.disabled = false" in body, "button not re-enabled on failure"


def test_regen_reloads_on_success():
    body = _regen_body((ASSETS / "dashboard.html").read_text(encoding="utf-8"))
    assert "location.reload()" in body, "handler no longer reloads on success"


def test_regen_contract_mirrored_to_plugin_tree():
    """Both dashboard.html trees must carry the same retry contract."""
    a = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    b = (
        ROOT
        / "plugins" / "token-optimizer" / "skills" / "token-optimizer" / "assets"
        / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert a == b, "dashboard.html drifted between the two install trees"


def test_job_status_is_polled_until_done():
    html = (ASSETS / 'dashboard.html').read_text(encoding='utf-8')
    start = html.index('  function waitForRegen(')
    helper = html[start:html.index('  window.__TOKEN_LOAD_HISTORY', start)]
    script = '''
let calls = 0;
const message = {textContent: ''};
const window = {__TOKEN_API_POST: async (path, body) => {
  if (path !== '/api/regenerate/status' || body.job_id !== 'job') throw Error('bad status request');
  calls++;
  return {json: async () => calls === 1 ? {state:'running',progress:'Collecting usage data.'} : {state:'done',progress:'Dashboard updated.'}};
}};
function setTimeout(fn) { fn(); }
''' + helper + '''
waitForRegen({ok:true,job_id:'job'}, message).then(() => {
  if (calls !== 2 || message.textContent !== 'Dashboard updated.') process.exit(1);
});
'''
    subprocess.run(['node', '-e', script], check=True, timeout=10)


@pytest.mark.parametrize("scenario,posts,fetches,reloads", [
    ("success", 1, 0, 1),
    ("empty", 1, 1, 1),
    ("stale", 2, 1, 1),
    ("denied", 2, 1, 0),
    ("network", 1, 0, 0),
    ("bad_json", 1, 0, 0),
    ("server_failure", 1, 0, 0),
])
def test_regenerate_click_behavior(scenario, posts, fetches, reloads):
    """Execute the shipped click handler, including retries and duplicate clicks."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser-handler execution")
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    handler = _regen_body(html)
    helper_start = html.index('  function waitForRegen(')
    helper = html[helper_start:html.index('  window.__TOKEN_LOAD_HISTORY', helper_start)]
    setup = r'''
const scenario = process.argv[2];
const counts = {posts: 0, fetches: 0, reloads: 0};
const btn = {disabled: false, textContent: 'Regenerate savings'};
const msg = {textContent: ''};
const error = {hidden: true, textContent: ''};
const document = {getElementById: id => ({'to-regen-btn': btn, 'to-regen-msg': msg, 'to-regen-error': error}[id])};
const location = {reload: () => counts.reloads++};
const window = {__TOKEN_API_TOKEN: scenario === 'empty' ? '' : 'initial', location: {protocol: 'http:'}};
function _fetchToken() { counts.fetches++; window.__TOKEN_API_TOKEN = 'fresh'; return Promise.resolve(); }
window.__TOKEN_API_POST = async function(path) {
  if (path !== '/api/regenerate') throw Error('wrong endpoint');
  counts.posts++;
  if (scenario === 'denied' || (scenario === 'stale' && counts.posts === 1)) throw {authError: true};
  if (scenario === 'network') throw Error('network unavailable');
  return {json: () => scenario === 'bad_json' ? Promise.reject(Error('invalid JSON')) : Promise.resolve({ok: scenario !== 'server_failure', msg: 'generation failed'})};
};
'''
    check = r'''
window.__TOKEN_REGENERATE();
window.__TOKEN_REGENERATE(); // A second click while pending must do nothing.
setImmediate(() => process.stdout.write(JSON.stringify({counts, btn, error})));
'''
    result = subprocess.run([node, "-", scenario], input=setup + helper + handler + check,
                            capture_output=True, text=True, check=True, timeout=30)
    state = json.loads(result.stdout)
    assert state["counts"] == {"posts": posts, "fetches": fetches, "reloads": reloads}, state
    if not reloads:
        assert state["btn"] == {"disabled": False, "textContent": "Regenerate savings"}
        assert state["error"]["hidden"] is False
        assert state["error"]["textContent"]
