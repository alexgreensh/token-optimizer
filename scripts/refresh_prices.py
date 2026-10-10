#!/usr/bin/env python3
"""Rebuild the bundled model price table from live sources.

Runs in CI (.github/workflows/refresh-prices.yml), never on a user's machine:
the shipped plugin only READS the generated files, so Token Optimizer keeps its
"no runtime network calls" promise while prices stay current without anyone
hand-editing a table.

Sources:
  - Claude: Anthropic's official pricing page (markdown), authoritative. The
    LiteLLM feed fills in when the page cannot be read.
  - OpenAI and Gemini: the LiteLLM community price feed, which tracks the
    providers' pages closely (verified against OpenAI's page on 2026-09-25).

Outputs (all generated, never edited by hand):
  skills/token-optimizer/pricing/prices.json   Python engines (measure.py, fleet.py)
  openclaw/src/prices.generated.ts              OpenClaw engine
  opencode/src/prices.generated.ts              OpenCode engine

Safety valve: a price that moves by more than 2x, or a model that disappears,
is not shipped automatically. Exit code 3 tells the workflow to open a PR for a
human instead of releasing.

Exit codes: 0 unchanged, 10 changed (safe to ship), 3 needs human review,
1 error (nothing written).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRICES_JSON = REPO / "skills" / "token-optimizer" / "pricing" / "prices.json"
TS_OUTPUTS = [REPO / "openclaw" / "src" / "prices.generated.ts",
              REPO / "opencode" / "src" / "prices.generated.ts"]

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
ANTHROPIC_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024

SCHEMA = 1
MAX_RATE = 1000.0          # $/MTok; anything above is a parse error, not a price
GUARD_RATIO = 2.0          # a move beyond 2x either way needs a human
OPENAI_LONG_CONTEXT_INPUT = 272_000
GEMINI_LONG_CONTEXT_INPUT = 200_000
ANTHROPIC_LONG_CONTEXT_INPUT = 100_000

# Family fallback cards, used only for ids with no parseable generation (e.g. a
# bare "opus" alias). Kept on the long-standing representative so an alias never
# silently changes price when a new generation ships.
CLAUDE_FAMILY_REPRESENTATIVE = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "sonnet_legacy": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
# Retired Claude models no source lists any more, at their final published
# rates, so old transcripts still price correctly. A live source always wins.
RETIRED_CLAUDE_CARDS = {
    "opus_3": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75, "cache_write_1h": 30.0},
    "haiku_3": {"input": 0.25, "output": 1.25, "cache_read": 0.03, "cache_write": 0.3, "cache_write_1h": 0.5},
}
CLAUDE_ID_RE = re.compile(r"^claude-(fable|mythos|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?$")
OPENAI_EXCLUDE_RE = re.compile(
    r"audio|realtime|transcribe|tts|image|search|container|computer-use|^ft:|embedding|moderation|dall-e|whisper")
DATED_RE = re.compile(r"\d{4}-\d{2}-\d{2}|-\d{4}$")


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "token-optimizer-price-refresh"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https URLs)
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"{url} exceeded {MAX_DOWNLOAD_BYTES} bytes")
    return data


def _per_mtok(value) -> float | None:
    """LiteLLM stores USD per token; the tables use USD per million tokens."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value * 1_000_000, 6)


def _card(entry: dict, suffix: str = "") -> dict | None:
    rates = {
        "input": _per_mtok(entry.get(f"input_cost_per_token{suffix}")),
        "output": _per_mtok(entry.get(f"output_cost_per_token{suffix}")),
        "cache_read": _per_mtok(entry.get(f"cache_read_input_token_cost{suffix}")),
        "cache_write": _per_mtok(entry.get(f"cache_creation_input_token_cost{suffix}")),
    }
    if rates["input"] is None or rates["output"] is None:
        return None
    return {k: v for k, v in rates.items() if v is not None}


def _claude_card_key(model_id: str) -> str | None:
    m = CLAUDE_ID_RE.match(model_id)
    if not m:
        return None
    family, major, minor = m.groups()
    return f"{family}_{major}_{minor}" if minor else f"{family}_{major}"


