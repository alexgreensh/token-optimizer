#!/usr/bin/env python3
"""W2 gate: post-run classify, deterministic checks 1-8 per item. Code only.

Pre-registration: prereg-w2.md (Token Optimizer coverage plan, W2).
Exit codes: 0 PASS, 1 FAIL, 2 CANNOT-MEASURE (crash, missing baseline,
fixture hash mismatch).

Arms: every fixture item is run through the real hook in a subprocess with an
isolated HOME / snapshot dir and a fresh session id.
  baseline  hook from --baseline-dir (start commit), or the kill switch arm
  on        TOKEN_OPTIMIZER_POST_RUN_CLASSIFY=1 (the bar applies here)
  default   env unset: must equal the explicit on arm, without shadow logging
  shadow    =shadow: must equal baseline on every item
  off       =0: must equal baseline on every item
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_fixture  # noqa: E402

# Fixture hashes pinned per pre-registration (v1: prereg-20261110, v2: -w2b).
PINNED_HASHES = {
    1: "e4db38e0686430868169333bdc9cc043929bb6ef2d9e786b3cfedbb6e0f4d9cf",
    2: "5325418e325ffa47eb2d79e0da4c57f45d2ea7957fe512fcad492f1aa704331e",
}
PY = os.environ.get("W2_PYTHON", sys.executable)
NC_ID = "NC-drop-error-line"

ERROR_LINE_RE = re.compile(r"(?i)\b(error|warning|failed|failure|exception|traceback|fatal)\b")
KEY_RE = re.compile(r"\b[0-9a-f]{16}\b")
# Marker grammar (check 3). Frozen from bare-command outputs on the DEV split
# only (gate_harness.py --dump-markers --split dev), before any held-out run.
MARKER_RES = [re.compile(p) for p in (
    r"^$",
    r"^\[Full result archived \([0-9,]+ chars\) — saved to disk, not lost\.$",
    r"^Do NOT re-run the original tool to get this data — read the saved copy by running this in Bash:$",
    r"^\s*python3? \S*measure\.py expand KEY$",  # Windows prints `python`, not `python3`
    r"^\]$",
    r"^\s*\.\.\. \d+ more matches in \S+ \(content omitted\) \.\.\.$",
    r"^\s*\S+: \d+ matches$",
    r"^\.\.\. \(\d+ more lines, \+\d+/-\d+ total\)$",
    r"^\.\.\. \(\d+ entries omitted\)$",
    r"^\.\.\. \(\d+ more rows, \d+ total\)$",
    r"^\.\.\. \d+ lines omitted \.\.\.$",
    r"^\.\.\. plus \d+ more files with matches \(filenames \+ counts, content omitted\):$",
    r"^\.\.\. plus \d+ more \(names only, none dropped\): ",
    r"^\d+ passed in [0-9.]+s =+$",
    r"^\d+ (untracked|unstaged|staged): ",
    r"^All \d+ changed files: ",
    r"^branch: \S+$",
    r"^\[Search results: \d+ matches across \d+ files\. ",
    r"^\.\.\. \d+ more credential-bearing lines archived in full original \(retrieve via expand key\) \.\.\.$",
)]


def canon_hash(data: dict) -> str:
    """Same canonicalization as eval-genius hash_fixture.py (items sorted)."""
    obj = dict(data)
    obj["items"] = sorted(data["items"], key=lambda r: (json.dumps(r.get("id"), sort_keys=True, ensure_ascii=False),
                                                         json.dumps(r, sort_keys=True, ensure_ascii=False)))
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_hook(hook_dir: Path, workdir: Path, run_id: str, command: str, stdout: str,
             stderr: str = "", exit_code: int = 0, mode: str | None = None, bad: str = "",
             session: str | None = None) -> dict:
    home = workdir / run_id
    snap = home / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "CLAUDE_PLUGIN_DATA": str(home / "plugin-data"), "TOKEN_OPTIMIZER_PLUGIN_DATA": str(home / "plugin-data"),
        "W2_HOOK_DIR": str(hook_dir), "W2_BAD": bad, "PYTHONDONTWRITEBYTECODE": "1",
    }
    if mode is not None:
        env["TOKEN_OPTIMIZER_POST_RUN_CLASSIFY"] = mode
    sid = session or ("w2-" + hashlib.sha1(run_id.encode()).hexdigest()[:12])
    tr = {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False, "exit_code": exit_code}
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": tr,
               "session_id": sid, "tool_use_id": "toolu_" + sid}
    p = subprocess.run([PY, str(HERE / "hook_runner.py")], input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=60)
    emitted = None
    if p.stdout.strip():
        env_out = json.loads(p.stdout.strip().splitlines()[-1])
        upd = env_out.get("hookSpecificOutput", {}).get("updatedToolOutput")
        if upd is not None:
            emitted = upd.get("stdout")
    return {"emitted": emitted, "rc": p.returncode, "stderr": p.stderr[-2000:], "snap": str(snap), "sid": sid}


MEASURE_PATH_RE = re.compile(r"\S*measure\.py\b")


def norm(text):
    """Normalise the per-run archive key and the install path in the pointer."""
    return None if text is None else MEASURE_PATH_RE.sub("measure.py", KEY_RE.sub("KEY", text))


def visible(res: dict, raw: str) -> str:
    """What the model sees: the replacement, or the raw output."""
    return raw if res["emitted"] is None else res["emitted"]


def opportunity_rows(snap: str) -> tuple[int, int]:
    db = Path(snap) / "trends.db"
    if not db.is_file():
        return 0, 0
    con = sqlite3.connect(str(db))
    try:
        opp = con.execute("SELECT COUNT(*) FROM compression_events WHERE tier='opportunity' "
                          "AND feature='post_run_classify_shadow'").fetchone()[0]
        meas = con.execute("SELECT COUNT(*) FROM compression_events WHERE tier='measured'").fetchone()[0]
        return opp, meas
    except sqlite3.Error:
        return 0, 0
    finally:
        con.close()


def archive_text(snap: str, key: str) -> str | None:
    for f in Path(snap, "tool-archive").glob(f"*/{key}.json"):
        return json.loads(f.read_text(encoding="utf-8")).get("response")
    return None


def all_archive_blobs(snap: str) -> str:
    root = Path(snap, "tool-archive")
    return "".join(f.read_text(encoding="utf-8", errors="replace") for f in root.rglob("*") if f.is_file()) \
        if root.is_dir() else ""


def invented_lines(out: str, raw: str) -> list:
    raw_lines = set(raw.splitlines()) | {ln.strip() for ln in raw.splitlines()}
    bad = []
    for ln in norm(out).splitlines():
        if ln in raw_lines or ln.strip() in raw_lines:
            continue
        if any(r.search(ln) for r in MARKER_RES):
            continue
        bad.append(ln)
    return bad


def check_item(it: dict, on: dict, base: dict, bare_on: dict | None, again: dict | None) -> list:
    """Return the list of failed check names for one item on the `on` arm."""
    fails = []
    raw = it["stdout"]
    out = visible(on, raw)
    fam, prefixed = it["family"], it.get("pair_of") is not None
    if on["rc"] != 0 or base["rc"] != 0:
        return ["crash"]
    if prefixed:  # check 1
        if bare_on is None or norm(out) != norm(visible(bare_on, raw)):
            fails.append("1-paired-equivalence")
    if len(out) > len(raw):  # check 2
        fails.append("2-never-worse")
    if on["emitted"] is not None and invented_lines(out, raw):  # check 3
        fails.append("3-no-invented-text")
    if on["emitted"] is not None:  # check 4
        out_lines = set(out.splitlines())
        if any(ERROR_LINE_RE.search(ln) and ln not in out_lines for ln in raw.splitlines()):
            fails.append("4-preserved")
    keys = KEY_RE.findall(on["emitted"] or "") if "[Full result archived" in (on["emitted"] or "") else []
    if keys and fam != "secrets":  # check 5
        if archive_text(on["snap"], keys[-1]) != raw:
            fails.append("5-round-trip")
    if on["emitted"] is not None and again is not None:  # check 6
        if again["emitted"] is not None and again["emitted"] != on["emitted"]:
            fails.append("6-idempotent")
    if not prefixed:  # check 7 (negative space, bare items, destructive)
        if norm(on["emitted"]) != norm(base["emitted"]):
            fails.append("7-negative-space")
    if fam == "secrets":  # check 8 (A3: archive files only)
        if any(s in all_archive_blobs(on["snap"]) for s in it["secrets"]):
            fails.append("8-secret-in-archive")
    return fails


def diagnostics(it: dict, on: dict) -> dict:
    """Reported, not gated: as-registered check 8 and the raw check-4 result."""
    out = visible(on, it["stdout"])
    d = {}
    if it["family"] == "secrets":
        d["secret_in_visible_text"] = on["emitted"] is not None and any(s in out for s in it["secrets"])
    if on["emitted"] is not None:
        out_lines = set(out.splitlines())
        d["check4_dropped"] = [ln[:80] for ln in it["stdout"].splitlines()
                               if ERROR_LINE_RE.search(ln) and ln not in out_lines]
    return d


def run_arms(items, hook_dir, base_dir, workdir, bad, jobs):
    """Run every arm for every item; return dict arm -> id -> result."""
    tasks = {}
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        for it in items:
            args = (it["command"], it["stdout"], it["stderr"], it["exit_code"])
            if base_dir is not None:
                tasks[("baseline", it["id"])] = ex.submit(run_hook, base_dir, workdir, f"b-{it['id']}", *args)
            else:
                tasks[("baseline", it["id"])] = ex.submit(run_hook, hook_dir, workdir, f"b-{it['id']}", *args, mode="0")
            tasks[("on", it["id"])] = ex.submit(run_hook, hook_dir, workdir, f"on-{it['id']}", *args, mode="1", bad=bad)
            tasks[("default", it["id"])] = ex.submit(run_hook, hook_dir, workdir, f"def-{it['id']}", *args, bad=bad)
            tasks[("shadow", it["id"])] = ex.submit(run_hook, hook_dir, workdir, f"sh-{it['id']}", *args, mode="shadow", bad=bad)
            tasks[("off", it["id"])] = ex.submit(run_hook, hook_dir, workdir, f"off-{it['id']}", *args, mode="0", bad=bad)
        res = {k: f.result() for k, f in tasks.items()}
        again_tasks = {}
        for it in items:
            on = res[("on", it["id"])]
            if on["emitted"] is not None:
                again_tasks[it["id"]] = ex.submit(run_hook, hook_dir, workdir, f"re-{it['id']}", it["command"],
                                                  on["emitted"], it["stderr"], it["exit_code"], mode="1", bad=bad)
        again = {k: f.result() for k, f in again_tasks.items()}
    out = {"baseline": {}, "on": {}, "default": {}, "shadow": {}, "off": {}, "again": again}
    for (arm, iid), r in res.items():
        out[arm][iid] = r
    return out


def nc_item(fixture_items, version):
    """v1: W-1-cd input. v2: E-02-bare (bare git status + one lone error line)."""
    src = next(i for i in fixture_items if i["id"] == ("W-1-cd" if version == 1 else "E-02-bare"))
    return dict(src, id=NC_ID, family="control", split="heldout")


def evaluate(items, arms, nc):
    by_id = {it["id"]: it for it in items}
    report = []
    for it in items:
        bare = arms["on"].get(it["pair_of"]) if it.get("pair_of") else None
        fails = check_item(it, arms["on"][it["id"]], arms["baseline"][it["id"]], bare, arms["again"].get(it["id"]))
        default, on = arms["default"][it["id"]], arms["on"][it["id"]]
        if default["rc"] != 0:
            fails.append("default-crash")
        elif norm(default["emitted"]) != norm(on["emitted"]):
            fails.append("default-not-identical-to-on")
        if opportunity_rows(default["snap"]) != opportunity_rows(on["snap"]):
            fails.append("default-accounting-not-identical-to-on")
        if opportunity_rows(on["snap"])[0] != 0:
            fails.append("on-logged-opportunity")
        base_v = visible(arms["baseline"][it["id"]], it["stdout"])
        for arm in ("shadow", "off"):
            r = arms[arm][it["id"]]
            if r["rc"] != 0:
                fails.append(f"{arm}-crash")
            elif norm(visible(r, it["stdout"])) != norm(base_v):
                fails.append(f"{arm}-not-identical-to-baseline")
        opp, meas = opportunity_rows(arms["shadow"][it["id"]]["snap"])
        newly = it.get("pair_of") is not None and arms["on"][it["id"]]["emitted"] is not None \
            and arms["baseline"][it["id"]]["emitted"] is None
        if newly and (opp != 1 or meas != 0):
            fails.append(f"shadow-log(opp={opp},measured={meas})")
        if not newly and opp != 0:
            fails.append(f"shadow-log-unexpected(opp={opp})")
        if opportunity_rows(arms["off"][it["id"]]["snap"])[0] != 0:
            fails.append("off-logged-opportunity")
        diag = diagnostics(it, arms["on"][it["id"]])
        if it.get("pair_of") and "4-preserved" in fails:
            bare_fails = check_item(by_id[it["pair_of"]], arms["on"][it["pair_of"]],
                                    arms["baseline"][it["pair_of"]], None, None)
            diag["check4_regression_vs_bare"] = "4-preserved" not in bare_fails
        report.append({"id": it["id"], "split": it["split"], "family": it["family"], "fails": fails,
                       "newly_eligible": newly, "diag": diag})
    nc_fails = check_item(nc, nc["_on"], nc["_base"], nc["_bare"], None)
    report.append({"id": NC_ID, "split": "heldout", "family": "control", "fails": nc_fails, "newly_eligible": True})
    return report


def gate_files(report, fixture_hash, live, outdir: Path, baseline_report=None):
    """Write eval-genius check_gate.py inputs; held-out deterministic layer."""
    def doc(rows, liveness):
        return {"manifest": {"fixture_hash": fixture_hash, "liveness": liveness, "negative_control_id": NC_ID},
                "items": [{"id": r["id"], "verdict": "fail" if r["fails"] else "pass", "layer": "deterministic"}
                          for r in rows if r["split"] == "heldout"]}
    (outdir / "treatment.json").write_text(json.dumps(doc(report, "passed" if live else "failed"), indent=1))
    if baseline_report is not None:
        (outdir / "baseline.json").write_text(json.dumps(doc(baseline_report, "not-applicable"), indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hook-dir", required=True, type=Path)
    ap.add_argument("--baseline-dir", type=Path, help="start-commit scripts dir (default: kill-switch arm)")
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--bad", default="", choices=["", "classifier", "compressor"])
    ap.add_argument("--split", default="heldout", choices=["dev", "heldout", "all"])
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--fixture-version", type=int, default=2, choices=[1, 2])
    ap.add_argument("--ids", default="", help="comma-separated item ids (unit tests); bar runs use --split")
    ap.add_argument("--dump-markers", action="store_true", help="print invented lines on bare dev items")
    a = ap.parse_args(argv)
    try:
        data = make_fixture.build(a.fixture_version)
        fh = canon_hash(data)
        if fh != PINNED_HASHES[a.fixture_version]:
            print(f"CANNOT-MEASURE: fixture hash {fh} != pinned {PINNED_HASHES[a.fixture_version]}")
            return 2
        items = [i for i in data["items"] if a.split == "all" or i["split"] == a.split]
        if a.ids:
            wanted = set(a.ids.split(","))
            items = [i for i in data["items"] if i["id"] in wanted]
        if a.dump_markers:
            items = [i for i in data["items"] if i["split"] == "dev" and not i.get("pair_of")]
        a.workdir.mkdir(parents=True, exist_ok=True)
        need = {i["pair_of"] for i in items if i.get("pair_of")} - {i["id"] for i in items}
        extra = [i for i in data["items"] if i["id"] in need]  # bare partners from the other split
        arms = run_arms(items + extra, a.hook_dir, a.baseline_dir, a.workdir, a.bad, a.jobs)
        if a.dump_markers:
            seen = {}
            for it in items:
                r = arms["baseline"][it["id"]]
                if r["emitted"] is not None:
                    for ln in invented_lines(r["emitted"], it["stdout"]):
                        seen.setdefault(ln[:120], it["id"])
            for ln, iid in sorted(seen.items()):
                print(f"{iid}\t{ln}")
            return 0
        nc = nc_item(data["items"], a.fixture_version)
        args = (nc["command"], nc["stdout"], nc["stderr"], nc["exit_code"])
        nc["_on"] = run_hook(a.hook_dir, a.workdir, "nc-on", *args, mode="1", bad="compressor")
        nc["_base"] = run_hook(a.hook_dir, a.workdir, "nc-base", *args, mode="0", bad="compressor")
        nc["_bare"] = None
        nc["pair_of"] = None  # checked as a bare item: check 4 must fail
        crashes = [k for arm in ("baseline", "on") for k, r in arms[arm].items() if r["rc"] != 0]
        if crashes:
            print(f"CANNOT-MEASURE: hook crashed on {crashes[:5]}")
            for k in crashes[:2]:
                print(arms["on"].get(k, arms["baseline"].get(k))["stderr"])
            return 2
        report = [r for r in evaluate(items + extra, arms, nc) if r["id"] in {i["id"] for i in items} | {NC_ID}]
    except Exception as exc:  # crash = CANNOT-MEASURE, never FAIL or PASS
        print(f"CANNOT-MEASURE: harness crashed: {type(exc).__name__}: {exc}")
        return 2
    live = any(r["newly_eligible"] for r in report if r["id"] != NC_ID)
    nc_row = next(r for r in report if r["id"] == NC_ID)
    rows = [r for r in report if r["id"] != NC_ID]
    failed = [r for r in rows if r["fails"]]
    by_check = {}
    for r in failed:
        for f in r["fails"]:
            by_check[f] = by_check.get(f, 0) + 1
    newly = sum(1 for r in rows if r["newly_eligible"])
    print(f"fixture sha256:{fh}  split={a.split}  items={len(rows)}  bad={a.bad or 'none'}")
    print(f"newly eligible (on arm compresses, baseline raw): {newly}")
    print(f"passed {len(rows) - len(failed)}/{len(rows)}; failures by check: {by_check or 'none'}")
    for r in failed[:40]:
        print(f"  FAIL {r['id']:28s} {r['family']:12s} {r['fails']} {r.get('diag', {})}")
    print(f"negative control {NC_ID}: {'failed as required' if nc_row['fails'] else 'PASSED (verifier broken)'}"
          f" {nc_row['fails']}")
    if a.json_out:
        a.json_out.write_text(json.dumps({"fixture_hash": fh, "report": report}, indent=1))
    if not nc_row["fails"]:
        print("VERDICT: CANNOT-MEASURE (negative control passed)")
        return 2
    if not live:
        print("VERDICT: CANNOT-MEASURE (treatment arm not live: nothing newly eligible)")
        return 2
    if failed:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
