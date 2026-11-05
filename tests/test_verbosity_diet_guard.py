#!/usr/bin/env python3
"""Guard test: no context-injected [Token Optimizer] string may exceed its token budget.

This test prevents the verbosity diet from regressing. The brief's headline
failure was a ~95-token UserPromptSubmit message telling the user they are
using too many tokens. The fix cut it to ~30 tokens. This test ensures no
MODEL_EVERY_TURN or SESSION_START string can silently grow back past its budget.

Budgets (chars at ~4 chars/token):
  MODEL_EVERY_TURN: 160 chars (40 tokens). These enter the cached prefix and
    are re-billed every turn for the rest of the session. The fresh-session
    nudge, quality nudge, loop warnings, fill/tool-call warnings, and
    continuity hints all live here. 40 tokens is generous for "number + action"
    but tight enough to catch the reassurance-paragraph regression.
  SESSION_START: 240 chars (60 tokens). Billed once per session, not every
    turn, so the budget is looser. The dashboard-daemon install notice is
    the longest; 60 tokens accommodates it while still
    catching the 253-char pre-diet dashboard message.

Terminal-only strings (stderr, CLI prints) are NOT checked here: they cost
zero model tokens. The brief explicitly says not to spend effort there at
the expense of category 1.

The test scans measure.py via AST to find every [Token Optimizer] string
literal that is returned from a MODEL_EVERY_TURN function or printed from
a SESSION_START function, then checks each against its budget. It also
checks the threshold constant lists (_FILL_WARN_THRESHOLDS,
_TOOL_CALL_WARN_THRESHOLDS) since those messages are injected via
systemMessage.

Run: pytest tests/test_verbosity_diet_guard.py -v
"""
import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

MODEL_EVERY_TURN_BUDGET_CHARS = 160  # ~40 tokens
SESSION_START_BUDGET_CHARS = 240     # ~60 tokens

# Functions whose return values or print() output reach the model's context
# via UserPromptSubmit systemMessage or additionalContext JSON.
MODEL_EVERY_TURN_FUNCS = {
    "_maybe_fresh_session_nudge",
    "_maybe_nudge",
    "_maybe_loop_warning",
    "_continuity_prompt_hint",
    "run_verbosity_steer",
    "quality_cache",
}

# Functions whose print() output reaches the model's context via SessionStart
# additionalContext JSON (captured by sessionstart_runner.py and wrapped).
SESSION_START_FUNCS = {
    "compact_restore",
    "run_ensure_health",
    "_print_intel_digest",
    "build_lean_resume_context",
}

# The verbosity steer nudges are data-tested phrasing (the comment at the call
# site says "used verbatim because it produced the largest reduction"). They
# are intentionally longer. Exempt them from the MODEL_EVERY_TURN budget but
# still cap at a higher ceiling so they cannot grow unbounded.
VERBOSITY_STEER_EXEMPT = {"run_verbosity_steer"}
VERBOSITY_STEER_CEILING = 400  # ~100 tokens


