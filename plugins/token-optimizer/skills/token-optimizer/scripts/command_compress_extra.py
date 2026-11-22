#!/usr/bin/env python3
"""Additional structured command-output compressors for Token Optimizer.

Stdlib-only helper module invoked lazily by ``bash_compress.py`` and
``build_output_compress.py`` to keep legacy modules under the file-size cap.

Covered command families:
  - ``git diff --stat`` / ``git show --stat`` (huge diffs): lossless file
    names grouped by directory (dir prefix once, every basename + churn),
    plus the exact trailing ``N files changed, ...`` summary line verbatim.
  - ``git ls-files`` / ``git ls-tree``: lossless directory-grouped path list
    (dir prefix once, every basename preserved).
  - ``git grep``: routes to existing ``search_results`` handler.
  - ``gh run view --log`` / ``--log-failed``: strips only known GitHub Actions
    setup/checkout/cache/cleanup boilerplate while keeping every line of any
    step/group that contains a failure.
  - ``gh {run,pr,issue,workflow,release} list``: keeps header, top rows, EVERY
    failed/flagged row (uncapped), and a compact summary of remaining entries.
  - ``terraform plan`` / ``tofu plan``: drops state-refresh/read chatter and
    repetitive ``(known after apply)`` / unchanged-attribute noise while
    keeping every resource action header, every ``->`` / ``forces replacement``
    / ``+`` / ``-`` / ``~`` attribute line, diagnostics, and ``Plan:`` summary.
  - ``kubectl describe``: keeps top metadata, container status, ``Conditions:``,
    full ``Events:`` table, secret/config ``Environment:`` references, and
    error/warning lines; collapses large non-sensitive blocks.
  - Package installers (``pip``/``pip3``/``uv pip``/``npm``/``pnpm``/``yarn``/
    ``brew`` install/upgrade/update): drops download/cache/pour progress noise
    while keeping every error, warning, deprecation, vulnerability, caveat,
    and final summary verbatim.
  - ``mypy``: preserves every ``error:``, ``warning:``, ``Revealed type``,
    error-attached ``note:`` (including ``Expected:``/``Got:`` pairs), and
    final summary line verbatim while collapsing only long overload dumps.

Safety invariants (all handlers):
  - Fail-open: any unexpected input or exception returns ``None`` so the caller
    leaves the raw output untouched.
  - Short outputs (< 15-25 lines depending on handler) return ``None``.
  - Never returns output longer than (or >= 85% of) the input; lossless path
    groupers pass through unless savings >= ~25%.
  - Errors, warnings, file:line locations, stack frames, and summaries are
    always preserved verbatim. Credential re-injection is additionally enforced
    by the calling pipelines (``bash_compress`` / ``build_output_compress``).
"""
from __future__ import annotations

import re
import shlex

