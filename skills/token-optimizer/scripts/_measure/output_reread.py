"""Output that was never written is also never re-read (savings-truth U5).

A verbosity nudge changes how many output tokens the next replies carry. Those
replies stay in the conversation, so each later call until the next compaction
re-reads them from the cache. This module walks a transcript once and scores
that carry turn by turn, in the estimated tier only.

Two rules keep it honest:

* Signed. A reply that grew after the nudge is a negative reduction and is
  carried as a debit, never clamped to zero.
* Net of a control. The before/after change of a nudged reply includes what
  would have happened anyway (replies vary a lot, and a single reply compared
  with a long running mean is noisy). Callers pass ``control_per_request``, the
  same change measured at user-prompt boundaries of sessions that were never
  nudged, and only the difference is credited.
"""
from __future__ import annotations

import math
import hashlib
import json
import os
import tempfile
from pathlib import Path
from bisect import bisect_left, bisect_right

from . import _compat, transcript_stream

# A control needs this many un-nudged boundaries before the pool is credited.
MIN_CONTROL_SAMPLES = 30


def signed_delta(baseline, outputs):
    """Baseline per request times requests, minus what was produced. Signed."""
    return int(baseline * len(outputs) - sum(outputs))


def _dt(raw):
    facade = _compat.facade()
    try:
        return facade.datetime.strptime(str(raw)[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def _is_user_prompt(entry):
    """A real prompt typed by the user, not a tool result or injected meta line."""
    if entry.get("isSidechain") or entry.get("isMeta"):
        return False
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content.lstrip()
        return bool(text) and not text.startswith(("<command-", "<local-command"))
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def walk_requests(path):
    """One pass over a transcript.

    Returns a dict with:
      ``outputs``: [(key, positive_lines_before, output, utc time)] in the order and with
        the request folding of ``_transcript_request_outputs`` (the list a nudge
        indexes with ``pre_turns``), sidechain lines included;
      ``turns``: parent API calls [(utc datetime, model, prompt tokens, key)]
        sorted by time, deduped per request, as the counted walker sees them;
      ``compacts``: sorted compaction times;
      ``prompts``: [(outputs seen before the prompt, last parent prompt tokens)]
        for each real user prompt;
      ``lines``: [(request key, output)] per output-bearing assistant line,
        unfolded: the unit nudges recorded before ``unit=req``.
    Never raises; an unreadable file gives empty lists.
    """
    facade = _compat.facade()
    out = {"outputs": [], "turns": [], "compacts": [], "prompts": [], "lines": []}
    by_key, turn_at, seen_lines = {}, {}, 0
    last_prompt_tokens = 0
    try:
        from .transcript_stream import open_transcript
        handle = open_transcript(path)
    except OSError:
        return out
    with handle as lines:
        for line in lines:
            if '"assistant"' not in line and '"user"' not in line and "compact_boundary" not in line:
                continue
            try:
                entry = facade.json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "system" and entry.get("subtype") == "compact_boundary":
                when = _dt(entry.get("timestamp"))
                if when is not None:
                    out["compacts"].append(when)
                continue
            if kind == "user":
                if _is_user_prompt(entry):
                    out["prompts"].append((len(out["outputs"]), last_prompt_tokens))
                continue
            if kind != "assistant":
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if usage is None:
                continue
            key = facade._usage_request_key(entry, msg)
            produced = facade._safe_int(usage.get("output_tokens", 0))
            if produced > 0:
                okey = key if key is not None else f"__noreq__{len(out['outputs'])}"
                slot = by_key.get(okey)
                if slot is None:
                    by_key[okey] = len(out["outputs"])
                    out["outputs"].append([okey, seen_lines, produced, _dt(entry.get("timestamp"))])
                else:
                    out["outputs"][slot][2] = max(out["outputs"][slot][2], produced)
                out["lines"].append((okey, produced))
                seen_lines += 1
            when = _dt(entry.get("timestamp"))
            if entry.get("isSidechain") or when is None:
                continue
            prompt = facade._context_token_count(usage)
            model = msg.get("model") if isinstance(msg.get("model"), str) else None
            if key is not None and key in turn_at:
                index = turn_at[key]
                old = out["turns"][index]
                out["turns"][index] = (old[0], old[1] or model, max(old[2], prompt), key)
            else:
                if key is not None:
                    turn_at[key] = len(out["turns"])
                out["turns"].append((when, model, prompt, key))
            last_prompt_tokens = max(prompt, 0)
    out["outputs"] = [tuple(row) for row in out["outputs"]]
    out["turns"].sort(key=lambda t: t[0])
    out["compacts"].sort()
    return out


def cached_walk(path, cache_dir):
    """Reuse a transcript walk only while its file identity is unchanged."""
    facade = _compat.facade()
    path, cache_dir = Path(path), Path(cache_dir)
    try:
        stat = path.stat()
        identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        name = hashlib.sha256(os.fsencode(str(path.resolve()))).hexdigest() + ".json"
        target = cache_dir / name
        try:
            saved = json.loads(target.read_text(encoding="utf-8"))
            if saved["version"] == 1 and saved["identity"] == identity and not transcript_stream.needs_resume(path):
                walk = saved["walk"]
                walk["outputs"] = [(r[0], r[1], r[2], facade.datetime.fromisoformat(r[3]) if r[3] else None) for r in walk["outputs"]]
                walk["turns"] = [(facade.datetime.fromisoformat(r[0]), *r[1:]) for r in walk["turns"]]
                walk["compacts"] = [facade.datetime.fromisoformat(t) for t in walk["compacts"]]
                walk["prompts"] = [tuple(r) for r in walk["prompts"]]
                walk["lines"] = [tuple(r) for r in walk["lines"]]
                return walk
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
        walk = walk_requests(path)
        after = path.stat()
        if identity == [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns] and not transcript_stream.needs_resume(path):
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_dir, delete=False) as handle:
                json.dump({"version": 1, "identity": identity, "walk": walk}, handle,
                          default=lambda value: value.isoformat())
                name = handle.name
            os.replace(name, target)
        return walk
    except OSError:
        return walk_requests(path)


def post_window(walk, pre_turns, post_turns):
    """The replies a ``unit=req`` nudge measured, or None when the file no
    longer holds that many (a rewritten or truncated transcript)."""
    chosen = walk["outputs"][pre_turns:pre_turns + post_turns]
    return chosen if len(chosen) == post_turns and post_turns > 0 else None


def legacy_line_window(walk, pre_turns, post_turns, pre_avg, booked):
    """Window of a nudge recorded before ``unit=req``.

    Those nudges counted output-bearing transcript LINES, not requests, so a
    request streamed over three lines counted three times. The line window is
    trusted only when it reproduces the booked amount and the recorded
    baseline exactly; it is then mapped back to whole requests. Returns
    (request rows, request-unit baseline) or None."""
    lines = walk["lines"]
    window = lines[pre_turns:pre_turns + post_turns]
    if pre_turns <= 0 or len(window) != post_turns or post_turns <= 0:
        return None
    outs = [o for _k, o in window]
    if round(sum(o for _k, o in lines[:pre_turns]) / pre_turns) != pre_avg:
        return None
    if max(0, int((pre_avg - sum(outs) / len(outs)) * len(outs))) != booked:
        return None
    keys = list(dict.fromkeys(k for k, _o in window))
    position = {row[0]: i for i, row in enumerate(walk["outputs"])}
    if any(k not in position for k in keys):
        return None
    first = position[keys[0]]
    rows = [walk["outputs"][position[k]] for k in keys]
    if first <= 0:
        return None
    return rows, round(sum(r[2] for r in walk["outputs"][:first]) / first)


def score_rereads(walk, post_rows, reduction_per_request, rate_for_turn):
    """Integrate signed reductions over the later parent calls that re-read them.

    Each post-nudge reply of ``reduction_per_request[i]`` fewer tokens is absent
    from every parent call after the one that produced it, up to the first
    compaction after that call. Calls are found by request key, so two calls in
    the same second are never confused. Sidechain replies never reach the
    parent context and carry nothing. Each call is priced at its own
    cache-read rate; calls without a known rate keep their tokens unpriced.
    """
    turns, compacts = walk["turns"], walk["compacts"]
    times = [t[0] for t in turns]
    index_of = {t[3]: i for i, t in enumerate(turns) if t[3] is not None}
    diff = [0] * (len(turns) + 1)
    unmapped = 0
    for row, reduction in zip(post_rows, reduction_per_request):
        key = row[0]
        at = index_of.get(key)
        if at is None:
            unmapped += 1
            continue
        nxt = bisect_right(compacts, times[at])
        boundary = compacts[nxt] if nxt < len(compacts) else None
        end = bisect_left(times, boundary) if boundary is not None else len(turns)
        if end > at + 1:
            diff[at + 1] += reduction
            diff[end] -= reduction
    carried = tokens = unpriced = turns_counted = 0
    usd = 0.0
    for i, turn in enumerate(turns):
        carried += diff[i]
        if not carried:
            continue
        turns_counted += 1
        tokens += carried
        rate = rate_for_turn(turn)
        if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0:
            usd += carried * rate / 1e6
        else:
            unpriced += abs(carried)
    return {"tokens": tokens, "usd": usd, "unpriced_tokens": unpriced,
            "turns_counted": turns_counted, "unmapped_requests": unmapped}


def boundary_changes(walk, min_pre=10):
    """Un-nudged change at each user prompt: running mean of all earlier
    replies minus the mean of the replies to that prompt, per request.

    Returns [(pre_count, post_count, pre_mean, change_per_request,
    context_tokens)], the same statistic a nudge books, for control use."""
    rows = [r[2] for r in walk["outputs"]]
    prompts = walk["prompts"]
    result = []
    prefix = [0]
    for value in rows:
        prefix.append(prefix[-1] + value)
    for i, (pre, context) in enumerate(prompts):
        nxt = prompts[i + 1][0] if i + 1 < len(prompts) else len(rows)
        post = nxt - pre
        if pre < min_pre or post <= 0:
            continue
        pre_mean = round(prefix[pre] / pre)
        post_mean = (prefix[nxt] - prefix[pre]) / post
        result.append((pre, post, pre_mean, pre_mean - post_mean, context))
    return result


def match_controls(treated, controls, k=5):
    """Nearest un-nudged boundaries for each nudge, with replacement.

    ``treated`` and ``controls`` hold (pre_count, post_count, pre_mean,
    change_per_request, context_tokens). Distance is on log scales of request
    position, reply count, running mean and context size. Returns, per treated
    item, the mean control change per request (None without candidates)."""
    def point(row):
        return tuple(math.log1p(max(0.0, float(v))) for v in (row[0], row[1], row[2], row[4]))
    pts = [point(c) for c in controls]
    out = []
    for row in treated:
        if not pts:
            out.append(None)
            continue
        here = point(row)
        dist = sorted((sum((a - b) ** 2 for a, b in zip(here, p)), i) for i, p in enumerate(pts))
        nearest = [controls[i][3] for _d, i in dist[:k]]
        out.append(sum(nearest) / len(nearest))
    return out


def net_effect(treated, control_change):
    """Per-event net reduction: post replies x (nudged change - control change)."""
    total, used = 0.0, 0
    per_event = []
    for row, control in zip(treated, control_change):
        if control is None:
            per_event.append(None)
            continue
        value = row[1] * (row[3] - control)
        per_event.append(value)
        total += value
        used += 1
    return {"events": used, "net_tokens": total, "per_event": per_event}


def cluster_interval(groups, values, resamples=1000, seed=7):
    """Mean per event with a 95% interval that resamples whole sessions."""
    import random
    by = {}
    for group, value in zip(groups, values):
        if value is not None:
            by.setdefault(group, []).append(value)
    flat = [v for vals in by.values() for v in vals]
    if not flat:
        return {"events": 0, "sessions": 0, "mean": None, "ci95": None}
    keys, rnd, means = sorted(by), random.Random(seed), []
    for _ in range(resamples):
        draw = [v for key in (rnd.choice(keys) for _ in keys) for v in by[key]]
        means.append(sum(draw) / len(draw))
    means.sort()
    return {"events": len(flat), "sessions": len(keys), "mean": sum(flat) / len(flat),
            "ci95": [means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]]}


