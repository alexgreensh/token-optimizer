"""The hook wire contract, driven from the shipped manifests.

WHY THIS FILE EXISTS
--------------------
Users broke on the path

    manifest command string -> /bin/sh -> bash -> python-launcher.sh -> run.py
        -> module_runner.py -> handler -> stdout / exit code

while every unit test of the handlers was green, because those tests call the
handler (or at best the launcher) directly. The escapes:

  #177 #162  hookEventName on the wire did not match the firing event
  #81  #76   Codex needs stdout that is empty or ONE JSON object
  #83        Codex skips (and warns about) async hooks
  #80        PATH without bash -> exit 127 on every tool call
  #92  #75   version-pinned path deleted by an upgrade -> exit 127 / dead hooks
  #145       mise / version-manager python shims rejected -> no interpreter
  #139       18 processes per prompt
  #53        an advisory re-injected into context on every prompt
  #253       the hook "succeeds" but the checkpoint it exists for is not written
  today      `${CLAUDE_PLUGIN_ROOT}` hooks silently never run when the host
             leaves the variable unset (Claude Code cloud)

So every test here starts from the LITERAL command string a host would run:
hooks/hooks.json (Claude Code plugin), plugins/token-optimizer/hooks/hooks.json
(Codex marketplace plugin), cowork/token-optimizer/hooks/hooks.json (Cowork),
the commands codex_install writes into ~/.codex/hooks.json, the settings hooks
the cloud kit writes into the VM's ~/.claude/settings.json, and the Copilot
hook file. Each one runs through ``/bin/sh -c`` with a realistic stdin payload,
a throwaway HOME, and the env the host really provides.

HOW "THE HANDLER REALLY RAN" IS PROVEN
--------------------------------------
Exit 0 with empty stdout is also what a hook that silently did nothing looks
like (that is the cloud bug and #253). So every Python process the hook starts
is traced: a ``sitecustomize.py`` on PYTHONPATH appends its argv to a per-run
trace file. A hook entry counts as having run only when module_runner.py was
started for the script the manifest names (or the Copilot bridge for its
event). The same trace gives the per-event process count (#139).

POSIX only: the commands are POSIX sh. Windows hooks run through Git Bash /
cmd.exe and are covered by tests/test_windows_hook_launcher.py and
tests/test_codex_windows_command.py.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX sh wire; Windows wire lives in test_windows_hook_launcher.py")

SID = "7a1c2e3f-4b5d-4e6f-8a9b-0c1d2e3f4a5b"
OTHER_SID = "0b9a8c7d-6e5f-4a3b-9c2d-1e0f9a8b7c6d"

# Codex's embedded session-start / user-prompt-submit output schema
# (additionalProperties: false at both levels). See
# tests/test_codex_sessionstart_json_contract.py for how it was extracted.
CODEX_TOP_KEYS = {"continue", "hookSpecificOutput", "stopReason", "suppressOutput", "systemMessage"}
CODEX_HSO_KEYS = {"additionalContext", "hookEventName"}
# Claude Code's common hook output keys.
CLAUDE_TOP_KEYS = {"continue", "suppressOutput", "stopReason", "decision", "reason",
                   "systemMessage", "hookSpecificOutput"}
# Events whose plain-text stdout Claude Code consumes (context / compact instructions).
CLAUDE_PLAIN_TEXT_EVENTS = {"SessionStart", "UserPromptSubmit", "PreCompact"}
# Events Codex actually fires (docs/codex.md: no PreCompact / PostCompact).
CODEX_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
                "SubagentStart", "SubagentStop", "PermissionRequest"}


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #

class Entry:
    def __init__(self, harness, event, matcher, hook, idx):
        self.harness = harness
        self.event = event
        self.matcher = matcher
        self.hook = hook
        self.idx = idx
        self.command = hook.get("command") or hook.get("bash") or ""
        self.timeout = float(hook.get("timeout") or hook.get("timeoutSec") or 60)
        self.is_async = bool(hook.get("async"))

    @property
    def id(self):
        m = (self.matcher if isinstance(self.matcher, str) else json.dumps(self.matcher)) or "*"
        return f"{self.harness}:{self.event}:{m[:24]}:{self.idx}"

    def expected_handler(self):
        """(kind, name): what the trace must show for this entry to have run."""
        if self.harness == "copilot":
            return ("copilot", self.command.rsplit(" ", 1)[-1])
        scripts = [s for s in re.findall(r"((?:hooks|skills)/[A-Za-z0-9_./-]+\.py)", self.command)
                   if not s.endswith("hooks/run.py")]
        assert scripts, f"cannot find the dispatched script in {self.command[:200]!r}"
        return ("module", Path(scripts[0]).stem)


def _entries_from(data, harness):
    out = []
    for event, groups in (data.get("hooks") or {}).items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group.get("hooks", [group]) if "hooks" in group else [group]):
                if hook.get("type") != "command":
                    continue
                out.append(Entry(harness, event, group.get("matcher"), hook, f"{gi}.{hi}"))
    return out


def _load(rel):
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


CLAUDE_MANIFEST = "hooks/hooks.json"
CODEX_PLUGIN_ROOT = REPO / "plugins" / "token-optimizer"
CODEX_PLUGIN_MANIFEST = "plugins/token-optimizer/hooks/hooks.json"
COWORK_ROOT = REPO / "cowork" / "token-optimizer"
COWORK_MANIFEST = "cowork/token-optimizer/hooks/hooks.json"


def codex_global_hooks(root: Path) -> dict:
    """The hooks codex_install writes to ~/.codex/hooks.json for a plugin at
    ``root``, every optional group switched on. Generated in a child so the
    module-level _repo_root can be pointed at ``root`` without touching this
    process's import of codex_install."""
    code = (
        "import json, sys, pathlib\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import codex_install as ci\n"
        f"ci._repo_root = lambda: pathlib.Path({str(root)!r})\n"
        "print(json.dumps({'hooks': ci._managed_hooks(enable_bash_compression=True,"
        " enable_hot_path_hooks=True, enable_prompt_hooks=True, enable_subagent_hooks=True)}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def cloud_kit_hooks(runner: Path) -> dict:
    import cloud_kit
    return {"hooks": cloud_kit.generate_hook_map(cloud_kit.load_hooks_json(REPO), runner)}


def write_cloud_runner(cache: Path) -> Path:
    import cloud_kit
    cache.mkdir(parents=True, exist_ok=True)
    runner = cache / cloud_kit.RUNNER_NAME
    runner.write_text(cloud_kit.render_run_sh(REPO, cache / "pycache", "5.13.51"), encoding="utf-8")
    runner.chmod(0o755)
    return runner


def copilot_hooks() -> dict:
    code = (
        "import json, sys, pathlib\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import copilot_install as ci\n"
        f"print(json.dumps(ci._hook_config(pathlib.Path({str(SCRIPTS / 'copilot_hook_bridge.py')!r}))))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- #
# Sandbox: throwaway HOME, realistic transcript, process tracing
# --------------------------------------------------------------------------- #

SITECUSTOMIZE = """\
import os, sys, json
_p = os.environ.get("WIRE_TRACE")
if _p:
    try:
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv}) + "\\n")
    except Exception:
        pass
"""


@pytest.fixture(scope="session")
def wire_shared(tmp_path_factory):
    """Bytecode + interpreter cache shared across tests (users run warm)."""
    root = tmp_path_factory.mktemp("wire-shared")
    site = root / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    cache = root / "xdg-cache"
    cache.mkdir()
    return {"site": site, "cache": cache}


class Run:
    def __init__(self, proc, elapsed, trace):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.elapsed = elapsed
        self.procs = trace

    def ran(self, kind, name):
        for p in self.procs:
            argv = p.get("argv") or []
            if kind == "module":
                if len(argv) >= 3 and str(argv[0]).endswith("module_runner.py") and argv[2] == name:
                    return True
            elif kind == "copilot":
                if argv and str(argv[0]).endswith("copilot_hook_bridge.py") and name in argv[1:2]:
                    return True
        return False

    def __repr__(self):
        return (f"rc={self.rc} elapsed={self.elapsed:.2f}s\nstdout={self.out[:800]!r}\n"
                f"stderr={self.err[-1200:]!r}\nprocs={[p.get('argv') for p in self.procs]}")


class Sandbox:
    def __init__(self, root: Path, shared):
        self.root = root
        self.shared = shared
        self.home = root / "home"
        self.claude = self.home / ".claude"
        self.proj = root / "proj"
        for d in (self.home, self.claude, self.proj):
            d.mkdir(parents=True, exist_ok=True)
        (self.proj / "app.py").write_text("".join(
            f"def handler_{i}(request, retries={i}):\n"
            f"    \"\"\"Route request {i} through the wire contract.\"\"\"\n"
            f"    for attempt in range(retries):\n"
            f"        if request.ok(attempt):\n"
            f"            return request.payload({i})\n"
            f"    raise RuntimeError('handler {i} gave up')\n\n\n" for i in range(80)),
            encoding="utf-8")
        tdir = self.claude / "projects" / "-wire-proj"
        tdir.mkdir(parents=True)
        self.transcript = tdir / f"{SID}.jsonl"
        self.transcript.write_text(self._transcript(SID), encoding="utf-8")
        self._n = 0

    def _transcript(self, sid):
        rows = []
        for i in range(8):
            rows.append({"type": "user", "sessionId": sid, "cwd": str(self.proj),
                         "timestamp": f"2026-10-19T09:{i:02d}:00Z",
                         "message": {"role": "user",
                                     "content": f"fix the wire contract bug {i} in app.py"}})
            rows.append({"type": "assistant", "sessionId": sid, "cwd": str(self.proj),
                         "timestamp": f"2026-10-19T09:{i:02d}:30Z",
                         "message": {"role": "assistant", "model": "claude-opus-5-5",
                                     "content": [
                                         {"type": "text", "text": "Reading app.py now."},
                                         {"type": "tool_use", "id": f"toolu_{i}", "name": "Read",
                                          "input": {"file_path": str(self.proj / "app.py")}}],
                                     "usage": {"input_tokens": 1200, "output_tokens": 300,
                                               "cache_read_input_tokens": 40000,
                                               "cache_creation_input_tokens": 2000}}})
            rows.append({"type": "user", "sessionId": sid, "cwd": str(self.proj),
                         "timestamp": f"2026-10-19T09:{i:02d}:40Z",
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": f"toolu_{i}",
                              "content": "def handler(): ..."}]}})
        return "\n".join(json.dumps(r) for r in rows) + "\n"

    @property
    def checkpoints(self):
        return self.claude / "token-optimizer" / "checkpoints"

    def checkpoints_for(self, harness):
        # codex-install pins TOKEN_OPTIMIZER_RUNTIME=codex: data lives in ~/.codex.
        if harness == "codex-global":
            return self.home / ".codex" / "token-optimizer" / "checkpoints"
        return self.checkpoints

    def codex_rollout(self, sid):
        """A Codex rollout transcript where Codex writes it (~/.codex/sessions):
        the session reads and edits app.py."""
        d = self.home / ".codex" / "sessions" / "2026" / "10" / "19"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"rollout-2026-10-19T09-00-00-{sid}.jsonl"
        if path.exists():
            return path
        app = str(self.proj / "app.py")
        rows = [{"timestamp": "2026-10-19T09:00:00Z", "type": "session_meta",
                 "payload": {"id": sid, "cwd": str(self.proj), "originator": "codex_cli_rs",
                             "cli_version": "0.150.0"}}]
        for i in range(6):
            ts = f"2026-10-19T09:{i:02d}:10Z"
            rows += [
                {"timestamp": ts, "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": f"fix the wire contract bug {i} in app.py"}]}},
                {"timestamp": ts, "type": "response_item",
                 "payload": {"type": "function_call", "name": "exec_command", "call_id": f"call_{i}",
                             "arguments": json.dumps({"cmd": f"cat {app}"})}},
                {"timestamp": ts, "type": "response_item",
                 "payload": {"type": "function_call_output", "call_id": f"call_{i}",
                             "output": "def handler(): ..."}},
                {"timestamp": ts, "type": "response_item",
                 "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": f"patch_{i}",
                             "input": f"*** Begin Patch\n*** Update File: {app}\n@@\n-a\n+b\n*** End Patch"}},
                {"timestamp": ts, "type": "response_item",
                 "payload": {"type": "message", "role": "assistant", "content": [
                     {"type": "output_text", "text": "Fixed the handler in app.py."}]}},
            ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def transcript_for(self, harness, sid=SID):
        # Codex sends its own rollout path, whichever way TO was installed.
        if harness in ("codex-global", "codex-plugin"):
            return self.codex_rollout(sid)
        if sid == SID:
            return self.transcript
        other = self.transcript.parent / f"{sid}.jsonl"
        if not other.exists():
            other.write_text(self._transcript(sid), encoding="utf-8")
        return other

    def env(self, *, plugin_root=None, extra=None, path=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "CODEX", "COPILOT", "AI_AGENT", "PYTHONPYCACHEPREFIX"))
               and not (k.startswith("TOKEN_OPTIMIZER") and k != "TOKEN_OPTIMIZER_TEST_SCHEDULER_LOG")}
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.shared["cache"]),
            "PYTHONPATH": str(self.shared["site"]),
        })
        if plugin_root is not None:
            env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        if path is not None:
            env["PATH"] = path
        env.update(extra or {})
        return env

    def payload(self, event, matcher=None, **over):
        base = {"session_id": SID, "transcript_path": str(self.transcript), "cwd": str(self.proj),
                "permission_mode": "default", "hook_event_name": event}
        m = matcher if isinstance(matcher, str) else ""
        if event == "PreToolUse":
            if m.startswith("Read"):
                base.update(tool_name="Read", tool_input={"file_path": str(self.proj / "app.py")})
            elif m.startswith("Agent"):
                base.update(tool_name="Agent", tool_input={"description": "scan", "prompt": "scan repo",
                                                           "subagent_type": "general-purpose"})
            elif m.startswith("mcp"):
                base.update(tool_name="mcp__docs__search", tool_input={"query": "hooks"})
            else:
                base.update(tool_name="Bash", tool_input={"command": "find . -name '*.py'",
                                                          "description": "list files"})
        elif event in ("PostToolUse", "PostToolUseFailure"):
            listing = "\n".join(f"./src/pkg{i % 9}/module_{i}.py" for i in range(700))
            base.update(tool_name="Bash", tool_input={"command": "find . -name '*.py'"})
            if event == "PostToolUse":
                base["tool_response"] = {"stdout": listing, "stderr": "", "interrupted": False,
                                         "isImage": False}
            else:
                base["error"] = "Exit code 1\nfind: ./private: Permission denied"
        elif event == "SessionStart":
            base.update(source="startup", model="claude-opus-5-5")
        elif event == "UserPromptSubmit":
            base.update(prompt="add a test for the hook wire contract")
        elif event == "PreCompact":
            base.update(trigger="auto", custom_instructions="")
        elif event == "PostCompact":
            base.update(trigger="auto", compact_summary="Worked on the wire contract in app.py.")
        elif event == "Stop":
            base.update(stop_hook_active=False, last_assistant_message="Done with app.py.")
        elif event == "StopFailure":
            base.update(error="rate_limit", error_details="429")
        elif event == "SessionEnd":
            base.update(reason="other")
        elif event == "CwdChanged":
            base.update(old_cwd=str(self.root), new_cwd=str(self.proj))
        elif event in ("SubagentStart", "SubagentStop"):
            base.update(agent_id="agent-1", agent_type="general-purpose")
        base.update(over)
        return base

    def copilot_payload(self, event):
        p = {"sessionId": SID, "cwd": str(self.proj), "timestamp": 1792400000000}
        if event in ("pre-tool-use", "post-tool-use"):
            p.update(toolName="bash", toolArgs=json.dumps({"command": "find . -name '*.py'"}))
        if event == "post-tool-use":
            p["toolResult"] = {"resultType": "success",
                               "textResultForLlm": "\n".join(f"./m{i}.py" for i in range(400))}
        return p

    def fire(self, command, payload, env, *, timeout=120):
        self._n += 1
        trace = self.root / f"trace-{self._n}.jsonl"
        env = dict(env, WIRE_TRACE=str(trace))
        t0 = time.monotonic()
        proc = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload), text=True,
                              capture_output=True, env=env, cwd=str(self.proj), timeout=timeout)
        elapsed = time.monotonic() - t0
        procs = []
        if trace.exists():
            for line in trace.read_text(encoding="utf-8").splitlines():
                try:
                    procs.append(json.loads(line))
                except ValueError:
                    pass
        return Run(proc, elapsed, procs)