# ---------------------------------------------------------------------------
# Feature map for compression_events logging (measured tier)
# ---------------------------------------------------------------------------
FEATURE_MAP: dict[str, str] = {
    "git_diff_stat": "cmd_git_diff_stat",
    "git_ls": "cmd_git_ls",
    "gh_run_log": "cmd_gh_log",
    "gh_list": "cmd_gh_list",
    "terraform_plan": "cmd_terraform",
    "kubectl_describe": "cmd_kubectl_describe",
    "pkg_install": "cmd_pkg_install",
    "mypy": "cmd_mypy",
}
FEATURE_LABELS: dict[str, str] = {
    "cmd_git_diff_stat": "Git diff --stat compression",
    "cmd_git_ls": "Git ls-files/ls-tree compression",
    "cmd_gh_log": "GitHub Actions log compression",
    "cmd_gh_list": "GitHub CLI list compression",
    "cmd_terraform": "Terraform/OpenTofu plan compression",
    "cmd_kubectl_describe": "kubectl describe compression",
    "cmd_pkg_install": "Package installer compression",
    "cmd_mypy": "mypy output compression",
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
# Case-sensitive failed-job row prefix (e.g. `gh run view <id>` summary rows).
_GH_FAIL_ROW_RE = re.compile(r"^\s*[X✗✖]\s+")


def _is_important_line(ln: str) -> bool:
    """Return True if a line carries an error, exception, failure, warning, or non-zero exit."""
    try:
        from visibility_rule import is_error_or_failure_line
        if is_error_or_failure_line(ln):
            return True
    except Exception:
        pass
    return bool(_IMPORTANT_LINE_RE.search(ln) or _GH_FAIL_ROW_RE.search(ln))


def feature_for_pattern(pattern: str) -> str | None:
    """Return the measured-tier feature name for an extra pattern."""
    return FEATURE_MAP.get(pattern)


def feature_for_command(command_str: str, output: str = "") -> str | None:
    """Return the dedicated measured feature name for a B2 command, or None."""
    try:
        pat = detect_pattern(command_str)
        if pat in FEATURE_MAP:
            return FEATURE_MAP[pat]
        tokens = _extract_primary_tokens(command_str)
        if not tokens:
            return None
        cmd = tokens[0].rsplit("/", 1)[-1]
        sub = tokens[1] if len(tokens) > 1 else ""
        if cmd == "git":
            j = 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 2 if tokens[j] in ("-C", "-c", "--git-dir", "--work-tree") else 1
            gsub = tokens[j] if j < len(tokens) else ""
            if gsub in ("diff", "show") and any(t == "--stat" or t.startswith("--stat=") for t in tokens[j + 1 :]):
                return "cmd_git_diff_stat"
        if cmd == "kubectl" and sub == "describe":
            return "cmd_kubectl_describe"
        if cmd == "npm" and sub in ("install", "ci"):
            return "cmd_pkg_install"
        return None
    except Exception:
        return None


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
                # Only route to the log compressor when --log / --log-failed is passed;
                # `gh run view <id>` without --log emits a job summary table.
                if any(t in ("--log", "--log-failed") for t in tokens[3:]):
                    return "gh_run_log"
                return None
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
# 1. git diff --stat / git show --stat (LOSSLESS names, grouped by directory)
# ---------------------------------------------------------------------------
_DIFF_STAT_LINE_RE = re.compile(
    r"^\s*(.+?)\s+\|\s+(?:(\d+)\s*[+-]*|(Bin\s+\d+\s*->\s*\d+\s*bytes))\s*$"
)
_DIFF_STAT_SUMMARY_RE = re.compile(
    r"^\s*\d+\s+files?\s+changed(?:,\s+\d+\s+insertions?\(\+\))?(?:,\s+\d+\s+deletions?\(-\))?\s*$"
)


def _split_stat_dir_base(path: str) -> tuple[str, str]:
    """Split a diff-stat path into (dir_prefix, basename) without losing renames."""
    if " => " in path:
        brace_idx = path.find("{")
        if brace_idx > 0:
            slash_idx = path.rfind("/", 0, brace_idx)
            if slash_idx >= 0:
                return path[: slash_idx + 1], path[slash_idx + 1 :]
        return "./", path
    if "/" in path:
        d, base = path.rsplit("/", 1)
        return d + "/", base
    return "./", path


def compress_git_diff_stat(output: str) -> str | None:
    """Compress ``git diff --stat`` losslessly by grouping basenames under each dir."""
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
        stat_entries: list[tuple[str, int, str | None]] = []  # (path, count, bin_info)
        extra_lines: list[str] = []

        for ln in lines[:summary_idx]:
            m = _DIFF_STAT_LINE_RE.match(ln)
            if m:
                path = m.group(1).strip()
                count = int(m.group(2)) if m.group(2) else 0
                bin_info = m.group(3)
                stat_entries.append((path, count, bin_info))
            elif not stat_entries:
                preamble.append(ln)
            elif ln.strip():
                extra_lines.append(ln)

        if len(stat_entries) < 20:
            return None

        # Group ALL entries by directory; keep every basename and its churn count.
        dir_groups: dict[str, list[tuple[str, int, str | None]]] = {}
        for path, count, bin_info in stat_entries:
            d, base = _split_stat_dir_base(path)
            dir_groups.setdefault(d, []).append((base, count, bin_info))

        out: list[str] = []
        if preamble:
            out.extend(preamble[:10])
        out.append(f"[git diff --stat: {len(stat_entries)} files, grouped by dir]")
        for d in sorted(dir_groups):
            items = dir_groups[d]
            rendered = [
                f"{base}({bin_info})" if bin_info else f"{base}({count})"
                for base, count, bin_info in items
            ]
            out.append(f"  {d} " + ", ".join(rendered))
        if extra_lines:
            out.extend(extra_lines[:10])
        # Exact trailing summary line(s) verbatim
        out.extend(lines[summary_idx:])

        result = "\n".join(out)
        # Pass through if savings < ~25%
        return result if len(result) < len(output) * 0.75 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2. git ls-files / git ls-tree (LOSSLESS names, grouped by directory)
# ---------------------------------------------------------------------------
_LS_TREE_LINE_RE = re.compile(r"^\d{6}\s+(?:blob|tree|commit)\s+[0-9a-f]{40,64}\t(.+)$")


def compress_git_ls(output: str, command_str: str = "") -> str | None:
    """Compress ``git ls-files`` or ``git ls-tree`` losslessly by directory prefix."""
    try:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        if len(lines) < 25 or len(lines) > 50_000:
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
        out = [f"[{label}: {len(paths)} paths total, grouped by directory]"]
        if root_files:
            out.append("./ " + " ".join(root_files))
        for d in sorted(groups):
            out.append(d + " " + " ".join(groups[d]))

        result = "\n".join(out)
        # Pass through if savings < ~25%
        return result if len(result) < len(output) * 0.75 else None
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
    r"Set up job|Run actions/checkout|Run actions/setup-|Run actions/cache|"
    r"Post Run actions/|Complete job\b)",
    re.IGNORECASE,
)
_GH_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+")
_GH_FAIL_BODY_RE = re.compile(
    r"^\s*(?:E\s+|>\s+|FAILED\s+|___+\s+.+?\s+___+|===+\s+(?:FAILURES|ERRORS)\s+===+)"
)