def _fields(detail):
    import re
    return dict(re.findall(r"(\w+)=(\S+)", detail or ""))


class StudyDeadline(Exception):
    """The study ran out of its time budget; nothing is written."""


def verbosity_study(conn, start_utc, end_utc, tier_data, path_for, deadline=None, k=5, details=False, cache_dir=None):
    """Nudged replies against matched un-nudged replies, and what both carry.

    Treated: each ``verbosity_steer_measured`` event in the UTC window whose
    post-nudge replies can be found again and reproduce the booked amount
    exactly (events that cannot are counted, never guessed). Controls: user
    prompt boundaries in sessions where Token Optimizer logged savings but never
    nudged, so the absence of a nudge is real. Each nudge is compared with its
    ``k`` nearest controls by position, reply count, running mean and context
    size. Everything is signed; the caller decides what to credit.
    Raises StudyDeadline when ``deadline`` (a monotonic time) passes.
    """
    facade = _compat.facade()
    clock = facade.time.monotonic

    def check():
        if deadline is not None and clock() > deadline:
            raise StudyDeadline()

    walks = {}

    def walk_for(uuid):
        if uuid not in walks:
            check()
            path = path_for(uuid)
            walks[uuid] = (cached_walk(path, cache_dir) if cache_dir else walk_requests(path)) if path else None
        return walks[uuid]

    def card(turn):
        return facade._counted_exact_card(turn[1], tier_data, turn[2]) or {}

    # Local-naive timestamps: prefilter two days early, then convert each one.
    loose = (start_utc - facade.timedelta(days=2)).isoformat()
    events = conn.execute(
        "SELECT id, timestamp, session_uuid, tokens_saved, detail FROM savings_events "
        "WHERE event_type='verbosity_steer_measured' AND timestamp >= ? ORDER BY timestamp",
        (loose,)).fetchall()
    status = {"events": 0, "verified": 0, "verified_line_unit": 0, "no_source": 0,
              "no_transcript": 0, "not_reproducible": 0}
    treated, meta = [], []
    for _id, ts, uuid, booked, detail in events:
        when = facade._counted_event_utc(ts)
        if when is None or not (start_utc <= when < end_utc):
            continue
        status["events"] += 1
        fields = _fields(detail)
        source = conn.execute(
            "SELECT timestamp, detail FROM savings_events WHERE event_type='verbosity_steer' "
            "AND session_uuid=? AND timestamp=?", (uuid, fields.get("calibrates_ts"))).fetchone()
        try:
            src = _fields(source[1])
            pre, post, pre_avg = int(src["pre_turns"]), int(fields["post_turns"]), int(fields["pre_avg_out"])
        except (TypeError, KeyError, ValueError):
            status["no_source"] += 1
            continue
        walk = walk_for(uuid)
        if not walk or not walk["outputs"]:
            status["no_transcript"] += 1
            continue

        if "unit=req" in (source[1] or ""):
            rows = post_window(walk, pre, post)
            outs = [r[2] for r in rows] if rows else []
            if not rows or max(0, int((pre_avg - sum(outs) / len(outs)) * len(outs))) != int(booked or 0):
                status["not_reproducible"] += 1
                continue
            base = pre_avg
            status["verified"] += 1
        else:
            legacy = legacy_line_window(walk, pre, post, pre_avg, int(booked or 0))
            if legacy is None:
                status["not_reproducible"] += 1
                continue
            rows, base = legacy
            status["verified_line_unit"] += 1
        index_of = {t[3]: i for i, t in enumerate(walk["turns"])}
        first = index_of.get(rows[0][0])
        context = walk["turns"][first - 1][2] if first else 0
        outs = [r[2] for r in rows]
        position = next(i for i, r in enumerate(walk["outputs"]) if r[0] == rows[0][0])
        treated.append((position, len(rows), base, base - sum(outs) / len(rows), context))
        turn = walk["turns"][first] if first is not None else (None, None, 0, None)
        meta.append((uuid, rows, base, int(booked or 0), card(turn).get("output")))

    nudged = {r[0] for r in conn.execute(
        "SELECT DISTINCT session_uuid FROM savings_events WHERE event_type LIKE 'verbosity_steer%'")}
    logged = {r[0] for r in conn.execute(
        "SELECT DISTINCT session_uuid FROM savings_events WHERE timestamp >= ? "
        "UNION SELECT DISTINCT session_uuid FROM compression_events WHERE timestamp >= ?", (loose, loose))}
    controls, control_sessions = [], 0
    for uuid in sorted(u for u in logged if u and u not in nudged):
        check()
        path = path_for(uuid)
        if not path:
            continue
        rows = boundary_changes(walk_requests(path))
        if rows:
            control_sessions += 1
            controls.extend(rows)

    control = match_controls(treated, controls, k=k) if len(controls) >= MIN_CONTROL_SAMPLES else [None] * len(treated)
    groups = [m[0] for m in meta]
    out_tok = {"booked": 0, "signed": 0, "net": 0.0}
    out_usd = {"booked": 0.0, "signed": 0.0, "net": 0.0}
    reread = {"signed": [0, 0.0, 0], "net": [0, 0.0, 0]}
    unmapped = 0
    net_events = []
    rate = lambda turn: card(turn).get("cache_read")  # noqa: E731
    for row, (uuid, rows, pre_avg, booked, out_rate), ctrl in zip(treated, meta, control):
        signed = signed_delta(pre_avg, [r[2] for r in rows])
        priced = isinstance(out_rate, (int, float)) and out_rate >= 0
        out_tok["booked"] += booked
        out_tok["signed"] += signed
        if priced:
            out_usd["booked"] += booked * out_rate / 1e6
            out_usd["signed"] += signed * out_rate / 1e6
        plans = [("signed", [pre_avg - r[2] for r in rows])]
        if ctrl is not None:
            net = row[1] * (row[3] - ctrl)
            net_events.append(net)
            out_tok["net"] += net
            if priced:
                out_usd["net"] += net * out_rate / 1e6
            plans.append(("net", [pre_avg - r[2] - ctrl for r in rows]))
        else:
            net_events.append(None)
        for name, reductions in plans:
            score = score_rereads(walks[uuid], rows, reductions, rate)
            if name == "signed":
                unmapped += score["unmapped_requests"]
            reread[name][0] += score["tokens"]
            reread[name][1] += score["usd"]
            reread[name][2] += score["unpriced_tokens"]
    result = {
        "window_utc": [start_utc.isoformat(), end_utc.isoformat()],
        "status": status, "treated": len(treated),
        "control_sessions": control_sessions, "control_boundaries": len(controls),
        "matched": sum(c is not None for c in control),
        "output_tokens": {k2: round(v) for k2, v in out_tok.items()},
        "output_usd": {k2: round(v, 6) for k2, v in out_usd.items()},
        "reread_tokens": {k2: round(v[0]) for k2, v in reread.items()},
        "reread_usd": {k2: round(v[1], 6) for k2, v in reread.items()},
        "reread_unpriced_tokens": {k2: round(v[2]) for k2, v in reread.items()},
        "replies_without_parent_call": unmapped,
        "per_event": {
            "booked": cluster_interval(groups, [m[3] for m in meta]),
            "signed": cluster_interval(groups, [row[1] * row[3] for row in treated]),
            "control": cluster_interval(groups, [row[1] * c if c is not None else None
                                                 for row, c in zip(treated, control)]),
            "net": cluster_interval(groups, net_events),
        },
    }
    if details:
        result["details"] = {"treated": treated, "groups": groups, "controls": controls,
                             "control_change": control, "net_events": net_events}
    return result