def claude_from_litellm(feed: dict) -> dict:
    cards = {}
    for model_id, entry in feed.items():
        if not isinstance(entry, dict) or entry.get("litellm_provider") != "anthropic":
            continue
        key = _claude_card_key(model_id)
        card = _card(entry) if key else None
        if not card:
            continue
        one_hour = _per_mtok(entry.get("cache_creation_input_token_cost_above_1hr"))
        card["cache_write_1h"] = one_hour if one_hour is not None else round(card["input"] * 2, 6)
        card.setdefault("cache_read", round(card["input"] * 0.1, 6))
        card.setdefault("cache_write", round(card["input"] * 1.25, 6))
        cards[key] = card
    return cards


_ANTHROPIC_ROW_RE = re.compile(r"^\|\s*Claude ([A-Za-z]+) (\d+)(?:\.(\d+))?\b[^|]*\|(.*)\|\s*$")
_ANTHROPIC_LC_ROW_RE = re.compile(
    r"^\|\s*Claude ([A-Za-z]+) (\d+)(?:\.(\d+))?[^|]*?\(for prompts over 100,000 tokens\)\s*\|(.*)\|\s*$")
_DOLLAR_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")


def claude_from_official(markdown: str) -> dict:
    """Parse the model table: input | 5m write | 1h write | cache read | output."""
    cards = {}
    for line in markdown.splitlines():
        if "(for prompts over" in line:
            continue  # long-context rows are parsed by claude_long_context_from_official
        m = _ANTHROPIC_ROW_RE.match(line.strip())
        if not m:
            continue
        family, major, minor, rest = m.groups()
        cells = [c for c in rest.split("|")]
        prices = []
        for cell in cells:
            found = _DOLLAR_RE.search(cell)
            prices.append(float(found.group(1)) if found else None)
        if len(prices) != 5 or any(p is None for p in prices):
            continue  # fast-mode and other side tables have a different shape
        key = f"{family.lower()}_{major}_{minor}" if minor else f"{family.lower()}_{major}"
        if key in cards:
            continue  # first table wins
        cards[key] = {"input": prices[0], "cache_write": prices[1], "cache_write_1h": prices[2],
                      "cache_read": prices[3], "output": prices[4]}
    return cards


def claude_long_context_from_official(markdown: str) -> dict:
    """Parse the "(for prompts over 100,000 tokens)" model-table rows.

    Anthropic lists long-context pricing as sibling rows in the same table
    (e.g. Claude Haiku 5.5 is 5x on every rate over 100K prompt tokens).
    The batch-pricing table repeats the labels but has a different cell
    shape, so the 5-price check already filters it out.
    """
    cards = {}
    for line in markdown.splitlines():
        m = _ANTHROPIC_LC_ROW_RE.match(line.strip())
        if not m:
            continue
        family, major, minor, rest = m.groups()
        cells = [c for c in rest.split("|")]
        prices = []
        for cell in cells:
            found = _DOLLAR_RE.search(cell)
            prices.append(float(found.group(1)) if found else None)
        if len(prices) != 5 or any(p is None for p in prices):
            continue
        key = f"{family.lower()}_{major}_{minor}" if minor else f"{family.lower()}_{major}"
        if key in cards:
            continue
        cards[key] = {"input": prices[0], "cache_write": prices[1], "cache_write_1h": prices[2],
                      "cache_read": prices[3], "output": prices[4]}
    return cards


def claude_long_context_from_litellm(feed: dict) -> dict:
    """Anthropic above-100K cards from the LiteLLM feed's *_above_100k_tokens fields."""
    suffix = f"_above_{ANTHROPIC_LONG_CONTEXT_INPUT // 1000}k_tokens"
    cards = {}
    for model_id, entry in feed.items():
        if not isinstance(entry, dict) or entry.get("litellm_provider") != "anthropic":
            continue
        key = _claude_card_key(model_id)
        if not key:
            continue
        card = _card(entry, suffix)
        if not card:
            continue
        one_hour = _per_mtok(entry.get(f"cache_creation_input_token_cost_above_1hr{suffix}"))
        card["cache_write_1h"] = one_hour if one_hour is not None else round(card["input"] * 2, 6)
        card.setdefault("cache_read", round(card["input"] * 0.1, 6))
        card.setdefault("cache_write", round(card["input"] * 1.25, 6))
        cards[key] = card
    return cards


