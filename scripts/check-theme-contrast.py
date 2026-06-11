#!/usr/bin/env python3
"""WCAG 2.1 AA contrast audit for the Token Optimizer dashboard themes.

This is the audit artifact for U9 (dashboard light mode, full platform parity).
It parses the CSS custom-property palette maps out of
``skills/token-optimizer/assets/dashboard.html`` -- the ``:root`` block (dark,
the DEFAULT) and the ``[data-theme="light"]`` block -- then computes the WCAG
relative-luminance contrast ratio for an explicit list of declared usage pairs
(text/background and UI-component pairs that actually render on the dashboard).

WCAG 2.1 AA thresholds:
  * 4.5:1 for normal body text
  * 3.0:1 for large text (>= 24px, or >= 19px bold) and UI components / graphics

Both themes must pass. Exits 1 on any failure and prints a table. Stdlib only,
so it runs unmodified in CI and from tests/test_dashboard_theme.py.

Usage:
    python3 scripts/check-theme-contrast.py [--dashboard PATH] [--quiet]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = (
    REPO_ROOT / "skills" / "token-optimizer" / "assets" / "dashboard.html"
)

# ---------------------------------------------------------------------------
# Color parsing + WCAG math (stdlib only)
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def parse_color(value: str) -> tuple[float, float, float, float]:
    """Parse #rgb / #rrggbb / #rrggbbaa / rgb()/rgba() into (r, g, b, a), 0..1."""
    v = value.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            r, g, b = (int(c * 2, 16) for c in h)
            return r / 255, g / 255, b / 255, 1.0
        if len(h) == 6:
            r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
            return r / 255, g / 255, b / 255, 1.0
        if len(h) == 8:
            r, g, b, a = (int(h[i : i + 2], 16) for i in (0, 2, 4, 6))
            return r / 255, g / 255, b / 255, a / 255
    m = re.fullmatch(r"rgba?\(([^)]+)\)", v)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", ",").split(",") if p.strip()]
        nums = []
        for i, p in enumerate(parts[:4]):
            if p.endswith("%"):
                nums.append(float(p[:-1]) / 100 * (255 if i < 3 else 1))
            else:
                nums.append(float(p))
        r, g, b = (n / 255 for n in nums[:3])
        a = nums[3] if len(nums) > 3 else 1.0
        return r, g, b, a
    raise ValueError(f"cannot parse color: {value!r}")


def _composite(fg: tuple, bg: tuple) -> tuple[float, float, float]:
    """Alpha-composite fg over an opaque bg. Returns opaque rgb (0..1)."""
    fr, fgc, fb, fa = fg
    br, bgc, bb, _ = bg
    return (
        _clamp01(fr * fa + br * (1 - fa)),
        _clamp01(fgc * fa + bgc * (1 - fa)),
        _clamp01(fb * fa + bb * (1 - fa)),
    )


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_value: str, bg_value: str, page_bg_value: str) -> float:
    """Contrast of fg over bg. Both fg and bg are composited over page_bg first
    so semi-transparent tokens (borders, soft fills) are evaluated honestly."""
    page = parse_color(page_bg_value)
    bg_opaque = _composite(parse_color(bg_value), page)
    fg_opaque = _composite(parse_color(fg_value), (*bg_opaque, 1.0))
    l1 = relative_luminance(fg_opaque)
    l2 = relative_luminance(bg_opaque)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# Palette extraction
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _resolve(name: str, raw: dict[str, str], seen=None) -> str:
    """Resolve a token to a literal color, following var(...) references and
    honoring var() fallbacks."""
    seen = seen or set()
    if name in seen:
        raise ValueError(f"var cycle at {name}")
    seen.add(name)
    val = raw.get(name, "").strip()
    m = re.fullmatch(r"var\((--[a-z0-9-]+)(?:,\s*([^)]+))?\)", val)
    if m:
        ref, fallback = m.group(1), m.group(2)
        if ref in raw:
            return _resolve(ref, raw, seen)
        if fallback:
            return fallback.strip()
        raise ValueError(f"unresolved var {ref}")
    return val


