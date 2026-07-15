#!/usr/bin/env python3
"""Tests for the respond_to_bash_commands detector (PR #74, ported to skills/).

Covers the detection logic plus the two integration fixes:
  B1 — finding carries savings_tokens (display does f["savings_tokens"]).
  B2 — always_show findings survive triage AND the registry dedup.

Run:  python3 tests/test_respond_to_bash_detector.py
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _run(code, env=None):
    full_env = {**os.environ, "PYTHONUTF8": "1"}
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SCRIPTS), env=full_env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


_PROBE = """
import sys, json, tempfile, os
sys.path.insert(0, '.')
from pathlib import Path
import detectors.respond_to_bash as rb

def probe(settings_dict_or_none):
    rb._load_settings.cache_clear()
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / 'settings.json'
        if settings_dict_or_none is not None:
            sp.write_text(json.dumps(settings_dict_or_none), encoding='utf-8')
        rb._get_claude_home = lambda: Path(td)
        return rb.detect_respond_to_bash(None)

absent = probe(None)            # no settings.json -> fires
enabled = probe({})             # key missing -> fires
explicit_true = probe({'respondToBashCommands': True})   # truthy -> fires
disabled = probe({'respondToBashCommands': False})       # False -> suppressed

out = {
  'absent_fires': bool(absent),
  'enabled_fires': bool(enabled),
  'true_fires': bool(explicit_true),
  'disabled_suppressed': not disabled,
  'has_savings_key': bool(absent) and 'savings_tokens' in absent[0],
  'always_show': bool(absent) and absent[0].get('always_show') is True,
}
print(json.dumps(out))
"""


def test_detector_logic():
    r = _run(_PROBE)
    assert r.returncode == 0, f"probe crashed: {r.stderr}"
    out = __import__("json").loads(r.stdout.strip().splitlines()[-1])
    assert out["absent_fires"], "missing settings.json must fire"
    assert out["enabled_fires"], "key absent must fire"
    assert out["true_fires"], "truthy value must fire"
    assert out["disabled_suppressed"], "explicit False must suppress"
    assert out["has_savings_key"], "B1: finding must carry savings_tokens"
    assert out["always_show"], "finding must be always_show"


_INTEGRATION = """
import sys, json, tempfile
sys.path.insert(0, '.')
from pathlib import Path
import detectors.respond_to_bash as rb
from detectors.registry import run_all_detectors, triage

# Force the detector to fire via a temp home with no settings.json.
import tempfile as _tf
_td = _tf.mkdtemp()
rb._load_settings.cache_clear()
rb._get_claude_home = lambda: Path(_td)

findings = run_all_detectors({'messages': []})
names = [f.get('name') for f in findings]
triaged = triage(findings)
tnames = [f.get('name') for f in triaged]
# dedup: run again-merged shouldn't duplicate the config finding
print(json.dumps({
  'in_findings': 'respond_to_bash_commands' in names,
  'survives_triage': 'respond_to_bash_commands' in tnames,
  'single_copy': names.count('respond_to_bash_commands') == 1,
}))
"""


def test_registry_integration():
    r = _run(_INTEGRATION)
    assert r.returncode == 0, f"integration crashed: {r.stderr}"
    out = __import__("json").loads(r.stdout.strip().splitlines()[-1])
    assert out["in_findings"], "detector must register and run"
    assert out["survives_triage"], "B2: always_show must survive triage token floor"
    assert out["single_copy"], "config finding must be deduped to one copy"


def test_dual_tree_parity_detector():
    canonical = SCRIPTS / "detectors" / "respond_to_bash.py"
    mirror = REPO / "plugins" / "token-optimizer" / "skills" / "token-optimizer" / "scripts" / "detectors" / "respond_to_bash.py"
    assert canonical.read_bytes() == mirror.read_bytes(), \
        "respond_to_bash.py drift between skills/ and plugins/ — run the sync"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
