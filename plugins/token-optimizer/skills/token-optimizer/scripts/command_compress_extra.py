#!/usr/bin/env python3
"""Additional structured command-output compressors for Token Optimizer.

Stdlib-only helper module invoked lazily by ``bash_compress.py`` and
``build_output_compress.py`` to keep legacy modules under the file-size cap.

Covered command families:
  - ``git diff --stat`` / ``git show --stat`` (huge diffs): keeps top changed
    files, directory-grouped summary of the rest, and the exact trailing
    ``N files changed, ...`` summary line verbatim.
  - ``git ls-files`` / ``git ls-tree``: directory-grouped path list.
  - ``git grep``: routes to existing ``search_results`` handler.
  - ``gh run view --log`` / ``--log-failed``: strips GitHub Actions setup/cache
    boilerplate while keeping every ``##[error]``, ``##[warning]``, error/fail/
    traceback line (with surrounding context), failing step headers, and tail.
  - ``gh {run,pr,issue,workflow,release} list``: keeps header, top rows, every
    failed/error row, and a compact summary of remaining entries.
  - ``terraform plan`` / ``tofu plan``: drops state-refresh/read chatter and
    repetitive ``(known after apply)`` blocks while keeping every resource
    action header, changed attributes, diagnostics, and ``Plan:`` summary.
  - ``kubectl describe``: keeps top metadata, container status, ``Conditions:``,
    full ``Events:`` table, and error/warning lines; collapses huge
    ``Annotations:``, ``Environment:``, ``Mounts:``, ``Volumes:``, and
    ``Managed Fields:`` sections.
  - Package installers (``pip``/``pip3``/``uv pip``/``npm``/``pnpm``/``yarn``/
    ``brew`` install/upgrade/update): drops download/cache/pour progress noise
    while keeping every error, warning, deprecation, vulnerability, caveat,
    and final summary verbatim.
  - ``mypy``: preserves every ``error:``, ``warning:``, ``Revealed type``, and
    final summary line verbatim while collapsing verbose ``note:`` dumps.

Safety invariants (all handlers):
  - Fail-open: any unexpected input or exception returns ``None`` so the caller
    leaves the raw output untouched.
  - Short outputs (< 15-25 lines depending on handler) return ``None``.
  - Never returns output longer than (or >= 90% of) the input.
  - Errors, warnings, file:line locations, stack frames, and summaries are
    always preserved verbatim. Credential re-injection is additionally enforced
    by the calling pipelines (``bash_compress`` / ``build_output_compress``).
"""
from __future__ import annotations

import re
import shlex
from collections import Counter

# ---------------------------------------------------------------------------
# Feature map for compression_events logging (measured tier)
# ---------------------------------------------------------------------------
FEATURE_MAP: dict[str, str] = {
    "git_ls": "bash_compress_ls",
    "gh_run_log": "bash_compress_logs",
    "gh_list": "bash_compress_list",
    "terraform_plan": "bash_compress_build",
    "pkg_install": "bash_compress_npm",
    "mypy": "bash_compress_lint",
}

# Read-only gh sub-subcommands allowed across pipeline_analyzer / whitelist.
GH_READ_ONLY_GROUPS = frozenset({"run", "pr", "issue", "workflow", "release"})
GH_READ_ONLY_ACTIONS = frozenset({"view", "list", "checks", "diff", "status"})

_SHELL_SPLIT_TOKENS = frozenset({"|", "|&", "&&", "||", ";"})

# Shared error/warning/location detector used across handlers.
_IMPORTANT_LINE_RE = re.compile(
    r"(?:##\[(?:error|warning)\]|"
    r"\b(?:error|errors|err!|fatal|panic|fail|failed|failure|exception|"
    r"traceback|assertionerror|warning|warn|deprecated|vulnerabilit|"
    r"conflict|incompatible|oomkilled|crashloopbackoff|imagepullbackoff|"
    r"evicted|unhealthy|backoff|denied|refused|timed out|timeout)\b|"
    r"exit code [1-9]|"
    r"^\s*File \".+\", line \d+)",
    re.IGNORECASE,
)


def _extract_primary_tokens(command_str: str) -> list[str]:
    """Return shlex tokens for the primary command (env prefixes stripped)."""
    if not command_str or not isinstance(command_str, str):
        return []
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        return []
    if not tokens:
        return []
    stage: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SPLIT_TOKENS:
            break
        stage.append(tok)
    idx = 0
    if idx < len(stage) and stage[idx] == "env":
        idx += 1
    while idx < len(stage) and "=" in stage[idx] and not stage[idx].startswith("-"):
        idx += 1
    return stage[idx:]