def _gh_log_line_parts(ln: str) -> tuple[str | None, str]:
    """Return (tab_step_key_or_None, message_body_without_timestamp)."""
    parts = ln.split("\t", 2)
    if len(parts) == 3:
        step_key = f"{parts[0]}\t{parts[1]}"
        body = _GH_TS_PREFIX_RE.sub("", parts[2])
        return step_key, body
    return None, _GH_TS_PREFIX_RE.sub("", ln)


def compress_gh_run_log(output: str) -> str | None:
    """Compress ``gh run view --log`` by stripping known CI noise only.

    Every line of any step or ``##[group]`` that contains a failure (assertion,
    ``E`` lines, test body, traceback, error, non-zero exit) is preserved whole.
    Non-noise lines outside failing steps are also preserved.
    """
    try:
        lines = output.splitlines()
        if len(lines) < 25:
            return None

        # Assign each line to its step (job\tstep) or ##[group]..##[endgroup] block.
        keys: list[tuple[str, str | int]] = []
        failing_keys: set[tuple[str, str | int]] = set()
        group_id = 0
        in_group = False

        for idx, ln in enumerate(lines):
            tab_key, body = _gh_log_line_parts(ln)
            if tab_key is not None:
                key: tuple[str, str | int] = ("step", tab_key)
            else:
                if "##[group]" in ln:
                    group_id += 1
                    in_group = True
                    key = ("grp", group_id)
                elif in_group:
                    key = ("grp", group_id)
                    if "##[endgroup]" in ln:
                        in_group = False
                else:
                    key = ("line", idx)
            keys.append(key)
            if _is_important_line(ln) or _GH_FAIL_BODY_RE.search(body):
                failing_keys.add(key)

        out: list[str] = []
        omitted = 0
        omitted_start = 0
        omitted_end = 0
        noise_dropped = 0
        for idx, (ln, key) in enumerate(zip(lines, keys), 1):
            if key in failing_keys:
                if omitted:
                    out.append(f"... [{omitted} CI setup/boilerplate lines omitted (lines {omitted_start}-{omitted_end})] ...")
                    omitted = 0
                out.append(ln)
            elif _GH_LOG_NOISE_RE.search(ln):
                if omitted == 0:
                    omitted_start = idx
                omitted_end = idx
                omitted += 1
                noise_dropped += 1
            else:
                if omitted:
                    out.append(f"... [{omitted} CI setup/boilerplate lines omitted (lines {omitted_start}-{omitted_end})] ...")
                    omitted = 0
                out.append(ln)
        if omitted:
            out.append(f"... [{omitted} CI setup/boilerplate lines omitted (lines {omitted_start}-{omitted_end})] ...")

        if noise_dropped < 5:
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


def _is_gh_list_alert(ln: str) -> bool:
    try:
        from visibility_rule import is_error_or_failure_line
        if is_error_or_failure_line(ln):
            return True
    except Exception:
        pass
    return bool(_GH_LIST_ALERT_RE.search(ln) or _GH_FAIL_ROW_RE.search(ln))


