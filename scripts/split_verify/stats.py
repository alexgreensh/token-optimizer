"""Paired statistics for L7. Stdlib only, seeded, deterministic.

Verdict rule (pre-registered in TEST-PLAN.md, made exact here):
  ratio r = stat(candidate) / stat(base), with a paired bootstrap 95% interval
  [lo, hi] that resamples sample *pairs* (base_i, candidate_i ran back to back).
  PASS            hi <= 1 + pass_bar
  FAIL            lo >  1 + pass_bar (regression established), or
                  r  >  1 + fail_bar (hard bar, e.g. warm p95 above +10%)
  CANNOT-MEASURE  otherwise: the interval crosses the bar ("not established"),
                  or fewer pairs than required, or a sample is missing.
"""
from __future__ import annotations

import math
import random

PASS, FAIL, CANNOT = 'PASS', 'FAIL', 'CANNOT-MEASURE'
RESAMPLES = 4000
SEED = 20261106


def percentile(values, q):
    """Linear interpolation between closest ranks (numpy's default)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError('no samples')
    position = (len(ordered) - 1) * q / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def statistic(name):
    return {'p50': lambda v: percentile(v, 50), 'p95': lambda v: percentile(v, 95),
            'max': max, 'median': lambda v: percentile(v, 50)}[name]


def paired_ratio_interval(base, candidate, stat, *, resamples=RESAMPLES, seed=SEED):
    if len(base) != len(candidate):
        raise ValueError('paired samples need equal length')
    rng = random.Random(seed)
    n = len(base)
    ratios = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        denominator = stat([base[i] for i in picks])
        if denominator > 0:
            ratios.append(stat([candidate[i] for i in picks]) / denominator)
    return percentile(ratios, 2.5), percentile(ratios, 97.5)


def bar_verdict(base, candidate, *, stat='p95', pass_bar, fail_bar=None, min_pairs=30, seed=SEED):
    fail_bar = pass_bar if fail_bar is None else fail_bar
    record = {'stat': stat, 'pass_bar': pass_bar, 'fail_bar': fail_bar, 'pairs': len(base),
              'min_pairs': min_pairs}
    if len(base) != len(candidate) or len(base) < min_pairs or any(
            v is None or v < 0 for v in list(base) + list(candidate)):
        return {**record, 'verdict': CANNOT, 'why': 'missing or too few paired samples'}
    fn = statistic(stat)
    if fn(base) <= 0:
        return {**record, 'verdict': CANNOT, 'why': 'BASE statistic is zero'}
    point = fn(candidate) / fn(base)
    lo, hi = paired_ratio_interval(base, candidate, fn, seed=seed)
    record.update({'base': fn(base), 'candidate': fn(candidate), 'ratio': point,
                   'interval_95': [lo, hi]})
    if hi <= 1 + pass_bar:
        verdict, why = PASS, 'whole interval within the bar'
    elif lo > 1 + pass_bar:
        verdict, why = FAIL, 'whole interval above the bar: regression established'
    elif point > 1 + fail_bar:
        verdict, why = FAIL, 'point estimate above the hard fail bar'
    else:
        verdict, why = CANNOT, 'interval crosses the bar: not established'
    return {**record, 'verdict': verdict, 'why': why}
