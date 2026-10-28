"""Environment fingerprint matrix: one fixture per real runtime, end to end.

WHY THIS EXISTS
---------------
Runtime detection kept escaping to users while the suite was green (#176 #120
#198 #249 #209 #140 #141 #57, and on 2026-10-19 Claude Code cloud read as
Cowork). The detection tests that existed were unit tests: they monkeypatched
os.environ inside the pytest process, cleared lru caches by hand, often stubbed
the neighbouring signals, and asserted a return value. Three things they never
saw:

  * the WHOLE environment a host really hands a hook (a real CLI hook carries
    AI_AGENT=claude-code_<v>_harness, which is what broke #176; a real cloud VM
    carries CLAUDE_CODE_CONTAINER_ID, which is what made cloud read as Cowork),
  * a fresh process (no cache, nothing inherited from the developer's own
    Claude Code session: a suite run INSIDE Claude Code leaks CLAUDECODE=1 and
    CLAUDE_CODE_AUTO_COMPACT_WINDOW into every in-process test),
  * the user-visible outcome: which runtime home the state lands in, which
    denominator the context % uses, and whether another harness's directory
    was touched.

Every fixture below is a complete host environment built from scratch (never
os.environ), and the real hook command strings from hooks/hooks.json run it
through the shell, the launcher and run.py, against a copy of the plugin placed
where that host puts it. Assertions are about outcomes, with hand-worked
expected values.

EVIDENCE FOR EACH MARKER
------------------------
Markers come from live captures, issue reports or code comments that cite an
observation. Anything not observed is listed in the fixture's ``assumed``
field and repeated in REPORT-tests-env.md.

  CLI hook       #176 live capture (AI_AGENT=claude-code_<v>_harness on a native
                 hook); #120 env dump (CLAUDECODE, CLAUDE_CODE_ENTRYPOINT=cli,
                 CLAUDE_CODE_SESSION_ID); plugin root/data paths from
                 plugin_env.py's own layout.
  CLI Bash tool  #120 env dump + this session's own env on 2026-10-19 (Claude
                 Code 2.1.303): CLAUDECODE=1, CLAUDE_CODE_ENTRYPOINT=cli,
                 CLAUDE_CODE_CHILD_SESSION=1, AI_AGENT=claude-code_2-1-303_agent,
                 no CLAUDE_PLUGIN_ROOT / CLAUDE_PLUGIN_DATA.
  Cloud VM       FINDINGS.md P8/P9 (cse_01Au8fmq..., cse_01DWjCoV...): REMOTE,
                 REMOTE_SESSION_ID, CONTAINER_ID, ENVIRONMENT_TYPE=cloud_default,
                 ENTRYPOINT=remote_trigger, HOME=/root, repo at /home/user/<repo>,
                 a plugins/synced/<org>_<account>/ dir; P10 live pass: the cloud
                 kit's run.sh exports CLAUDE_PLUGIN_ROOT=<kit cache>,
                 PYTHONPYCACHEPREFIX and TOKEN_OPTIMIZER_CLOUD_KIT_VERSION.
  Cowork VM      COWORK_BUILD_PLAN.md 2026-08-13 live probe: CLAUDE_PLUGIN_ROOT=
                 /root/.claude/plugins/synced/..., AI_AGENT=..._harness,
                 CLAUDE_CODE_CONTAINER_ID present, root, cwd /home/claude;
                 SessionStart never fires there. ASSUMED: whether Cowork also
                 sets CLAUDE_CODE_REMOTE / REMOTE_SESSION_ID /
                 REMOTE_ENVIRONMENT_TYPE (no Cowork probe since cloud split).
  Codex          the hook command codex_install._managed_hooks() really writes
                 (TOKEN_OPTIMIZER_RUNTIME=codex inline), CODEX_HOME set.
  CLAUDE_CONFIG_DIR  #198: a config dir outside HOME.
  autoCompactWindow  #249 (settings.json "autoCompactWindow": 600000 on a 1M
                 model) and #209 (CLAUDE_CODE_AUTO_COMPACT_WINDOW env).

Run: python3 -m pytest tests/test_env_fingerprint_matrix.py -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
PY_DIR = str(Path(sys.executable).resolve().parent)

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX hook wire (bash launcher); the Windows wire is "
    "covered by the platform suite")

SID = "5e55a7e1-0000-4000-8000-00000000c0de"
MODEL = "claude-opus-5-5"          # 1M-native model (model window 1,000,000)
CONTEXT_TOKENS = 450_000           # input + cache_read + cache_creation in the last turn
SCHEDULERS = ("launchctl", "systemctl", "schtasks", "crontab", "loginctl")
PLUGIN_DATA_NAME = "token-optimizer-alexgreensh-token-optimizer"


# --------------------------------------------------------------------------- #
# Sandbox: one throwaway machine per test
# --------------------------------------------------------------------------- #

class Machine:
    """A fake machine rooted at ``root``: a home, a scheduler shim dir that
    records every call, a project, and a copy of the plugin wherever the host
    under test installs it."""

    def __init__(self, root: Path):
        self.root = root
        self.shims = root / "shims"
        self.calls = root / "scheduler-calls.log"
        self.tmp = root / "tmp"
        for d in (self.shims, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        for name in SCHEDULERS:
            shim = self.shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\nexit 0\n')
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    def path(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    def install_plugin(self, rel: str, src: str = "") -> Path:
        """Copy a plugin build (hooks + scripts, no dashboard assets) to ``rel``.
        ``src`` "" is the Claude Code plugin (the repo root);
        "plugins/token-optimizer" is the Codex marketplace build."""
        dst = self.path(rel)
        dst.mkdir(parents=True, exist_ok=True)
        base = REPO / src if src else REPO
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "assets")
        shutil.copytree(base / "hooks", dst / "hooks", ignore=ignore)
        # Scripts always come from the canonical skills/ tree: the Codex build's
        # copy is byte-identical (scripts/check-mirror-sync.sh enforces it), and
        # using the canonical bytes keeps a planted bug in skills/ visible here.
        shutil.copytree(REPO / "skills" / "token-optimizer", dst / "skills" / "token-optimizer", ignore=ignore)
        for meta in (".claude-plugin", ".codex-plugin"):
            if (base / meta).is_dir():
                shutil.copytree(base / meta, dst / meta, ignore=ignore)
        # A real install has run before: compile now so the first hook does not
        # spend the PostToolUse runner's 2.5s self-budget compiling measure.py
        # (on a slow CI runner it then rightly skips and prints nothing, which
        # read as a missing envelope). Hosts that move bytecode elsewhere
        # (hosted VMs) still compile on first use, as they do in real life.
        import compileall
        compileall.compile_dir(str(dst), quiet=1)
        return dst

    def base_env(self, home: Path) -> dict:
        return {
            "HOME": str(home),
            "PATH": f"{self.shims}:{PY_DIR}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(self.tmp),
            # The ancestor scan would see pytest's real parents; fixtures that
            # model an OpenCode ancestor say so explicitly.
            "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
        }

    def files(self) -> set[str]:
        out = set()
        for p in self.root.rglob("*"):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and "/.cache/token-optimizer/pycache/" not in "/" + rel \
                    and "/Library/Caches/" not in "/" + rel and not rel.startswith(("tmp/", "shims/")) \
                    and rel != "scheduler-calls.log":
                out.add(rel)
        return out

    def scheduler_calls(self) -> list[str]:
        try:
            return self.calls.read_text().splitlines()
        except OSError:
            return []


def write_transcript(path: Path, cwd: Path, *, model: str = MODEL, tokens: int = CONTEXT_TOKENS,
                     prompt: str = "fix the failing login test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "sessionId": SID, "cwd": str(cwd), "timestamp": "2026-10-19T10:00:00Z",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "sessionId": SID, "cwd": str(cwd), "timestamp": "2026-10-19T10:00:05Z",
         "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 0, "cache_read_input_tokens": tokens,
                               "cache_creation_input_tokens": 0, "output_tokens": 100}}},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def claude_hook_commands(event: str, tool: str | None = None, hooks_json: Path = HOOKS_JSON) -> list[str]:
    """The real command strings hooks.json registers for ``event`` (and, for
    tool events, the groups whose matcher covers ``tool``)."""
    groups = json.loads(hooks_json.read_text(encoding="utf-8"))["hooks"].get(event, [])
    out = []
    for g in groups:
        matcher = g.get("matcher")
        if tool is not None and matcher and not re.fullmatch(matcher, tool):
            continue
        for h in g.get("hooks", []):
            if h.get("type") == "command":
                out.append(h["command"])
    return out


def run_shell(command: str, env: dict, cwd: Path, payload: dict | None = None,
              timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", "-c", command], input=json.dumps(payload or {}, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(cwd), timeout=timeout)


def probe(env: dict, cwd: Path, scripts: Path) -> dict:
    """What a fresh TO process concludes about its host. Runs in a brand-new
    interpreter with exactly ``env``: no cache, nothing inherited."""
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "import runtime_env as r, plugin_env as p;"
        "d = p.resolve_plugin_data_dir();"
        "print(json.dumps({'runtime': r.detect_runtime(), 'host': r.remote_host(),"
        " 'label': r.runtime_name_for_humans(), 'home': str(r.runtime_home()),"
        " 'origin': r.run_origin(), 'plugin_data': str(d) if d else ''}))"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(scripts)], capture_output=True,
                          text=True, env=env, cwd=str(cwd), timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# The fingerprints
# --------------------------------------------------------------------------- #

@dataclass
class Host:
    """A runtime as a hook sees it. Paths are machine-relative."""
    name: str
    evidence: str
    home: str                      # $HOME
    plugin: str                    # where the host installs the plugin
    project: str                   # the session cwd
    env: dict                      # host env on top of Machine.base_env (values may use {plugin} {home} {root})
    runtime: str                   # expected detect_runtime()
    host: str                      # expected remote_host()
    label: str                     # expected runtime_name_for_humans()
    state_root: str                # where TO state must land (machine-relative)
    transcript_root: str = ""      # <claude home>/projects, for Claude hosts
    events: tuple = ("SessionStart", "UserPromptSubmit", "PostToolUse")
    other_homes: tuple = ("{home}/.codex", "{home}/.config/opencode", "{home}/.copilot")
    assumed: tuple = ()
    settings: dict = field(default_factory=dict)  # written to <claude home>/settings.json
    plugin_src: str = ""           # "" Claude Code plugin; "plugins/token-optimizer" Codex build
    marketplace_install: bool = False  # registered in <claude home>/plugins/installed_plugins.json
    codex_rollout: str = ""        # Codex: sessions dir for the rollout transcript


CLI_PLUGIN = "home/alex/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.51"

HOSTS = {
    # Native Claude Code CLI on a desktop that also has Codex/OpenCode/Copilot
    # installed (#120's leftover ~/.codex, #57's two-harness host).
    "cli": Host(
        name="cli",
        evidence="#176 live AI_AGENT capture; #120 env dump",
        home="home/alex", plugin=CLI_PLUGIN, project="home/alex/work/app",
        env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": SID,
             "AI_AGENT": "claude-code_2.1.258_harness",
             "CLAUDE_PLUGIN_ROOT": "{plugin}",
             "CLAUDE_PLUGIN_DATA": "{home}/.claude/plugins/data/" + PLUGIN_DATA_NAME},
        runtime="claude", host="", label="Claude Code",
        state_root="home/alex/.claude", transcript_root="home/alex/.claude/projects",
        marketplace_install=True),
    # #198: Claude Code relocated with CLAUDE_CONFIG_DIR, outside HOME.
    "cli_config_dir": Host(
        name="cli_config_dir",
        evidence="#198 (statusline + hooks split state when CLAUDE_CONFIG_DIR was ignored)",
        home="home/alex", plugin="srv/claude-config/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.51",
        project="home/alex/work/app",
        env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": SID,
             "AI_AGENT": "claude-code_2.1.258_harness",
             "CLAUDE_CONFIG_DIR": "{root}/srv/claude-config",
             "CLAUDE_PLUGIN_ROOT": "{plugin}",
             "CLAUDE_PLUGIN_DATA": "{root}/srv/claude-config/plugins/data/" + PLUGIN_DATA_NAME},
        runtime="claude", host="", label="Claude Code",
        state_root="srv/claude-config", transcript_root="srv/claude-config/projects",
        other_homes=("{home}/.claude", "{home}/.codex"), marketplace_install=True),
    # #93-style unusual home plus a non-English user name (#127 #73 users):
    # spaces and non-ASCII in every path the hooks touch, Hebrew locale.
    "cli_unicode_home": Host(
        name="cli_unicode_home",
        evidence="#93 (non-standard HOME), #73/#127 (he_IL users); assumed: path shape",
        home="Users/José Núñez אלכס",
        plugin="Users/José Núñez אלכס/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.51",
        project="Users/José Núñez אלכס/עבודה/my app",
        env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": SID,
             "AI_AGENT": "claude-code_2.1.258_harness", "LANG": "he_IL.UTF-8", "LC_ALL": "he_IL.UTF-8",
             "CLAUDE_PLUGIN_ROOT": "{plugin}",
             "CLAUDE_PLUGIN_DATA": "{home}/.claude/plugins/data/" + PLUGIN_DATA_NAME},
        runtime="claude", host="", label="Claude Code",
        state_root="Users/José Núñez אלכס/.claude", transcript_root="Users/José Núñez אלכס/.claude/projects",
        marketplace_install=True, assumed=("user name / project path shape",)),
    # Claude Code cloud VM with the cloud kit (P10 live pass).
    "cloud": Host(
        name="cloud",
        evidence="FINDINGS.md P8/P9 env, P10 kit run.sh exports",
        home="root", plugin="root/.cache/token-optimizer-cloud/5.13.51", project="home/user/app",
        env={"CLAUDE_CODE_REMOTE": "true",
             "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01Au8fmq3iUyLMULzXTMuy5o",
             "CLAUDE_CODE_CONTAINER_ID": "container_01ABCDEF",
             "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default",
             "CLAUDE_CODE_ENTRYPOINT": "remote_trigger",
             "CLAUDE_PLUGIN_ROOT": "{plugin}",
             "PYTHONPYCACHEPREFIX": "{home}/.cache/token-optimizer-cloud/pycache",
             "TOKEN_OPTIMIZER_CLOUD_KIT_VERSION": "5.13.51"},
        runtime="claude", host="cloud", label="Claude Code (cloud)",
        state_root="root/.claude", transcript_root="root/.claude/projects",
        other_homes=("{home}/.claude/plugins/synced",),
        assumed=("container id value shape",)),
    # Cowork VM (2026-08-13 live probe). No SessionStart there.
    "cowork": Host(
        name="cowork",
        evidence="COWORK_BUILD_PLAN.md 2026-08-13 live probe",
        home="root", plugin="root/.claude/plugins/synced/org-uuid_account-uuid/token-optimizer",
        project="home/claude",
        env={"CLAUDE_CODE_CONTAINER_ID": "container_01COWORK",
             "AI_AGENT": "claude-code_2-1-231_harness",
             "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
             "CLAUDE_PLUGIN_ROOT": "{plugin}"},
        runtime="claude", host="cowork", label="Claude Code (Cowork)",
        state_root="root/.claude", transcript_root="root/.claude/projects",
        events=("UserPromptSubmit", "PostToolUse"),
        other_homes=("{home}/.codex",),
        assumed=("CLAUDE_CODE_REMOTE / REMOTE_SESSION_ID / REMOTE_ENVIRONMENT_TYPE unset in Cowork "
                 "(not captured by the 2026-08-13 probe)",)),
    # Codex running the Codex marketplace build of the plugin, on a machine
    # that also has Claude Code. Codex loads the plugin's hooks.json itself and
    # gives the hook ONLY CLAUDE_PLUGIN_ROOT / CLAUDE_PLUGIN_DATA: no CODEX_HOME,
    # no TOKEN_OPTIMIZER_RUNTIME (verified live with an env-dumping hook, see
    # tests/test_codex_sessionstart_json_contract.py). The binary of Codex
    # 0.163.0-alpha.10.2 also names PLUGIN_ROOT / PLUGIN_DATA beside them. Paths:
    # ~/.codex/plugins/cache/<marketplace>/token-optimizer/<version> and
    # ~/.codex/plugins/data/token-optimizer-<marketplace> (a real Codex install).
    "codex_plugin": Host(
        name="codex_plugin",
        evidence="test_codex_sessionstart_json_contract.py live env dump; Codex plugin dir layout",
        home="home/alex",
        plugin="home/alex/.codex/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.51",
        project="home/alex/work/app",
        env={"CLAUDE_PLUGIN_ROOT": "{plugin}", "PLUGIN_ROOT": "{plugin}",
             "CLAUDE_PLUGIN_DATA": "{home}/.codex/plugins/data/" + PLUGIN_DATA_NAME,
             "PLUGIN_DATA": "{home}/.codex/plugins/data/" + PLUGIN_DATA_NAME},
        runtime="codex", host="", label="Codex",
        state_root="home/alex/.codex",
        plugin_src="plugins/token-optimizer", codex_rollout="home/alex/.codex/sessions/2026/10/19",
        other_homes=("{home}/.claude", "{home}/.config/opencode", "{home}/.copilot"),
        assumed=("PLUGIN_ROOT / PLUGIN_DATA exported (binary strings only; TO does not read them)",)),
}


def materialize(m: Machine, h: Host) -> tuple[dict, Path, Path, Path]:
    """Build the machine for host ``h``. Returns (env, home, project, plugin)."""
    home = m.path(h.home)
    project = m.path(h.project)
    home.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    # Other harnesses present on the same machine (#120 #57): real, populated
    # dirs that must come out byte-identical.
    for d in (".codex", ".config/opencode", ".copilot"):
        p = home / d
        p.mkdir(parents=True, exist_ok=True)
        (p / "config.toml" if d == ".codex" else p / "settings.json").write_text("{}\n")
    if h.runtime != "claude":
        # A real Claude Code install beside the other harness (#57): settings
        # with a status line of its own, and a projects dir.
        (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "settings.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "~/.claude/my-statusline.sh"}}))
    if h.codex_rollout:
        (home / ".codex" / "plugins" / "data" / PLUGIN_DATA_NAME).mkdir(parents=True, exist_ok=True)
    plugin = m.install_plugin(h.plugin, h.plugin_src)
    subs = {"plugin": str(plugin), "home": str(home), "root": str(m.root)}
    env = m.base_env(home)
    for k, v in h.env.items():
        env[k] = v.format(**subs)
    if h.transcript_root:
        claude_home = m.path(h.transcript_root).parent
        claude_home.mkdir(parents=True, exist_ok=True)
        settings = {"model": MODEL, **h.settings}
        (claude_home / "settings.json").write_text(json.dumps(settings))
        if h.marketplace_install:
            # What Claude Code writes for a marketplace install: the registry
            # entry and the plugin's data dir (CLAUDE_PLUGIN_DATA).
            (claude_home / "plugins" / "data" / PLUGIN_DATA_NAME).mkdir(parents=True, exist_ok=True)
            (claude_home / "plugins" / "installed_plugins.json").write_text(json.dumps({
                "version": 2, "plugins": {"token-optimizer@alexgreensh-token-optimizer": [
                    {"scope": "user", "installPath": str(plugin), "version": "5.13.51"}]}}))
    return env, home, project, plugin


def transcript_for(m: Machine, h: Host, project: Path) -> Path:
    if h.codex_rollout:
        return m.path(h.codex_rollout) / f"rollout-2026-10-19T10-00-00-{SID}.jsonl"
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    return m.path(h.transcript_root) / enc / f"{SID}.jsonl"


def run_session(m: Machine, h: Host, env: dict, project: Path, transcript: Path,
                prompt: str = "fix the failing login test") -> dict:
    """Drive the host's hook events the way Claude Code does. Returns the raw
    stdout per event so callers can check envelopes."""
    out = {}
    base = {"session_id": SID, "transcript_path": str(transcript), "cwd": str(project)}
    find_out = "\n".join(f"./src/pkg{i // 50}/module_{i}.py" for i in range(500))
    payloads = {
        "SessionStart": {**base, "hook_event_name": "SessionStart", "source": "startup"},
        "UserPromptSubmit": {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt},
        "PostToolUse": {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                        "tool_use_id": "toolu_01FINGERPRINT",
                        "tool_input": {"command": "find . -name '*.py'"},
                        "tool_response": {"stdout": find_out, "stderr": "", "interrupted": False}},
    }
    hooks_json = (REPO / h.plugin_src / "hooks" / "hooks.json") if h.plugin_src else HOOKS_JSON
    for event in h.events:
        tool = "Bash" if event == "PostToolUse" else None
        for cmd in claude_hook_commands(event, tool, hooks_json):
            proc = run_shell(cmd, env, project, payloads[event])
            assert proc.returncode == 0, f"{h.name} {event} exit {proc.returncode}: {proc.stderr[-800:]}"
            out.setdefault(event, []).append(proc.stdout)
    return out


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path / "m")


# --------------------------------------------------------------------------- #
# 1. Detection, in a fresh process with the host's whole environment
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", list(HOSTS))
def test_fresh_process_detects_the_host(machine, name):
    h = HOSTS[name]
    env, home, project, plugin = materialize(machine, h)
    got = probe(env, project, plugin / "skills" / "token-optimizer" / "scripts")
    assert (got["runtime"], got["host"], got["label"]) == (h.runtime, h.host, h.label), got
    assert got["origin"] == (h.host or "local")
    # State home: the runtime home TO writes under is the host's own.
    assert Path(got["home"]) == machine.path(h.state_root).resolve(), got


# --------------------------------------------------------------------------- #
# 2. State lands in the host's own home and nowhere else
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", list(HOSTS))
def test_session_state_stays_in_the_hosts_own_home(machine, name):
    h = HOSTS[name]
    env, home, project, plugin = materialize(machine, h)
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    before = machine.files()
    claude_settings = home / ".claude" / "settings.json"
    settings_before = claude_settings.read_bytes() if h.runtime != "claude" else b""
    run_session(machine, h, env, project, transcript)
    new = machine.files() - before
    if h.runtime != "claude":
        # #57, user-visible: another harness's session must not rewrite Claude
        # Code's settings (a Codex session used to point Claude Code's
        # statusLine at the Codex plugin cache, which Codex deletes on update).
        assert claude_settings.read_bytes() == settings_before, claude_settings.read_text()
    # The runtime home, plus the per-user cache dir (python-launcher's
    # interpreter cache, bytecode): both are the user's own, outside any repo.
    allowed = (h.state_root.rstrip("/") + "/", h.home + "/.cache/")
    stray = sorted(f for f in new if not f.startswith(allowed))
    assert not stray, f"{name}: files written outside {h.state_root}: {stray[:10]}"
    # Positive control: the session really ran (a quality cache for THIS
    # session exists), so "nothing stray" is not vacuous.
    assert any("/quality-cache-" in f and f.endswith(f"{SID}.json") for f in new), sorted(new)[:20]
    # The plugin install and the user's repo get no state (a write into the
    # plugin dir is lost on the next update; one into the repo gets committed).
    # Bytecode beside the plugin's own scripts is Python's cache and intended
    # on a desktop (tests/test_cloud_parity.py pins where it goes in a VM).
    for d in (plugin, project):
        rel = d.relative_to(machine.root).as_posix() + "/"
        wrote = [f for f in new if f.startswith(rel) and "/__pycache__/" not in f]
        assert not wrote, f"{name}: wrote into {rel}: {wrote[:5]}"
    # Another harness's home is untouched.
    subs = {"home": str(home)}
    for other in h.other_homes:
        rel = Path(other.format(**subs)).relative_to(machine.root).as_posix() + "/"
        touched = sorted(f for f in new if f.startswith(rel))
        assert not touched, f"{name}: wrote into another home {rel}: {touched[:5]}"


# --------------------------------------------------------------------------- #
# 3. Trends rows say where they were recorded; cloud is lean
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", list(HOSTS))
def test_compression_rows_carry_the_hosts_origin(machine, name):
    h = HOSTS[name]
    env, home, project, plugin = materialize(machine, h)
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    run_session(machine, h, env, project, transcript)
    dbs = list(machine.path(h.state_root).rglob("trends.db"))
    assert len(dbs) == 1, dbs
    con = sqlite3.connect(str(dbs[0]))
    try:
        origins = {r[0] for r in con.execute("SELECT origin FROM compression_events")}
    finally:
        con.close()
    # The 500-line find output was compressed (positive control) and every row
    # names this host, never another one.
    assert origins == {h.host or "local"}, origins


def test_cloud_vm_session_is_lean(machine):
    """FINDINGS.md: the cloud VM is thrown away, so desktop upkeep there is
    wasted work, and a settings.json rewrite lands in the VM's only config
    (lean profile, measure._remote_lean_profile). The user-visible contract:
    the session leaves settings.json byte-identical and never calls a service
    manager. Positive control: the same session with the documented opt-out
    TOKEN_OPTIMIZER_REMOTE_LEAN=0 does rewrite settings.json, so the check can
    see a write when one happens."""
    h = HOSTS["cloud"]
    results = {}
    for lean in ("default", "0"):
        m = Machine(machine.root.parent / f"lean-{lean}")
        env, home, project, plugin = materialize(m, h)
        if lean == "0":
            env["TOKEN_OPTIMIZER_REMOTE_LEAN"] = "0"
        settings = m.path(h.state_root) / "settings.json"
        before = settings.read_bytes()
        transcript = transcript_for(m, h, project)
        write_transcript(transcript, project)
        run_session(m, h, env, project, transcript)
        results[lean] = (settings.read_bytes() == before, m.scheduler_calls())
    assert results["default"] == (True, []), results
    assert results["0"][0] is False, "positive control: opt-out session did not touch settings.json"


# --------------------------------------------------------------------------- #
# 4. Context fill: which window is the denominator (#249 #209 #198)
# --------------------------------------------------------------------------- #

def _quality_cache(machine: Machine, h: Host) -> dict:
    caches = list(machine.path(h.state_root).rglob(f"quality-cache-{SID}.json"))
    assert len(caches) == 1, caches
    return json.loads(caches[0].read_text())["breakdown"]["context_fill_degradation"]


# 450,000 tokens of context on a 1,000,000-token model:
#   no override          -> 45% of the model window
#   600k compact window  -> 450/600 = 75% of the compact window, 45% of the model
FILL_CASES = {
    "default": ({}, {}, 45.0, False),
    "settings_autoCompactWindow": ({"autoCompactWindow": 600000}, {}, 75.0, True),        # #249
    "env_auto_compact_window": ({}, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"}, 75.0, True),  # #209
}


@pytest.mark.parametrize("host_name", ["cli", "cli_config_dir"])
@pytest.mark.parametrize("case", list(FILL_CASES))
def test_context_fill_uses_the_users_compact_window(machine, host_name, case):
    settings, extra_env, want_fill, reduced = FILL_CASES[case]
    base = HOSTS[host_name]
    h = Host(**{**base.__dict__, "settings": settings, "events": ("SessionStart", "UserPromptSubmit")})
    env, home, project, plugin = materialize(machine, h)
    env.update(extra_env)
    if host_name == "cli_config_dir":
        # A decoy ~/.claude/settings.json with a DIFFERENT window: CLAUDE_CONFIG_DIR
        # users must get their real settings, not the stale home copy (#198).
        decoy = home / ".claude"
        decoy.mkdir(parents=True, exist_ok=True)
        (decoy / "settings.json").write_text(json.dumps({"autoCompactWindow": 300000}))
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    run_session(machine, h, env, project, transcript)
    fill = _quality_cache(machine, h)
    assert fill["model_context_window"] == 1_000_000
    assert abs(fill["fill_pct"] - want_fill) < 0.5, fill
    assert bool(fill.get("compact_window_reduced")) is reduced, fill
    assert abs(fill["model_fill_pct"] - 45.0) < 0.5, fill
    if reduced:
        assert fill["compact_window"] == 600000, fill
        # What the user reads (#249): the % is named as a share of the 600k
        # compact window, with the model-window share beside it.
        sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))
        import measure  # noqa: E402  (pure label renderer; no state)
        note = measure._format_window_note(fill)
        assert "600k compact window" in note and "45% of 1M model window" in note, note


# --------------------------------------------------------------------------- #
# 5. The skill/command shell preamble (#120 #57): what the MODEL's Bash tool runs
# --------------------------------------------------------------------------- #
# /token-optimizer:quick, :health and the token-coach / fleet-auditor /
# token-dashboard skills open with a bash block that picks the runtime and then
# EXPORTS TOKEN_OPTIMIZER_RUNTIME, which overrides every Python-side signal.
# The old test (test_command_runtime_detection.py) only checked the text order
# of two `[ -n ...` snippets. These run the block.

PREAMBLE_FILES = (
    "commands/quick.md",
    "commands/health.md",
    "skills/token-coach/SKILL.md",
    "skills/fleet-auditor/SKILL.md",
    "skills/token-dashboard/SKILL.md",
)

# The Bash-tool env of a live Claude Code 2.1.303 session (2026-10-19) and the
# #120 dump: no CLAUDE_PLUGIN_ROOT / CLAUDE_PLUGIN_DATA in a Bash tool call.
CC_BASH_TOOL_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": SID,
                    "CLAUDE_CODE_CHILD_SESSION": "1", "AI_AGENT": "claude-code_2-1-303_agent"}

# name -> (env, dirs that exist under HOME, expected runtime)
BASH_TOOL_HOSTS = {
    # #120: Claude Code with a leftover ~/.codex (and OpenCode) on disk.
    "claude_bash_tool_codex_on_disk": (CC_BASH_TOOL_ENV, (".codex", ".config/opencode", ".claude/projects"), "claude"),
    "claude_hook_env": ({"CLAUDE_PLUGIN_ROOT": "{root}/plugin", "CLAUDECODE": "1"}, (".codex",), "claude"),
    # Codex's own Bash tool: no Claude env; Codex is identified by its home.
    "codex_bash_tool": ({}, (".codex", ".claude/projects"), "codex"),
    "codex_home_set": ({"CODEX_HOME": "{home}/.codex"}, (".codex", ".claude/projects"), "codex"),
    # Codex launched from a Claude Code Bash tool inherits CLAUDECODE=1.
    "codex_nested_in_claude": ({"CODEX_HOME": "{home}/.codex", "CLAUDECODE": "1"}, (".codex",), "codex"),
    # #57: OpenCode on a host that also has Claude Code (no Codex).
    "opencode_beside_claude": ({}, (".config/opencode", ".claude/projects"), "opencode"),
    "opencode_env_signal": ({"OPENCODE_CONFIG_DIR": "{home}/.config/opencode"},
                            (".config/opencode", ".claude/projects", ".codex"), "opencode"),
    # Harnesses Python names by their config-dir env. The preamble exports its
    # answer as TOKEN_OPTIMIZER_RUNTIME, which overrides Python, so it must give
    # the same name (it used to say codex/claude here).
    "copilot_home": ({"COPILOT_HOME": "{home}/.copilot"}, (".copilot", ".claude/projects"), "copilot"),
    "copilot_home_codex_on_disk": ({"COPILOT_HOME": "{home}/.copilot"}, (".copilot", ".codex"), "copilot"),
    "copilot_nested_in_claude": ({"COPILOT_HOME": "{home}/.copilot", "CLAUDECODE": "1"},
                                 (".copilot", ".claude/projects"), "copilot"),
    "copilot_to_override": ({"TOKEN_OPTIMIZER_COPILOT_HOME": "{home}/.copilot"}, (".copilot", ".codex"), "copilot"),
    "hermes_home_codex_on_disk": ({"HERMES_HOME": "{home}/.hermes"}, (".hermes", ".codex"), "hermes"),
    "hermes_nested_in_claude": ({"HERMES_HOME": "{home}/.hermes", "CLAUDECODE": "1"},
                                (".hermes", ".claude/projects", ".codex"), "hermes"),
    "cursor_to_home": ({"TOKEN_OPTIMIZER_CURSOR_HOME": "{home}/.cursor"}, (".cursor", ".codex"), "cursor"),
    "antigravity_to_home": ({"TOKEN_OPTIMIZER_ANTIGRAVITY_HOME": "{home}/.gemini/antigravity"},
                            (".gemini/antigravity", ".codex"), "antigravity"),
    "grok_home": ({"GROK_HOME": "{home}/.grok", "CLAUDECODE": "1"}, (".grok", ".codex"), "grok"),
}


def extract_preamble(rel: str) -> str:
    """The runtime block, de-indented (two files nest it under a list item)."""
    lines = (REPO / rel).read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == 'RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"')
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = []
    for line in lines[start:]:
        out.append(line[indent:])
        if line.strip() == "fi" and len(line) - len(line.lstrip()) == indent:
            break
    return "\n".join(out) + "\n"


def bash_tool_machine(root: Path, name: str) -> tuple[dict, Path]:
    env_extra, dirs, _want = BASH_TOOL_HOSTS[name]
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    for d in dirs:
        (home / d).mkdir(parents=True, exist_ok=True)
        (home / d / "state.json").write_text("{}")
    if ".claude/projects" in dirs:
        (home / ".claude" / "settings.json").write_text("{}")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1"}
    env.update({k: v.format(home=home, root=root) for k, v in env_extra.items()})
    return env, home


@pytest.mark.parametrize("rel", PREAMBLE_FILES)
@pytest.mark.parametrize("name", list(BASH_TOOL_HOSTS))
def test_skill_preamble_picks_the_running_harness(tmp_path, name, rel):
    env, home = bash_tool_machine(tmp_path, name)
    proc = subprocess.run(["bash", "-c", extract_preamble(rel) + 'printf %s "$RUNTIME"'],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == BASH_TOOL_HOSTS[name][2], f"{rel} under {name}: {proc.stdout!r}"


# Rows with no env signal at all: there the preamble's directory heuristic is
# the only signal (Python, with the process scan off, has nothing to go on), so
# its answer is the one that should win. Every other row must agree.
DIR_HEURISTIC_ONLY = {"codex_bash_tool", "opencode_beside_claude"}


@pytest.mark.parametrize("name", [n for n in BASH_TOOL_HOSTS if n not in DIR_HEURISTIC_ONLY])
def test_skill_preamble_agrees_with_python_detector(tmp_path, name):
    """The preamble's RUNTIME is exported and wins over Python, so wherever
    the host gives an env signal the two must name the same harness
    (Copilot/Hermes used to differ)."""
    env, home = bash_tool_machine(tmp_path, name)
    got = probe(env, tmp_path, REPO / "skills" / "token-optimizer" / "scripts")
    assert got["runtime"] == BASH_TOOL_HOSTS[name][2], (name, got)


def test_python_detection_in_a_claude_bash_tool_ignores_codex_on_disk(tmp_path):
    """#120, Python side: `python3 measure.py ...` typed in a Claude Code Bash
    tool (no plugin env, ~/.codex on disk) must still be Claude."""
    env, home = bash_tool_machine(tmp_path, "claude_bash_tool_codex_on_disk")
    got = probe(env, tmp_path, REPO / "skills" / "token-optimizer" / "scripts")
    assert got["runtime"] == "claude" and Path(got["home"]) == (home / ".claude").resolve(), got


def test_quick_command_end_to_end_in_a_claude_bash_tool(machine):
    """#120 as the user hit it: /token-optimizer:quick run by the model's Bash
    tool on a host with ~/.codex reported a Codex window. Runs the command's
    own preamble and run line against an installed plugin."""
    h = HOSTS["cli"]
    env, home, project, plugin = materialize(machine, h)
    for k in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "AI_AGENT"):
        env.pop(k, None)
    env.update(CC_BASH_TOOL_ENV)
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    codex_before = {p: p.read_bytes() for p in (home / ".codex").rglob("*") if p.is_file()}
    script = extract_preamble("commands/quick.md")
    md = (REPO / "commands" / "quick.md").read_text(encoding="utf-8")
    resolve = md[md.index("MEASURE_PY=\"\"; TO_LAUNCHER"):md.index('export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"')]
    script += resolve + 'export TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"\n'
    script += 'TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" bash "$TO_LAUNCHER" "$MEASURE_PY" quick --json\n'
    proc = subprocess.run(["bash", "-c", script], env=env, cwd=str(project), capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert out["context_window"] == 1_000_000, out.get("context_source")
    assert "codex" not in str(out.get("context_source", "")).lower(), out["context_source"]
    assert {p: p.read_bytes() for p in (home / ".codex").rglob("*") if p.is_file()} == codex_before


# --------------------------------------------------------------------------- #
# 6. #140: another plugin's CLAUDE_PLUGIN_DATA leaks into the Bash tool
# --------------------------------------------------------------------------- #

def test_archived_result_expands_when_a_foreign_plugin_data_leaked_in(machine):
    """#140 as the user hit it: the PostToolUse hook archived a big result and
    told the model to run `measure.py expand <id>`. The model's Bash tool had a
    foreign CLAUDE_PLUGIN_DATA (another plugin appended its own to
    CLAUDE_ENV_FILE), the CLI looked in that plugin's dir and answered
    "Tool result not found" while the re-fetch guard blocked a re-run."""
    h = HOSTS["cli"]
    env, home, project, plugin = materialize(machine, h)
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    run_session(machine, h, env, project, transcript)
    foreign = home / ".claude" / "plugins" / "data" / "other-plugin-othermarket"
    (foreign / "data").mkdir(parents=True)
    bash_env = {k: v for k, v in env.items() if k not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "AI_AGENT")}
    bash_env.update(CC_BASH_TOOL_ENV)
    bash_env["CLAUDE_PLUGIN_DATA"] = str(foreign)
    measure_py = plugin / "skills" / "token-optimizer" / "scripts" / "measure.py"
    proc = subprocess.run(["bash", str(plugin / "hooks" / "python-launcher.sh"), str(measure_py),
                           "expand", "toolu_01FINGERPRINT"], env=bash_env, cwd=str(project),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "not found" not in (proc.stdout + proc.stderr).lower(), \
        (proc.stdout[-500:], proc.stderr[-500:])
    # Hand-built input: 500 paths, the last one is module_499.py.
    assert "module_499.py" in proc.stdout and "module_0.py" in proc.stdout


# --------------------------------------------------------------------------- #
# 7. #73 (non-English locale) and #192 (Claude Desktop WSL 2 launcher):
#    `health` against a process table shaped like the reporters' machines
# --------------------------------------------------------------------------- #
# A fake `ps` stands in for the OS, nothing inside Token Optimizer is stubbed:
# the CLI really spawns `ps`, really parses its output. Like real ps, the fake
# prints lstart in the caller's locale (#73 quoted the he_IL.UTF-8 form) unless
# LC_ALL/LC_TIME is C. The table holds one terminal CLI session and the #192
# Claude Desktop WSL 2 tree (systemd -> server --serve -> ccd-cli/<version>),
# with PIDs above any real pid_max so /proc lookups find nothing.

FAKE_PS = r'''#!/bin/sh
H="$PSHOME"
case "$LC_ALL$LC_TIME" in
  *C*) L1="Mon Oct 19 09:00:00 2026"; L2="Mon Oct 19 09:30:00 2026" ;;
  *)   L1="אוק 19 2026 09:00:00";     L2="אוק 19 2026 09:30:00" ;;
esac
case "$*" in
  *lstart,etime,command*)
    echo "    PID   PPID   UID TTY      STARTED                      ELAPSED COMMAND"
    echo "9100100      1 $PSUID ttys001  $L1     01:00:00 /bin/zsh -l"
    echo "9100101 9100100 $PSUID ttys001  $L1     01:00:00 claude --model opus"
    echo "9100200      1 $PSUID ??       $L2        30:00 $H/.claude/remote/srv/abc123/server --serve --socket $H/.claude/remote/run/x/sock"
    echo "9100201 9100200 $PSUID ??       $L2        30:00 $H/.claude/remote/ccd-cli/2.1.271 --output-format stream-json --verbose --input-format stream-json --effort high --model opus"
    ;;
  *pid,ppid,comm*)
    echo "    PID   PPID COMM"
    echo "9100100      1 /bin/zsh"
    echo "9100101 9100100 claude"
    echo "9100200      1 $H/.claude/remote/srv/abc123/server"
    echo "9100201 9100200 $H/.claude/remote/ccd-cli/2.1.271"
    ;;
esac
exit 0
'''


@pytest.mark.parametrize("locale_name", ["en_US.UTF-8", "he_IL.UTF-8"])
def test_health_sees_terminal_and_desktop_wsl_sessions_in_any_locale(machine, locale_name):
    home = machine.path("home/alex")
    (home / ".claude").mkdir(parents=True)
    bindir = machine.root / "fakebin"
    bindir.mkdir()
    (bindir / "ps").write_text(FAKE_PS, encoding="utf-8")
    (bindir / "ps").chmod(0o755)
    env = machine.base_env(home)
    env.update({"PATH": f"{bindir}:{env['PATH']}", "PSHOME": str(home), "PSUID": str(os.getuid()),
                "LANG": locale_name, "LC_ALL": locale_name})
    measure_py = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"
    proc = subprocess.run(["bash", str(REPO / "hooks" / "python-launcher.sh"), str(measure_py), "health"],
                          env=env, cwd=str(home), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    out = proc.stdout
    assert "RUNNING SESSIONS (2)" in out, out
    assert "PID 9100101" in out, out          # the terminal CLI session
    assert "PID 9100201" in out, out          # the Claude Desktop WSL 2 session (#192)


# --------------------------------------------------------------------------- #
# 8. #93: a container whose runtime home is a PARENT of $HOME (Umbrel)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("opt_in,want_home", [("1", "opt/data"), ("", "opt/data/home/.hermes")])
def test_umbrel_hermes_home_above_home(tmp_path, opt_in, want_home):
    """#93's exact layout: HERMES_HOME=/opt/data, HOME=/opt/data/home. With the
    documented opt-in TO uses /opt/data; without it, the guarded default."""
    data = tmp_path / "opt" / "data"
    home = data / "home"
    (home / ".hermes").mkdir(parents=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
           "HERMES_HOME": str(data)}
    if opt_in:
        env["TOKEN_OPTIMIZER_ALLOW_UNSAFE_RUNTIME_HOME"] = opt_in
    got = probe(env, home, REPO / "skills" / "token-optimizer" / "scripts")
    assert got["runtime"] == "hermes", got
    assert Path(got["home"]) == (tmp_path / want_home).resolve(), got


# --------------------------------------------------------------------------- #
# 9. Checkpoint git label: independent of repo size and branch-name script
# --------------------------------------------------------------------------- #
# git's --short abbreviation grows with the object count, so a test that
# compared against `git rev-parse --short HEAD` of THIS repo passed in CI's
# shallow clone and failed in a full one (2026-10-19, fb2dadee). Here a tiny
# repo is told to abbreviate to 12 (core.abbrev), which is what a large repo
# does on its own, so the label rule is checked the same way everywhere.

def test_checkpoint_git_label_ignores_abbrev_length_and_keeps_unicode_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_CONFIG_NOSYSTEM": "1", "LANG": "en_US.UTF-8"}
    def run(*args):
        return subprocess.run([*git, *args], cwd=str(repo), env=env, capture_output=True, text=True,
                              check=True).stdout.strip()
    run("init", "-q", "-b", "פיצר/login")
    run("config", "core.abbrev", "12")
    (repo / "a.txt").write_text("x")
    run("add", "a.txt")
    run("commit", "-q", "-m", "init")
    head = run("rev-parse", "HEAD")
    assert len(run("rev-parse", "--short", "HEAD")) == 12   # the big-repo condition holds
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import measure;"
            "print(json.dumps(measure._capture_git_state(cwd=sys.argv[2]), ensure_ascii=False))")
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(REPO / "skills" / "token-optimizer" / "scripts"),
                           str(repo)], env={**env, "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1"},
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    branch, sha = json.loads(proc.stdout.strip().splitlines()[-1])
    assert branch == "פיצר/login"
    assert sha == head[:7]


# --------------------------------------------------------------------------- #
# 10. What the host reads back: envelopes name the firing event (#176 #162
#     #177), and a worktree-isolated session's Bash commands are left alone (#141)
# --------------------------------------------------------------------------- #

def _envelopes(stdouts):
    for raw in stdouts:
        raw = raw.strip()
        if raw:
            yield json.loads(raw)   # one JSON document per hook, or nothing


@pytest.mark.parametrize("name", list(HOSTS))
def test_every_hook_envelope_names_the_event_that_fired_it(machine, name):
    """Claude Code rejects a hook whose hookEventName differs from the firing
    event ("expected 'UserPromptSubmit' but got 'SessionStart'"). #176 got
    there through a host misdetection, #162 through a static runtime check."""
    h = HOSTS[name]
    env, home, project, plugin = materialize(machine, h)
    transcript = transcript_for(machine, h, project)
    # 800k of 1M: high enough that the prompt hook speaks (a context nudge),
    # so the UserPromptSubmit envelope is really exercised.
    write_transcript(transcript, project, tokens=800_000)
    outs = run_session(machine, h, env, project, transcript)
    seen = set()
    for event, stdouts in outs.items():
        for doc in _envelopes(stdouts):
            hso = doc.get("hookSpecificOutput")
            if hso is not None:
                seen.add(event)
                assert hso.get("hookEventName") == event, (event, doc)
    # Positive control: both a tool hook and the prompt hook printed one.
    # Cowork's prompt hook answers this fill with a user-facing systemMessage
    # (no event name); the Codex fixture's rollout carries no token counts, so
    # its prompt hook has no fill to report.
    want = {"PostToolUse"} if name in ("cowork", "codex_plugin") else {"PostToolUse", "UserPromptSubmit"}
    assert want <= seen, f"{name}: envelopes seen only for {sorted(seen)}"
    # Nothing a hook says tells the user to drive another harness's UI.
    if h.runtime != "claude":
        said = "".join(o for outs_ in outs.values() for o in outs_)
        assert "In Claude Code" not in said, said[:400]
        assert "/plugin  ->" not in said, said[:400]