@pytest.fixture
def sandbox(tmp_path, wire_shared):
    return Sandbox(tmp_path, wire_shared)


# --------------------------------------------------------------------------- #
# Harness wiring: how each manifest is resolved and what env its host gives
# --------------------------------------------------------------------------- #

def harness_entries(harness, sb: Sandbox):
    if harness == "claude":
        return _entries_from(_load(CLAUDE_MANIFEST), "claude")
    if harness == "codex-plugin":
        return _entries_from(_load(CODEX_PLUGIN_MANIFEST), "codex-plugin")
    if harness == "cowork":
        return _entries_from(_load(COWORK_MANIFEST), "cowork")
    if harness == "cloud-kit":
        runner = write_cloud_runner(sb.home / ".cache" / "token-optimizer-cloud")
        return _entries_from(cloud_kit_hooks(runner), "cloud-kit")
    if harness == "codex-global":
        return _entries_from(codex_global_hooks(REPO), "codex-global")
    if harness == "copilot":
        try:
            return _entries_from(copilot_hooks(), "copilot")
        except AssertionError as exc:
            if "no trusted python interpreter" in str(exc):
                pytest.skip("copilot-install refuses this interpreter (not admin-owned)")
            raise
    raise AssertionError(harness)


def harness_env(harness, sb: Sandbox, **kw):
    if harness == "claude":
        return sb.env(plugin_root=REPO, **kw)
    if harness == "codex-plugin":
        # What Codex gives a plugin hook: CLAUDE_PLUGIN_ROOT, no CODEX_HOME,
        # no runtime hint (tests/test_codex_sessionstart_json_contract.py).
        return sb.env(plugin_root=CODEX_PLUGIN_ROOT, **kw)
    if harness == "cowork":
        extra = {"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_CONTAINER_ID": "wire-container",
                 **(kw.pop("extra", None) or {})}
        return sb.env(plugin_root=COWORK_ROOT, extra=extra, **kw)
    if harness == "cloud-kit":
        # Live cloud VM (FINDINGS P8/P9): CLAUDE_PLUGIN_ROOT is NOT set.
        extra = {"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_wire",
                 "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE": "cloud_default",
                 **(kw.pop("extra", None) or {})}
        return sb.env(extra=extra, **kw)
    if harness == "codex-global":
        return sb.env(**kw)  # ~/.codex/hooks.json hooks get no CLAUDE_PLUGIN_ROOT
    if harness == "copilot":
        return sb.env(**kw)
    raise AssertionError(harness)