def extract_palette(html: str, selector: str) -> dict[str, str]:
    """Pull a CSS rule's custom properties into a resolved {token: literal} map."""
    # Find the selector where it actually opens a rule (selector + optional
    # whitespace + "{"), so a mention inside a CSS comment never matches.
    rule_re = re.compile(re.escape(selector) + r"\s*\{")
    m = rule_re.search(html)
    if not m:
        raise ValueError(f"selector not found: {selector}")
    brace = html.index("{", m.start())
    depth, end = 0, None
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = html[brace + 1 : end]
    raw = {m.group(1): m.group(2).strip() for m in _VAR_RE.finditer(body)}
    resolved: dict[str, str] = {}
    for k in raw:
        try:
            resolved[k] = _resolve(k, raw)
        except ValueError:
            resolved[k] = raw[k]  # keep non-color (fonts/spacing) verbatim
    return resolved


def merged_palette(html: str, selector: str) -> dict[str, str]:
    """Light theme inherits dark :root then overrides; return the effective map."""
    base = extract_palette(html, ":root")
    if selector == ":root":
        return base
    overrides = extract_palette(html, selector)
    merged = dict(base)
    merged.update(overrides)
    # Re-resolve overrides that referenced base vars.
    raw_all = dict(base)
    raw_all.update(overrides)
    for k in overrides:
        try:
            merged[k] = _resolve(k, raw_all)
        except ValueError:
            pass
    return merged


# ---------------------------------------------------------------------------
# Declared usage pairs (the contract this script audits).
# Each: (label, fg_token, bg_token, min_ratio). bg "page" is the body bg token.
# AA: 4.5 body text, 3.0 large text / UI components & graphics.
# ---------------------------------------------------------------------------

PAGE_BG = "--c-bg"

PAIRS = [
    # --- core text on backgrounds (body, 4.5) ---
    ("Body text on page bg", "--c-text-main", "--c-bg", 4.5),
    ("Body text on surface", "--c-text-main", "--c-surface", 4.5),
    ("Body text on surface-hover", "--c-text-main", "--c-surface-hover", 4.5),
    ("Dim text on page bg", "--c-text-dim", "--c-bg", 4.5),
    ("Dim text on surface", "--c-text-dim", "--c-surface", 4.5),
    ("Dim text on surface-hover", "--c-text-dim", "--c-surface-hover", 4.5),
    # --- status colors as text on surfaces (body, 4.5) ---
    ("Savings green on surface", "--c-savings", "--c-surface", 4.5),
    ("Savings green on page bg", "--c-savings", "--c-bg", 4.5),
    ("Waste red on surface", "--c-waste", "--c-surface", 4.5),
    ("Waste red on page bg", "--c-waste", "--c-bg", 4.5),
    ("Warning amber on surface", "--c-warning", "--c-surface", 4.5),
    ("Warning amber on page bg", "--c-warning", "--c-bg", 4.5),
    ("Info blue on surface", "--c-info", "--c-surface", 4.5),
    ("Info blue on page bg", "--c-info", "--c-bg", 4.5),
    # --- accent link/heading text (body, 4.5) ---
    ("Accent on surface", "--c-accent", "--c-surface", 4.5),
    ("Accent on page bg", "--c-accent", "--c-bg", 4.5),
    # --- keep-warm tile (U7) numbers + states (body, 4.5) ---
    ("Keep-warm NET positive", "--c-savings", "--c-surface", 4.5),
    ("Keep-warm NET negative", "--c-waste", "--c-surface", 4.5),
    ("Keep-warm demoted note", "--c-warning", "--c-surface", 4.5),
    # --- chart / legend secondary palette as graphics (UI, 3.0) ---
    ("Chart trend mid (teal)", "--c-chart-teal", "--c-surface", 3.0),
    ("Chart trend low (coral)", "--c-chart-coral", "--c-surface", 3.0),
    ("Model dot opus on surface", "--c-model-opus", "--c-surface", 3.0),
    ("Model dot haiku on surface", "--c-model-haiku", "--c-surface", 3.0),
    ("Model dot fable on surface", "--c-model-fable", "--c-surface", 3.0),
    ("Model dot other on surface", "--c-model-other", "--c-surface", 3.0),
    ("Cache bar output (blue)", "--c-cache-output", "--c-surface", 3.0),
    ("Cache bar read (green)", "--c-cache-read", "--c-surface", 3.0),
    ("Cache bar create (purple)", "--c-cache-create", "--c-surface", 3.0),
    # --- JS-injected badge SWATCHES: vivid fills painted from JS with black
    #     label text on top. They bypass the body-text status tokens, so audit
    #     them as their own pairs (black #000 over the swatch fill, body 4.5). ---
    ("Badge savings (black text)", "#000000", "--c-badge-savings", 4.5),
    ("Badge info (black text)", "#000000", "--c-badge-info", 4.5),
    ("Badge cyan (black text)", "#000000", "--c-badge-cyan", 4.5),
    ("Badge amber (black text)", "#000000", "--c-badge-amber", 4.5),
    # --- Codex model-mix cycle swatches (segment fills + legend dots, UI 3.0) ---
    ("Codex model cyan on surface", "--c-model-codex-cyan", "--c-surface", 3.0),
    ("Codex model amber on surface", "--c-model-codex-amber", "--c-surface", 3.0),
    ("Codex model pink on surface", "--c-model-codex-pink", "--c-surface", 3.0),
    # --- Display-title gradient endpoints clipped to text (large >=24px, 3.0).
    #     Audited against page bg AND surface so the title is legible wherever a
    #     header sits. ---
    ("Title grad start on page bg", "--c-title-grad-from", "--c-bg", 3.0),
    ("Title grad end on page bg", "--c-title-grad-to", "--c-bg", 3.0),
    ("Title grad start on surface", "--c-title-grad-from", "--c-surface", 3.0),
    ("Title grad end on surface", "--c-title-grad-to", "--c-surface", 3.0),
]

