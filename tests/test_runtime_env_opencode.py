#!/usr/bin/env python3
"""Regression tests for OpenCode runtime detection in runtime_env (issue #57).

Two failure modes are covered:

1. Detection was too narrow — it matched only a bare ``opencode`` ancestor
   basename, so OpenCode launched through node/bun (its real launch shape) went
   undetected and the skill audited/mutated ~/.claude.
2. Detection was mis-ordered — a soft Claude plugin-env heuristic ran before the
   OpenCode signal, so a coexisting Claude Code install on the same host could
   shadow a genuine OpenCode session.

The central correctness risk is the inverse: a genuine Claude Code session whose
cwd (or a later CLI argument) merely mentions a repo named ``opencode`` must NOT
be flipped into OpenCode mode. That false-positive guard is tested explicitly.

Run directly:  python3 tests/test_runtime_env_opencode.py
Exits non-zero on first failure.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402

# Every env var detect_runtime() consults — cleared before each controlled case.
_CONTROLLED_ENV = (
    "TOKEN_OPTIMIZER_RUNTIME",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CODEX_HOME",
    "HERMES_HOME",
    "COPILOT_HOME",
    "TOKEN_OPTIMIZER_NO_PROC_SCAN",
    "OPENCODE_BIN",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CLIENT",
)


def _detect_with(env=None, process_opencode=False):
    """Resolve detect_runtime() under a controlled env + faked process signal.

    process_opencode stands in for the live ps-based ancestor scan so the tests
    are deterministic and never shell out.
    """
    env = env or {}
    saved = {k: os.environ.get(k) for k in _CONTROLLED_ENV}
    saved_proc = runtime_env._opencode_process_signal
    saved_copilot = runtime_env._copilot_signal
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        runtime_env._opencode_process_signal = lambda: process_opencode
        runtime_env._copilot_signal = lambda: False
        runtime_env.detect_runtime.cache_clear()
        return runtime_env.detect_runtime()
    finally:
        runtime_env._opencode_process_signal = saved_proc
        runtime_env._copilot_signal = saved_copilot
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        runtime_env.detect_runtime.cache_clear()


# --- Pure command-line classification (no subprocess) -----------------------

def test_cmd_bare_opencode_binary():
    assert runtime_env._is_opencode_command("/usr/local/bin/opencode")
    assert runtime_env._is_opencode_command("/usr/local/bin/opencode serve --port 0")


def test_cmd_node_launcher_named_entry():
    assert runtime_env._is_opencode_command("node /home/me/.opencode/bin/opencode.mjs")


def test_cmd_npm_package_install():
    assert runtime_env._is_opencode_command(
        "node /opt/app/node_modules/opencode-ai/dist/index.js"
    )


def test_cmd_launcher_flags_are_skipped():
    assert runtime_env._is_opencode_command(
        "node --enable-source-maps /home/me/.opencode/bin/opencode.mjs"
    )


def test_cmd_value_taking_flags_dont_hide_script():
    # Torture FN-E: --require/--loader/--import consume the NEXT token as a value;
    # scanning every token (not stopping at the first non-flag) still finds the
    # real opencode entry behind the flag value.
    assert runtime_env._is_opencode_command(
        "node --require source-map-support/register "
        "/app/node_modules/opencode-ai/dist/index.js"
    )
    assert runtime_env._is_opencode_command(
        "node --loader ts-node/esm /app/node_modules/opencode-ai/dist/index.js"
    )


def test_cmd_bun_run_subcommand():
    # Torture FN-A: `bun run <script>` — "run" must not short-circuit the scan.
    assert runtime_env._is_opencode_command(
        "bun run /app/node_modules/opencode-ai/dist/index.js"
    )


def test_cmd_pnpm_versioned_package_dir():
    # Torture FN-VERSIONED: pnpm stores deps as opencode-ai@<version>.
    assert runtime_env._is_opencode_command(
        "node /app/.pnpm/opencode-ai@0.3.1/node_modules/opencode-ai/dist/index.js"
    )


def test_cmd_source_checkout_not_detected_known_limit():
    # Documented trade-off (KTD-2): an uninstalled source checkout run via a
    # generic entry filename is NOT auto-detected. Dev runs use the override.
    # The point of accepting this False is that it KILLS the false-positive class
    # below (a Claude user's project named opencode with an index.js).
    assert not runtime_env._is_opencode_command(
        "bun /opt/opencode/packages/opencode/src/index.ts"
    )


def test_cmd_empty_is_false():
    assert not runtime_env._is_opencode_command("")
    assert not runtime_env._is_opencode_command("   ")


def test_cmd_claude_code_is_not_opencode():
    # Claude Code itself: node running @anthropic-ai/claude-code/cli.js.
    assert not runtime_env._is_opencode_command(
        "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
    )


def test_cmd_false_positive_repo_named_opencode():
    # FALSE-POSITIVE GUARD (KTD-2) — the central correctness risk. A genuine
    # Claude Code session must never be misdetected as OpenCode.
    # Torture F1 (CRITICAL): a user project literally named opencode with a stock
    # entry script. The OLD `"opencode" in parts` check flipped this to True.
    assert not runtime_env._is_opencode_command("node /home/me/opencode/index.js")
    assert not runtime_env._is_opencode_command("node /home/me/opencode/src/index.js")
    assert not runtime_env._is_opencode_command("bun /home/me/dev/opencode/main.ts")
    # A non-entry script under an opencode dir.
    assert not runtime_env._is_opencode_command(
        "node /Users/me/projects/opencode/build.js"
    )
    # Bare `opencode` as a directory ARGUMENT value (not the executable).
    assert not runtime_env._is_opencode_command(
        "node app.js --dir /home/me/opencode"
    )
    # opencode only as a later argument (e.g. a path passed to a tool).
    assert not runtime_env._is_opencode_command(
        "node /Users/me/dev/myapp/index.js --config /Users/me/opencode/cfg.json"
    )
    # A non-launcher executable opening a file under an opencode dir.
    assert not runtime_env._is_opencode_command("vim /Users/me/opencode/notes.md")
    # Claude Code's own real process tree must resolve to NOT-opencode.
    assert not runtime_env._is_opencode_command(
        "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
    )
    # A user project named exactly `opencode-ai` (the npm package name) run from
    # source — NOT under node_modules — must not match (residual-FP guard).
    assert not runtime_env._is_opencode_command(
        "bun /home/me/dev/opencode-ai/main.ts"
    )
    assert not runtime_env._is_opencode_command(
        "node /home/me/projects/opencode-ai/dist/index.js"
    )


def test_entrypoint_predicate_precision():
    looks = runtime_env._looks_like_opencode_entrypoint
    # Named entry scripts (carry an extension) and the npm package match.
    assert looks("/home/me/.opencode/bin/opencode.mjs")
    assert looks("/opt/app/node_modules/opencode-ai/dist/index.js")
    assert looks("/app/.pnpm/opencode-ai@0.3.1/node_modules/opencode-ai/dist/index.js")
    # opencode-ai must be an exact path component, not a substring.
    assert not looks("/Users/me/opencode-ai-notes/index.js")
    # Bare `opencode` (no extension) as an argument is NOT an entry script.
    assert not looks("/home/me/opencode")
    # A generic entry script under an opencode dir is NOT enough (FP guard).
    assert not looks("/opt/opencode/src/index.ts")
    assert not looks("/Users/me/projects/opencode/build.js")
    assert not looks("/Users/me/projects/myapp/index.js")
    assert not looks("")


# --- detect_runtime ordering (faked process signal) -------------------------

def test_detect_opencode_when_process_signal_true():
    assert _detect_with(env={}, process_opencode=True) == "opencode"


def test_detect_coexistence_opencode_beats_claude_env():
    # Issue #57 core: Claude plugin env present AND under an opencode ancestor.
    # The definitive process signal must win.
    assert _detect_with(
        env={"CLAUDE_PLUGIN_ROOT": "/home/me/.claude/plugins"},
        process_opencode=True,
    ) == "opencode"


def test_detect_override_wins_over_process_signal():
    assert _detect_with(
        env={"TOKEN_OPTIMIZER_RUNTIME": "claude"},
        process_opencode=True,
    ) == "claude"


def test_detect_genuine_claude_no_ancestor():
    # Regression floor: no opencode ancestor, no override -> claude.
    assert _detect_with(env={}, process_opencode=False) == "claude"


def test_detect_claude_env_without_ancestor():
    assert _detect_with(
        env={"CLAUDE_PLUGIN_ROOT": "/home/me/.claude/plugins"},
        process_opencode=False,
    ) == "claude"


def test_detect_codex_and_hermes_unaffected():
    assert _detect_with(env={"CODEX_HOME": "/home/me/.codex"}) == "codex"
    assert _detect_with(env={"HERMES_HOME": "/home/me/.hermes"}) == "hermes"


def test_detect_opencode_env_only_fallback():
    # OPENCODE_* env signal with no claude/codex env, no ancestor -> opencode.
    assert _detect_with(env={"OPENCODE_BIN": "/usr/local/bin/opencode"}) == "opencode"


def test_detect_opencode_env_does_not_override_claude_env():
    # A stray OPENCODE_* var must not beat a genuine Claude plugin-env session
    # when there is no opencode ancestor process (env fallback stays at step 6).
    assert _detect_with(
        env={
            "OPENCODE_BIN": "/usr/local/bin/opencode",
            "CLAUDE_PLUGIN_ROOT": "/home/me/.claude/plugins",
        },
        process_opencode=False,
    ) == "claude"


# --- Real process scan safety (actually shells out) -------------------------

def test_real_scan_returns_bool_and_never_raises():
    # In the test process there is no opencode ancestor -> False, but the call
    # must not raise regardless of host.
    assert runtime_env._opencode_in_process_tree() is False


def test_real_scan_skipped_when_disabled():
    saved = os.environ.get("TOKEN_OPTIMIZER_NO_PROC_SCAN")
    try:
        os.environ["TOKEN_OPTIMIZER_NO_PROC_SCAN"] = "1"
        assert runtime_env._opencode_in_process_tree() is False
    finally:
        if saved is None:
            os.environ.pop("TOKEN_OPTIMIZER_NO_PROC_SCAN", None)
        else:
            os.environ["TOKEN_OPTIMIZER_NO_PROC_SCAN"] = saved


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