def fire_entry(sb: Sandbox, entry: Entry, env, **payload_over):
    if entry.harness == "copilot":
        payload = sb.copilot_payload(entry.expected_handler()[1])
    else:
        payload_over.setdefault("transcript_path", str(sb.transcript_for(entry.harness,
                                                                         payload_over.get("session_id", SID))))
        payload = sb.payload(entry.event, entry.matcher, **payload_over)
    return sb.fire(entry.command, payload, env)


def warm(sb: Sandbox, harness):
    """One SessionStart, the way every real session begins: compiles bytecode
    into the shared cache and records the interpreter, so timing assertions
    measure the steady state users see."""
    for e in harness_entries(harness, sb):
        if e.event in ("SessionStart", "sessionStart"):
            fire_entry(sb, e, harness_env(harness, sb))
            return


# --------------------------------------------------------------------------- #
# Output contract
# --------------------------------------------------------------------------- #

def stdout_problem(harness, event, stdout):
    """None when ``stdout`` is what the host for ``harness`` accepts for ``event``."""
    text = stdout.strip()
    if not text:
        return None
    codex = harness in ("codex-plugin", "codex-global")
    if harness == "copilot":
        try:
            obj = json.loads(text)
        except ValueError as exc:
            return f"Copilot stdout is not one JSON document: {exc}"
        return None if isinstance(obj, dict) else "Copilot stdout is not a JSON object"
    if codex and event not in CODEX_EVENTS:
        # Codex never fires this event (the marketplace manifest mirrors the
        # Claude one); judge it by the Claude contract.
        codex = False
    if not text.startswith(("{", "[")):
        if codex:
            return f"Codex {event} stdout must be empty or JSON, got plain text"
        if event in CLAUDE_PLAIN_TEXT_EVENTS:
            return None
        return f"{event} stdout must be empty or JSON, got plain text"
    try:
        obj = json.loads(text)
    except ValueError as exc:
        return f"stdout is not ONE JSON document ({exc})"
    if not isinstance(obj, dict):
        return "stdout JSON is not an object"
    allowed = CODEX_TOP_KEYS if (codex and event in ("SessionStart", "UserPromptSubmit")) else CLAUDE_TOP_KEYS
    extra = set(obj) - allowed
    if extra:
        return f"unknown top-level keys {sorted(extra)}"
    if "systemMessage" in obj and not isinstance(obj["systemMessage"], str):
        return "systemMessage is not a string"
    if "continue" in obj and not isinstance(obj["continue"], bool):
        return "continue is not a boolean"
    hso = obj.get("hookSpecificOutput")
    if hso is not None:
        if not isinstance(hso, dict):
            return "hookSpecificOutput is not an object"
        if hso.get("hookEventName") != event:
            return f"hookEventName {hso.get('hookEventName')!r} != firing event {event!r}"
        if codex and event in ("SessionStart", "UserPromptSubmit") and set(hso) - CODEX_HSO_KEYS:
            return f"Codex rejects hookSpecificOutput keys {sorted(set(hso) - CODEX_HSO_KEYS)}"
        if "additionalContext" in hso and not isinstance(hso["additionalContext"], str):
            return "additionalContext is not a string"
    return None