@pytest.mark.parametrize("name", ["cli", "cli_config_dir", "cli_unicode_home"])
def test_worktree_isolated_session_bash_commands_pass_through(machine, name):
    """#141: Claude Code's worktree isolation guard refuses any Bash command it
    cannot statically verify, so a rewritten command fails in a session whose
    cwd is under .claude/worktrees/. The PreToolUse Bash hook must answer
    nothing there (it is also never allowed to approve a command)."""
    h = HOSTS[name]
    env, home, project, plugin = materialize(machine, h)
    wt = project / ".claude" / "worktrees" / "feat-x"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {project}/.git/worktrees/feat-x\n")
    payload = {"session_id": SID, "transcript_path": str(transcript_for(machine, h, project)), "cwd": str(wt),
               "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "toolu_01WT",
               "tool_input": {"command": "ls src"}}
    cmds = claude_hook_commands("PreToolUse", "Bash")
    assert cmds, "hooks.json has no PreToolUse Bash entry: nothing to check"
    for cmd in cmds:
        proc = run_shell(cmd, env, wt, payload)
        assert proc.returncode == 0, proc.stderr[-500:]
        assert proc.stdout.strip() == "", proc.stdout


# --------------------------------------------------------------------------- #
# 11. Installed layout != repo layout: Copilot / Cursor / Antigravity / Grok
# --------------------------------------------------------------------------- #
# These installers copy a hand-kept list of modules next to the hook bridge.
# In the repo every module sits in one directory, so a module missing from the
# list imports fine in every repo-based test and fails only on users' machines
# (#148 was this for Hermes). The bridges catch the ImportError and degrade
# silently: Copilot shipped without spawn_utils / hook_runtime (Cursor and Grok
# had already been fixed), so every Copilot hook logged "spawn_utils import
# failed" and its Stop-hook spawn did not detach.

SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
INSTALLERS = ("copilot_install", "cursor_install", "antigravity_install", "grok_install")

_IMPORT_PROBE = r'''
import builtins, importlib, json, sys
local = set(json.loads(sys.argv[2]))
missing = set()
real = builtins.__import__
def tracking_import(name, *a, **k):
    try:
        return real(name, *a, **k)
    except ImportError:
        if name.split(".")[0] in local:
            missing.add(name.split(".")[0])
        raise
builtins.__import__ = tracking_import
sys.path.insert(0, sys.argv[1])
for mod in json.loads(sys.argv[3]):
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.add(mod + ": " + type(exc).__name__)
print(json.dumps(sorted(missing)))
'''


def _payload(installer: str) -> tuple:
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import importlib;"
            "print(json.dumps(importlib.import_module(sys.argv[2])._PAYLOAD_MODULES))")
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS), installer], capture_output=True,
                          text=True, timeout=60, env={"HOME": os.environ.get("HOME", "/nonexistent"),
                                                      "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr[-500:]
    return tuple(json.loads(proc.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("installer", INSTALLERS)
def test_installed_bridge_payload_imports_without_the_repo(tmp_path, installer):
    """Import every shipped module from a directory holding exactly what the
    installer ships, with nothing else on sys.path, and record any Token
    Optimizer module that failed to import (caught or not)."""
    payload = _payload(installer)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    for name in payload:
        shutil.copy2(SCRIPTS / name, plugin / name)
    local = sorted(p.stem for p in SCRIPTS.glob("*.py"))
    proc = subprocess.run([sys.executable, "-I", "-c", _IMPORT_PROBE, str(plugin), json.dumps(local),
                           json.dumps([n[:-3] for n in payload])], capture_output=True, text=True,
                          timeout=120, cwd=str(tmp_path),
                          env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1"})
    assert proc.returncode == 0, proc.stderr[-500:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [], (installer, proc.stdout, proc.stderr[-300:])


def _py_trust(path: str):
    """The product's own interpreter gate (py_trust), run out of process so the
    test never imports product modules into pytest."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import py_trust;"
            "print(json.dumps(py_trust.py_trust_reason(sys.argv[2])))")
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS), path],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-500:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _owner_only_venv(where: Path) -> tuple[str, dict]:
    """A `venv --copies` interpreter the current user owns, mode 0755 in a
    0700 dir: what a user on a host with a world-writable system Python (the
    GitHub hosted toolcache) points TOKEN_OPTIMIZER_PYTHON at. Returns the
    interpreter and any extra env it needs to start (a shared-libpython build
    copied out of its tree needs LD_LIBRARY_PATH to find libpython)."""
    import sysconfig
    subprocess.run([sys.executable, "-m", "venv", "--copies", "--without-pip", str(where)],
                   check=True, capture_output=True, timeout=120)
    os.chmod(where, 0o700)
    py = where / "bin" / "python3"
    for f in (where / "bin").iterdir():
        if f.is_file() and not f.is_symlink():
            os.chmod(f, 0o755)
    extra = {}
    if subprocess.run([str(py), "-c", "pass"], capture_output=True, timeout=60).returncode != 0:
        libdir = sysconfig.get_config_var("LIBDIR") or ""
        extra = {"LD_LIBRARY_PATH": libdir, "DYLD_LIBRARY_PATH": libdir}
        probe_env = {**os.environ, **extra}
        assert subprocess.run([str(py), "-c", "pass"], env=probe_env, capture_output=True,
                              timeout=60).returncode == 0, "venv --copies interpreter cannot start"
    return str(py), extra


def _trusted_python(where: Path) -> tuple[str, dict]:
    """sys.executable when the product trusts it, else an owner-only venv."""
    if _py_trust(sys.executable) is None:
        return sys.executable, {}
    py, extra = _owner_only_venv(where)
    assert _py_trust(py) is None, _py_trust(py)
    return py, extra


def _copilot_install(env: dict, project: Path) -> subprocess.CompletedProcess:
    return subprocess.run([env["TOKEN_OPTIMIZER_PYTHON"], str(SCRIPTS / "measure.py"), "copilot-install"],
                          env={**env, "TOKEN_OPTIMIZER_RUNTIME": "copilot"}, cwd=str(project),
                          capture_output=True, text=True, timeout=120)


def test_copilot_install_refuses_a_world_writable_interpreter(machine):
    """The security gate behind the Copilot hook: a group/world-writable
    python is never baked into the hooks file, and the refusal says why.
    (GitHub's hosted toolcache Python is world-writable, so CI hits this for
    real; here it is planted on every host.)"""
    home = machine.path("home/alex")
    (home / ".copilot").mkdir(parents=True)
    project = machine.path("home/alex/work/app")
    project.mkdir(parents=True)
    py, extra = _owner_only_venv(machine.path("ww-venv"))
    for f in Path(py).parent.iterdir():
        if f.is_file() and not f.is_symlink():
            os.chmod(f, 0o777)
    real = os.path.realpath(py)
    env = {**machine.base_env(home), **extra, "TOKEN_OPTIMIZER_PYTHON": py,
           # Keep every fallback candidate untrusted too, so only the gate decides.
           "PATH": f"{Path(py).parent}:{machine.base_env(home)['PATH']}"}
    proc = subprocess.run([py, str(SCRIPTS / "measure.py"), "copilot-install"],
                          env={**env, "TOKEN_OPTIMIZER_RUNTIME": "copilot"}, cwd=str(project),
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-800:]
    assert "no trusted python interpreter found" in out and f"{real} is world-writable" in out, out[-800:]
    assert not (home / ".copilot" / "hooks" / "token-optimizer.json").exists()


def test_copilot_install_then_hooks_run_clean_and_leave_claude_alone(machine):
    """Copilot CLI on a machine that also has Claude Code: install through the
    real CLI, then run the hook commands Copilot reads from its hooks file.
    Uses an interpreter the product's gate trusts: sys.executable, or on a
    host whose system Python is world-writable (GitHub runners) an owner-only
    venv passed as TOKEN_OPTIMIZER_PYTHON, the documented escape hatch."""
    home = machine.path("home/alex")
    (home / ".claude" / "projects").mkdir(parents=True)
    claude_settings = home / ".claude" / "settings.json"
    claude_settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "~/.claude/s.sh"}}))
    (home / ".copilot").mkdir()
    project = machine.path("home/alex/work/app")
    project.mkdir(parents=True)
    py, extra = _trusted_python(machine.path("venv"))
    env = {**machine.base_env(home), **extra, "TOKEN_OPTIMIZER_PYTHON": py}
    proc = _copilot_install(env, project)
    assert proc.returncode == 0, proc.stderr[-800:]
    hooks = json.loads((home / ".copilot" / "hooks" / "token-optimizer.json").read_text())["hooks"]
    # The hook bakes in exactly the trusted interpreter, never a PATH lookup.
    assert os.path.realpath(py) in hooks["sessionEnd"][0]["bash"], hooks["sessionEnd"][0]["bash"]
    before = machine.files()
    settings_before = claude_settings.read_bytes()
    sid = "c0b1107a-0000-4000-8000-000000000001"
    payloads = {
        "sessionStart": {"sessionId": sid, "cwd": str(project), "timestamp": 1760860800000, "source": "new"},
        "postToolUse": {"sessionId": sid, "cwd": str(project), "toolName": "bash",
                        "toolArgs": json.dumps({"command": "find . -name '*.py'"}),
                        "toolResult": {"resultType": "success",
                                       "textResultForLlm": "\n".join(f"./m{i}.py" for i in range(500))}},
        "sessionEnd": {"sessionId": sid, "cwd": str(project), "timestamp": 1760860900000, "reason": "complete"},
    }
    for event, payload in payloads.items():
        for entry in hooks[event]:
            res = run_shell(entry["bash"], env, project, payload)
            assert res.returncode == 0, (event, res.stderr[-500:])
            assert "import failed" not in res.stderr, (event, res.stderr[-500:])
    new = machine.files() - before
    assert claude_settings.read_bytes() == settings_before
    assert not [f for f in new if f.startswith("home/alex/.claude/")], sorted(new)
    assert any(f.startswith("home/alex/.copilot/token-optimizer/") for f in new), sorted(new)


# --------------------------------------------------------------------------- #
# 12. #57: OpenCode on a machine that also has Claude Code
# --------------------------------------------------------------------------- #
# OpenCode loads ~/.claude/skills, so Token Optimizer's skill runs inside it.
# The ground-truth signal is an `opencode` ancestor process; here a real one:
# a shell started under the name `opencode` (a symlink to /bin/sh, so ps shows
# `.../opencode -c ...` like the installed binary). The trailing `; :` keeps
# the shell alive as the parent instead of exec-ing the command. A stray
# CLAUDE_PLUGIN_ROOT (KTD-3 in runtime_env) must not pull it back to Claude.

def test_opencode_session_beside_claude_code_leaves_claude_untouched(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "x"}}))
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text("{}")
    launcher = tmp_path / "bin" / "opencode"
    launcher.parent.mkdir()
    launcher.symlink_to("/bin/sh")
    env = {"HOME": str(home), "PATH": f"{PY_DIR}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "CLAUDE_PLUGIN_ROOT": str(tmp_path / "stray-claude-plugin")}
    claude_before = {p: p.read_bytes() for p in (home / ".claude").rglob("*") if p.is_file()}
    scripts = REPO / "skills" / "token-optimizer" / "scripts"
    detect = ("import json, sys; sys.path.insert(0, sys.argv[1]); import runtime_env as r;"
              "print(json.dumps([r.detect_runtime(), str(r.runtime_home())]))")
    # Positive control: the same process WITHOUT the opencode parent is Claude.
    direct = subprocess.run([sys.executable, "-I", "-c", detect, str(scripts)], env=env,
                            capture_output=True, text=True, timeout=60)
    assert json.loads(direct.stdout.strip().splitlines()[-1])[0] == "claude", direct.stderr
    under = subprocess.run([str(launcher), "-c", '"$@"; :', "opencode", sys.executable, "-I", "-c", detect,
                            str(scripts)], env=env, capture_output=True, text=True, timeout=60)
    runtime, rt_home = json.loads(under.stdout.strip().splitlines()[-1])
    assert runtime == "opencode", under.stderr
    assert not Path(rt_home).resolve().is_relative_to((home / ".claude").resolve())
    # The skill's own entry point, run by the model inside OpenCode.
    quick = subprocess.run([str(launcher), "-c", '"$@"; :', "opencode", sys.executable,
                            str(scripts / "measure.py"), "quick", "--json"], env=env, cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=120)
    assert "OpenCode" in quick.stdout + quick.stderr, (quick.stdout[-400:], quick.stderr[-400:])
    assert {p: p.read_bytes() for p in (home / ".claude").rglob("*") if p.is_file()} == claude_before


# --------------------------------------------------------------------------- #
# Repairing users the Codex-plugin bug already hit: Claude Code's statusLine
# pointing into ~/.codex/plugins/cache/... (or at a statusline.js that is gone)
# --------------------------------------------------------------------------- #

SL_JS = "skills/token-optimizer/scripts/statusline.js"
CODEX_CACHE_SL = "{home}/.codex/plugins/cache/alexgreensh-token-optimizer/token-optimizer/{ver}/" + SL_JS
# A script (install.sh) install: not under /plugins/cache/, so the old
# cache-root heal never applied to it.
SCRIPT_INSTALL = dataclasses.replace(HOSTS["cli"], name="cli_script_install",
                                     plugin="home/alex/.claude/token-optimizer", marketplace_install=False)


def _seed_statusline(home: Path, command: str) -> tuple[Path, dict]:
    sp = home / ".claude" / "settings.json"
    s = json.loads(sp.read_text())
    s.update({"theme": "dark", "env": {"FOO": "bar"}, "permissions": {"allow": ["Bash(ls:*)"]},
              "statusLine": {"type": "command", "command": command, "padding": 0}})
    sp.write_text(json.dumps(s, indent=2))
    return sp, s


def _sl_session(machine, h, command_fn, make_file=None):
    env, home, project, plugin = materialize(machine, h)
    command = command_fn(home=home, plugin=plugin)
    if make_file:
        target = Path(make_file.format(home=home, ver="5.13.49"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// some statusline\n")
    sp, before = _seed_statusline(home, command)
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    run_session(machine, h, env, project, transcript)
    return home, plugin, before, json.loads(sp.read_text())


def _assert_healed(home, plugin, before, after):
    cmd = after["statusLine"]["command"]
    path = re.search(r"'([^']*statusline\.js)'", cmd).group(1)
    assert Path(path).is_file(), cmd
    assert ".codex" not in path and Path(path).resolve().is_relative_to((home / ".claude").resolve()), cmd
    assert cmd.startswith("node '") and cmd.endswith("'"), cmd
    # Every other key, and every other statusLine key, unchanged.
    # (SessionStart's own unrelated heals may ADD keys such as cleanupPeriodDays.)
    assert {k: after.get(k) for k in before if k != "statusLine"} == \
        {k: v for k, v in before.items() if k != "statusLine"}
    assert {k: v for k, v in after["statusLine"].items() if k != "command"} == \
        {k: v for k, v in before["statusLine"].items() if k != "command"}


@pytest.mark.parametrize("h", [HOSTS["cli"], SCRIPT_INSTALL], ids=["plugin_cache", "script_install"])
def test_claude_session_repoints_statusline_from_codex_cache(machine, h):
    """Bug 1's victims: the Codex copy still exists (Codex hasn't updated yet),
    so nothing looks broken until Codex prunes it. Re-point it now."""
    home, plugin, before, after = _sl_session(
        machine, h, lambda home, plugin: "node '" + CODEX_CACHE_SL.format(home=home, ver="5.13.49") + "'",
        make_file=CODEX_CACHE_SL)
    _assert_healed(home, plugin, before, after)


@pytest.mark.parametrize("h", [HOSTS["cli"], SCRIPT_INSTALL], ids=["plugin_cache", "script_install"])
def test_claude_session_repoints_statusline_to_a_deleted_file(machine, h):
    home, plugin, before, after = _sl_session(
        machine, h, lambda home, plugin: f"node '{home}/old-clone/token-optimizer/{SL_JS}'")
    _assert_healed(home, plugin, before, after)


@pytest.mark.parametrize("command_fn,make_file", [
    # A dev clone of Token Optimizer that exists: the user's choice.
    (lambda home, plugin: f"node '{home}/dev/token-optimizer/{SL_JS}'", "{home}/dev/token-optimizer/" + SL_JS),
    # Another tool's status line.
    (lambda home, plugin: "npx -y ccstatusline@latest", None),
    # Someone else's statusline.js that happens to be gone.
    (lambda home, plugin: f"node '{home}/gone/other-tool/statusline.js'", None),
], ids=["dev_clone", "foreign_tool", "foreign_missing_js"])
def test_claude_session_leaves_other_statuslines_alone(machine, command_fn, make_file):
    home, plugin, before, after = _sl_session(machine, HOSTS["cli"], command_fn, make_file)
    assert {k: after.get(k) for k in before} == before


def test_codex_session_never_touches_claude_statusline(machine):
    h = HOSTS["codex_plugin"]
    env, home, project, plugin = materialize(machine, h)
    sp, before = _seed_statusline(
        home, f"node '{home}/.claude/plugins/cache/alexgreensh-token-optimizer/token-optimizer/5.13.40/{SL_JS}'")
    raw = sp.read_bytes()
    transcript = transcript_for(machine, h, project)
    write_transcript(transcript, project)
    run_session(machine, h, env, project, transcript)
    assert sp.read_bytes() == raw


def _doctor(machine, h, command_fn, make_file=None):
    env, home, project, plugin = materialize(machine, h)
    if make_file:
        target = Path(make_file.format(home=home))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// statusline\n")
    _seed_statusline(home, command_fn(home=home, plugin=plugin))
    measure = plugin / "skills" / "token-optimizer" / "scripts" / "measure.py"
    proc = subprocess.run([sys.executable, str(measure), "doctor"], env=env, cwd=str(project),
                          capture_output=True, text=True, timeout=120)
    return [l for l in proc.stdout.splitlines() if "Quality bar" in l]


def test_doctor_flags_a_status_line_running_a_missing_file(machine):
    lines = _doctor(machine, HOSTS["cli"], lambda home, plugin: f"node '{home}/old-clone/token-optimizer/{SL_JS}'")
    assert lines and "missing file" in lines[0] and "old-clone" in lines[0], lines


def test_doctor_names_another_token_optimizer_checkout(machine):
    lines = _doctor(machine, HOSTS["cli"], lambda home, plugin: f"node '{home}/dev/token-optimizer/{SL_JS}'",
                    "{home}/dev/token-optimizer/" + SL_JS)
    assert lines and "another Token Optimizer copy" in lines[0] and "/dev/token-optimizer/" in lines[0], lines
