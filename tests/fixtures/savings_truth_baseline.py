#!/usr/bin/env python3
"""Savings-truth U0 baseline: reproduce the pre-change savings figures.

Regression-fixture SOURCE for the savings-truth work. It is NOT a test and is
never collected by pytest (no test_ prefix). Every input path is a required
argument with no default, so nothing here can touch real user data unless an
operator points it there explicitly. All DB access is read-only (sqlite URI
mode=ro); product functions run in a child process against a private COPY of
the DB with an isolated HOME and TOKEN_OPTIMIZER_SNAPSHOT_DIR.

Figures (plan section 2):
  1. counted ledger, 30d       -> product _counted_cumulative_summary(days=30)
  2. write-class premium        -> same call, oneshot_class_premium_usd
  3. cloud-stub sessions, 30d   -> session_log SQL (method of fable active.py)
  4. ledger per-session share   -> counted_reread + session_log (active.py)
  5. cloud transcript scan      -> byte scan of a transcript dir (fable scan)
  6. structural                 -> product _resolve_structural_baseline on a
                                   copied snapshot_before.json; current total
                                   from the payload (live audit of real HOME);
                                   delta = max(0, baseline - current) exactly as
                                   _compute_structural_savings does.

Usage:
  python3 savings_truth_baseline.py --db TRENDS_COPY.db --payload DASH.html \
      --owner-data OWNER_DATA_DIR --scripts REPO/skills/token-optimizer/scripts \
      --workdir SCRATCH_DIR --expected PRIVATE_EXPECTED.json [--cloud-dir STUB_TRANSCRIPT_DIR] [--json OUT.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Expected figures and window anchors come from a private JSON (--expected),
# never from this file: they are one user's real usage data. Shape:
#   {"session_cut": "YYYY-MM-DD", "cloud_scan_at": "ISO-8601 instant",
#    "figures": {"ledger_events": N, ...}}


def ro(db):
    return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db), uri=True)


def payload_obj(html, key):
    """raw_decode the first JSON value that follows "key": in the dashboard HTML."""
    i = html.find('"%s":' % key)
    if i < 0:
        return None
    j = i + len(key) + 3
    while html[j] in " \t\n":
        j += 1
    return json.JSONDecoder().raw_decode(html, j)[0]


CHILD = r"""
import json, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
import measure as m
now = datetime.fromisoformat(sys.argv[2])
assert str(m.TRENDS_DB).startswith(sys.argv[3]), m.TRENDS_DB
out = {"trends_db": str(m.TRENDS_DB)}
out["counted_period"] = m._counted_cumulative_summary(days=30, now=now)
out["structural_baseline"] = m._resolve_structural_baseline(days=30)
print("@@JSON@@" + json.dumps(out, default=str))
"""


def product_figures(args, period_end):
    """Run product functions on a DB copy with an isolated HOME."""
    work = os.path.abspath(args.workdir)
    home = os.path.join(work, "home")
    data = os.path.join(work, "data")
    os.makedirs(home, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    dst = os.path.join(data, "trends.db")
    if os.path.exists(dst):
        os.replace(dst, dst + ".prev")  # keep, never delete
    src = ro(args.db)
    out = sqlite3.connect(dst)
    src.backup(out)  # consistent copy including WAL content
    out.close()
    src.close()
    for name in ("snapshot_before.json", "snapshot_after.json", "pretool_baseline.json"):
        p = os.path.join(args.owner_data, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(data, name))
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k.startswith("TOKEN_OPTIMIZER")
                   or k in ("CODEX_HOME", "HERMES_HOME", "COPILOT_HOME"))}
    env.update(HOME=home, TOKEN_OPTIMIZER_SNAPSHOT_DIR=data,
               TOKEN_OPTIMIZER_RUNTIME="claude", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", CHILD, os.path.abspath(args.scripts),
                        period_end, data], env=env, cwd=work,
                       capture_output=True, text=True, timeout=600)
    line = [x for x in r.stdout.splitlines() if x.startswith("@@JSON@@")]
    if r.returncode or not line:
        raise SystemExit("product call failed:\n" + r.stdout[-2000:] + r.stderr[-4000:])
    return json.loads(line[0][8:])


def session_figures(db, cut):
    """Cloud-stub and ledger-session figures (fable active.py method)."""
    c = ro(db)
    act = {r[0] for r in c.execute(
        "select distinct session_uuid from savings_events where timestamp>=? and session_uuid is not null", (cut,))}
    act |= {r[0] for r in c.execute(
        "select distinct session_uuid from compression_events where timestamp>=? and session_uuid is not null", (cut,))}
    led = {r[0] for r in c.execute("select distinct session_uuid from counted_reread where event_ts>=?", (cut,))}
    agg = defaultdict(lambda: [0, 0, 0, 0])
    stub_ids = []
    for su, proj, api, inp, tj in c.execute(
            "select session_uuid,project,api_calls,input_tokens,tool_calls_json from session_log where date>=?", (cut,)):
        try:
            b = json.loads(tj or "{}").get("Bash", 0)
        except Exception:
            b = 0
        b = b if isinstance(b, int) else 0
        k = "ledger" if su in led else ("any_event" if su in act else "no_event")
        stub = "cloud-stubs" in (proj or "")
        a = agg[(k, stub)]
        a[0] += 1; a[1] += inp or 0; a[2] += api or 0; a[3] += b
        if stub:
            stub_ids.append(su)
    # events attached to any stub session, any table, any time
    ev = 0
    for i in range(0, len(stub_ids), 500):
        chunk = stub_ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for t, col in (("savings_events", "session_uuid"), ("compression_events", "session_uuid"),
                       ("counted_reread", "session_uuid")):
            ev += c.execute("select count(*) from %s where %s in (%s)" % (t, col, q), chunk).fetchone()[0]
    stub_all = [sum(v[i] for k, v in agg.items() if k[1]) for i in range(4)]
    stub_noev = agg[("no_event", True)]
    per = []
    for su, tk, rt in c.execute(
            "select session_uuid,sum(tokens),sum(reread_tokens) from counted_reread where event_ts>=? group by 1", (cut,)):
        r = c.execute("select input_tokens from session_log where session_uuid=?", (su,)).fetchone()
        if r and r[0]:
            per.append((tk + rt) / (r[0] + tk + rt))
    per.sort()
    n = len(per)
    return {
        "cloud_stub_sessions_no_event": stub_noev[0],
        "cloud_stub_input_no_event": stub_noev[1],
        "cloud_stub_api_no_event": stub_noev[2],
        "cloud_stub_bash_no_event": stub_noev[3],
        "cloud_stub_sessions_all": stub_all[0],
        "cloud_stub_input_all": stub_all[1],
        "cloud_stub_bash_all": stub_all[3],
        "cloud_stub_events_any_time": ev,
        "ledger_sessions": n,
        "ledger_share_median": per[n // 2] if n else None,
        "ledger_share_p75": per[3 * n // 4] if n else None,
        "ledger_share_p90": per[int(n * .9)] if n else None,
        "ledger_share_max": per[-1] if n else None,
    }


def cloud_scan(cloud_dir, scan_at):
    """Fable's cloud transcript scan, with a fixed window: mtime >= scan_at - 30d."""
    at = datetime.fromisoformat(scan_at).timestamp()
    cut = at - 30 * 86400
    n = hook = mark = bud = cbud = later = 0
    for f in sorted(glob.glob(os.path.join(cloud_dir, "*.jsonl"))):
        mt = os.path.getmtime(f)
        if mt < cut:
            continue
        n += 1
        later += mt > at
        d = open(f, "rb").read()
        p = d.count(b'"hookName":"PostToolUse:Bash"')
        if d.count(b"[Token Optimizer") > 0 or p > 0:
            hook += 1
        if re.search(rb"lines omitted|expand with|Full result archived|\[compressed", d):
            mark += 1
        cbud += d.count(b"bash_compress_hook budget exceeded")
        bud += d.count(b"budget exceeded")
    return {"cloud_files": n, "cloud_files_with_to_traces": hook, "cloud_files_with_markers": mark,
            "cloud_budget_exceeded": bud, "cloud_budget_exceeded_bash_compress": cbud,
            "cloud_files_modified_after_scan": later}