def additional_context(stdout):
    text = stdout.strip()
    if not text.startswith("{"):
        return text
    obj = json.loads(text)
    return ((obj.get("hookSpecificOutput") or {}).get("additionalContext") or "") + (
        obj.get("systemMessage") or "")


# --------------------------------------------------------------------------- #
# 1. Every entry of every manifest, normal host env
# --------------------------------------------------------------------------- #

HARNESSES = ["claude", "codex-plugin", "cowork", "cloud-kit", "codex-global", "copilot"]


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_manifest_entry_runs_its_handler_and_honours_the_output_contract(harness, sandbox):
    entries = harness_entries(harness, sandbox)
    assert entries, f"{harness}: manifest produced no command hooks"
    warm(sandbox, harness)
    failures = []
    for e in entries:
        r = fire_entry(sandbox, e, harness_env(harness, sandbox))
        kind, name = e.expected_handler()
        if r.rc != 0:
            failures.append(f"{e.id}: exit {r.rc} (a non-zero hook blocks or errors)\n{r}")
            continue
        problem = stdout_problem(harness, e.event, r.out)
        if problem:
            failures.append(f"{e.id}: {problem}\n{r}")
        if not r.ran(kind, name):
            failures.append(f"{e.id}: exited 0 but {name} never ran (silent no-op)\n{r}")
        if r.elapsed >= e.timeout:
            failures.append(f"{e.id}: took {r.elapsed:.1f}s, the host kills it at {e.timeout:.0f}s")
    assert not failures, "\n\n".join(failures)


def test_manifest_list_is_complete():
    """Guard against a vacuous pass: every shipped hooks.json with command hooks
    (except the Cowork probe plugin) is driven above."""
    driven = {CLAUDE_MANIFEST, CODEX_PLUGIN_MANIFEST, COWORK_MANIFEST}
    shipped = set()
    # Shipped = tracked by git. A local build/ (npm pack staging) or any other
    # untracked copy is not shipped and must not decide this test. Copies
    # without .git (the regression-guard harness) fall back to a scan that
    # skips build output.
    try:
        tracked = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "--", "*hooks.json"],
            capture_output=True, text=True, check=True).stdout.split()
        candidates = [REPO / t for t in tracked]
    except (OSError, subprocess.CalledProcessError):
        candidates = list(REPO.rglob("hooks.json"))
    for p in candidates:
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith("build/") or any(
                part in rel for part in ("node_modules/", "/examples/", "to-hook-probe/", "tests/")):
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if _entries_from(data, "x"):
            shipped.add(rel)
    assert shipped == driven, f"undriven hook manifests: {sorted(shipped - driven)}"
    total = sum(len(_entries_from(_load(rel), "x")) for rel in driven)
    assert total >= 30, f"only {total} command hooks across the shipped manifests"


def pick(entries, event, *, matcher_prefix=None, handler=None):
    for e in entries:
        if e.event != event:
            continue
        m = e.matcher if isinstance(e.matcher, str) else ""
        if matcher_prefix is not None and not m.startswith(matcher_prefix):
            continue
        if handler is not None and handler not in e.command:
            continue
        return e
    raise AssertionError(f"no {event} entry (matcher {matcher_prefix!r}, handler {handler!r})")


def scheduler_shim_dirs():
    """The no-op launchctl/systemctl/schtasks dirs the suite put on PATH. Every
    stripped PATH below keeps them first so no hook can reach a real scheduler."""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and (Path(d).name == "scheduler-shims" or (Path(d) / "schtasks").is_file())]
    # Empty is safe too: with no shim dir on PATH no scheduler binary is reachable.
    return os.pathsep.join(dirs)