def openai_from_litellm(feed: dict):
    base, long_ctx = {}, {}
    for model_id, entry in feed.items():
        if not isinstance(entry, dict) or entry.get("litellm_provider") != "openai":
            continue
        if entry.get("mode") not in ("chat", "responses"):
            continue
        if not re.match(r"^(gpt-\d|o\d)", model_id) or OPENAI_EXCLUDE_RE.search(model_id) or DATED_RE.search(model_id):
            continue
        card = _card(entry)
        if not card:
            continue
        card.setdefault("cache_read", card["input"])  # no cache discount listed: billed at input
        base[model_id] = card
        lc = _card(entry, f"_above_{OPENAI_LONG_CONTEXT_INPUT // 1000}k_tokens")
        if lc:
            lc.setdefault("cache_read", lc["input"])
            long_ctx[model_id] = lc
    return base, long_ctx


def gemini_from_litellm(feed: dict):
    base, long_ctx = {}, {}
    for model_id, entry in feed.items():
        if not isinstance(entry, dict) or entry.get("litellm_provider") != "gemini":
            continue
        if entry.get("mode") != "chat":
            continue
        name = model_id.split("/", 1)[1] if model_id.startswith("gemini/") else model_id
        if not name.startswith("gemini-") or DATED_RE.search(name) or re.search(r"image|tts|live|embedding|exp", name):
            continue
        card = _card(entry)
        if not card:
            continue
        card.pop("cache_write", None)  # implicit caching: no write charge
        card.setdefault("cache_read", round(card["input"] * 0.1, 6))
        base[name] = card
        lc = _card(entry, f"_above_{GEMINI_LONG_CONTEXT_INPUT // 1000}k_tokens")
        if lc:
            lc.pop("cache_write", None)
            lc.setdefault("cache_read", round(lc["input"] * 0.1, 6))
            long_ctx[name] = lc
    return base, long_ctx


def build(feed: dict, official_md: str | None) -> tuple[dict, list[str]]:
    notes = []
    claude = claude_from_litellm(feed)
    claude_lc = claude_long_context_from_litellm(feed)
    if official_md:
        official = claude_from_official(official_md)
        if len(official) < 5:
            notes.append("Anthropic page parsed fewer than 5 models; kept the LiteLLM Claude rates.")
        else:
            for key, card in official.items():
                prior = claude.get(key)
                if prior and any(abs(prior.get(f, 0) - card[f]) > 1e-9 for f in card):
                    notes.append(f"Claude {key}: official page overrides LiteLLM {prior} -> {card}")
                claude[key] = card
            official_lc = claude_long_context_from_official(official_md)
            for key, card in official_lc.items():
                prior = claude_lc.get(key)
                if prior and any(abs(prior.get(f, 0) - card[f]) > 1e-9 for f in card):
                    notes.append(f"Claude {key} long-context: official page overrides LiteLLM {prior} -> {card}")
                claude_lc[key] = card
    else:
        notes.append("Anthropic pricing page unavailable; Claude rates from LiteLLM only.")
    for key, card in RETIRED_CLAUDE_CARDS.items():
        claude.setdefault(key, dict(card))
    for family, rep in CLAUDE_FAMILY_REPRESENTATIVE.items():
        rep_key = _claude_card_key(rep)
        if rep_key in claude:
            claude[family] = dict(claude[rep_key])
    openai, openai_lc = openai_from_litellm(feed)
    gemini, gemini_lc = gemini_from_litellm(feed)
    doc = {
        "schema": SCHEMA,
        "units": "USD per million tokens",
        "sources": {"claude": ANTHROPIC_URL, "openai_gemini": LITELLM_URL},
        "thresholds": {"anthropic_long_context_input": ANTHROPIC_LONG_CONTEXT_INPUT,
                       "openai_long_context_input": OPENAI_LONG_CONTEXT_INPUT,
                       "gemini_long_context_input": GEMINI_LONG_CONTEXT_INPUT},
        "anthropic": dict(sorted(claude.items())),
        "anthropic_long_context": dict(sorted(claude_lc.items())),
        "openai": dict(sorted(openai.items())),
        "openai_long_context": dict(sorted(openai_lc.items())),
        "gemini": dict(sorted(gemini.items())),
        "gemini_long_context": dict(sorted(gemini_lc.items())),
    }
    notes += drop_bad_cards(doc)
    return doc, notes