def credited(study):
    """What the estimated tier may credit: the net-of-control reduction and its
    rereads, never below zero, and nothing without a matched control."""
    if not study or not study.get("matched"):
        return {"tokens": 0, "usd": 0.0, "reread_tokens": 0, "reread_usd": 0.0, "basis": "not_measured"}
    tokens = max(0, int(study["output_tokens"]["net"]))
    usd = max(0.0, float(study["output_usd"]["net"]))
    reread_tokens = max(0, int(study["reread_tokens"]["net"]))
    reread_usd = max(0.0, float(study["reread_usd"]["net"]))
    basis = "net_of_control" if (tokens or reread_tokens) else "no_reduction_net_of_control"
    return {"tokens": tokens, "usd": usd, "reread_tokens": reread_tokens,
            "reread_usd": reread_usd, "basis": basis}


STUDY_FILE = "verbosity-study.json"
STUDY_VERSION = 1
STUDY_DAYS = 30
STUDY_MAX_AGE_HOURS = 20
STUDY_STALE_DAYS = 7


def _study_path():
    return _compat.facade().SNAPSHOT_DIR / STUDY_FILE


def load_study():
    """The last stored study, or None when missing, unreadable or too old."""
    facade = _compat.facade()
    try:
        data = facade.json.loads(_study_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != STUDY_VERSION:
            return None
        made = facade.datetime.fromisoformat(data["computed_at_utc"])
        age = facade.datetime.now(facade.timezone.utc).replace(tzinfo=None) - made
        return data if age <= facade.timedelta(days=STUDY_STALE_DAYS) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def refresh_study(conn, deadline=None, force=False, now_utc=None):
    """Recompute the stored study when it is older than STUDY_MAX_AGE_HOURS.

    Read-only on the database; writes one derived JSON file atomically. Runs
    from user-run and detached collects only, never from a hook. Returns the
    study, or None when it was fresh, timed out or failed."""
    facade = _compat.facade()
    try:
        current = None if force else load_study()
        now = now_utc or facade.datetime.now(facade.timezone.utc).replace(tzinfo=None)
        if current and now - facade.datetime.fromisoformat(current["computed_at_utc"]) < facade.timedelta(hours=STUDY_MAX_AGE_HOURS):
            return None
        tier = facade.PRICING_TIERS.get(facade._load_pricing_tier(), facade.PRICING_TIERS["anthropic"])
        study = verbosity_study(conn, now - facade.timedelta(days=STUDY_DAYS), now, tier,
                                lambda uuid: facade._counted_session_path(conn, uuid), deadline=deadline,
                                cache_dir=facade.SNAPSHOT_DIR / "verbosity-walk-cache")
        study.update(version=STUDY_VERSION, computed_at_utc=now.isoformat(), days=STUDY_DAYS)
        path = _study_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(facade.json.dumps(study, indent=1), encoding="utf-8")
        facade.os.replace(tmp, path)
        return study
    except StudyDeadline:
        return None
    except Exception:
        return None


def summary_pool(booked_entry, study=None):
    """Estimated-tier line for nudged replies, sized net of the control.

    ``booked_entry`` is the relocated ``verbosity_steer_measured`` aggregate
    (kept for reference, never credited). The credit is the study's
    net-of-control reduction per matched event, output plus its rereads,
    floored at zero, times this window's events. Without a study the line is
    NOT MEASURED and credits nothing."""
    entry = booked_entry or {}
    events = int(entry.get("events", 0) or 0)
    pool = {"events": events, "tokens_saved": 0, "cost_saved_usd": 0.0,
            "booked_tokens": int(entry.get("tokens_saved", 0) or 0),
            "booked_usd": round(float(entry.get("cost_saved_usd", 0.0) or 0.0), 4),
            "output_tokens": 0, "reread_tokens": 0, "reread_usd": 0.0,
            "basis": "not_measured", "tier": "estimated"}
    if study is None:
        study = load_study()
    if not study or not study.get("matched"):
        return pool
    matched = int(study["matched"])
    net = study["per_event"]["net"]
    pool.update(
        basis="net_of_control", study_window_utc=study.get("window_utc"),
        study_events=matched, control_boundaries=int(study.get("control_boundaries", 0)),
        net_per_event_tokens=net.get("mean"), net_per_event_ci95=net.get("ci95"),
        booked_per_event_tokens=study["per_event"]["booked"].get("mean"),
        control_per_event_tokens=study["per_event"]["control"].get("mean"))
    per = lambda value: max(0.0, float(value)) / matched  # noqa: E731
    out_tok = per(study["output_tokens"]["net"]) * events
    out_usd = per(study["output_usd"]["net"]) * events
    rr_tok = per(study["reread_tokens"]["net"]) * events
    rr_usd = per(study["reread_usd"]["net"]) * events
    pool.update(output_tokens=int(out_tok), reread_tokens=int(rr_tok), reread_usd=round(rr_usd, 4),
                tokens_saved=int(out_tok + rr_tok), cost_saved_usd=round(out_usd + rr_usd, 4))
    if not pool["tokens_saved"]:
        pool["basis"] = "no_reduction_net_of_control"
    return pool


def cli_line(pool, amount_text=None):
    """One CLI line for the pool, or "" when there were no nudges."""
    pool = pool or {}
    if not pool.get("events"):
        return ""
    if pool.get("basis") == "not_measured":
        return (f"  Concise output nudges: NOT MEASURED ({pool['events']:,} calibrated nudges; "
                "no un-nudged control yet, so nothing is credited)")
    net = pool.get("net_per_event_tokens") or 0.0
    ci = pool.get("net_per_event_ci95") or [0.0, 0.0]
    if pool.get("basis") == "no_reduction_net_of_control":
        return (f"  Concise output nudges: no reduction net of an un-nudged control "
                f"({net:+,.0f} tokens per nudge, 95% interval {ci[0]:+,.0f} to {ci[1]:+,.0f}); $0 credited")
    amount = amount_text or f"{pool['cost_saved_usd']:.2f}"
    return (f"  + est. concise output nudges: ~${amount} net of an un-nudged control, "
            f"including {pool['reread_tokens']:,} re-read tokens "
            f"({net:+,.0f} tokens per nudge, 95% interval {ci[0]:+,.0f} to {ci[1]:+,.0f}) [estimated]")