# --------------------------------------------------------------------------- #
# 2. CLAUDE_PLUGIN_ROOT unset (Claude Code cloud)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("harness", ["claude", "codex-plugin", "cowork"])
def test_plugin_manifest_without_plugin_root_exits_clean(harness, sandbox):
    """A plugin manifest cannot locate itself without CLAUDE_PLUGIN_ROOT (cloud
    VMs leave it unset). It must then do nothing at all: exit 0, no stdout the
    host would parse, no stderr a host would surface. The cloud route is the
    kit (next test)."""
    failures = []
    for e in harness_entries(harness, sandbox):
        env = harness_env(harness, sandbox)
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = fire_entry(sandbox, e, env)
        if r.rc != 0 or r.out.strip() or r.err.strip():
            failures.append(f"{e.id}\n{r}")
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("harness", ["cloud-kit", "codex-global"])
def test_settings_level_hooks_run_without_plugin_root(harness, sandbox):
    """Hooks written into a settings file (cloud VM ~/.claude/settings.json,
    ~/.codex/hooks.json) never get CLAUDE_PLUGIN_ROOT from the host. Live cloud
    probe P4: a command that leans on ``${CLAUDE_PLUGIN_ROOT}`` there never runs.
    These must run their handler with the variable absent."""
    entries = harness_entries(harness, sandbox)
    failures = []
    for event, mp in (("SessionStart", None), ("UserPromptSubmit", None), ("PostToolUse", None),
                      ("Stop", None)):
        e = pick(entries, event, matcher_prefix=mp)
        env = harness_env(harness, sandbox)
        assert "CLAUDE_PLUGIN_ROOT" not in env
        r = fire_entry(sandbox, e, env)
        if r.rc != 0 or not r.ran(*e.expected_handler()):
            failures.append(f"{e.id}\n{r}")
    assert not failures, "\n\n".join(failures)


# --------------------------------------------------------------------------- #
# 3. PATH without bash (#80)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("harness", ["claude", "codex-plugin", "cowork", "cloud-kit", "codex-global"])
def test_hooks_run_when_path_has_no_bash(harness, sandbox):
    """#80: the hook runner's PATH can be stripped. `/bin/sh: bash: No such file`
    exit 127 then fired on nearly every tool call. With a PATH holding nothing
    but the scheduler shims the hook must still find bash and Python by
    absolute path and run its handler."""
    warm(sandbox, harness)
    path = scheduler_shim_dirs()
    entries = harness_entries(harness, sandbox)
    failures = []
    for event, mp in (("SessionStart", None), ("PreToolUse", "Bash"), ("PostToolUse", None)):
        try:
            e = pick(entries, event, matcher_prefix=mp)
        except AssertionError:
            continue  # codex-global has no SessionStart-less variant; cowork has no SessionStart
        r = fire_entry(sandbox, e, harness_env(harness, sandbox, path=path))
        if r.rc != 0 or not r.ran(*e.expected_handler()) or stdout_problem(harness, e.event, r.out):
            failures.append(f"{e.id}\n{r}")
    assert not failures, "\n\n".join(failures)