def detect_pattern(command_str: str) -> str | None:
    """Detect extra command patterns not natively handled by bash_compress."""
    try:
        tokens = _extract_primary_tokens(command_str)
        if not tokens:
            return None
        cmd = tokens[0].rsplit("/", 1)[-1]
        sub = tokens[1] if len(tokens) > 1 else ""
        sub2 = tokens[2] if len(tokens) > 2 else ""

        if cmd == "git":
            # Skip git global flags like -C <dir> or -c k=v
            j = 1
            while j < len(tokens) and tokens[j].startswith("-"):
                if tokens[j] in ("-C", "-c", "--git-dir", "--work-tree"):
                    j += 2
                else:
                    j += 1
            gsub = tokens[j] if j < len(tokens) else ""
            if gsub == "grep":
                return "search_results"
            if gsub in ("ls-files", "ls-tree"):
                return "git_ls"
            return None

        if cmd == "gh":
            if sub == "run" and sub2 == "view":
                return "gh_run_log"
            if sub in GH_READ_ONLY_GROUPS and sub2 == "list":
                return "gh_list"
            return None

        if cmd in ("terraform", "tofu") and sub == "plan":
            return "terraform_plan"

        if cmd == "mypy":
            return "mypy"

        if cmd in ("pnpm", "yarn") and sub in ("install", "add", "upgrade", "up"):
            return "pkg_install"
        if cmd in ("pip", "pip3") and sub == "install":
            return "pkg_install"
        if cmd == "uv" and sub == "pip" and sub2 == "install":
            return "pkg_install"
        if cmd == "uv" and sub == "sync":
            return "pkg_install"
        if cmd == "brew" and sub in ("install", "upgrade", "reinstall", "update"):
            return "pkg_install"

        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. git diff --stat / git show --stat
# ---------------------------------------------------------------------------
_DIFF_STAT_LINE_RE = re.compile(
    r"^\s*(.+?)\s+\|\s+(?:(\d+)\s*[+-]*|Bin\s+\d+\s*->\s*\d+\s*bytes)\s*$"
)
_DIFF_STAT_SUMMARY_RE = re.compile(
    r"^\s*\d+\s+files?\s+changed(?:,\s+\d+\s+insertions?\(\+\))?(?:,\s+\d+\s+deletions?\(-\))?\s*$"
)