def drop_bad_cards(doc: dict) -> list[str]:
    """Drop cards with an absurd rate instead of failing the whole refresh, so
    one bad upstream entry cannot block every other price update. A dropped
    card that was already shipped trips the removal alarm in guard()."""
    dropped = []
    for section in ("anthropic", "anthropic_long_context", "openai", "openai_long_context", "gemini", "gemini_long_context"):
        cards = doc.get(section) or {}
        for key in list(cards):
            bad = [f for f, v in cards[key].items()
                   if not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= MAX_RATE]
            if bad:
                dropped.append(f"dropped {section}.{key}: out-of-range {', '.join(bad)}")
                del cards[key]
    return dropped


def validate(doc: dict) -> list[str]:
    errors = []
    for section in ("anthropic", "openai", "gemini"):
        if len(doc.get(section) or {}) < 3:
            errors.append(f"section {section} has fewer than 3 models; refusing to ship a gutted table")
    for section in ("anthropic", "anthropic_long_context", "openai", "openai_long_context", "gemini", "gemini_long_context"):
        for key, card in (doc.get(section) or {}).items():
            if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", key):
                errors.append(f"{section}.{key}: unsafe key")
            for field in ("input", "output"):
                if field not in card:
                    errors.append(f"{section}.{key}: missing {field}")
            for field, value in card.items():
                if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= MAX_RATE:
                    errors.append(f"{section}.{key}.{field}={value!r} out of range")
    for family in CLAUDE_FAMILY_REPRESENTATIVE:
        if family not in doc["anthropic"]:
            errors.append(f"anthropic family card {family} missing")
    return errors


def guard(old: dict | None, new: dict) -> list[str]:
    """Changes a human must approve: a model or rate removed, a rate dropping to
    zero, or a rate moving beyond 2x. Long-context tables count too."""
    if not old:
        return []
    alarms = []
    for section in ("anthropic", "anthropic_long_context", "openai", "openai_long_context", "gemini", "gemini_long_context"):
        for key, old_card in (old.get(section) or {}).items():
            new_card = (new.get(section) or {}).get(key)
            if new_card is None:
                alarms.append(f"{section}.{key} disappeared")
                continue
            for field, before in old_card.items():
                after = new_card.get(field)
                if before <= 0:
                    continue
                # A dropped rate is re-derived by the loaders (cache_read = input/10),
                # and a zero usually means a feed glitch, so neither ships unreviewed.
                if after is None or after <= 0:
                    alarms.append(f"{section}.{key}.{field}: {before} -> {after}")
                    continue
                ratio = after / before
                if ratio > GUARD_RATIO or ratio < 1 / GUARD_RATIO:
                    alarms.append(f"{section}.{key}.{field}: {before} -> {after}")
    return alarms


def diff_lines(old: dict | None, new: dict) -> list[str]:
    lines = []
    for section in ("anthropic", "anthropic_long_context", "openai", "openai_long_context", "gemini", "gemini_long_context"):
        o, n = (old or {}).get(section) or {}, new.get(section) or {}
        for key in sorted(set(o) | set(n)):
            if o.get(key) != n.get(key):
                lines.append(f"{section}.{key}: {o.get(key)} -> {n.get(key)}")
    return lines


def _ts_key(section: str, key: str) -> str:
    return key.replace("_", "-") if section in ("anthropic", "anthropic_long_context") else key