# --------------------------------------------------------------------------- #
# 4. Version-manager Python shims (#145)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("harness", ["claude", "cloud-kit", "codex-global"])
def test_version_manager_python_shim_is_the_interpreter_used(harness, sandbox):
    """#145: mise / pyenv / asdf put a ``python3`` shim first on PATH. The
    launcher rejected it and every hook failed (exit 127) for users with no
    system Python 3.9+. A user-owned shim dir that nobody else can write is the
    interpreter the user chose: the hook must run through it."""
    shim_dir = sandbox.home / ".local" / "share" / "mise" / "shims"
    shim_dir.mkdir(parents=True)
    for d in (shim_dir, shim_dir.parent, shim_dir.parent.parent):
        d.chmod(0o755)
    marker = sandbox.root / "mise-shim-used"
    shim = shim_dir / "python3"
    shim.write_text(f"#!/bin/sh\necho used >> {shlex.quote(str(marker))}\n"
                    f"exec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    path = os.pathsep.join(d for d in (scheduler_shim_dirs(), str(shim_dir), "/usr/bin", "/bin") if d)
    e = pick(harness_entries(harness, sandbox), "UserPromptSubmit")
    r = fire_entry(sandbox, e, harness_env(harness, sandbox, path=path))
    assert r.rc == 0 and r.ran(*e.expected_handler()), r
    assert marker.exists(), f"the hook bypassed the user's mise python3 shim\n{r}"


# --------------------------------------------------------------------------- #
# 5. Upgrade removed the version dir the host still points at (#92 #75)
# --------------------------------------------------------------------------- #

def _version_cache(sb: Sandbox, live_root: Path, host_dir=".codex"):
    """The host's real plugin cache layout:
    ~/<.claude|.codex>/plugins/cache/<marketplace>/token-optimizer/<version>/"""
    base = sb.home / host_dir / "plugins" / "cache" / "alexgreensh-token-optimizer" / "token-optimizer"
    base.mkdir(parents=True)
    old, new = base / "5.13.50", base / "5.13.51"
    return base, old, new


def test_codex_plugin_hooks_survive_an_upgrade_mid_session(sandbox):
    """Codex hands a plugin hook the CLAUDE_PLUGIN_ROOT it loaded at session
    start. An upgrade deletes that version dir under the running session. The
    manifest resolves the newest sibling, and the handler must actually run
    from it (not exit 0 having done nothing)."""
    base, old, new = _version_cache(sandbox, CODEX_PLUGIN_ROOT)
    new.symlink_to(CODEX_PLUGIN_ROOT, target_is_directory=True)
    entries = harness_entries("codex-plugin", sandbox)
    failures = []
    for event, mp in (("SessionStart", None), ("PreToolUse", "Bash"), ("PostToolUse", None),
                      ("Stop", None), ("UserPromptSubmit", None)):
        e = pick(entries, event, matcher_prefix=mp)
        env = harness_env("codex-plugin", sandbox)
        env["CLAUDE_PLUGIN_ROOT"] = str(old)  # deleted by the upgrade
        r = fire_entry(sandbox, e, env)
        if r.rc != 0 or not r.ran(*e.expected_handler()) or stdout_problem("codex-plugin", e.event, r.out):
            failures.append(f"{e.id}\n{r}")
    assert not failures, "\n\n".join(failures)


def test_codex_global_hooks_survive_an_upgrade(sandbox):
    """#92 #75: codex-install baked .../token-optimizer/<X.Y.Z>/ into
    ~/.codex/hooks.json; the next upgrade deleted it and every hook exited 127.
    Install at 5.13.50, upgrade to 5.13.51 (old dir removed), fire."""
    base, old, new = _version_cache(sandbox, REPO)
    old.symlink_to(REPO, target_is_directory=True)
    entries = _entries_from(codex_global_hooks(old), "codex-global")
    assert all("5.13.50" in e.command for e in entries)
    old.unlink()
    new.symlink_to(REPO, target_is_directory=True)
    failures = []
    for e in entries:
        r = fire_entry(sandbox, e, harness_env("codex-global", sandbox))
        if r.rc != 0 or not r.ran(*e.expected_handler()):
            failures.append(f"{e.id}\n{r}")
    assert not failures, "\n\n".join(failures)


def test_claude_plugin_hooks_survive_an_upgrade_mid_session(sandbox):
    """Claude Code hands every hook the CLAUDE_PLUGIN_ROOT of the version the
    session loaded. When an update replaces .../token-optimizer/5.13.50/ with
    5.13.51/ under a running session, every entry must find the newest sibling
    and really run its handler there (not exit 0 having done nothing)."""
    base, old, new = _version_cache(sandbox, REPO, host_dir=".claude")
    new.symlink_to(REPO, target_is_directory=True)
    # A stray non-version sibling must never be picked over the real one.
    (base / "latest").mkdir()
    warm(sandbox, "claude")
    failures = []
    for e in harness_entries("claude", sandbox):
        env = harness_env("claude", sandbox)
        env["CLAUDE_PLUGIN_ROOT"] = str(old)  # deleted by the update
        r = fire_entry(sandbox, e, env)
        problem = stdout_problem("claude", e.event, r.out)
        ran_from_new = any(str(new) in " ".join(map(str, p.get("argv") or [])) for p in r.procs)
        if r.rc != 0 or r.err.strip() or problem or not r.ran(*e.expected_handler()) or not ran_from_new:
            failures.append(f"{e.id} {problem or ''}\n{r}")
    assert not failures, "\n\n".join(failures)


@pytest.mark.skipif(sys.platform == "win32", reason="simulates MSYS separators with POSIX file names")
def test_claude_plugin_hooks_survive_an_upgrade_with_a_windows_style_root(sandbox):
    """On Windows, Claude Code hands Git Bash a CLAUDE_PLUGIN_ROOT with
    backslashes (C:\\...\\token-optimizer\\5.13.50). The upgrade fallback cut
    the version off at the last "/" only, found no sibling, and every hook
    quietly exited 0 until a restart. It now cuts at the last "/" or "\\".

    Simulated on POSIX with a folder whose NAME holds a backslash: the root
    ``<cache>/plugins\\token-optimizer\\5.13.50`` must resolve to the sibling
    ``5.13.51`` inside ``<cache>/plugins\\token-optimizer`` (Git Bash reads the
    same text as nested folders)."""
    warm(sandbox, "claude")
    cache = sandbox.home / ".claude" / "plugins" / "cache" / "mk"
    parent = cache / "plugins\\token-optimizer"
    parent.mkdir(parents=True)
    new = parent / "5.13.51"
    new.symlink_to(REPO, target_is_directory=True)
    e = pick(harness_entries("claude", sandbox), "PreToolUse", matcher_prefix="Read")
    env = harness_env("claude", sandbox)
    env["CLAUDE_PLUGIN_ROOT"] = str(parent) + "\\5.13.50"  # deleted by the update
    r = fire_entry(sandbox, e, env)
    used = " ".join(" ".join(map(str, p.get("argv") or [])) for p in r.procs)
    assert r.rc == 0 and r.ran(*e.expected_handler()), r
    assert str(new) + "/hooks/run.py" in used, used


def test_claude_plugin_hooks_pick_the_highest_semver_sibling(sandbox):
    """sort -V, not lexical order: 5.13.10 is newer than 5.13.9, and 5.9.0 is
    older than 5.13.0. Hosts sort with BSD sort (macOS) and GNU sort (Linux,
    Git Bash); both must land on the highest version."""
    base, old, _new = _version_cache(sandbox, REPO, host_dir=".claude")
    for v in ("5.9.0", "5.13.9", "5.13.10"):
        (base / v).symlink_to(REPO, target_is_directory=True)
    e = pick(harness_entries("claude", sandbox), "PreToolUse", matcher_prefix="Bash")
    env = harness_env("claude", sandbox)
    env["CLAUDE_PLUGIN_ROOT"] = str(old)
    r = fire_entry(sandbox, e, env)
    used = " ".join(" ".join(map(str, p.get("argv") or [])) for p in r.procs)
    assert r.rc == 0 and r.ran(*e.expected_handler()), r
    assert str(base / "5.13.10") + "/hooks/run.py" in used, used
    assert str(base / "5.13.9") + "/" not in used and str(base / "5.9.0") + "/" not in used, used


# --------------------------------------------------------------------------- #
# 6. Codex never sees async hooks (#83)
# --------------------------------------------------------------------------- #

def test_codex_loaded_manifests_carry_no_async_hooks(sandbox):
    """#83: Codex warns 'async hooks are not supported yet' and SKIPS the entry,
    so an async hook in a Codex-loaded manifest silently never runs there."""
    offenders = [e.id for e in harness_entries("codex-plugin", sandbox) + harness_entries("codex-global", sandbox)
                 if e.is_async]
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# 7. Processes per prompt / per tool call (#139)
# --------------------------------------------------------------------------- #

HOT_PATH = [("UserPromptSubmit", None, None), ("PreToolUse", "Read", "Read"),
            ("PreToolUse", "Bash", "Bash"), ("PostToolUse", None, "Bash"),
            ("PostToolUse", None, "Read")]
MAX_PY_PROCS_PER_EVENT = 3


def _matches(entry, tool):
    if tool is None or entry.matcher in (None, "", "*"):
        return True
    return re.fullmatch(entry.matcher, tool) is not None


@pytest.mark.parametrize("harness", ["claude", "codex-plugin", "cowork", "cloud-kit", "codex-global"])
def test_hot_path_events_stay_within_a_process_budget(harness, sandbox):
    """#139: six UserPromptSubmit entries spawned 18 processes and ~800 ms of
    blocking time per prompt. Fire every manifest entry that matches one hot
    event, the way the host does, and count the Python processes started."""
    entries = harness_entries(harness, sandbox)
    warm(sandbox, harness)
    over = []
    for event, _mp, tool in HOT_PATH:
        matching = [e for e in entries if e.event == event and _matches(e, tool)]
        if not matching:
            continue
        total = 0
        for e in matching:
            over_payload = {"tool_name": tool} if tool else {}
            r = fire_entry(sandbox, e, harness_env(harness, sandbox), **over_payload)
            total += len(r.procs)
        if total > MAX_PY_PROCS_PER_EVENT:
            over.append(f"{event}({tool}): {len(matching)} entries, {total} python processes")
    assert not over, over


# --------------------------------------------------------------------------- #
# 8. The side effect each hook exists for (#253 class: exit 0 is not success)
# --------------------------------------------------------------------------- #

def _new_files(directory: Path, before: set, pattern="*.md"):
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(pattern) if p not in before)


