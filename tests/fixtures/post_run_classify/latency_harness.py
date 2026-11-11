#!/usr/bin/env python3
"""W2 latency layer: added p95 wall time per Bash result, treatment minus baseline.

Each sample is one full hook subprocess (import + run), as Claude Code pays
it. Arms are interleaved per round so machine load hits all arms alike.
Bar (pre-registered): added p95 <= 30 ms for every item class, N=30.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_harness as g  # noqa: E402
import make_fixture  # noqa: E402

CLASSES = {
    "newly-eligible git diff": "P00-cd_semi",
    "newly-eligible git status": "P02-newline",
    "newly-eligible grep": "P03-stacked",
    "newly-eligible pytest": "P07-cd_semi",
    "rejected multi-statement": "N-multi-0",
    "rejected file print": "N-fc0-20",
    "bare read-only (unchanged path)": "P00-bare",
}


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hook-dir", required=True, type=Path)
    ap.add_argument("--baseline-dir", required=True, type=Path)
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--json-out", type=Path)
    a = ap.parse_args(argv)
    items = {i["id"]: i for i in make_fixture.build(2)["items"]}
    # --baseline-dir equal to --hook-dir = kill-switch arm on the same install
    # (removes install-path confounds; the gate proves it byte-identical).
    same = a.baseline_dir.resolve() == a.hook_dir.resolve()
    arms = {"baseline": (a.baseline_dir, "0" if same else None), "on": (a.hook_dir, "1"), "shadow": (a.hook_dir, "shadow")}
    samples = {(c, arm): [] for c in CLASSES for arm in arms}
    cpu = {(c, arm): [] for c in CLASSES for arm in arms}
    for rnd in range(a.n):
        for cname, iid in CLASSES.items():
            it = items[iid]
            for arm, (d, mode) in arms.items():
                ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
                t0 = time.perf_counter()
                r = g.run_hook(d, a.workdir, f"lat-{arm}-{iid}-{rnd}", it["command"], it["stdout"],
                               it["stderr"], it["exit_code"], mode=mode)
                samples[(cname, arm)].append((time.perf_counter() - t0) * 1000)
                ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
                cpu[(cname, arm)].append(((ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)) * 1000)
                if r["rc"] != 0:
                    print(f"CANNOT-MEASURE: hook crashed ({arm}, {iid})")
                    return 2
    worst = 0.0
    worst_cpu = 0.0
    out = {}
    print(f"{'class':34s} {'wall base':>9s} {'+on':>7s} {'+shadow':>8s} | {'cpu base':>8s} {'+on':>7s} {'+shadow':>8s}"
          f"  (p95 ms, N={a.n})")
    for cname in CLASSES:
        b, o, s_ = (p95(samples[(cname, k)]) for k in ("baseline", "on", "shadow"))
        cb, co, cs = (p95(cpu[(cname, k)]) for k in ("baseline", "on", "shadow"))
        worst = max(worst, o - b, s_ - b)
        worst_cpu = max(worst_cpu, co - cb, cs - cb)
        out[cname] = {"wall_p95": [b, o, s_], "cpu_p95": [cb, co, cs]}
        print(f"{cname:34s} {b:9.1f} {o - b:+7.1f} {s_ - b:+8.1f} | {cb:8.1f} {co - cb:+7.1f} {cs - cb:+8.1f}")
    verdict = "PASS" if worst <= 30.0 else "FAIL"
    print(f"worst added wall p95: {worst:+.1f} ms  bar <= +30 ms  {verdict} (wall, as registered)")
    print(f"worst added CPU p95:  {worst_cpu:+.1f} ms  (diagnostic: user+sys of the hook process)")
    if a.json_out:
        a.json_out.write_text(json.dumps({"n": a.n, "classes": out, "worst_added_wall_p95_ms": worst, "worst_added_cpu_p95_ms": worst_cpu}, indent=1))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