def render_ts(doc: dict) -> str:
    def per_token(card: dict) -> str:
        parts = [f"input: {card['input']} / 1e6", f"output: {card['output']} / 1e6",
                 f"cacheRead: {card.get('cache_read', card['input'])} / 1e6",
                 f"cacheWrite: {card.get('cache_write', 0)} / 1e6"]
        if "cache_write_1h" in card:
            parts.append(f"cacheWrite1h: {card['cache_write_1h']} / 1e6")
        return "{ " + ", ".join(parts) + " }"

    def table(sections) -> str:
        rows = []
        for section in sections:
            for key, card in doc[section].items():
                rows.append(f"  {json.dumps(_ts_key(section, key))}: {per_token(card)},")
        return "\n".join(rows)

    return (
        "// GENERATED by scripts/refresh_prices.py from skills/token-optimizer/pricing/prices.json.\n"
        "// Do not edit by hand: the daily refresh-prices workflow rewrites this file.\n"
        "// USD per token. Claude generation cards use hyphens (\"opus-5-5\").\n\n"
        "import type { ModelPricing } from \"./pricing.js\";\n\n"
        "export const GENERATED_PRICING: Record<string, ModelPricing> = {\n"
        f"{table(('anthropic', 'openai', 'gemini'))}\n"
        "};\n\n"
        f"// Above {OPENAI_LONG_CONTEXT_INPUT:,} input tokens per request.\n"
        "export const GENERATED_OPENAI_LONG_CONTEXT_PRICING: Record<string, ModelPricing> = {\n"
        f"{table(('openai_long_context',))}\n"
        "};\n\n"
        f"// Above {GEMINI_LONG_CONTEXT_INPUT:,} input tokens per request.\n"
        "export const GENERATED_GEMINI_LONG_CONTEXT_PRICING: Record<string, ModelPricing> = {\n"
        f"{table(('gemini_long_context',))}\n"
        "};\n\n"
        f"// Above {ANTHROPIC_LONG_CONTEXT_INPUT:,} prompt tokens per request "
        "(input + cache reads + cache writes).\n"
        "export const GENERATED_ANTHROPIC_LONG_CONTEXT_PRICING: Record<string, ModelPricing> = {\n"
        f"{table(('anthropic_long_context',))}\n"
        "};\n"
    )


def write_outputs(doc: dict) -> None:
    PRICES_JSON.parent.mkdir(parents=True, exist_ok=True)
    PRICES_JSON.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    ts = render_ts(doc)
    for path in TS_OUTPUTS:
        path.write_text(ts, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--litellm-file", help="use a local LiteLLM JSON instead of downloading")
    ap.add_argument("--anthropic-file", help="use a local Anthropic pricing markdown")
    ap.add_argument("--summary", help="write a markdown change summary here")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    try:
        feed_raw = Path(args.litellm_file).read_bytes() if args.litellm_file else _fetch(LITELLM_URL)
        feed = json.loads(feed_raw)
        if not isinstance(feed, dict):
            raise ValueError("LiteLLM feed is not a JSON object")
    except Exception as exc:  # network, JSON, size
        print(f"ERROR: could not load the LiteLLM feed: {exc}", file=sys.stderr)
        return 1
    official_md = None
    try:
        official_md = (Path(args.anthropic_file).read_text(encoding="utf-8") if args.anthropic_file
                       else _fetch(ANTHROPIC_URL).decode("utf-8", "replace"))
    except Exception as exc:
        print(f"WARNING: Anthropic pricing page unavailable: {exc}", file=sys.stderr)

    doc, notes = build(feed, official_md)
    errors = validate(doc)
    if errors:
        print("ERROR: generated table failed validation; nothing written:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    try:
        old = json.loads(PRICES_JSON.read_text(encoding="utf-8")) if PRICES_JSON.exists() else None
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read the current {PRICES_JSON.name}: {exc}", file=sys.stderr)
        return 1
    changes = diff_lines(old, doc)
    alarms = guard(old, doc)
    doc["generated_at"] = (old or {}).get("generated_at") if not changes and old else \
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = ["# Model price refresh", ""]
    summary += [f"- {n}" for n in notes] or ["- sources read cleanly"]
    summary += ["", f"## {len(changes)} change(s)", ""] + [f"- {c}" for c in changes]
    if alarms:
        summary += ["", "## Needs review (moved beyond 2x or removed)", ""] + [f"- {a}" for a in alarms]
    text = "\n".join(summary) + "\n"
    print(text)
    if args.summary:
        Path(args.summary).write_text(text, encoding="utf-8")

    if not changes:
        return 0
    if not args.dry_run:
        write_outputs(doc)
    return 3 if alarms else 10


if __name__ == "__main__":
    sys.exit(main())