def _snapshot(directory: Path, pattern="*.md"):
    return set(directory.glob(pattern)) if directory.is_dir() else set()


CHECKPOINT_HOOKS = [
    # (event, matcher prefix, handler text, checkpoint filename suffix)
    ("PreCompact", None, "compact-capture", ""),
    ("Stop", None, "stop_runner", "-stop"),
    ("StopFailure", None, "compact-capture", "-stop-failure"),
    ("PreToolUse", "Agent", "checkpoint-trigger", "-milestone-pre-fanout"),
]


@pytest.mark.parametrize("harness", ["claude", "cloud-kit", "cowork", "codex-global", "codex-plugin"])
def test_checkpoint_hooks_really_write_the_checkpoint(harness, sandbox):
    """#253: 'the PreCompact hook reports success, but no checkpoint appears'.
    Each checkpoint-writing hook, fired through its manifest command, must leave
    a NEW checkpoint for THIS session that carries the session's real work
    (the file it was editing), not just exit 0."""
    warm(sandbox, harness)
    entries = harness_entries(harness, sandbox)
    failures = []
    ran_any = False
    for event, mp, handler, suffix in CHECKPOINT_HOOKS:
        try:
            e = pick(entries, event, matcher_prefix=mp, handler=handler)
        except AssertionError:
            continue  # this harness does not wire that event (Codex: no PreCompact)
        ran_any = True
        cps = sandbox.checkpoints_for(harness)
        before = _snapshot(cps)
        time.sleep(1.05)  # checkpoint names carry a 1 s timestamp
        r = fire_entry(sandbox, e, harness_env(harness, sandbox))
        made = [p for p in _new_files(cps, before)
                if p.name.startswith(SID) and p.stem.endswith(suffix or p.stem[-6:])
                and (suffix or not re.search(r"-[a-z]", p.stem[len(SID) + 17:]))]
        if r.rc != 0 or not made:
            failures.append(f"{e.id}: no new '{SID}-*{suffix}.md' checkpoint\n{r}\n"
                            f"dir={sorted(p.name for p in _snapshot(cps))}")
            continue
        body = made[-1].read_text(encoding="utf-8")
        if "app.py" not in body:
            failures.append(f"{e.id}: checkpoint {made[-1].name} lost the session's work:\n{body[:600]}")
    assert ran_any, f"{harness}: no checkpoint-writing hook wired"
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("harness", ["codex-global", "codex-plugin"])
def test_codex_stop_finds_the_transcript_from_the_session_id_alone(harness, sandbox):
    """#278: an automatic Codex Stop wrote ``unknown-...-stop.md`` holding only
    "No transcript data available" while the rollout was right there. With
    just the session id (Codex may send no transcript_path), Stop must find
    the real rollout and write a checkpoint with the session's work; with no
    identity at all it must write nothing rather than an empty placeholder."""
    warm(sandbox, harness)
    e = pick(harness_entries(harness, sandbox), "Stop", handler="stop_runner")
    cps = sandbox.checkpoints_for(harness)
    sandbox.codex_rollout(SID)

    before = _snapshot(cps)
    time.sleep(1.05)
    r = fire_entry(sandbox, e, harness_env(harness, sandbox), transcript_path=None)
    made = [p for p in _new_files(cps, before) if p.suffix == ".md"]
    assert r.rc == 0, r
    ours = [p for p in made if p.name.startswith(SID) and p.stem.endswith("-stop")]
    assert ours, f"no {SID}-*-stop.md checkpoint\n{r}\nnew={[p.name for p in made]}"
    body = ours[-1].read_text(encoding="utf-8")
    assert "app.py" in body and "No transcript data available" not in body, body[:600]

    # Neither a session id nor a transcript, or an id with no rollout on disk:
    # nothing real to capture, so no placeholder either.
    before = _snapshot(cps)
    time.sleep(1.05)
    # (fire_entry would create a rollout for the id it is given, so call fire.)
    for sid in (None, "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"):
        payload = sandbox.payload(e.event, e.matcher, session_id=sid, transcript_path=None)
        r = sandbox.fire(e.command, payload, harness_env(harness, sandbox))
        assert r.rc == 0, r
    deadline = time.time() + 10  # the deferred Stop worker runs detached
    while time.time() < deadline:
        made = [p for p in _new_files(cps, before) if p.suffix == ".md"]
        if made:
            break
        time.sleep(0.5)
    for p in _new_files(cps, before):
        if p.suffix == ".md":
            text = p.read_text(encoding="utf-8")
            assert not p.name.startswith("unknown-") and "No transcript data available" not in text, \
                f"{p.name}: {text[:300]}"


def test_posttooluse_bash_compresses_and_archives_on_the_wire(sandbox):
    """The PostToolUse entry's claim: a 700-line `find` comes back to the model
    shorter, with the full output archived for `measure.py expand`."""
    warm(sandbox, "claude")
    e = pick(harness_entries("claude", sandbox), "PostToolUse")
    r = fire_entry(sandbox, e, harness_env("claude", sandbox))
    assert r.rc == 0 and not stdout_problem("claude", "PostToolUse", r.out), r
    obj = json.loads(r.out)
    replaced = obj["hookSpecificOutput"].get("updatedToolOutput") or {}
    original = sandbox.payload("PostToolUse")["tool_response"]["stdout"]
    new_text = replaced.get("stdout") if isinstance(replaced, dict) else str(replaced)
    assert new_text and len(new_text) < len(original) / 3, r
    archive = sandbox.claude / "_backups" / "token-optimizer" / "tool-archive" / SID
    assert archive.is_dir() and any(archive.iterdir()), "full output was not archived for expand"


