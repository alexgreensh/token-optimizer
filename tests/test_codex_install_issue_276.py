"""Regression tests for the Codex install problems in issue #276, plus the
model-pin tip in issue #275.

Covers: the compact-prompt setting landing inside a TOML table, the doctor
missing the consolidated runner hooks and misreading Windows paths, runner
hooks duplicated on reinstall, the Stop hook's last-run record, the Codex
compact window, and the plugin skill count across cached versions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_compact_prompt as ccp  # noqa: E402
import codex_doctor  # noqa: E402
import codex_install  # noqa: E402

tomllib = pytest.importorskip("tomllib")

PROMPT = Path("/home/u/.codex/token-optimizer/codex-compact-prompt.md")
WIN_PROMPT = r"C:\Users\u\.codex\token-optimizer\codex-compact-prompt.md"


def _root_value(text):
    return tomllib.loads(text).get("experimental_compact_prompt_file")


# --- compact prompt placement ------------------------------------------------

CONFIG_ENDING_IN_TABLE = (
    'model = "gpt-6.1-sol"\n'
    "\n"
    "[tui]\n"
    'theme = "dark"\n'
    "\n"
    "[projects.'E:\\example-project']\n"
    'trust_level = "trusted"\n'
)


def test_install_puts_setting_at_root_not_in_last_table():
    updated, action = ccp._replace_or_append_config(CONFIG_ENDING_IN_TABLE, PROMPT, force=False)
    assert action == "installed"
    data = tomllib.loads(updated)
    assert data["experimental_compact_prompt_file"] == str(PROMPT)
    assert "experimental_compact_prompt_file" not in data["projects"]["E:\\example-project"]
    assert data["tui"] == {"theme": "dark"}


def test_install_into_file_that_starts_with_a_table():
    text = "[tui]\ntheme = 'dark'\n"
    updated, _ = ccp._replace_or_append_config(text, PROMPT, force=False)
    assert _root_value(updated) == str(PROMPT)
    stripped, removed = ccp._strip_managed_block(updated)
    assert removed and stripped == text


def test_install_with_no_tables_appends_and_uninstall_is_byte_faithful():
    text = 'model = "x"\n'
    updated, _ = ccp._replace_or_append_config(text, PROMPT, force=False)
    assert _root_value(updated) == str(PROMPT)
    assert ccp._strip_managed_block(updated)[0] == text


def test_uninstall_after_root_insert_restores_original_bytes():
    updated, _ = ccp._replace_or_append_config(CONFIG_ENDING_IN_TABLE, PROMPT, force=False)
    assert ccp._strip_managed_block(updated)[0] == CONFIG_ENDING_IN_TABLE


def test_reinstall_moves_a_misplaced_block_to_the_root():
    misplaced = CONFIG_ENDING_IN_TABLE + "\n" + ccp._managed_block(PROMPT)
    assert _root_value(misplaced) is None  # the shipped bug: key is inside the table
    assert ccp.configured_prompt_state(misplaced, PROMPT) == "misplaced"
    updated, action = ccp._replace_or_append_config(misplaced, PROMPT, force=False)
    assert action == "updated"
    assert _root_value(updated) == str(PROMPT)
    assert updated.count(ccp.MANAGED_BEGIN) == 1


def test_reinstall_is_idempotent():
    once, _ = ccp._replace_or_append_config(CONFIG_ENDING_IN_TABLE, PROMPT, force=False)
    twice, _ = ccp._replace_or_append_config(once, PROMPT, force=False)
    assert once == twice


def test_array_line_that_looks_like_a_header_is_not_used():
    text = 'paths = [\n  ["a"]\n]\n\n[tui]\ntheme = "dark"\n'
    updated, _ = ccp._replace_or_append_config(text, PROMPT, force=False)
    assert _root_value(updated) == str(PROMPT)
    assert tomllib.loads(updated)["paths"] == [["a"]]


def test_status_decodes_escaped_windows_path():
    # TOML stores backslashes escaped, so the raw path never appears verbatim.
    block = ccp._managed_block(Path(WIN_PROMPT)) if sys.platform == "win32" else (
        f"{ccp.MANAGED_BEGIN}\nexperimental_compact_prompt_file = {json.dumps(WIN_PROMPT)}\n{ccp.MANAGED_END}\n"
    )
    text = block + "\n[tui]\n"
    assert WIN_PROMPT not in text
    assert ccp.configured_prompt_state(text, Path(WIN_PROMPT) if sys.platform == "win32" else WIN_PROMPT) == "ours"


def test_status_reports_custom_prompt_as_custom():
    text = 'experimental_compact_prompt_file = "/somewhere/else.md"\n'
    assert ccp.configured_prompt_state(text, PROMPT) == "custom"


# --- runner hook ownership: no duplicates on reinstall -----------------------

@pytest.mark.parametrize("root", [
    "/home/u/.codex/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.56",
    "/home/u/src/token-optimizer",
])
def test_posix_runner_hooks_are_replaced_not_duplicated(monkeypatch, root):
    monkeypatch.setattr(codex_install.sys, "platform", "linux")
    monkeypatch.setattr(codex_install, "_repo_root", lambda: Path(root))
    kw = dict(enable_prompt_hooks=True, enable_hot_path_hooks=True, enable_subagent_hooks=True)
    once = codex_install._merge_hooks({"hooks": {}}, **kw)
    twice = codex_install._merge_hooks(once, **kw)
    assert once == twice
    assert all(len(groups) == 1 for groups in twice["hooks"].values())
    assert codex_install._remove_hooks(twice)["hooks"] == {}


def test_foreign_runner_named_hook_outside_our_folder_is_kept(monkeypatch):
    foreign = {"hooks": [{"type": "command", "command": "python3 /opt/tools/hooks/stop_runner.py"}]}
    assert not codex_install._is_token_optimizer_group(foreign)


# --- doctor -------------------------------------------------------------------

def _write_hooks(path: Path, monkeypatch, platform="linux"):
    monkeypatch.setattr(codex_install.sys, "platform", platform)
    monkeypatch.setattr(
        codex_install, "_repo_root",
        lambda: Path("/home/u/.codex/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.56"))
    hooks = codex_install._merge_hooks(
        {"hooks": {}}, enable_prompt_hooks=True, enable_hot_path_hooks=True, enable_subagent_hooks=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hooks), encoding="utf-8")


def test_doctor_sees_consolidated_runners_in_global_hooks(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setattr(codex_doctor, "codex_home", lambda: home)
    _write_hooks(home / "hooks.json", monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    checks = {c["name"]: c for c in codex_doctor._project_feature_checks(project)}
    assert checks["Feature: Session continuity and dashboard refresh"]["detail"] == "available in current Codex adapter"
    for name in ("Prompt quality nudges", "Tool output archive", "Context intelligence"):
        assert checks[f"Optional feature: {name}"]["detail"].startswith("enabled"), name
    assert codex_doctor._global_hook_check()["status"] == "OK"


def test_doctor_flags_misplaced_compact_prompt(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "token-optimizer").mkdir(parents=True)
    expected = home / "token-optimizer" / "codex-compact-prompt.md"
    expected.write_text("x", encoding="utf-8")
    monkeypatch.setattr(codex_doctor, "codex_home", lambda: home)
    (home / "config.toml").write_text(
        CONFIG_ENDING_IN_TABLE + "\n" + ccp._managed_block(expected), encoding="utf-8")
    check = codex_doctor._compact_prompt_check()
    assert check["status"] == "FAIL" and "inside a table" in check["detail"]
    fixed, _ = ccp._replace_or_append_config(
        (home / "config.toml").read_text(encoding="utf-8"), expected, force=False)
    (home / "config.toml").write_text(fixed, encoding="utf-8")
    assert codex_doctor._compact_prompt_check()["status"] == "OK"


def test_doctor_reports_stop_hook_last_run(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_doctor, "runtime_home", lambda: tmp_path)
    assert codex_doctor._stop_hook_activity_check()["status"] == "WARN"
    status = tmp_path / "token-optimizer" / "hook-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"stop_flush": {"at": "2026-10-22T10:00:00+00:00", "result": "ran"}}))
    check = codex_doctor._stop_hook_activity_check()
    assert check["status"] == "OK" and "ran" in check["detail"]


# --- measure.py pieces -------------------------------------------------------

@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    original = sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        sys.modules.pop("measure", None)
        if original is not None:
            sys.modules["measure"] = original


def test_stop_flush_records_throttled_skip(measure, tmp_path, monkeypatch):
    status = tmp_path / "hook-status.json"
    monkeypatch.setattr(measure, "HOOK_STATUS_FILE", status)
    monkeypatch.setattr(measure, "_session_refresh_due", lambda *a, **k: False)
    monkeypatch.setattr(measure, "_run_post_flush_extensions", lambda **k: None)
    measure._run_session_end_flush_worker(["session-end-flush", "--trigger", "stop"], budget_seconds=5)
    result = json.loads(status.read_text())["stop_flush"]["result"]
    assert result.startswith("skipped:") and "2 minutes" in result


def test_codex_compact_window_uses_codex_model_window(measure, monkeypatch):
    monkeypatch.setattr(measure, "detect_runtime", lambda: "codex")
    import codex_models
    monkeypatch.setattr(codex_models, "effective_window", lambda model: 475_000)
    resolved = measure._resolve_compact_window("gpt-6.1-sol", env={}, settings={})
    assert resolved["tokens"] == 475_000
    assert resolved["model_window"] == 475_000


def test_claude_compact_window_unchanged(measure, monkeypatch):
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    resolved = measure._resolve_compact_window("claude-opus-5-5", env={}, settings={})
    assert resolved["tokens"] == measure._COMPACT_WINDOW_1M_DEFAULT


def test_plugin_skills_count_only_newest_cached_version(measure, tmp_path, monkeypatch):
    runtime = tmp_path / "codex"
    cache = runtime / "plugins" / "cache"
    for version in ("5.13.49", "5.13.55", "5.13.56"):
        for skill in ("token-optimizer", "fleet-auditor"):
            d = cache / "alexgreensh-token-optimizer" / "token-optimizer" / version / "skills" / skill
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: d\n---\n")
    other = cache / "other-market" / "other-plugin" / "1.0.0" / "skills" / "helper"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: helper\ndescription: d\n---\n")
    monkeypatch.setattr(measure, "RUNTIME_DIR", runtime)
    inv = measure._collect_codex_skill_inventory({}, project=tmp_path)
    plugin = [i for i in inv["active"] if i["source"] == "plugin"]
    assert len(plugin) == 3
    assert all("5.13.56" in i["path"] or "other-plugin" in i["path"] for i in plugin)
    assert {i.get("plugin") for i in plugin} == {
        "token-optimizer@alexgreensh-token-optimizer", "other-plugin@other-market"}


# --- issue #275: model-pin tip -------------------------------------------------

_OPUS_TRENDS = {"model_mix": {"opus": 90, "haiku": 5, "sonnet": 5}}
_PINNED = {"settings_local": {"defaultModel": "opus"}}


def _has_pin_tip(measure):
    plan, _ = measure.generate_auto_recommendations(_PINNED, trends=_OPUS_TRENDS)
    return "Remove hardcoded model" in plan


def test_pin_tip_shown_without_a_router(measure, monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_SKIP_MODEL_PIN_HINT", raising=False)
    monkeypatch.setattr(measure, "_resolve_settings_value", lambda k, d=None: {} if k == "enabledPlugins" else d)
    assert _has_pin_tip(measure)


def test_pin_tip_hidden_when_router_plugin_enabled(measure, monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_SKIP_MODEL_PIN_HINT", raising=False)
    plugins = {"claude-model-router-hook@some-market": True}
    monkeypatch.setattr(measure, "_resolve_settings_value", lambda k, d=None: plugins if k == "enabledPlugins" else d)
    assert not _has_pin_tip(measure)
    plan, _ = measure.generate_auto_recommendations(_PINNED, trends=_OPUS_TRENDS)
    assert "Route subagents by task type" in plan  # the habit advice stays


def test_pin_tip_hidden_by_env_opt_out(measure, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SKIP_MODEL_PIN_HINT", "1")
    monkeypatch.setattr(measure, "_resolve_settings_value", lambda k, d=None: {} if k == "enabledPlugins" else d)
    assert not _has_pin_tip(measure)


def test_disabled_router_plugin_does_not_hide_tip(measure, monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_SKIP_MODEL_PIN_HINT", raising=False)
    plugins = {"claude-model-router-hook@some-market": False}
    monkeypatch.setattr(measure, "_resolve_settings_value", lambda k, d=None: plugins if k == "enabledPlugins" else d)
    assert _has_pin_tip(measure)


def test_status_without_a_toml_parser(monkeypatch):
    # Python < 3.11 has no tomllib: the regex fallback must still decode the
    # escaped path and spot a block sitting under a table.
    monkeypatch.setattr(ccp, "_toml_root_value", lambda text: ("unavailable", None))
    block = f"{ccp.MANAGED_BEGIN}\nexperimental_compact_prompt_file = {json.dumps(WIN_PROMPT)}\n{ccp.MANAGED_END}\n"
    assert ccp.configured_prompt_state(block + "\n[tui]\n", WIN_PROMPT) == "ours"
    assert ccp.configured_prompt_state("[tui]\nx = 1\n\n" + block, WIN_PROMPT) == "misplaced"


def test_heal_repairs_only_our_quoted_launcher_commands(tmp_path):
    marker = codex_install._LAUNCHER_MARKER
    ours = (r'"C:\Python315\python.exe" "C:\u\.codex\plugins\cache\alexgreensh-token-optimizer\token-optimizer\windows-launcher.py"'
            r' --baked-root "C:\u\.codex\plugins\cache\alexgreensh-token-optimizer\token-optimizer\5.13.56"'
            f' -- "hooks/stop_runner.py" "{marker}" >NUL 2>&1')
    foreign = r'"C:\tools\python.exe" "C:\tools\my_hook.py"'
    hooks = {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": ours}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": foreign}]}],
    }}
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(hooks), encoding="utf-8")
    assert codex_install.heal_quoted_launcher_commands([path]) == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    fixed = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    # Issue #278: the exact generated shape is rewritten to the shell-neutral
    # form (PowerShell and cmd.exe both run it), not the cmd-only set prefix.
    assert fixed == codex_install.neutralize_launcher_command(ours)
    assert codex_install.is_shell_neutral_command(fixed)
    assert not fixed.startswith('"')
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == foreign
    # Second run is a no-op.
    assert codex_install.heal_quoted_launcher_commands([path]) == 0


def test_heal_falls_back_to_unquoted_set_prefix_when_no_neutral_form(tmp_path, monkeypatch):
    """A quoted launcher command whose interpreter cannot be written
    shell-neutral (spaced path, no 8.3 short name) still gets the #276
    cmd-only repair, so cmd.exe at least stops stripping its quotes."""
    monkeypatch.setattr(codex_install, "_short_path", lambda p: None)
    marker = codex_install._LAUNCHER_MARKER
    ours = (r'"C:\Program Files\Python\python.exe" "C:\u\w.py" --baked-root "C:\u\5.13.56"'
            f' -- "hooks/stop_runner.py" "{marker}"')
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ours}]}]}}),
                    encoding="utf-8")
    assert codex_install.neutralize_launcher_command(ours) is None
    assert codex_install.heal_quoted_launcher_commands([path]) == 1
    fixed = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert fixed == f"{codex_install._WIN_UNQUOTED_PREFIX} {ours}"
    assert codex_install.heal_quoted_launcher_commands([path]) == 0
