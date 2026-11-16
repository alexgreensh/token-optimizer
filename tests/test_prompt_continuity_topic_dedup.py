"""#339: the lightweight prior-session hint is injected once per session.

The resume path already deduped (#303); the topic-matched hint and teaser did
not, so a session that stayed on one topic got the same pointer on most
prompts, each copy re-read until compaction. Real hook subprocess, isolated home.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prompt_continuity_harness_dedup import (  # noqa: E402
    CUR_SID, CUR_SID_2, MEASURE, PRIOR_SID, _checkpoint_dir, _env, _hook,
)

TOPIC = "refactor the billing module retry loop"
FULL_PROMPT = "refactor the billing module retry loop backoff"
TEASER_PROMPT = "check the billing page colors and fonts today"  # scores about 0.37: teaser band
OTHER_SID = "77777777-6666-5555-4444-333333333333"


def _checkpoint(cp_dir, sid, cwd, task=TOPIC, kind="stop", age_s=0):
    cp_dir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now() - timedelta(seconds=age_s)).strftime("%Y%m%d-%H%M%S")
    base = f"{sid}-{ts}-{kind}"
    sidecar = {
        "session_id": sid,
        "active_task": task,
        "decisions": ["Keep the retry loop idempotent"],
        "modified_files": [{"path": f"{cwd}/src/billing.py"}],
        "recent_reads": [f"{cwd}/README.md"],
    }
    (cp_dir / f"{base}.md").write_text(f"# checkpoint\n\n{task}\n", encoding="utf-8")
    (cp_dir / f"{base}.json").write_text(json.dumps(sidecar), encoding="utf-8")


@pytest.fixture
def flow(tmp_path):
    env = _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    cp_dir = _checkpoint_dir(env)
    _checkpoint(cp_dir, PRIOR_SID, proj, age_s=60)
    return env, proj, cp_dir


def _full(out):
    return "Prior session hint:" in out


def _teaser(out):
    return out.startswith("[Token Optimizer] Prior session rel")


def test_fixture_produces_both_forms(flow):
    env, proj, _ = flow
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))
    assert _teaser(_hook(env, TEASER_PROMPT, CUR_SID_2, proj))


def test_topic_hint_injected_once_per_session(flow):
    env, proj, _ = flow
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))
    for _ in range(3):
        assert _hook(env, FULL_PROMPT, CUR_SID, proj) == ""


def test_teaser_injected_once_per_session(flow):
    env, proj, _ = flow
    assert _teaser(_hook(env, TEASER_PROMPT, CUR_SID, proj))
    assert _hook(env, TEASER_PROMPT, CUR_SID, proj) == ""


def test_teaser_does_not_suppress_later_full_hint(flow):
    env, proj, _ = flow
    assert _teaser(_hook(env, TEASER_PROMPT, CUR_SID, proj))
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))


def test_topic_hint_other_current_session_still_injects(flow):
    env, proj, _ = flow
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))
    assert _full(_hook(env, FULL_PROMPT, CUR_SID_2, proj))


def test_different_prior_session_still_injects(flow):
    env, proj, cp_dir = flow
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))
    _checkpoint(cp_dir, OTHER_SID, proj, task="migrate the invoice tables schema")
    out = _hook(env, "migrate the invoice tables schema now", CUR_SID, proj)
    assert _full(out) and "invoice tables" in out


def test_newer_checkpoint_of_same_prior_session_not_reinjected(flow):
    env, proj, cp_dir = flow
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))
    _checkpoint(cp_dir, PRIOR_SID, proj, kind="milestone-edit-batch")
    assert _hook(env, FULL_PROMPT, CUR_SID, proj) == ""


def test_compaction_rearms_topic_hint(flow):
    env, proj, _ = flow
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))
    assert _hook(env, FULL_PROMPT, CUR_SID, proj) == ""
    payload = json.dumps({"session_id": CUR_SID, "source": "compact", "cwd": str(proj)})
    subprocess.run([sys.executable, str(MEASURE), "compact-restore", "--compact"], env=env,
                   input=payload, capture_output=True, text=True, timeout=60)
    assert _full(_hook(env, FULL_PROMPT, CUR_SID, proj))


def test_topic_hint_no_session_id_fails_open(flow):
    env, proj, _ = flow
    assert _full(_hook(env, FULL_PROMPT, "", proj))
    assert _full(_hook(env, FULL_PROMPT, "", proj))