def _reconstruct_string(node):
    """Reconstruct a string/fstring literal with placeholder names visible."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                try:
                    expr = ast.unparse(v.value)
                except Exception:
                    expr = "..."
                parts.append(f"{{{expr}}}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _reconstruct_string(node.left)
        right = _reconstruct_string(node.right)
        if left is not None and right is not None:
            return left + right
        return left or right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # %-formatted string: reconstruct the template (left side),
        # ignore the format args (right side). Placeholders appear as %s etc.
        left = _reconstruct_string(node.left)
        if left is not None:
            return left
    return None


def _enclosing_func(node, parents):
    for p in reversed(parents):
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return p.name
    return None


class _StringCollector(ast.NodeVisitor):
    """Collect [Token Optimizer] strings with their injection category."""

    def __init__(self):
        self.parents = []
        self.results = []  # (line, text, category, func)

    def visit(self, node):
        self.parents.append(node)
        super().visit(node)
        self.parents.pop()

    def _check_node(self, node, text, func):
        if not text or "[Token Optimizer]" not in text:
            return
        if func in MODEL_EVERY_TURN_FUNCS:
            cat = "MODEL_EVERY_TURN"
        elif func in SESSION_START_FUNCS:
            cat = "SESSION_START"
        else:
            return  # TERMINAL_ONLY, skip
        self.results.append((node.lineno, text, cat, func or ""))

    def visit_Return(self, node):
        if node.value is not None:
            text = _reconstruct_string(node.value)
            if text:
                func = _enclosing_func(node, self.parents)
                self._check_node(node, text, func)
        self.generic_visit(node)

    def visit_Call(self, node):
        # print() calls inside SESSION_START/MODEL_EVERY_TURN functions
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            func = _enclosing_func(node, self.parents)
            for arg in node.args:
                text = _reconstruct_string(arg)
                if text:
                    self._check_node(node, text, func)
        # .append() to a list that becomes a systemMessage
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and "system_messages" in node.func.value.id):
            func = _enclosing_func(node, self.parents)
            for arg in node.args:
                text = _reconstruct_string(arg)
                if text:
                    self._check_node(node, text, func)
        self.generic_visit(node)

    def visit_Assign(self, node):
        # Check threshold constant lists: _FILL_WARN_THRESHOLDS,
        # _TOOL_CALL_WARN_THRESHOLDS. These are lists of tuples where the
        # third element is the message string, injected via systemMessage.
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in (
                "_FILL_WARN_THRESHOLDS", "_TOOL_CALL_WARN_THRESHOLDS"
            ):
                if isinstance(node.value, ast.List):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Tuple) and len(elt.elts) >= 3:
                            text = _reconstruct_string(elt.elts[2])
                            if text and "[Token Optimizer]" not in text:
                                # The threshold messages don't have the prefix
                                # but are injected as "[Token Optimizer] {level}: {message}"
                                # so we measure the full injected form.
                                text = f"[Token Optimizer] LEVEL: {text}"
                            if text:
                                self.results.append(
                                    (node.lineno, text, "MODEL_EVERY_TURN",
                                     target.id))
        self.generic_visit(node)


def _collect_strings():
    """Parse measure.py and return all context-injected [Token Optimizer] strings."""
    src = MEASURE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    collector = _StringCollector()
    collector.visit(tree)
    # Deduplicate by (line, text)
    seen = set()
    distinct = []
    for line, text, cat, func in collector.results:
        key = (line, text)
        if key not in seen:
            seen.add(key)
            distinct.append((line, text, cat, func))
    return distinct


@pytest.fixture(scope="module")
def collected_strings():
    return _collect_strings()


def test_model_every_turn_strings_within_budget(collected_strings):
    """Every MODEL_EVERY_TURN string must be <= 160 chars (~40 tokens).

    These enter the cached prefix and are re-billed every turn for the rest
    of the session. A 95-token "you are using too many tokens" message was
    the headline failure this guard prevents from regressing.
    """
    violations = []
    for line, text, cat, func in collected_strings:
        if cat != "MODEL_EVERY_TURN":
            continue
        if func in VERBOSITY_STEER_EXEMPT:
            # Data-tested phrasing, higher ceiling
            if len(text) > VERBOSITY_STEER_CEILING:
                violations.append(
                    f"  L{line} ({func}): {len(text)} chars > "
                    f"{VERBOSITY_STEER_EXEMPT} ceiling {VERBOSITY_STEER_CEILING}"
                    f"\n    {text[:100]}..."
                )
            continue
        if len(text) > MODEL_EVERY_TURN_BUDGET_CHARS:
            violations.append(
                f"  L{line} ({func}): {len(text)} chars > budget "
                f"{MODEL_EVERY_TURN_BUDGET_CHARS}"
                f"\n    {text[:100]}..."
            )
    assert not violations, (
        f"{len(violations)} MODEL_EVERY_TURN string(s) exceed "
        f"{MODEL_EVERY_TURN_BUDGET_CHARS} chars (~40 tokens). These are "
        f"re-billed every turn:\n" + "\n".join(violations)
    )


def test_session_start_strings_within_budget(collected_strings):
    """Every SESSION_START string must be <= 240 chars (~60 tokens).

    Billed once per session via additionalContext, not every turn, so the
    budget is looser than MODEL_EVERY_TURN.
    """
    violations = []
    for line, text, cat, func in collected_strings:
        if cat != "SESSION_START":
            continue
        if len(text) > SESSION_START_BUDGET_CHARS:
            violations.append(
                f"  L{line} ({func}): {len(text)} chars > budget "
                f"{SESSION_START_BUDGET_CHARS}"
                f"\n    {text[:100]}..."
            )
    assert not violations, (
        f"{len(violations)} SESSION_START string(s) exceed "
        f"{SESSION_START_BUDGET_CHARS} chars (~60 tokens):\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize("breakeven", [1, 10, 100])
@pytest.mark.parametrize("context_tokens", [600_000, 999_000])
def test_fresh_session_nudge_is_diet_compliant(monkeypatch, breakeven, context_tokens):
    """The headline failure: the fresh-session nudge must stay lean.

    Before the diet it was ~95 tokens (380 chars). After, it must be under
    40 tokens (160 chars). This is the single most embarrassing string to
    regress because it tells the user they are using too many tokens while
    itself using too many tokens.
    """
    import importlib
    sys.path.insert(0, str(MEASURE.parent))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    measure = importlib.import_module("measure")

    # Build a fake result that triggers the nudge
    result = {
        "score": 49,
        "fill_pct": context_tokens / 10_000,
        "model_context_window": 1_000_000,
        "_fresh_nudge_fired": False,
        "_nudge_previous_score": 80,
    }
    # Bypass the feature flag and once-per-session gate
    monkeypatch.setattr(measure, "_is_v5_feature_enabled", lambda f: True)
    monkeypatch.setattr(measure, "_ran_once_this_session", lambda *a: False)
    monkeypatch.setattr(measure, "_log_compression_event", lambda **kw: None)
    monkeypatch.setattr(measure, "_fresh_session_turn_economics", lambda *a: {
        "per_turn_usd": 0.12, "one_time_usd": 0.12 * breakeven,
        "breakeven_turns": breakeven,
    })
    nudge = measure._maybe_fresh_session_nudge(result, None, {
        "context_tokens": context_tokens, "model_context_window": 1_000_000,
    })
    assert nudge is not None
    assert "Checkpoint saved" not in nudge
    assert f"~{breakeven} turn" in nudge
    if nudge:
        assert len(nudge) <= MODEL_EVERY_TURN_BUDGET_CHARS, (
            f"Fresh-session nudge is {len(nudge)} chars "
            f"({len(nudge)//4} tokens), must be <= "
            f"{MODEL_EVERY_TURN_BUDGET_CHARS} chars (~40 tokens). "
            f"This is the headline failure string:\n  {nudge}"
        )
        # Must keep the action verbatim
        assert "continue this" in nudge, (
            "The 'continue this' action must survive the diet"
        )
    # Clean up
    if "measure" in sys.modules:
        del sys.modules["measure"]


def test_no_warning_dropped_entirely(collected_strings):
    """The diet must never drop a warning entirely to save tokens.

    Checks that the key warning types still produce output:
    - Fresh session nudge (long + degraded)
    - Quality nudge (score drop)
    - Loop warnings (message_loop, retry_churn)
    - Fill warnings (CRITICAL, WARNING)
    - Tool call warnings (CRITICAL, WARNING)
    - Quality CLI warnings (critical, stale)
    """
    texts = " ".join(text for _, text, _, _ in collected_strings)
    # Each warning category must still have at least one string
    required_fragments = [
        ("fresh session", "Fresh session"),
        ("quality nudge", "Quality"),
        ("loop warning", "Loop"),
        ("retry loop", "Retry loop"),
        ("fill warning", "context fill"),
        ("tool call warning", "tool calls"),
    ]
    missing = []
    for label, fragment in required_fragments:
        if fragment.lower() not in texts.lower():
            missing.append(f"  {label}: missing '{fragment}'")
    assert not missing, (
        "Warnings dropped entirely to save tokens (the brief forbids this):\n"
        + "\n".join(missing)
    )