def compress_gh_list(output: str) -> str | None:
    """Compress long ``gh run/pr/issue list`` tabular outputs without capping failures."""
    try:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        if len(lines) < 20:
            return None

        keep_head = 12
        head = lines[:keep_head]
        rest = lines[keep_head:]
        alert_rows: list[str] = []
        other_rows: list[tuple[int, str]] = []
        for idx, ln in enumerate(rest, keep_head + 1):
            if _is_gh_list_alert(ln):
                alert_rows.append(ln)
            else:
                other_rows.append((idx, ln))

        out = list(head)
        if alert_rows:
            out.append(f"... [{len(alert_rows)} additional failing/flagged rows preserved] ...")
            out.extend(alert_rows)
        if other_rows:
            ids = []
            for _idx, ln in other_rows:
                cols = ln.split("\t") if "\t" in ln else ln.split()
                if cols:
                    ids.append(cols[0].strip())
            sample = ", ".join(ids[:20])
            suffix = f", ... (+{len(ids) - 20} more)" if len(ids) > 20 else ""
            lo, hi = other_rows[0][0], other_rows[-1][0]
            loc = f"line {lo}" if lo == hi else f"lines {lo}-{hi}"
            out.append(f"... plus {len(other_rows)} more rows ({loc} of {len(lines)} total): {sample}{suffix}")

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
    """Compress verbose ``terraform plan`` / ``tofu plan`` output safely.

    Never drops any ``->``, ``forces replacement``, or ``+``/``-``/``~``
    attribute line inside a changed resource; collapses only state-refresh
    chatter, ``(known after apply)`` lines, and unchanged-attribute noise.
    """
    try:
        lines = output.splitlines()
        if len(lines) < 20:
            return None
        if not _TF_DETECT_RE.search(output):
            return None

        out: list[str] = []
        noise_dropped = 0
        noise_first = 0
        noise_last = 0
        in_resource = False
        resource_indent = 0
        unchanged_kept = 0
        unchanged_dropped = 0
        known_after_dropped = 0
        res_first = 0
        res_last = 0
        in_outputs = False

        def _flush_resource_omissions() -> None:
            nonlocal unchanged_dropped, known_after_dropped, res_first, res_last
            parts = []
            if known_after_dropped:
                parts.append(f"{known_after_dropped} '(known after apply)'")
            if unchanged_dropped:
                parts.append(f"{unchanged_dropped} unchanged attributes")
            if parts:
                loc = f", lines {res_first}-{res_last}" if res_first else ""
                out.append(f"      # ... ({', '.join(parts)} collapsed{loc})")
            unchanged_dropped = 0
            known_after_dropped = 0
            res_first = 0
            res_last = 0

        for idx, ln in enumerate(lines, 1):
            if _TF_NOISE_RE.match(ln):
                if noise_dropped == 0:
                    noise_first = idx
                noise_last = idx
                noise_dropped += 1
                continue

            if _TF_DIAG_RE.search(ln) or _is_important_line(ln):
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
                resource_indent = len(ln) - len(ln.lstrip(" "))
                unchanged_kept = 0
                out.append(ln)
                continue

            if _TF_RESOURCE_BLOCK_RE.match(ln):
                in_resource = True
                resource_indent = len(ln) - len(ln.lstrip(" "))
                unchanged_kept = 0
                out.append(ln)
                continue

            if in_resource:
                stripped = ln.strip()
                indent = len(ln) - len(ln.lstrip(" "))
                if stripped == "}":
                    _flush_resource_omissions()
                    out.append(ln)
                    if indent <= resource_indent + 2:
                        in_resource = False
                    continue
                # Never drop `->` or `forces replacement` lines.
                if "forces replacement" in ln or "->" in ln:
                    _flush_resource_omissions()
                    out.append(ln)
                    continue
                # Collapse `(known after apply)` placeholder lines.
                if "(known after apply)" in ln:
                    if not res_first:
                        res_first = idx
                    res_last = idx
                    known_after_dropped += 1
                    continue
                # Never drop any `+`, `-`, or `~` attribute line inside a resource.
                if stripped.startswith(("+", "-", "~")):
                    _flush_resource_omissions()
                    out.append(ln)
                    continue
                # Keep structural block openers/closers and a few unchanged context lines.
                if stripped.endswith("{") or not stripped:
                    _flush_resource_omissions()
                    out.append(ln)
                    continue
                if unchanged_kept < 4:
                    _flush_resource_omissions()
                    out.append(ln)
                    unchanged_kept += 1
                else:
                    if not res_first:
                        res_first = idx
                    res_last = idx
                    unchanged_dropped += 1
                continue

            if ln.strip():
                out.append(ln)

        _flush_resource_omissions()
        if noise_dropped:
            out.insert(0, f"[terraform plan: {noise_dropped} state-refresh/unchanged lines collapsed (lines {noise_first}-{noise_last})]")

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
_K8S_ENV_KEEP_RE = re.compile(
    r"(?:valueFrom|secretKeyRef|configMapKeyRef|<set to the key\b|"
    r"\b[A-Za-z0-9_]*_(?:URL|KEY|SECRET|TOKEN|PASSWORD|DSN)\b)",
    re.IGNORECASE,
)


