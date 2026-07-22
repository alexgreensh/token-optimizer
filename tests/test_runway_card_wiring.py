"""The runway card is gated on `data.runway` in the dashboard template.

Two generators build a dashboard payload. The card was added to only one of them,
so the CLI wrote a page whose template contained the card and whose payload never
carried the key: the template's `if (rw && rw.available ...)` guard then rendered
nothing, which is indistinguishable from the feature being switched off. It shipped
that way and no test noticed, because every test asserted on the function that
computes the snapshot rather than on the payload the page actually receives.

These tests assert the wiring, not the arithmetic.
"""

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
ASSETS = ROOT / "skills" / "token-optimizer" / "assets"


def _assigned_dict_keys(func: ast.FunctionDef) -> set:
    """Every key of every dict literal assigned to a bare name `data` in `func`."""
    keys = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "data" for t in node.targets):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return keys


def _func(name: str) -> ast.FunctionDef:
    tree = ast.parse((SCRIPTS / "measure.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in measure.py")


def test_every_dashboard_generator_puts_runway_in_its_payload():
    for name in ("generate_dashboard", "generate_standalone_dashboard"):
        assert "runway" in _assigned_dict_keys(_func(name)), (
            f"{name} builds a dashboard payload without a 'runway' key, so the "
            f"card silently renders nothing in the page it produces"
        )


def test_template_reads_runway_from_the_payload():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    assert "data.runway" in html, "template no longer reads data.runway"


def test_runway_card_is_shared_by_savings_and_trends():
    """One definition, two call sites. Duplicated markup drifts."""
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    assert html.count("function runwayCardHtml(") == 1, "expected one definition"
    assert html.count("runwayCardHtml()") >= 3, (
        "expected the definition plus a call from renderSavings and renderTrends"
    )
    for fn in ("renderSavings", "renderTrends"):
        start = html.index("function " + fn + "(")
        body = html[start:start + 40000]
        assert "runwayCardHtml()" in body, f"{fn} does not render the runway card"


def test_runway_headline_carries_no_dark_glow():
    """`.metric-large` glows with --c-border-strong, which is dark in light mode."""
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("function runwayCardHtml(")
    body = html[start:start + 12000]
    headline = re.search(r"metric-large[^>]*style=\"([^\"]*)\"", body)
    assert headline, "runway headline no longer uses .metric-large"
    assert "text-shadow:none" in headline.group(1), (
        "runway headline must cancel the inherited text-shadow"
    )