def pct(exp, got):
    if got is None:
        return None
    if exp == 0:
        return 0.0 if got == 0 else float("inf")
    return 100.0 * (got - exp) / exp


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="read-only trends.db copy")
    ap.add_argument("--payload", required=True, help="dashboard HTML holding the live payload")
    ap.add_argument("--owner-data", required=True, help="dir with snapshot_before.json etc (copied, never written)")
    ap.add_argument("--scripts", required=True, help="repo skills/token-optimizer/scripts")
    ap.add_argument("--workdir", required=True, help="scratch dir for the isolated HOME and DB copy")
    ap.add_argument("--cloud-dir", help="cloud-stub transcript dir to scan (read only)")
    ap.add_argument("--expected", required=True, help="private JSON with expected figures and window anchors")
    ap.add_argument("--json", help="write all figures here")
    a = ap.parse_args(argv)
    with open(a.expected, encoding="utf-8") as fh:
        spec = json.load(fh)
    EXPECTED = spec["figures"]

    html = open(a.payload, encoding="utf-8").read()
    pay_cp = payload_obj(html, "counted_period") or {}
    pay_sd = payload_obj(html, "structural_detail") or {}
    m = re.search(r'"current_total":\s*(\d+)', html)
    pay_current = int(m.group(1)) if m else None
    period_end = pay_cp.get("period_end") or datetime.now(timezone.utc).isoformat()

    prod = product_figures(a, period_end)
    cp = prod["counted_period"]
    sb = prod["structural_baseline"] or {}
    sess = session_figures(a.db, spec["session_cut"])
    cloud = cloud_scan(a.cloud_dir, spec["cloud_scan_at"]) if a.cloud_dir else {}
    base = sb.get("baseline_tokens")
    delta = max(0, base - pay_current) if base is not None and pay_current is not None else None

    got = {
        "ledger_events": cp.get("events"),
        "ledger_removed_tokens": cp.get("removed_tokens"),
        "ledger_reread_tokens": cp.get("reread_tokens"),
        "ledger_reread_usd": cp.get("reread_usd"),
        "write_class_premium_usd": cp.get("oneshot_class_premium_usd"),
        "cloud_stub_sessions": sess["cloud_stub_sessions_no_event"],
        "cloud_stub_input_tokens": sess["cloud_stub_input_no_event"],
        "cloud_stub_bash_calls": sess["cloud_stub_bash_no_event"],
        "cloud_stub_events": sess["cloud_stub_events_any_time"],
        "ledger_sessions": sess["ledger_sessions"],
        "ledger_share_median": sess["ledger_share_median"],
        "ledger_share_p75": sess["ledger_share_p75"],
        "ledger_share_p90": sess["ledger_share_p90"],
        "cloud_files": cloud.get("cloud_files"),
        "cloud_files_with_to_traces": cloud.get("cloud_files_with_to_traces"),
        "cloud_budget_exceeded": cloud.get("cloud_budget_exceeded"),
        "cloud_budget_exceeded_bash_compress": cloud.get("cloud_budget_exceeded_bash_compress"),
        "structural_baseline": base,
        "structural_current": pay_current,
        "structural_delta": delta,
    }
    payload = {
        "ledger_events": pay_cp.get("events"), "ledger_removed_tokens": pay_cp.get("removed_tokens"),
        "ledger_reread_tokens": pay_cp.get("reread_tokens"), "ledger_reread_usd": pay_cp.get("reread_usd"),
        "write_class_premium_usd": pay_cp.get("oneshot_class_premium_usd"),
        "structural_delta": pay_sd.get("overhead_delta"),
    }
    print("%-38s %16s %16s %16s %8s" % ("figure", "expected", "reproduced", "payload", "diff%"))
    flagged = []
    for k, exp in EXPECTED.items():
        g = got[k]
        d = pct(exp, g)
        mark = "" if d is not None and abs(d) <= 2 else "  <-- >2%"
        if mark:
            flagged.append(k)
        print("%-38s %16s %16s %16s %8s%s" % (k, exp, g, payload.get(k, ""),
                                              "n/a" if d is None else "%.2f" % d, mark))
    extra = {"trends_db_used": prod["trends_db"], "period_end": period_end,
             "structural_baseline_date": sb.get("date"),
             "payload_structural_detail": pay_sd, "sessions": sess, "cloud": cloud,
             "counted_period_reproduced": cp}
    print("\nextra:", json.dumps({k: v for k, v in extra.items() if k != "counted_period_reproduced"},
                                 indent=1, default=str))
    print("\nflagged (>2%):", flagged or "none")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"expected": EXPECTED, "reproduced": got, "payload": payload, "extra": extra},
                      fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
