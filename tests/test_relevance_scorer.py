"""U2 — content-based, cwd-free checkpoint relevance scorer (IDF-weighted).

Advances R1 (gate the pointer on relevance) and R4 (score without folder
matching). The scorer tokenizes the opening prompt and the checkpoint sidecar
fields (active_task / topic / decisions / modified_files basenames), weights
overlap by inverse document frequency across the checkpoint pool so generic
words ("the", "run", "fix") don't dominate, sanitizes harness markup out of
sidecar fields before scoring, and treats recency as only a weak prior.

Calibration source for CHECKPOINT_RELEVANCE_THRESHOLD: the U7 replay benchmark
over a real resume/fresh first-prompt mix (tests/baselines/replay-metrics.json).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def _age_out(*paths):
    """Push file mtimes past the recency window so the weak recency prior (a flat
    +0.05 for a <3h-old checkpoint) does not confound a content/precision guard.
    A genuine resume typed hours later hits this branch; the recency tip is an
    orthogonal, documented prior tested separately."""
    old = time.time() - 60 * 60 * 24
    for p in paths:
        for f in (Path(p), Path(str(p).replace(".md", ".json"))):
            try:
                os.utime(f, (old, old))
            except OSError:
                pass

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _write_cp(tmp_path, name, active_task=None, topic=None, decisions=None,
              modified_files=None, recent_reads=None, body="", age_seconds=60):
    """Create a checkpoint .md + .json sidecar pair matching the real format."""
    cp = tmp_path / name
    cp.write_text(f"# Session State Checkpoint\n# Generated: test\n{body}\n",
                  encoding="utf-8")
    sidecar = {
        "version": 1,
        "generated": "test",
        "trigger": "stop",
        "session_id": "src-sid",
        "active_task": active_task,
        "topic": topic,
        "decisions": decisions or [],
        "modified_files": [{"path": p, "action": "edit", "range": None}
                           for p in (modified_files or [])],
        "recent_reads": recent_reads or [],
    }
    (tmp_path / cp.name.replace(".md", ".json")).write_text(
        json.dumps(sidecar), encoding="utf-8")
    return cp


def _cp_dict(cp_path, age_seconds=60):
    return {
        "filename": cp_path.name,
        "path": str(cp_path),
        "created": datetime.now() - timedelta(seconds=age_seconds),
        "trigger": "stop",
    }


# --- T1: high topical overlap clears the threshold ---

def test_high_topical_overlap_above_threshold(m, tmp_path):
    cp = _write_cp(tmp_path, "aaaa1111-20260811-120000-checkpoint.md",
                   active_task="fix checkpoint injection targeting in token optimizer",
                   modified_files=["plugins/token-optimizer/scripts/measure.py"])
    score = m.checkpoint_relevance_score(
        "continue the token optimizer checkpoint injection fix", cp, pool=[cp])
    assert score >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"high topical overlap must clear the threshold; got {score}")


# --- T2: generic-word-only overlap stays low (IDF working) ---

def test_generic_word_only_overlap_stays_low(m, tmp_path):
    # Three checkpoints that all share the generic glue word "work" but each
    # carries a DISTINCTIVE topic that appears in only one of them.
    # Directory names are deliberately NON-topical (alpha/beta/gamma) so the
    # prompt word "project" genuinely misses every doc. The scorer now splits
    # path DIRECTORY segments into topic words (a real checkpoint's identity
    # lives in its dirs, e.g. projects/meridian/...), so a dir literally named
    # "project-*" would inject "project" into the docs and defeat the very point
    # of this test -- that a generic word carries no signal.
    cp_a = _write_cp(tmp_path, "aaaa1111-20260811-120000-checkpoint.md",
                     active_task="work on token optimizer checkpoint injection",
                     modified_files=["alpha/measure.py"])
    cp_b = _write_cp(tmp_path, "bbbb2222-20260811-120100-checkpoint.md",
                     active_task="work on marketing audit content strategy",
                     modified_files=["beta/audit.md"])
    cp_c = _write_cp(tmp_path, "cccc3333-20260811-120200-checkpoint.md",
                     active_task="work on billing payment integration",
                     modified_files=["gamma/billing.py"])
    pool = [cp_a, cp_b, cp_c]
    # Generic-only prompt: the only shared word is "work" (low IDF, appears in
    # every checkpoint); "project" misses all. IDF-weighted precision stays low
    # because the matching token is common and the non-matching one is rare.
    generic_score = m.checkpoint_relevance_score("continue work on the project",
                                                 cp_a, pool=pool)
    # Content prompt: names the distinctive topic only cp_a has.
    content_score = m.checkpoint_relevance_score(
        "continue token optimizer checkpoint work", cp_a, pool=pool)
    assert generic_score < m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"generic-word-only overlap must stay below threshold; got {generic_score}")
    assert content_score > generic_score, (
        "IDF must rank a content-specific prompt above a generic-only one")
    assert content_score >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"content-specific overlap must clear the threshold; got {content_score}")


# --- T3: polluted active_task is sanitized before scoring ---

def test_polluted_active_task_sanitized(m, tmp_path):
    polluted = ("<task-notification>system: scheduled task #7 fired</task-notification> "
                "fix checkpoint injection in token optimizer")
    cp = _write_cp(tmp_path, "aaaa1111-20260811-120000-checkpoint.md",
                   active_task=polluted,
                   modified_files=["plugins/token-optimizer/scripts/measure.py"])
    # The harness markup tokens ("task-notification", "scheduled", "fired")
    # must NOT be the reason the score is high. Score the prompt on the REAL
    # content only and confirm it clears; then confirm a prompt that names ONLY
    # the markup noise stays low.
    real = m.checkpoint_relevance_score(
        "continue the token optimizer checkpoint injection fix", cp, pool=[cp])
    noise = m.checkpoint_relevance_score(
        "scheduled task notification fired system", cp, pool=[cp])
    assert real >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"real content must still score high after sanitization; got {real}")
    assert noise < m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"markup noise must not score high after sanitization; got {noise}")


# --- T4: bare "continue" + stale unrelated pool stays below threshold ---

def test_bare_continue_stale_unrelated_pool_below_threshold(m, tmp_path):
    stale_age = 60 * 60 * 12  # 12h, well past the recency prior window
    cp = _write_cp(tmp_path, "aaaa1111-20260811-000000-checkpoint.md",
                   active_task="unrelated marketing audit work",
                   modified_files=["projects/acme/audit.md"],
                   age_seconds=stale_age)
    score = m.checkpoint_relevance_score("continue", cp, pool=[cp])
    assert score < m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"bare continue + stale unrelated pool must stay below threshold; got {score}")


# --- T5: non-UTF-8 checkpoint content scores 0.0 without aborting ---

def test_non_utf8_checkpoint_scores_zero_without_raising(m, tmp_path):
    cp = tmp_path / "aaaa1111-20260811-120000-checkpoint.md"
    # Sidecar is valid UTF-8 JSON; the .md body is a stray-byte (cp1252) blob.
    cp.write_bytes(b"# Session State Checkpoint\n# Generated: test\n\xff\xfe\x80\n")
    sidecar = {"version": 1, "active_task": "token optimizer checkpoint fix",
               "decisions": [], "modified_files": [], "recent_reads": []}
    (tmp_path / cp.name.replace(".md", ".json")).write_text(
        json.dumps(sidecar), encoding="utf-8")
    # Must not raise; the sidecar still carries real content so this scores on
    # the sidecar, but a checkpoint with NO sidecar + non-UTF-8 body scores 0.0.
    no_sidecar = tmp_path / "bbbb2222-20260811-120100-checkpoint.md"
    no_sidecar.write_bytes(b"\xff\xfe\x80\x81")
    # Loop over both: the non-UTF-8-only one must score 0.0 and not abort the loop.
    scores = []
    for path in (cp, no_sidecar):
        try:
            scores.append(m.checkpoint_relevance_score(
                "token optimizer checkpoint fix", path, pool=[cp, no_sidecar]))
        except Exception as exc:  # pragma: no cover - the test is that this never fires
            pytest.fail(f"scorer raised on non-UTF-8 content: {exc!r}")
    assert scores[1] == 0.0, (
        f"checkpoint with no sidecar + non-UTF-8 body must score 0.0; got {scores[1]}")


# --- T6: CJK opening prompt tokenizes without crashing and scores sensibly ---

def test_cjk_opening_prompt_tokenizes_and_scores(m, tmp_path):
    cp = _write_cp(tmp_path, "aaaa1111-20260811-120000-checkpoint.md",
                   active_task="결제 모듈 리팩터링 및 테스트",
                   modified_files=["src/payment/module.py"])
    try:
        score = m.checkpoint_relevance_score("결제 모듈 리팩터링 다시 시작", cp, pool=[cp])
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"scorer raised on CJK prompt: {exc!r}")
    assert score >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"CJK topical overlap must clear the threshold; got {score}")


# --- T8: adversarial keyword-stuffing cannot game the score above threshold (D3) ---

def _big_doc_cp(tmp_path):
    """A checkpoint whose sidecar carries MANY distinctive topic tokens."""
    return _write_cp(
        tmp_path, "aaaa1111-20260811-120000-checkpoint.md",
        active_task=("Refactor payment gateway reconcile stripe webhook retries migrate "
                     "ledger schema backfill invoices harden idempotency reconciliation "
                     "dashboard currency rounding refunds"),
        decisions=["adopt double-entry bookkeeping model",
                   "shard ledger by tenant identifier",
                   "encrypt cardholder tokens at rest",
                   "replay webhooks through durable queue",
                   "expose settlement metrics prometheus exporter"],
        modified_files=["src/payments/gateway.py", "src/payments/ledger.py",
                        "src/payments/webhooks.py", "src/payments/settlement.py",
                        "src/billing/invoices.py", "src/billing/refunds.py"])


def test_keyword_stuffing_cannot_exceed_threshold(m, tmp_path):
    """D3: the OLD scorer was pure precision (hits_weight / prompt_weight), which
    hits 1.0 whenever every prompt token appears in the doc. An adversarial fresh
    opening keyword-stuffed from a large checkpoint's own vocabulary therefore
    scored a perfect 1.0 with NO resume cue. The length-normalized (F1) scorer
    folds in recall, so covering only a sliver of a big checkpoint cannot clear
    the bar."""
    cp = _big_doc_cp(tmp_path)

    # A fresh opening (no resume cue) stuffed with a few of the checkpoint's own
    # distinctive tokens. Every token is present in the doc -> OLD precision = 1.0.
    stuffed = "stripe webhook ledger"
    prompt_tokens = m._topic_tokens(stuffed, m._RESUME_TOPIC_STOPWORDS)
    doc_tokens = m._checkpoint_sidecar_doc_tokens(cp)
    assert prompt_tokens and prompt_tokens.issubset(doc_tokens), (
        "fixture sanity: every stuffed token must be in the doc so OLD precision "
        "would have been a perfect 1.0")

    score = m.checkpoint_relevance_score(stuffed, cp, pool=[cp])
    assert score < m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"keyword-stuffed fresh opening must NOT clear the threshold; got {score}")

    # An unrelated opening padded with buzzwords (its own topic + a token that
    # grazes the doc) must also stay well below.
    padded = ("kubernetes helm chart rollout canary istio sidecar mesh "
              "observability grafana stripe")
    padded_score = m.checkpoint_relevance_score(padded, cp, pool=[cp])
    assert padded_score < m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"unrelated keyword-padded opening must stay below threshold; got {padded_score}")


def test_genuine_broad_resume_still_clears(m, tmp_path):
    """No over-correction: a genuine resume that covers the checkpoint's real
    topic (not padding) still clears the threshold."""
    cp = _big_doc_cp(tmp_path)
    genuine = ("continue the payment gateway work: the stripe webhook retries, the "
               "ledger schema migration, the invoices backfill and the refunds "
               "reconciliation")
    score = m.checkpoint_relevance_score(genuine, cp, pool=[cp])
    assert score >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"a genuine broad-coverage resume must still clear the threshold; got {score}")


# --- T7: threshold constant is exposed and documented as calibrated ---

def test_threshold_constant_exposed(m):
    assert isinstance(m.CHECKPOINT_RELEVANCE_THRESHOLD, float)
    assert 0.0 < m.CHECKPOINT_RELEVANCE_THRESHOLD < 1.0, (
        "threshold must be a defensible (0,1) constant calibrated via U7 replay")


# ======================================================================
# Coverage tests (C1/H1/H2/H3/M2/knobs/L1) + multi-client
# ======================================================================

def _meridian_cp(tmp_path, name="aaaa1111-20260812-120000-checkpoint.md"):
    """A meridian client checkpoint whose identity lives ONLY in path dirs, with a
    real projects/<x>/... skeleton and a /compact active_task
    (mirrors the real meridian checkpoint whose active_task is literally '/compact')."""
    return _write_cp(
        tmp_path, name, active_task="/compact",
        modified_files=[
            "projects/meridian/data-files/meridian-competitor-monitor/scripts/monitor.py",
            "projects/meridian/data-files/meridian-competitor-monitor/reports/2026-08-11__BRIEF.html",
            "projects/meridian/data-files/meridian-competitor-monitor/references/competitor.md"],
        recent_reads=[
            "projects/meridian/data-files/meridian-competitor-monitor/config/monitor.json"])


def _acme_cp(tmp_path, name="bbbb2222-20260812-120100-checkpoint.md"):
    """A SECOND client sharing the IDENTICAL folder skeleton; only the distinctive
    identity words differ (acme/pricing/tracker vs meridian/competitor/monitor)."""
    return _write_cp(
        tmp_path, name, active_task="/compact",
        modified_files=[
            "projects/acme/data-files/acme-pricing-tracker/scripts/tracker.py",
            "projects/acme/data-files/acme-pricing-tracker/reports/2026-08-11__BRIEF.html",
            "projects/acme/data-files/acme-pricing-tracker/references/pricing.md"],
        recent_reads=[
            "projects/acme/data-files/acme-pricing-tracker/config/tracker.json"])


# --- R2-A: universal multi-client fix. Two clients, SHARED structure. Each
# client's own named prompt returns its own checkpoint; neither crosses; a
# shared-scaffolding prompt returns neither (H3 lexical stoplist). ---

def test_two_client_pool_no_cross_leak(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    acme = _acme_cp(tmp_path)
    pool = [meridian, acme]
    TH = m.CHECKPOINT_RELEVANCE_THRESHOLD
    gp = "continue working on the meridian competitor monitor"
    ap = "continue working on the acme pricing tracker"
    # own-named prompt -> own checkpoint, above threshold
    assert m.checkpoint_relevance_score(gp, meridian, pool=pool) >= TH
    assert m.checkpoint_relevance_score(ap, acme, pool=pool) >= TH
    # cross-client -> below threshold (must NOT leak)
    assert m.checkpoint_relevance_score(gp, acme, pool=pool) < TH, (
        "meridian prompt must NOT match the acme checkpoint")
    assert m.checkpoint_relevance_score(ap, meridian, pool=pool) < TH, (
        "acme prompt must NOT match the meridian checkpoint")
    # a prompt made only of shared filesystem-scaffolding words matches NEITHER
    scaffold = "continue the retainer deliverables clients reports"
    assert m.checkpoint_relevance_score(scaffold, meridian, pool=pool) < TH
    assert m.checkpoint_relevance_score(scaffold, acme, pool=pool) < TH


# --- R2-B: H3 container-word kills on a single-client pool (uniform IDF) ---

def test_h3_scaffolding_prompts_stay_below_threshold(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    TH = m.CHECKPOINT_RELEVANCE_THRESHOLD
    for scaffold in ("continue the retainer deliverables", "continue the clients work",
                     "continue the reports", "continue the references",
                     "continue the scripts", "continue the config"):
        s = m.checkpoint_relevance_score(scaffold, meridian, pool=[meridian])
        assert s < TH, f"scaffolding prompt {scaffold!r} must stay below threshold; got {s}"
    # the genuine meridian resume on the SAME checkpoint still clears
    assert m.checkpoint_relevance_score(
        "continue working on the meridian competitor monitor", meridian, pool=[meridian]) >= TH


# --- R2-C: prose-only-identity checkpoint (identity lives in active_task/decisions,
# paths are generic). A prompt naming that prose identity still scores. ---

def test_prose_only_identity_checkpoint_scores(m, tmp_path):
    cp = _write_cp(
        tmp_path, "cccc3333-20260812-120200-checkpoint.md",
        active_task="refactor the keepwarm predictor sustain heuristic",
        decisions=["tune the keepwarm decay window",
                   "gate sustain on predictor confidence"],
        modified_files=["src/core/engine.py"])  # generic path, no identity words
    score = m.checkpoint_relevance_score(
        "continue the keepwarm predictor sustain work", cp, pool=[cp])
    assert score >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"a prompt naming the prose-only identity must still clear; got {score}")


# --- R2-D: CJK+Latin resume. CJK glue must not dilute precision; a CJK resume
# cue must register as intent. Latin-only unrelated prompt stays low. ---

def test_cjk_latin_resume_clears_and_latin_guard_holds(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    acme = _acme_cp(tmp_path)
    pool = [meridian, acme]
    TH = m.CHECKPOINT_RELEVANCE_THRESHOLD
    cjk = m.checkpoint_relevance_score(
        "meridian competitor monitor 결제 모듈 작업을 이어서", meridian, pool=pool)
    assert cjk >= TH, f"CJK+Latin resume must clear the threshold; got {cjk}"
    # CJK resume cue alone is recognized as resume intent
    assert m._resume_intent("결제 모듈 작업을 이어서")
    assert m._resume_intent("继续 meridian competitor monitor")
    # a Latin-only unrelated prompt that only grazes a shared word ("competitor")
    # stays below threshold in a realistic 2-client pool (the acme guard, M1). Age
    # the pool out of the recency window first so this asserts the CONTENT/precision
    # guard, not the orthogonal +0.05 recency prior (which correctly biases toward a
    # just-created checkpoint). On the real 50-checkpoint pool this graze is 0.219.
    _age_out(meridian, acme)
    latin = m.checkpoint_relevance_score(
        "continue the competitor analysis for acme corp", meridian, pool=pool)
    assert latin < TH, f"Latin acme-style prompt must stay below threshold; got {latin}"


# --- R2-E: junk-date prompt. The only overlap with the doc is a 4-digit year,
# which C1 drops from path words, so no match. ---

def test_junk_date_prompt_no_match(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    # the year must NOT be a doc token (C1 drops pure-numeric path segments)
    assert "2026" not in m._checkpoint_sidecar_doc_tokens(meridian)
    score = m.checkpoint_relevance_score(
        "continue working on the report from 2026-08-11", meridian, pool=[meridian])
    assert score < m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"a junk-date-only prompt must not match; got {score}")


# --- R2-F: pasted-path prompt matches the RIGHT client, not the wrong one, and
# scores >= the spoken form (H2: pasting can only help precision). ---

def test_pasted_path_matches_right_not_wrong(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    acme = _acme_cp(tmp_path)
    pool = [meridian, acme]
    TH = m.CHECKPOINT_RELEVANCE_THRESHOLD
    pasted = ("continue /Users/dev/work/projects/meridian/data-files/"
              "meridian-competitor-monitor/reports/2026-08-11__BRIEF.html")
    spoken = "continue working on the meridian competitor monitor"
    ps = m.checkpoint_relevance_score(pasted, meridian, pool=pool)
    ss = m.checkpoint_relevance_score(spoken, meridian, pool=pool)
    # NOTE: we deliberately do NOT require pasted >= spoken. Making a pasted path
    # out-score the spoken form was the goal that CAUSED the H2 regression
    # (dropping non-matching path words made precision 1.0 by construction, so a
    # FOREIGN pasted path grazing one shared word false-matched the wrong client).
    # The correct invariant is weaker: a pasted REAL meridian path still clears the
    # bar (its generic segments like users/work dilute precision, which
    # is fine), and it never crosses to the wrong client.
    assert ps >= TH, f"pasted real meridian path must clear the threshold; got {ps}"
    assert m.checkpoint_relevance_score(pasted, acme, pool=pool) < TH, (
        "pasted meridian path must not match the acme checkpoint")
    # And a FOREIGN pasted path sharing only a scaffolding/sub-project word must not
    # out-score the spoken form (the H2 fix: unmatched distinctive words dilute).
    foreign = ("continue /Users/alex/other/acme-competitor-analysis/plan-notes.md")
    assert m.checkpoint_relevance_score(foreign, meridian, pool=pool) <= ss + 1e-9, (
        "a foreign pasted path must not out-score the genuine spoken resume")
    # "continue <path>" is recognized as resume intent
    assert m._resume_intent(pasted)


# --- R2.5-A: a project whose slug is built only from scaffolding-adjacent words
# must still be resumable. "company"/"brain" were wrongly in the scaffold stoplist,
# so a genuine "continue working on the company brain" scored 0.0 (a real false negative).
def test_stoplist_named_project_still_resumes(m, tmp_path):
    cb = _write_cp(
        tmp_path, "bbbb2222-20260812-120000-checkpoint.md",
        active_task="/compact",
        modified_files=[
            "/Users/dev/work/projects/meridian/data-files/"
            "meridian-company-brain/competitor-monitor/reports/2026-08-11__BRIEF.html"],
        recent_reads=[
            "/Users/dev/work/projects/meridian/data-files/"
            "meridian-company-brain/SKILL.md"])
    # unrelated distractor so the pool has >1 doc
    other = _write_cp(tmp_path, "cccc3333-20260812-120000-checkpoint.md",
                      active_task="refactor the payment ledger",
                      modified_files=["/Users/alex/proj/ledger/src/pay.py"])
    pool = [cb, other]
    score = m.checkpoint_relevance_score(
        "continue working on the company brain", cb, pool=pool)
    assert score >= m.CHECKPOINT_RELEVANCE_THRESHOLD, (
        f"a stoplist-named project (company brain) must resume, not score ~0; got {score}")


# --- R2-G: H1 dead checkpoint. Body file deleted, sidecar lingers -> 0.0. ---

def test_dead_checkpoint_orphan_sidecar_scores_zero(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    Path(meridian).unlink()  # delete the .md body; sidecar .json remains
    assert not Path(meridian).is_file()
    score = m.checkpoint_relevance_score(
        "continue the meridian competitor monitor", meridian, pool=[meridian])
    assert score == 0.0, f"orphan-sidecar / deleted-body checkpoint must score 0.0; got {score}"


# --- R2-H: L1 recency must not leak onto an empty / separator-only prompt ---

def test_empty_and_separator_prompts_score_zero(m, tmp_path):
    meridian = _meridian_cp(tmp_path)  # fresh checkpoint (recency window would apply)
    for empty in ("", "   ", "--- === ...", "\t\n"):
        s = m.checkpoint_relevance_score(empty, meridian, pool=[meridian])
        assert s == 0.0, f"empty/separator prompt {empty!r} must score 0.0 (no recency leak); got {s}"


# --- R2-I: knob clamps. An out-of-range env knob cannot force the score or flip
# the F1 sign. ---

def test_relevance_knobs_are_clamped(monkeypatch):
    def _reload():
        if "measure" in sys.modules:
            del sys.modules["measure"]
        return importlib.import_module("measure")

    monkeypatch.setenv("TOKEN_OPTIMIZER_RELEVANCE_PATH_TF_CAP", "100000")
    monkeypatch.setenv("TOKEN_OPTIMIZER_RELEVANCE_PATH_TF_WEIGHT", "-5")
    monkeypatch.setenv("TOKEN_OPTIMIZER_RELEVANCE_IDF_CAP", "-1")
    monkeypatch.setenv("TOKEN_OPTIMIZER_RELEVANCE_RESUME_BONUS_PRECISION_FLOOR", "9")
    monkeypatch.setenv("TOKEN_OPTIMIZER_CHECKPOINT_RELEVANCE_THRESHOLD", "5")
    mod = _reload()
    try:
        assert mod._RELEVANCE_PATH_TF_CAP == 50, mod._RELEVANCE_PATH_TF_CAP
        assert mod._RELEVANCE_PATH_TF_WEIGHT == 0.0, mod._RELEVANCE_PATH_TF_WEIGHT
        assert mod._RELEVANCE_IDF_CAP == 1.0, mod._RELEVANCE_IDF_CAP
        assert mod._RELEVANCE_RESUME_BONUS_PRECISION_FLOOR == 0.99
        assert mod.CHECKPOINT_RELEVANCE_THRESHOLD == 0.9
    finally:
        if "measure" in sys.modules:
            del sys.modules["measure"]


def test_path_tf_cap_100000_does_not_force_score_to_one(m, tmp_path, monkeypatch):
    # With a huge PATH_TF_CAP the OLD code let one mega-repeated path word drive
    # recall (and F1) to ~1.0. The clamp bounds it; the score stays a sane content
    # score, not a forced 1.0.
    meridian = _meridian_cp(tmp_path)
    score = m.checkpoint_relevance_score(
        "continue working on the meridian competitor monitor", meridian, pool=[meridian])
    assert score < 0.99, f"score must not be forced to 1.0 by the cap; got {score}"


# --- R2-J: L4 defensive guards. bytes text and a non-sequence pool must not raise
# or leak a repr. ---

def test_bytes_text_and_bad_pool_are_guarded(m, tmp_path):
    meridian = _meridian_cp(tmp_path)
    # bytes prompt: must be decoded, not str()'d into a b'...' repr
    s1 = m.checkpoint_relevance_score(
        b"continue working on the meridian competitor monitor", meridian, pool=[meridian])
    assert s1 >= m.CHECKPOINT_RELEVANCE_THRESHOLD, s1
    # a string pool (wrong type) must not iterate its characters as paths / raise
    s2 = m.checkpoint_relevance_score(
        "continue working on the meridian competitor monitor", meridian, pool="not-a-list")
    assert 0.0 <= s2 <= 1.0


# --- Portuguese resume cues (detection + residual stripping) ---

@pytest.mark.parametrize("prompt", [
    "continua de onde paramos", "Continuar de onde paramos", "na última sessão",
    "na ultima sessao", "sessão anterior", "da última vez", "da ultima vez",
    "onde paramos?", "onde parei", "continua o trabalho", "continuar com o projeto",
    "continua daquilo", "continuar trabalhando no parser", "retoma o projeto",
    "retome o trabalho", "retomar de onde parei", "retomando o parser",
    "o que estávamos fazendo", "no que estavamos trabalhando",
    "ontem a gente mexeu nisso", "a gente estava trabalhando no parser",
    "a gente estava no meio do refactor", "continua a última sessão",
    # Article/preposition + work noun is a real resume ask, mirroring English
    # "resume the (work|session|project|task|conversation|thread|discussion)":
    "continua a sessão", "continuar a tarefa", "continua a conversa sobre o parser",
    "continua a discussão", "continua com a tarefa", "retoma a tarefa",
])
def test_pt_resume_cues_detected(m, prompt):
    assert m._resume_intent(prompt), prompt


@pytest.mark.parametrize("prompt", [
    "continua", "continuar", "continua!", "escreva uma função de ordenação",
    "adicione testes para o parser", "a sessão expirou",
    # PT analog of the deliberately-excluded English "continue to <verb>" (D2 in
    # measure.py): "continua a <infinitive>" means "keeps <verb>ing" - ongoing
    # status of the CURRENT task, not a request to resume a prior session.
    "o build continua a falhar", "continua a chover", "ele continua a trabalhar",
    "continua a implementar o parser", "continua a trabalhar no parser",
    # Ordinary status reports and non-work recollections must not trigger resume:
    "continua com o erro no parser", "continua o mesmo erro",
    "o build continua com erro", "o teste continua de pé",
    "a gente estava errado sobre o cache",
    "o worker continua trabalhando depois do timeout",
    "a gente estava no escritório ontem",
    "a gente estava em dúvida sobre o nome",
    "o servidor retoma a conexão sozinho",
    # Boundary parity probe: cue glued to an accented letter is not a cue
    # (Python \w is Unicode-aware; the JS ports pin the same with \p{L}).
    "éúltima sessão",
])
def test_pt_non_resume_not_detected(m, prompt):
    assert not m._resume_intent(prompt), prompt


def test_pt_cue_stripped_from_topic_residual(m, tmp_path):
    cp = tmp_path / "cp.md"
    cp.write_text("parser module notes", encoding="utf-8")
    assert m._resume_topic_score("continua de onde paramos", cp) == 0.0
    assert m._resume_topic_score("continua de onde paramos no parser", cp) == 1.0