# Informational only (NOT gated). Hairline card/divider borders are decorative
# per WCAG 2.1 SC 1.4.11 (which targets controls/state needed to identify a UI
# component, not aesthetic separators). We still report their ratios for
# transparency so a reviewer can see they read as soft, intentional dividers.
INFO_PAIRS = [
    ("Border (strong) on page bg", "--c-border-strong", "--c-bg", 3.0),
    ("Border (strong) on surface", "--c-border-strong", "--c-surface", 3.0),
    ("Border (hairline) on surface", "--c-border", "--c-surface", 3.0),
    # The right config rail intentionally matches the page base in dark (its
    # separation comes from the 1px border + radial glow, not a fill contrast);
    # in light it lifts to near-white. Reported, not gated.
    ("Config rail vs page bg", "--c-config-rail", "--c-bg", 1.05),
]


def _is_literal(value: str) -> bool:
    """A pair endpoint given as a raw color literal rather than a token name."""
    return value.startswith("#") or value.startswith("rgb")


def _lookup(value: str, pal: dict[str, str]) -> str | None:
    """Resolve a pair endpoint: literal -> itself, token -> palette value."""
    if _is_literal(value):
        return value
    return pal.get(value)


def audit(html: str, theme: str) -> tuple[bool, list[tuple]]:
    selector = ":root" if theme == "dark" else '[data-theme="light"]'
    pal = merged_palette(html, selector)
    page_bg = pal[PAGE_BG]
    rows = []
    ok = True
    for label, fg, bg, minimum in PAIRS:
        fg_v, bg_v = _lookup(fg, pal), _lookup(bg, pal)
        if fg_v is None or bg_v is None:
            rows.append((label, fg, bg, None, minimum, "MISSING"))
            ok = False
            continue
        ratio = contrast_ratio(fg_v, bg_v, page_bg)
        passed = ratio >= minimum - 1e-9
        ok = ok and passed
        rows.append(
            (label, fg, bg, ratio, minimum, "PASS" if passed else "FAIL")
        )
    return ok, rows


def info_rows(html: str, theme: str) -> list[tuple]:
    selector = ":root" if theme == "dark" else '[data-theme="light"]'
    pal = merged_palette(html, selector)
    page_bg = pal[PAGE_BG]
    out = []
    for label, fg, bg, minimum in INFO_PAIRS:
        if fg in pal and bg in pal:
            out.append((label, contrast_ratio(pal[fg], pal[bg], page_bg), minimum))
    return out


