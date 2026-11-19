"""Model-routing savings realized/potential calculation.

Split out of the pricing provider: the frozen facade symbol
_compute_model_routing_savings delegates through routing_credit.compute,
which resolves this module to avoid a facade-binding recursion.
"""
from . import _compat


def unrestricted(days=None, since=None, *, until=None):
    """Model-routing savings: realized (mix shift vs baseline) + potential (remaining Opus).

    REALIZED: compare current model mix against the install-era baseline (snapshot
    model_mix, else the earliest model_daily window). If the user moved tokens off
    expensive models, the counterfactual cost (current volume spread over the
    baseline mix) minus the current cost, both on one standardized price
    schedule, is the estimated saving. This is the pillar that can show a
    realized win (e.g. Opus 94% -> 67%).
    Models priced above the baseline's costliest model (a premium model the user
    chose) are left out of the comparison and the rest renormalized, so they do not
    cancel a real shift toward lighter models; `premium_share` and
    `premium_cost_usd` report them separately.

    POTENTIAL: an assumption, not a measurement. A fixed share
    (`routable_fraction`) of Opus fresh input and output is priced at the
    Opus-to-Sonnet API rate delta. Opus usage without a class split is left out
    and counted in `potential_unsplit_excluded_tokens`. Cache reads and
    writes represent main-session history: switching models rebuilds that cache,
    while routine subagent work carries its own smaller context. The result is
    shown separately from concrete opportunities.

    Realized routing prices fresh input, cache creation, cache reads and output
    by class. Usage without a class split stays outside both comparisons and
    their coverage, and is reported as `unsplit_tokens`. Potential uses the model's
    own generation card for fresh input and output, and the newest Sonnet card
    for its target.
    The class split comes from per-day, per-model session usage.

    Realized routing is a standardized family comparison: the remaining work's
    class volumes per current family, and the same class volumes spread over the
    baseline family shares, are valued on one fixed schedule (the newest card of
    each family's tier) chosen independently of the window's volumes. A
    generation change inside a family therefore cannot create routing credit.
    The exact-card cost of the same work and its gap to the standardized value
    are reported separately (`actual_cost_usd`, `generation_price_delta_usd`).
    The schedule is assumed because the frozen baseline records no generations,
    so the result is labelled "estimated".

    Models with no exact rate card are unpriced, not free (an unknown Claude
    generation does not borrow its family's price): they are left out of the
    comparison and reported as `unpriced_models`, `unpriced_tokens` and
    `unpriced_share`. Explicitly free tiers (":free") are left out the same way
    and reported as `free_models`.
    Labelled estimated (realized) and opportunity (potential). Never raises.
    """
    # `zero` is returned only on no-data / error paths (no current mix, no
    # baseline, or an exception). Label it "unavailable" so a $0 here reads as
    # "couldn't compute" rather than a measured zero -- the success return below
    # sets its own evidence="estimated" when comparable work remains.
    _measure_facade = _compat.facade()
    zero = {
        "realized_cost_usd": 0.0, "potential_cost_usd": 0.0,
        "potential_tokens": 0, "potential_tokens_monthly": 0,
        "potential_billable_tokens": 0, "potential_by_class": {}, "unsplit_tokens": 0,
        "potential_unsplit_excluded_tokens": 0,
        "premium_models": [], "premium_share": 0.0, "premium_cost_usd": 0.0,
        "unpriced_models": [], "unpriced_tokens": 0, "unpriced_share": 0.0,
        "free_models": [], "free_tokens": 0,
        "compared_share": 0.0,
        "baseline_rate_basis": {}, "baseline_reference_priced": [],
        "standardized_baseline_cost_usd": 0.0, "standardized_actual_cost_usd": 0.0,
        "actual_cost_usd": 0.0, "generation_price_delta_usd": 0.0,
        "baseline_opus_share": 0.0, "current_opus_share": 0.0,
        "measured_baseline_opus_share": None,
        "measured_baseline_realized_cost_usd": None,
        "measured_baseline_source": None,
        "measured_baseline_first_date": None,
        "baseline_source": None, "evidence": "unavailable",
    }
    try:
        window_end = {"until": until} if until else {}
        current = _measure_facade._model_mix_shares(days=days, since=since, **window_end)
        zero["activity_coverage"] = current.get("activity_coverage", {"complete": True, "undated_sessions": 0})
        cur_shares = current.get("shares", {})
        cur_total = int(current.get("total_tokens", 0) or 0)
        if not cur_shares or cur_total <= 0:
            return zero

        from . import routing_credit
        baseline_shares, baseline_source, baseline_stated, early_mix = routing_credit.baseline(_measure_facade)
        if not baseline_shares:
            return zero

        # While collect is still re-reading window sessions (an older usage
        # parser, or per-day usage not yet rebuilt), their per-class usage is
        # incomplete and the figure would move pass to pass. Report the count
        # instead of a number.
        pending = _measure_facade._routing_pending_sessions(days=days, since=since, **window_end)
        if pending:
            return {**zero, "evidence": "pending", "pending_sessions": pending,
                    "baseline_source": baseline_source}

        # Rows of per-model, per-class usage. When the DB holds none (adapters
        # that only record model totals), the model-daily mix stands in as
        # unsplit tokens, reported outside comparison coverage.
        usage = _measure_facade._routing_usage_by_model(days=days, since=since, **window_end)
        from_rows = bool(usage)
        if not from_rows:
            usage = {m: {"unsplit": share * cur_total} for m, share in cur_shares.items()}

        # Mix keys from the model-daily fallback are already family buckets.
        fam_of = {m: (_measure_facade._normalize_model_name(m) if from_rows else m) for m in usage}
        rates_cache = {}

        def _rates(model):
            _measure_facade = _compat.facade()
            if model not in rates_cache:
                rates_cache[model] = _measure_facade._routing_class_rates(model)
            return rates_cache[model]

        # Only a model with an exact rate card is compared. A model with no card
        # is unpriced (not free) and an explicitly free tier is not credited as a
        # saving: both keep their token counts, are reported, and stay out of
        # every mix and cost below.
        priced_usage = {}
        unpriced_by_model = {}
        free_by_model = {}
        unsplit_tokens = 0.0
        potential_unsplit_excluded = 0.0
        for m, classes in usage.items():
            if not fam_of[m]:
                continue
            if _rates(m) is not None:
                unsplit_tokens += classes.get("unsplit", 0)
                if fam_of[m] == "opus":
                    potential_unsplit_excluded += classes.get("unsplit", 0)
                split = {k: v for k, v in classes.items() if k != "unsplit"}
                if sum(split.values()) > 0:
                    priced_usage[m] = split
                continue
            tokens = sum(classes.get(k, 0) for k in _measure_facade._ROUTING_CLASSES)
            bucket = free_by_model if _measure_facade._routing_model_status(m) == "free" else unpriced_by_model
            bucket[m] = bucket.get(m, 0) + tokens

        fam_models = {}
        for m in priced_usage:
            fam_models.setdefault(fam_of[m], []).append(m)
        fam_classes = {}
        class_totals = {k: 0.0 for k in _measure_facade._ROUTING_CLASSES}
        for fam, ms in fam_models.items():
            dst = fam_classes[fam] = {k: 0.0 for k in _measure_facade._ROUTING_CLASSES}
            for m in ms:
                for k in _measure_facade._ROUTING_CLASSES:
                    dst[k] += priced_usage[m].get(k, 0)
                    class_totals[k] += priced_usage[m].get(k, 0)
        pooled = sum(class_totals.values())

        def _unpriced_report():
            _measure_facade = _compat.facade()
            return [mid for mid, _t in sorted(unpriced_by_model.items(), key=lambda kv: -kv[1])]

        def _free_report():
            _measure_facade = _compat.facade()
            return [mid for mid, _t in sorted(free_by_model.items(), key=lambda kv: -kv[1])]

        unpriced_tokens = int(sum(unpriced_by_model.values()))
        free_tokens = int(sum(free_by_model.values()))

        def _coverage_fields():
            _measure_facade = _compat.facade()
            total = unpriced_tokens + free_tokens + pooled + unsplit_tokens
            return {
                "unpriced_models": _unpriced_report(), "unpriced_tokens": unpriced_tokens,
                "unpriced_share": round(unpriced_tokens / total, 4) if total > 0 else 0.0,
                "free_models": _free_report(), "free_tokens": free_tokens,
                "unsplit_tokens": int(round(unsplit_tokens)),
                "potential_unsplit_excluded_tokens": int(round(potential_unsplit_excluded)),
                "baseline_source": baseline_source,
            }

        if pooled <= 0:
            return {**zero, **_coverage_fields()}

        # STANDARDIZED ROUTING VALUATION. Realized routing measures a shift of work
        # between model families, so both arms are valued on one fixed price
        # schedule: one rate per family and token class, chosen without looking at
        # how much of that family ran in the window. The frozen baseline records
        # shares by family only (no generation), so the schedule is a documented
        # reference-card scenario: each family at the newest generation card of
        # its tier (legacy Sonnet counts as Sonnet; a provider model stands for
        # itself). It is the same for present and absent families, so one token of
        # any generation changes the result only by that token's own contribution,
        # and a generation change within a family cannot become routing credit.
        # What the work actually cost at the exact cards used is reported
        # separately (`actual_cost_usd`, `generation_price_delta_usd`). Because the
        # scenario is assumed, the result is labelled estimated. A baseline that
        # recorded generations would replace the reference card in
        # `_fixed_family_rates` for the families it covers.
        fam_rate_cache = {}
        fam_rate_basis = {}

        def _fixed_family_rates(fam):
            _measure_facade = _compat.facade()
            if fam in fam_rate_cache:
                return fam_rate_cache[fam]
            ref_model = _measure_facade._routing_reference_model(fam)
            out = _rates(ref_model)
            if out is None and fam_models.get(fam):
                # No reference card for a family that did run: its lowest-sorted
                # model id stands in, so the choice never depends on input order.
                ref_model = sorted(fam_models[fam])[0]
                out = _rates(ref_model)
            if out is not None:
                fam_rate_basis[fam] = f"reference:{ref_model}"
            fam_rate_cache[fam] = out
            return out

        def _renormalized(shares):
            _measure_facade = _compat.facade()
            kept = {f: s for f, s in shares.items() if s and s > 0 and _fixed_family_rates(f) is not None}
            total = sum(kept.values())
            return {f: s / total for f, s in kept.items()} if total > 0 else {}

        base_priced = _renormalized(baseline_shares)

        # Effective $/MTok of a family under the window's own class mix.
        def _family_price(f):
            _measure_facade = _compat.facade()
            if pooled <= 0:
                return 0.0
            r = _fixed_family_rates(f)
            return sum(class_totals[k] * r[k] for k in _measure_facade._ROUTING_CLASSES) / pooled

        # Models priced above the most expensive model of the baseline are the
        # user's own choice, not routing: they are taken out of the comparison
        # so it measures the shift toward lighter models. Their share and cost
        # are reported separately. A model must be dearer than the baseline's
        # costliest family on the window's pooled mix AND on the output card:
        # cache-read rates alone do not rank tiers across generations (Sonnet 4.6
        # reads cache at $0.30, Opus 5.5 at $0.20), so a lighter older model is
        # never mistaken for a premium one.
        ceiling_fam = max(base_priced, key=_family_price, default=None)
        baseline_ceiling = _family_price(ceiling_fam) if ceiling_fam else 0.0
        ceiling_output = _fixed_family_rates(ceiling_fam)["output"] if ceiling_fam else 0.0
        premium = sorted(
            f for f in fam_classes
            if _family_price(f) > baseline_ceiling > 0 and _fixed_family_rates(f)["output"] > ceiling_output
        )
        premium_share = sum(cur_shares.get(f, 0.0) for f in premium)
        premium_cost = sum(
            classes.get(k, 0) * _rates(m)[k]
            for m, classes in priced_usage.items() if fam_of[m] in premium
            for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000

        # REALIZED: the remaining work's class volumes, per current family, valued
        # on the standardized schedule, against the same total class volumes spread
        # over the baseline family shares on that same schedule. Only the shift
        # between families moves the number. Never negative. Premium and unpriced
        # models are outside both arms.
        kept_models = [m for m in priced_usage if fam_of[m] not in premium]
        kept_totals = {k: sum(priced_usage[m].get(k, 0) for m in kept_models) for k in _measure_facade._ROUTING_CLASSES}
        standardized_actual = sum(
            priced_usage[m].get(k, 0) * _fixed_family_rates(fam_of[m])[k]
            for m in kept_models for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000
        standardized_baseline = sum(
            kept_totals[k] * sum(s * _fixed_family_rates(f)[k] for f, s in base_priced.items())
            for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000 if base_priced else 0.0
        # Exact-card API-equivalent cost of the same work (each model's own card).
        actual_cost = sum(
            priced_usage[m].get(k, 0) * _rates(m)[k] for m in kept_models for k in _measure_facade._ROUTING_CLASSES
        ) / 1_000_000
        realized_cost = max(0.0, standardized_baseline - standardized_actual) if base_priced and kept_models else 0.0

        def _realized_for_baseline(shares):
            base = _renormalized(shares or {})
            if not base or not kept_models:
                return 0.0
            baseline_cost = sum(
                kept_totals[k] * sum(s * _fixed_family_rates(f)[k] for f, s in base.items())
                for k in _measure_facade._ROUTING_CLASSES
            ) / 1_000_000
            return max(0.0, baseline_cost - standardized_actual)

        measured_baseline = {}
        compared_tokens = sum(kept_totals.values())
        window_tokens = unpriced_tokens + free_tokens + pooled + unsplit_tokens

        # POTENTIAL: a fixed share of remaining Opus fresh input and output, priced
        # at each Opus model's own card minus the current Sonnet card. It is
        # computed apart from the realized figure: with no target card the
        # potential is zero and the realized routing above is unaffected. Usage
        # without a class split cannot be told apart as input or output, so it is
        # left out and its count is reported.
        target = _measure_facade._routing_target_model()
        target_rates = _rates(target)
        by_class = {k: {"tokens": 0, "usd": 0.0} for k in _measure_facade._ROUTING_CLASSES}
        for m, classes in priced_usage.items():
            if fam_of[m] != "opus":
                continue
            if target_rates is None:
                continue
            m_rates = _rates(m)
            for k in ("fresh_input", "output"):
                tok = classes.get(k, 0)
                if tok:
                    by_class[k]["tokens"] += tok * _measure_facade._ROUTABLE_OPUS_FRACTION
                    by_class[k]["usd"] += tok * _measure_facade._ROUTABLE_OPUS_FRACTION * (m_rates[k] - target_rates[k]) / 1_000_000
        potential_cost = max(0.0, sum(v["usd"] for v in by_class.values()))
        potential_tokens = int(round(sum(v["tokens"] for v in by_class.values())))
        potential_tokens_monthly = int(round(potential_tokens / max(days, 1) * 30))
        potential_billable_tokens = int(round(sum(by_class[k]["tokens"] for k in _measure_facade._ROUTING_BILLABLE_CLASSES)))
        opus_share = cur_shares.get("opus", 0.0)

        return {
            "activity_coverage": zero["activity_coverage"],
            "realized_cost_usd": round(realized_cost, 4),
            "potential_cost_usd": round(potential_cost, 4),
            # Routable tokens cover observed fresh input and output only.
            "potential_tokens": potential_tokens,
            "potential_tokens_monthly": potential_tokens_monthly,
            "potential_billable_tokens": potential_billable_tokens,
            "potential_token_classes": [k for k in _measure_facade._ROUTING_CLASSES if by_class[k]["tokens"]],
            "potential_by_class": {
                k: {"tokens": int(round(v["tokens"])), "usd": round(v["usd"], 4)}
                for k, v in by_class.items() if v["tokens"] or v["usd"]
            },
            "potential_basis": "api_rate_equivalent",
            "target_model": target,
            "pricing_basis": "per_token_class_generation_card",
            # Premium models (priced above the baseline's costliest model) are the
            # user's own choice: excluded from realized routing, shown here.
            "premium_models": premium,
            "premium_share": round(premium_share, 4),
            "premium_cost_usd": round(premium_cost, 4),
            # Models with no exact rate card (unpriced) and explicitly free tiers are
            # left out of the comparison, never priced at zero or at a stand-in rate.
            **_coverage_fields(),
            "baseline_opus_share": round(baseline_shares.get("opus", 0.0), 4),
            "current_opus_share": round(opus_share, 4),
            "measured_baseline_opus_share": measured_baseline.get("opus_share"),
            "measured_baseline_realized_cost_usd": measured_baseline.get("realized_cost_usd"),
            "measured_baseline_source": measured_baseline.get("source"),
            "measured_baseline_first_date": measured_baseline.get("first_date"),
            "baseline_source": baseline_source, "baseline_stated": baseline_stated,
            "routable_fraction": _measure_facade._ROUTABLE_OPUS_FRACTION,
            # Realized routing is a standardized comparison on a fixed price
            # schedule (newest card per family) in both arms, because the baseline
            # records no generations. That schedule is an assumption, so the
            # result is an estimate. The exact-card cost of the same work and its
            # gap to the standardized valuation are reported apart: the gap is
            # price/generation, never routing.
            "realized_basis": "standardized_reference_card",
            "baseline_rate_basis": {f: fam_rate_basis[f] for f in sorted(set(base_priced) | set(fam_classes)) if f in fam_rate_basis},
            "baseline_reference_priced": sorted(base_priced),
            "standardized_baseline_cost_usd": round(standardized_baseline, 4),
            "standardized_actual_cost_usd": round(standardized_actual, 4),
            "actual_cost_usd": round(actual_cost, 4),
            "generation_price_delta_usd": round(actual_cost - standardized_actual, 4),
            # Share of the window's class tokens inside both arms (premium,
            # unpriced and free models, and unsplit tokens are outside).
            "compared_share": round(compared_tokens / window_tokens, 4) if window_tokens > 0 else 0.0,
            "evidence": "estimated" if compared_tokens > 0 and base_priced else "unavailable",
        }
    except Exception:
        return zero