def test_reread_of_an_unchanged_file_is_answered_on_the_wire(sandbox):
    """The PreToolUse:Read entry's claim: a second Read of an unchanged file in
    the same session is intercepted with a PreToolUse decision the host accepts."""
    warm(sandbox, "claude")
    entries = harness_entries("claude", sandbox)
    pre = pick(entries, "PreToolUse", matcher_prefix="Read")
    post = pick(entries, "PostToolUse")
    env = harness_env("claude", sandbox)
    app = sandbox.proj / "app.py"
    content = app.read_text(encoding="utf-8")
    read_response = {"type": "text", "file": {"filePath": str(app), "content": content,
                                              "numLines": content.count("\n"), "startLine": 1,
                                              "totalLines": content.count("\n")}}
    first = fire_entry(sandbox, pre, env)
    after = fire_entry(sandbox, post, env, tool_name="Read", tool_input={"file_path": str(app)},
                       tool_response=read_response)
    second = fire_entry(sandbox, pre, env)
    assert after.rc == 0, after
    for r in (first, second):
        assert r.rc == 0 and not stdout_problem("claude", "PreToolUse", r.out), r
    assert second.out.strip(), f"second Read of an unchanged file was not intercepted\n{second}"
    hso = json.loads(second.out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso.get("permissionDecision") or hso.get("additionalContext") or hso.get("updatedInput"), hso


# --------------------------------------------------------------------------- #
# 9. Talkative hooks: event name and JSON shape when there IS output
#    (#177 #162 #81 #76). An empty stdout passes every schema, so these seed
#    the state that makes each hook speak and require that it did.
# --------------------------------------------------------------------------- #

def _prior_session(sb: Sandbox, harness):
    """A previous session in the same project ended normally: ITS Stop hook,
    through this harness's own command, wrote the checkpoint."""
    e = pick(harness_entries(harness, sb), "Stop")
    r = fire_entry(sb, e, harness_env(harness, sb), session_id=OTHER_SID)
    cps = sb.checkpoints_for(harness)
    assert r.rc == 0 and any(p.name.startswith(OTHER_SID) for p in _snapshot(cps)), (
        f"{r}\n{sorted(p.name for p in _snapshot(cps))}")


def _this_session_checkpoint(sb: Sandbox, harness):
    """This session already has a checkpoint (written before its compaction)."""
    entries = harness_entries(harness, sb)
    try:
        e = pick(entries, "PreCompact", handler="compact-capture")
    except AssertionError:
        e = pick(entries, "Stop")  # Codex has no PreCompact; its Stop hook checkpoints
    r = fire_entry(sb, e, harness_env(harness, sb))
    cps = sb.checkpoints_for(harness)
    assert r.rc == 0 and any(p.name.startswith(SID) for p in _snapshot(cps)), (
        f"{r}\n{sorted(p.name for p in _snapshot(cps))}")


TALKATIVE = ["claude", "codex-plugin", "cowork", "cloud-kit", "codex-global"]


@pytest.mark.parametrize("harness", TALKATIVE)
def test_resume_prompt_injects_context_under_the_userpromptsubmit_event(harness, sandbox):
    """#177: on a prompt the UserPromptSubmit runner stamped hookEventName
    'SessionStart'; Claude Code rejected the hook and the hint never arrived."""
    warm(sandbox, harness)
    _prior_session(sandbox, harness)
    e = pick(harness_entries(harness, sandbox), "UserPromptSubmit")
    r = fire_entry(sandbox, e, harness_env(harness, sandbox),
                   prompt="continue where we left off on the wire contract bug in app.py")
    assert r.rc == 0, r
    assert r.out.strip(), f"a resume prompt with a prior session on disk injected nothing\n{r}"
    problem = stdout_problem(harness, "UserPromptSubmit", r.out)
    assert problem is None, f"{problem}\n{r}"
    assert "wire contract" in additional_context(r.out), r


@pytest.mark.parametrize("harness", TALKATIVE)
@pytest.mark.parametrize("source", ["compact", "startup"])
def test_session_start_speaks_valid_json_under_its_own_event(harness, source, sandbox):
    """#162: in a remote/container session SessionStart (source compact/fork)
    stamped 'UserPromptSubmit' and was rejected. #81: Codex SessionStart printed
    plain '[Token Optimizer] ...' text and failed 'invalid session start JSON'.
    After a compaction the restore must speak; on a fresh start with a prior
    session on disk the pointer must speak; both as ONE JSON object under
    SessionStart."""
    if harness == "cowork":
        pytest.skip("the Cowork build wires no SessionStart (Cowork does not fire it)")
    warm(sandbox, harness)
    if source == "compact":
        _this_session_checkpoint(sandbox, harness)
    else:
        _prior_session(sandbox, harness)
    e = pick(harness_entries(harness, sandbox), "SessionStart")
    r = fire_entry(sandbox, e, harness_env(harness, sandbox), source=source)
    assert r.rc == 0, r
    assert r.out.strip(), f"SessionStart({source}) with a checkpoint on disk said nothing\n{r}"
    problem = stdout_problem(harness, "SessionStart", r.out)
    assert problem is None, f"{problem}\n{r}"
    assert r.out.strip().startswith("{"), f"SessionStart output must be the JSON envelope\n{r}"


@pytest.mark.parametrize("harness", ["claude", "codex-plugin", "codex-global"])
def test_stop_after_real_work_keeps_stdout_host_valid(harness, sandbox):
    """#76: a Stop hook exited 0 but printed a plain line; Codex failed the hook."""
    warm(sandbox, harness)
    e = pick(harness_entries(harness, sandbox), "Stop")
    for _ in range(2):
        r = fire_entry(sandbox, e, harness_env(harness, sandbox))
        assert r.rc == 0, r
        problem = stdout_problem(harness, "Stop", r.out)
        assert problem is None, f"{problem}\n{r}"


# --------------------------------------------------------------------------- #
# 10. Per-turn re-emits (#53)
# --------------------------------------------------------------------------- #

def test_prompts_do_not_reinject_the_same_advisory_every_turn(sandbox):
    """#53: with another tool's status line configured, every prompt re-injected
    the same ~150-token 'custom status line' advisory. Real plugin install
    (registered in installed_plugins.json), foreign status line, five prompts:
    nothing a prompt injects may repeat on a later prompt unless the later
    prompt asked for it."""
    plugins = sandbox.claude / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "token-optimizer@alexgreensh-token-optimizer": [
            {"scope": "user", "installPath": str(REPO), "version": "5.13.51"}]}}), encoding="utf-8")
    (sandbox.claude / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command", "command": "node ~/.claude/other-tool/statusline.js"},
        "enabledPlugins": {"token-optimizer@alexgreensh-token-optimizer": True}}), encoding="utf-8")
    warm(sandbox, "claude")
    e = pick(harness_entries("claude", sandbox), "UserPromptSubmit")
    seen = {}
    repeats = []
    prompts = ["rename the handler", "add a docstring", "run the tests", "fix the lint", "commit it"]
    for i, prompt in enumerate(prompts):
        r = fire_entry(sandbox, e, harness_env("claude", sandbox), prompt=prompt)
        assert r.rc == 0 and stdout_problem("claude", "UserPromptSubmit", r.out) is None, r
        for para in (p.strip() for p in additional_context(r.out).split("\n\n")):
            if len(para) < 40:
                continue
            if para in seen:
                repeats.append(f"prompt {i + 1} re-injected (first on prompt {seen[para] + 1}): {para[:200]!r}")
            else:
                seen[para] = i
    assert not repeats, "\n".join(repeats)