# ---------------------------------------------------------------------------
# Structural audits (F21): catch theming bugs the per-pair table can't see.
# ---------------------------------------------------------------------------

# Every var(...) usage in the file: capture token name + optional fallback.
_VAR_USE_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*(?:,\s*([^)]+))?\)")


def undefined_var_refs(html: str) -> list[tuple[str, bool]]:
    """Return (token, has_fallback) for every var(--x) whose --x is defined in
    NEITHER :root NOR [data-theme="light"]. A reference with no definition and
    no fallback resolves to nothing (invisible text / transparent fill) -- the
    F7 bug class. Both kinds are reported; fallback-less ones FAIL the build."""
    defined: set[str] = set()
    for sel in (":root", '[data-theme="light"]'):
        try:
            defined |= set(extract_palette(html, sel).keys())
        except ValueError:
            pass
    seen: dict[str, bool] = {}
    for m in _VAR_USE_RE.finditer(html):
        tok, fallback = m.group(1), m.group(2)
        if tok in defined:
            continue
        # Record the strictest case: if ANY use lacks a fallback, flag it.
        has_fb = bool(fallback)
        if tok not in seen or (seen[tok] and not has_fb):
            seen[tok] = has_fb
    return sorted(seen.items())


# Gradient endpoints that aren't a var() token (raw hex/rgb literals baked into
# a linear-/radial-gradient). These bypass the theme layer entirely (F5/F6).
_GRADIENT_RE = re.compile(r"(?:linear|radial)-gradient\(([^;{}]*)\)")
_RAW_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")


def gradient_literal_endpoints(html: str) -> list[str]:
    """Raw color literals used directly inside gradient() calls (not via var()).
    Reported so a reviewer can confirm each is an intentional decorative graphic
    rather than a theme-blind text/icon color."""
    out: list[str] = []
    for gm in _GRADIENT_RE.finditer(html):
        body = gm.group(1)
        # Strip var(...) groups so their literal *fallbacks* aren't double-counted.
        stripped = _VAR_USE_RE.sub("", body)
        for cm in _RAW_COLOR_RE.finditer(stripped):
            out.append(cm.group(0))
    return out


def print_table(theme: str, rows: list[tuple], info: list[tuple]) -> None:
    print(f"\n=== {theme.upper()} theme ===")
    print(f"{'pair':<34}{'ratio':>8}  {'min':>5}  result")
    print("-" * 60)
    for label, _fg, _bg, ratio, minimum, status in rows:
        rs = f"{ratio:6.2f}" if ratio is not None else "  n/a "
        print(f"{label:<34}{rs:>8}  {minimum:>5.1f}  {status}")
    if info:
        print(f"{'-- decorative (informational, not gated) --':<60}")
        for label, ratio, minimum in info:
            print(f"{label:<34}{ratio:6.2f}  {minimum:>5.1f}  info")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    html = args.dashboard.read_text(encoding="utf-8")

    all_ok = True
    for theme in ("dark", "light"):
        ok, rows = audit(html, theme)
        all_ok = all_ok and ok
        if not args.quiet:
            print_table(theme, rows, info_rows(html, theme))

    # F21c: any var(--x) with no definition AND no fallback is a hard failure.
    undefined = undefined_var_refs(html)
    fatal = [tok for tok, has_fb in undefined if not has_fb]
    if undefined and not args.quiet:
        print("\n=== UNDEFINED CSS VARIABLE REFERENCES ===")
        for tok, has_fb in undefined:
            kind = "has fallback (degrades)" if has_fb else "NO fallback (FAIL)"
            print(f"  {tok:<28} {kind}")
    if fatal:
        all_ok = False

    # F21b: gradient endpoints baked as raw literals (theme-blind). Informational.
    grad_lits = gradient_literal_endpoints(html)
    if grad_lits and not args.quiet:
        print("\n=== gradient literal endpoints (decorative, not gated) ===")
        for c in sorted(set(grad_lits)):
            print(f"  {c}")

    if not args.quiet:
        print()
        print("ALL CHECKS PASS" if all_ok else "THEME AUDIT FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