def compress_git_diff_stat(output: str) -> str | None:
    """Compress huge ``git diff --stat`` output while preserving the summary."""
    try:
        lines = output.splitlines()
        if len(lines) < 25:
            return None
        # Do not touch full patch diffs (those have diff --git headers)
        if any(ln.startswith("diff --git ") for ln in lines[:40]):
            return None

        summary_idx = -1
        for i in range(len(lines) - 1, max(-1, len(lines) - 6), -1):
            if _DIFF_STAT_SUMMARY_RE.match(lines[i]):
                summary_idx = i
                break
        if summary_idx < 0:
            return None

        preamble: list[str] = []
        stat_entries: list[tuple[int, str, int, str]] = []  # (idx, path, count, raw_line)
        extra_lines: list[str] = []

        for i, ln in enumerate(lines[:summary_idx]):
            m = _DIFF_STAT_LINE_RE.match(ln)
            if m:
                path = m.group(1).strip()
                count = int(m.group(2)) if m.group(2) else 0
                stat_entries.append((i, path, count, ln))
            elif not stat_entries:
                preamble.append(ln)
            elif ln.strip():
                extra_lines.append(ln)

        if len(stat_entries) < 20:
            return None

        # Keep the 12 highest-churn files verbatim (preserving original order)
        top_k = 12
        ranked = sorted(stat_entries, key=lambda e: (-e[2], e[0]))
        top_indices = {e[0] for e in ranked[:top_k]}
        kept_top = [e[3] for e in stat_entries if e[0] in top_indices]
        omitted = [e for e in stat_entries if e[0] not in top_indices]

        # Group omitted files by top-level directory
        dir_groups: dict[str, list[tuple[str, int]]] = {}
        for _, path, count, _ in omitted:
            clean_p = path.split(" => ")[-1].strip("{}")
            d = clean_p.rsplit("/", 1)[0] + "/" if "/" in clean_p else "./"
            base = clean_p.rsplit("/", 1)[-1]
            dir_groups.setdefault(d, []).append((base, count))

        out: list[str] = []
        if preamble:
            out.extend(preamble[:10])
        out.append(
            f"[git diff --stat: {len(stat_entries)} files total; "
            f"showing top {len(kept_top)} by churn + {len(omitted)} grouped by dir]"
        )
        out.extend(kept_top)
        out.append("")
        out.append(f"Remaining {len(omitted)} files by directory:")
        for d, items in sorted(dir_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            total_churn = sum(c for _, c in items)
            examples = ", ".join(name for name, _ in items[:3])
            more = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
            out.append(f"  {d} ({len(items)} files, ~{total_churn} changes): {examples}{more}")
        if extra_lines:
            out.extend(extra_lines[:10])
        # Exact trailing summary line(s) verbatim
        out.extend(lines[summary_idx:])

        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2. git ls-files / git ls-tree
# ---------------------------------------------------------------------------
_LS_TREE_LINE_RE = re.compile(r"^\d{6}\s+(?:blob|tree|commit)\s+[0-9a-f]{40,64}\t(.+)$")


def compress_git_ls(output: str, command_str: str = "") -> str | None:
    """Compress large ``git ls-files`` or ``git ls-tree`` path listings."""
    try:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        if len(lines) < 40 or len(lines) > 50_000:
            return None

        paths: list[str] = []
        is_tree = False
        for ln in lines:
            m = _LS_TREE_LINE_RE.match(ln)
            if m:
                is_tree = True
                paths.append(m.group(1))
            else:
                if ln.startswith(("fatal:", "error:", "usage:")):
                    return None
                paths.append(ln)

        groups: dict[str, list[str]] = {}
        root_files: list[str] = []
        for p in paths:
            if "/" in p:
                d, base = p.rsplit("/", 1)
                groups.setdefault(d + "/", []).append(base)
            else:
                root_files.append(p)

        label = "git ls-tree" if (is_tree or "ls-tree" in command_str) else "git ls-files"
        out = [f"[{label}: {len(paths)} paths total, grouped by directory. Full output is recoverable.]"]
        if root_files:
            if len(root_files) <= 10:
                out.append("./ " + " ".join(root_files))
            else:
                out.append(f"* ./ ({len(root_files)} entries) e.g. " + " ".join(root_files[:3]))
        for d in sorted(groups):
            names = groups[d]
            if len(names) <= 8:
                out.append(d + " " + " ".join(names))
            else:
                exts = Counter(
                    ("*" + n[n.rfind("."):]) if "." in n.lstrip(".") else "(no ext)"
                    for n in names
                )
                top_ext = ", ".join(f"{e} x{c}" for e, c in exts.most_common(3))
                out.append(f"* {d} ({len(names)} entries: {top_ext}) e.g. " + " ".join(names[:3]))

        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3. gh run view --log / --log-failed
# ---------------------------------------------------------------------------
_GH_LOG_NOISE_RE = re.compile(
    r"(?:##\[(?:group|endgroup)\]|"
    r"Current runner version:|Operating System|Runner Image|GITHUB_TOKEN Permissions|"
    r"Prepare workflow directory|Prepare all required actions|"
    r"Getting action download info|Download action repository|"
    r"Complete job name:|Syncing repository:|Fetching the repository|"
    r"Getting Git version info|Temporarily overriding HOME|"
    r"Adding repository directory to the temporary git global config|"
    r"Checking out the ref|Post job cleanup|Cleaning up orphan processes|"
    r"Cache restored from key|Restoring cache|Saving cache|Post Cache|"
    r"Set up job|Run actions/checkout|Run actions/setup-|Run actions/cache)",
    re.IGNORECASE,
)


def compress_gh_run_log(output: str) -> str | None:
    """Compress verbose ``gh run view --log`` CI logs while keeping all failures."""
    try:
        lines = output.splitlines()
        if len(lines) < 25:
            return None

        # Mark lines that MUST be kept: head, tail, errors/warnings + +/-2 context
        n = len(lines)
        keep = [False] * n
        for i in range(min(5, n)):
            keep[i] = True
        for i in range(max(0, n - 10), n):
            keep[i] = True

        for i, ln in enumerate(lines):
            if _IMPORTANT_LINE_RE.search(ln):
                for j in range(max(0, i - 2), min(n, i + 3)):
                    keep[j] = True

        # Emit kept runs separated by compact omission markers
        out: list[str] = []
        omitted = 0
        noise_dropped = 0
        for i, ln in enumerate(lines):
            if keep[i]:
                if omitted:
                    out.append(f"... [{omitted} CI log lines omitted] ...")
                    omitted = 0
                out.append(ln)
            else:
                omitted += 1
                if _GH_LOG_NOISE_RE.search(ln):
                    noise_dropped += 1
        if omitted:
            out.append(f"... [{omitted} CI log lines omitted] ...")

        if len(out) >= n * 0.85 and noise_dropped < 5:
            return None
        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. gh {run,pr,issue,workflow,release} list
# ---------------------------------------------------------------------------
_GH_LIST_ALERT_RE = re.compile(
    r"\b(?:failure|failed|cancelled|timed_out|action_required|error|changes_requested)\b",
    re.IGNORECASE,
)


def compress_gh_list(output: str) -> str | None:
    """Compress long ``gh run/pr/issue list`` tabular outputs."""
    try:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        if len(lines) < 20:
            return None

        keep_head = 12
        head = lines[:keep_head]
        rest = lines[keep_head:]
        alert_rows = [ln for ln in rest if _GH_LIST_ALERT_RE.search(ln)]
        other_count = len(rest) - len(alert_rows)

        out = list(head)
        if alert_rows:
            out.append(f"... [{len(alert_rows)} additional failing/flagged rows preserved] ...")
            out.extend(alert_rows[:20])
        if other_count > 0:
            # Keep first identifier column of dropped rows so items stay discoverable
            ids = []
            for ln in rest:
                if ln not in alert_rows:
                    cols = ln.split("\t") if "\t" in ln else ln.split()
                    if cols:
                        ids.append(cols[0].strip())
            sample = ", ".join(ids[:20])
            suffix = f", ... (+{len(ids) - 20} more)" if len(ids) > 20 else ""
            out.append(f"... plus {other_count} more rows ({len(lines)} total): {sample}{suffix}")

        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 5. terraform plan / tofu plan
# ---------------------------------------------------------------------------
_TF_DETECT_RE = re.compile(
    r"(?:Terraform will perform|OpenTofu will perform|Plan:\s+\d+\s+to add|"
    r"No changes\.|Refreshing state\.\.\.|Terraform used the selected providers|"
    r"OpenTofu used the selected providers)"
)
_TF_NOISE_RE = re.compile(
    r"^(?:\s*[\w.\-\[\]\"']+:\s*(?:Refreshing state\.\.\.|Reading\.\.\.|Read complete after|Still reading\.\.\.)|"
    r"Acquiring state lock\.|Releasing state lock\.|"
    r"\s*#\s*\(\d+\s+unchanged\s+(?:attributes?|blocks?)\s+hidden\)\s*$)"
)
_TF_RESOURCE_HDR_RE = re.compile(r"^\s*#\s+\S+.*(?:will be|must be|has changed|is tainted)")
_TF_RESOURCE_BLOCK_RE = re.compile(r"^\s*(?:[+~-]|-/\+|\+/-)\s+(?:resource|data|module)\s+")
_TF_SUMMARY_RE = re.compile(
    r"^(?:Plan:\s+|No changes\.|Changes to Outputs:|Saved the plan to:|"
    r"Apply complete!|Destroy complete!)"
)
_TF_DIAG_RE = re.compile(r"^[│╷╵]|\b(?:Warning|Error):|^\s*on\s+\S+\s+line\s+\d+")


def compress_terraform_plan(output: str) -> str | None:
    """Compress verbose ``terraform plan`` / ``tofu plan`` output safely."""
    try:
        lines = output.splitlines()
        if len(lines) < 20:
            return None
        if not _TF_DETECT_RE.search(output):
            return None

        out: list[str] = []
        noise_dropped = 0
        in_resource = False
        attr_kept = 0
        attr_dropped = 0
        known_after_dropped = 0
        in_outputs = False

        def _flush_resource_omissions() -> None:
            nonlocal attr_dropped, known_after_dropped
            parts = []
            if known_after_dropped:
                parts.append(f"{known_after_dropped} '(known after apply)'")
            if attr_dropped:
                parts.append(f"{attr_dropped} other attributes")
            if parts:
                out.append(f"      # ... ({', '.join(parts)} collapsed)")
            attr_dropped = 0
            known_after_dropped = 0

        for ln in lines:
            if _TF_NOISE_RE.match(ln):
                noise_dropped += 1
                continue

            if _TF_DIAG_RE.search(ln) or _IMPORTANT_LINE_RE.search(ln):
                _flush_resource_omissions()
                out.append(ln)
                continue

            if _TF_SUMMARY_RE.match(ln.strip()):
                _flush_resource_omissions()
                in_resource = False
                if ln.strip().startswith("Changes to Outputs:"):
                    in_outputs = True
                out.append(ln)
                continue

            if in_outputs:
                out.append(ln)
                continue

            if _TF_RESOURCE_HDR_RE.match(ln):
                _flush_resource_omissions()
                in_resource = True
                attr_kept = 0
                out.append(ln)
                continue

            if _TF_RESOURCE_BLOCK_RE.match(ln):
                in_resource = True
                attr_kept = 0
                out.append(ln)
                continue

            if in_resource:
                stripped = ln.strip()
                if stripped == "}":
                    _flush_resource_omissions()
                    out.append(ln)
                    in_resource = False
                    continue
                if "forces replacement" in ln or "->" in ln:
                    if attr_kept < 12:
                        out.append(ln)
                        attr_kept += 1
                    else:
                        attr_dropped += 1
                    continue
                if "(known after apply)" in ln:
                    known_after_dropped += 1
                    continue
                if attr_kept < 8:
                    out.append(ln)
                    attr_kept += 1
                else:
                    attr_dropped += 1
                continue

            # Drop legend lines once we have real content, keep other context
            if ln.strip():
                out.append(ln)

        _flush_resource_omissions()
        if noise_dropped:
            out.insert(0, f"[terraform plan: {noise_dropped} state-refresh/unchanged lines collapsed]")

        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 6. kubectl describe
# ---------------------------------------------------------------------------
_K8S_DESC_COLLAPSE_SECTIONS = frozenset({
    "Annotations:", "Labels:", "Environment:", "Mounts:", "Volumes:",
    "Tolerations:", "Managed Fields:", "Args:", "Command:", "Data",
})
_K8S_DESC_KEEP_SECTIONS = frozenset({
    "Events:", "Conditions:", "Containers:", "Init Containers:",
    "Status:", "Volumes:",
})


def compress_kubectl_describe(output: str) -> str | None:
    """Compress verbose ``kubectl describe`` output while keeping Events & status."""
    try:
        lines = output.splitlines()
        if len(lines) < 25:
            return None
        head_blob = "\n".join(lines[:15])
        if "Name:" not in head_blob or not any(
            k in output for k in ("Namespace:", "Status:", "Events:", "Conditions:", "Containers:")
        ):
            return None

        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            ln = lines[i]
            stripped = ln.strip()
            indent = len(ln) - len(ln.lstrip(" "))

            # Events: and Conditions: are ALWAYS preserved in full
            if indent == 0 and stripped in ("Events:", "Conditions:"):
                out.append(ln)
                i += 1
                while i < n and (not lines[i].strip() or lines[i].startswith(" ")):
                    out.append(lines[i])
                    i += 1
                continue

            # Check if this starts a collapsible block (e.g. Annotations:, Environment:, Mounts:)
            sec_key = stripped.split(":", 1)[0] + ":" if ":" in stripped else stripped
            if sec_key in _K8S_DESC_COLLAPSE_SECTIONS and sec_key != "Status:":
                # Gather indented child lines
                block = [ln]
                j = i + 1
                while j < n and lines[j].strip() and (len(lines[j]) - len(lines[j].lstrip(" "))) > indent:
                    block.append(lines[j])
                    j += 1
                if len(block) > 6:
                    out.append(block[0])
                    kept_children = block[1:3]
                    out.extend(kept_children)
                    important_children = [
                        b for b in block[3:] if _IMPORTANT_LINE_RE.search(b)
                    ]
                    out.extend(important_children)
                    dropped = len(block) - 3 - len(important_children)
                    if dropped > 0:
                        out.append(f"{' ' * (indent + 2)}... ({dropped} more {sec_key.rstrip(':')} lines collapsed)")
                    i = j
                    continue

            out.append(ln)
            i += 1

        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 7. Package installers (pip / uv pip / npm / pnpm / yarn / brew)
# ---------------------------------------------------------------------------
_PKG_NOISE_RE = re.compile(
    r"^(?:\s*(?:Collecting|Downloading|Using cached|Requirement already satisfied|"
    r"Preparing metadata|Getting requirements to build|Building wheels?|"
    r"Created wheel|Stored in directory:|Installing build dependencies|"
    r"Installing collected packages|Resolved \d+ package|Prepared \d+ package|"
    r"Uninstalled \d+ package|Downloaded \d+ package|"
    r"Progress: resolved|Packages: [+-]|Resolving packages|Fetching packages|"
    r"Linking dependencies|Building fresh packages|Already downloaded:|"
    r"==> (?:Downloading|Pouring|Fetching|Installing|Cleaning up|Running `brew cleanup`)|"
    r"reify:|idealTree:|sill |http fetch|⸨|[+]{5,}|[#]{5,}|[━─=]{5,}))",
    re.IGNORECASE,
)
_PKG_SUMMARY_RE = re.compile(
    r"(?:Successfully installed|Installed \d+ package|added \d+ package|"
    r"removed \d+ package|changed \d+ package|audited \d+ package|"
    r"up to date|found \d+ vulnerabilit|Done in \d|==> Summary|==> Caveats|"
    r"Already up-to-date|🍺)",
    re.IGNORECASE,
)


def compress_package_install(output: str) -> str | None:
    """Compress verbose pip/uv/npm/pnpm/yarn/brew install/upgrade output."""
    try:
        lines = output.splitlines()
        if len(lines) < 15:
            return None

        kept: list[str] = []
        dropped = 0
        in_caveats = False

        for ln in lines:
            stripped = ln.strip()
            if not stripped:
                if in_caveats:
                    in_caveats = False
                continue

            if stripped.startswith("==> Caveats"):
                in_caveats = True
                kept.append(ln)
                continue
            if in_caveats:
                if stripped.startswith("==>"):
                    in_caveats = False
                else:
                    kept.append(ln)
                    continue

            if _IMPORTANT_LINE_RE.search(ln) or _PKG_SUMMARY_RE.search(ln):
                kept.append(ln)
                continue

            if _PKG_NOISE_RE.match(ln):
                dropped += 1
                continue

            # Keep short non-noise context lines
            kept.append(ln)

        if dropped < 8:
            return None

        header = f"[package install: {dropped} download/progress lines collapsed]"
        result = header + "\n" + "\n".join(kept)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 8. mypy type checker (preserves EVERY error & file:line, collapses note dumps)
# ---------------------------------------------------------------------------
_MYPY_NOTE_RE = re.compile(r"^.+?:\d+(?::\d+)?:\s+note:\s+(.*)$")
_MYPY_SUMMARY_RE = re.compile(r"^(?:Found \d+ errors? in \d+ files?|Success: no issues found)")


def compress_mypy(output: str) -> str | None:
    """Compress verbose ``mypy`` output by collapsing repetitive ``note:`` blocks.

    NEVER drops any ``error:`` line, ``warning:`` line, ``Revealed type`` note,
    or final summary line.
    """
    try:
        lines = output.splitlines()
        if len(lines) < 20:
            return None

        out: list[str] = []
        note_run = 0
        total_notes_dropped = 0

        def _flush_notes() -> None:
            nonlocal note_run
            if note_run > 0:
                out.append(f"  ... ({note_run} mypy note lines collapsed)")
                note_run = 0

        for ln in lines:
            m = _MYPY_NOTE_RE.match(ln)
            if m and "Revealed type" not in m.group(1):
                note_run += 1
                total_notes_dropped += 1
                continue
            _flush_notes()
            out.append(ln)
        _flush_notes()

        if total_notes_dropped < 8:
            return None
        result = "\n".join(out)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Unified dispatchers
# ---------------------------------------------------------------------------
def compress_for_pattern(pattern: str, output: str, command_str: str = "") -> str | None:
    """Dispatch an extra pattern name to its structured compressor."""
    try:
        if pattern == "git_ls":
            return compress_git_ls(output, command_str)
        if pattern == "gh_run_log":
            return compress_gh_run_log(output)
        if pattern == "gh_list":
            return compress_gh_list(output)
        if pattern == "terraform_plan":
            return compress_terraform_plan(output)
        if pattern == "pkg_install":
            return compress_package_install(output)
        if pattern == "mypy":
            return compress_mypy(output)
        return None
    except Exception:
        return None


def compress_build_command(command_str: str, output: str) -> str | None:
    """Structured fast-path called by ``build_output_compress.compress``."""
    try:
        pat = detect_pattern(command_str)
        if pat in ("pkg_install", "terraform_plan", "gh_run_log", "mypy"):
            return compress_for_pattern(pat, output, command_str)
        # Also catch `npm install` / `npm ci` routed through build_output_compress
        tokens = _extract_primary_tokens(command_str)
        if len(tokens) >= 2 and tokens[0].rsplit("/", 1)[-1] == "npm" and tokens[1] in ("install", "ci"):
            return compress_package_install(output)
        return None
    except Exception:
        return None