def compress_kubectl_describe(output: str) -> str | None:
    """Compress verbose ``kubectl describe`` output while keeping Events, status & secret env refs."""
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
                block = [ln]
                j = i + 1
                while j < n and lines[j].strip() and (len(lines[j]) - len(lines[j].lstrip(" "))) > indent:
                    block.append(lines[j])
                    j += 1
                if len(block) > 6:
                    out.append(block[0])
                    kept_children = block[1:3]
                    out.extend(kept_children)
                    is_env = sec_key == "Environment:"
                    important_children = [
                        b for b in block[3:]
                        if _is_important_line(b) or (is_env and _K8S_ENV_KEEP_RE.search(b))
                    ]
                    out.extend(important_children)
                    dropped = len(block) - 3 - len(important_children)
                    if dropped > 0:
                        out.append(f"{' ' * (indent + 2)}... ({dropped} more {sec_key.rstrip(':')} lines collapsed, lines {i + 4}-{j})")
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
        first_dropped = 0
        last_dropped = 0
        in_caveats = False

        for idx, ln in enumerate(lines, 1):
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

            if _is_important_line(ln) or _PKG_SUMMARY_RE.search(ln):
                kept.append(ln)
                continue

            if _PKG_NOISE_RE.match(ln):
                if dropped == 0:
                    first_dropped = idx
                last_dropped = idx
                dropped += 1
                continue

            kept.append(ln)

        if dropped < 8:
            return None

        header = f"[package install: {dropped} download/progress lines collapsed (lines {first_dropped}-{last_dropped})]"
        result = header + "\n" + "\n".join(kept)
        return result if len(result) < len(output) * 0.85 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 8. mypy type checker (keeps error-attached notes; collapses long overload dumps)
# ---------------------------------------------------------------------------
_MYPY_NOTE_RE = re.compile(r"^.+?:\d+(?::\d+)?:\s+note: (.*)$")
_MYPY_OVERLOAD_HDR_RE = re.compile(r"^\s*Possible overload variants:\s*$", re.IGNORECASE)
_MYPY_OVERLOAD_ITEM_RE = re.compile(r"^\s*Overload candidate\b", re.IGNORECASE)


def compress_mypy(output: str) -> str | None:
    """Compress verbose ``mypy`` output by collapsing only long overload dumps.

    Preserves every ``error:``, ``warning:``, ``Revealed type``, summary, and
    error-attached ``note:`` (including ``Expected:`` / ``Got:`` pairs).
    """
    try:
        lines = output.splitlines()
        if len(lines) < 15:
            return None

        out: list[str] = []
        total_notes_dropped = 0
        i = 0
        n = len(lines)

        while i < n:
            m = _MYPY_NOTE_RE.match(lines[i])
            if m:
                body = m.group(1)
                # Case A: "Possible overload variants:" followed by indented signature notes
                if _MYPY_OVERLOAD_HDR_RE.match(body):
                    j = i + 1
                    while j < n:
                        mj = _MYPY_NOTE_RE.match(lines[j])
                        if mj and mj.group(1).startswith((" ", "\t")) and not _is_important_line(lines[j]):
                            j += 1
                        else:
                            break
                    block_len = j - i
                    if block_len >= 4:
                        out.append(f"  ... ({block_len} mypy overload variant notes collapsed, lines {i + 1}-{j})")
                        total_notes_dropped += block_len
                        i = j
                        continue
                # Case B: contiguous "Overload candidate ..." notes
                if _MYPY_OVERLOAD_ITEM_RE.match(body):
                    j = i + 1
                    while j < n:
                        mj = _MYPY_NOTE_RE.match(lines[j])
                        if mj and _MYPY_OVERLOAD_ITEM_RE.match(mj.group(1)) and not _is_important_line(lines[j]):
                            j += 1
                        else:
                            break
                    block_len = j - i
                    if block_len >= 4:
                        out.append(f"  ... ({block_len} mypy overload variant notes collapsed, lines {i + 1}-{j})")
                        total_notes_dropped += block_len
                        i = j
                        continue
            out.append(lines[i])
            i += 1

        if total_notes_dropped < 4:
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
