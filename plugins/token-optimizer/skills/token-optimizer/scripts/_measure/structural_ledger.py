"""Structural (setup-trim) ledger: per-component, dated trim events.

A setup trim is an event, not a snapshot delta:

    {component, item, scope, tokens_before, tokens_after, ts, evidence, source}

It is credited from its own timestamp, for the sessions where that component
loads, for as long as the trimmed item stays trimmed. Growth anywhere else in
the prefix never offsets it (the old "baseline total minus current total"
design let any new plugin cancel every trim).

Storage is append-only JSONL in the data dir, so history is never rewritten:

  structural_trims.jsonl   "trim" records plus "close" (item came back) and
                           "void" (seed import reverted) records
  prefix_history.jsonl     per-component, per-item snapshots, appended only
                           when the itemized prefix changes
  structural_baseline.json first per-component snapshot of an install,
                           written once (O_EXCL) and never recaptured

Sources and tiers:
  audit_action  TO archived or rewrote the item; a backup path is the evidence.
                Tier "measured".
  detected      the item shrank between two snapshots and no TO backup holds
                it. Tier "unattributed": shown, never summed (TO may not have
                caused it, integrity rule 3).
  attested      owner-entered historic trim. Tier "estimated".

Scopes: "global" loads in every local session; "project:<enc>" only in
sessions whose project dir is exactly <enc> (auto-memory is per project);
"tree:<enc>" in sessions at or below <enc> (a CLAUDE.md in a parent dir).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

from . import _compat

SCHEMA = 1
TRIMS_FILE = "structural_trims.jsonl"
HISTORY_FILE = "prefix_history.jsonl"
BASELINE_FILE = "structural_baseline.json"
TIER_BY_SOURCE = {"audit_action": "measured", "attested": "estimated", "dated_archive": "estimated", "detected": "unattributed"}
SUMMED_TIERS = ("measured", "estimated")
MIN_TRIM_TOKENS = 20          # below this a shrink is estimator noise, not a trim
SNAPSHOT_INTERVAL_S = 6 * 3600
# Backup dirs that only Token Optimizer writes (manual `skill archive`, the
# dedup pass, the audit's own backups). An item found here was trimmed by TO.
TO_BACKUP_PREFIXES = ("skills-archived", "skills-deduped", "skills-archive-",
                      "commands-archived", "commands-archive-", "token-optimizer-")
UNITEMIZED = "*"


def encode_project(path):
    """Claude Code's project dir name for a path: every non-alphanumeric -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_epoch(ts):
    """ISO-8601 (naive = UTC) -> epoch seconds, or None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def trim_id(source, scope, component, item, ts):
    raw = "|".join(str(x) for x in (source, scope, component, item, ts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------- itemize

def itemize(components, claude_dir, estimate_fm):
    """Per-component, per-item token map from measure_components() output.

    Returns {component: {"scope": str, "items": {item: tokens}}}. Components
    the audit only totals (plugin skills, MCP) carry one "*" item: they are
    tracked for history but never auto-detected or auto-closed, because a
    total moves with unrelated items.
    """
    out = {}
    claude_dir = str(claude_dir)
    for key, info in (components or {}).items():
        if not isinstance(info, dict):
            continue
        if key == "claude_md_global" and info.get("exists"):
            out[key] = {"scope": "global", "items": {"CLAUDE.md": int(info.get("tokens") or 0)}}
        elif key == "claude_md_home" and info.get("exists"):
            home_dir = os.path.dirname(str(info.get("path") or ""))
            out[key] = {"scope": "tree:" + encode_project(home_dir),
                        "items": {"CLAUDE.md": int(info.get("tokens") or 0)}}
        elif key.startswith("claude_md_project_") and info.get("exists"):
            p = str(info.get("path") or "")
            d = os.path.dirname(p)
            if key.endswith("_dotclaude"):
                d = os.path.dirname(d)
            out[key] = {"scope": "tree:" + encode_project(d),
                        "items": {os.path.basename(p): int(info.get("tokens") or 0)}}
        elif key == "memory_md" and info.get("exists") and info.get("path"):
            proj = os.path.basename(os.path.dirname(os.path.dirname(str(info["path"]))))
            out[key] = {"scope": "project:" + proj, "items": {"MEMORY.md": int(info.get("tokens") or 0)}}
    detail = (components or {}).get("skills_detail") or {}
    skills = {}
    for name in ((components or {}).get("skills") or {}).get("names") or []:
        skills[name] = int((detail.get(name) or {}).get("frontmatter_tokens") or 0)
    out["skills"] = {"scope": "global", "items": skills}
    for comp, sub in (("commands", "commands"), ("agents", "agents")):
        items = {}
        base = os.path.join(claude_dir, sub)
        for name in ((components or {}).get(comp) or {}).get("names") or []:
            path = os.path.join(base, name + ".md")
            items[name] = int(estimate_fm(path)) if os.path.isfile(path) else 0
        out[comp] = {"scope": "global", "items": items}
    rules = (components or {}).get("rules") or {}
    r_items = {}
    for f in rules.get("files") or []:
        if isinstance(f, dict) and f.get("name"):
            r_items[str(f["name"])] = int(f.get("tokens") or 0)
    out["rules"] = {"scope": "global", "items": r_items}
    for comp in ("plugin_skills", "plugin_commands", "mcp_tools"):
        info = (components or {}).get(comp) or {}
        out[comp] = {"scope": "global", "items": {UNITEMIZED: int(info.get("tokens") or 0)}}
    return out


def snapshot_hash(items_by_component):
    blob = json.dumps(items_by_component, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


# ---------------------------------------------------------------- storage

def _append(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def make_trim(component, item, scope, tokens_before, tokens_after, ts, source,
              evidence="", origin="", batch="", note=""):
    if source not in TIER_BY_SOURCE:
        raise ValueError("unknown trim source: %r" % (source,))
    before, after = int(tokens_before), int(tokens_after)
    if before - after <= 0:
        raise ValueError("a trim must shrink the item")
    return {
        "kind": "trim", "schema": SCHEMA, "component": str(component), "item": str(item),
        "scope": str(scope), "tokens_before": before, "tokens_after": after,
        "ts": str(ts), "source": source, "tier": TIER_BY_SOURCE[source],
        "evidence": str(evidence or ""), "origin": str(origin or source),
        "batch": str(batch or ""), "note": str(note or ""),
        "id": trim_id(source, scope, component, item, ts),
    }


def record_trim(data_dir, trim):
    """Append a trim once. Returns True when written, False when its id exists."""
    path = os.path.join(str(data_dir), TRIMS_FILE)
    if any(r.get("kind") == "trim" and r.get("id") == trim["id"] for r in _read_jsonl(path)):
        return False
    _append(path, trim)
    return True


def close_trim(data_dir, tid, ts, reason):
    _append(os.path.join(str(data_dir), TRIMS_FILE),
            {"kind": "close", "trim_id": tid, "ts": str(ts), "reason": str(reason)})


def void_batch(data_dir, batch, ts, reason):
    _append(os.path.join(str(data_dir), TRIMS_FILE),
            {"kind": "void", "batch": str(batch), "ts": str(ts), "reason": str(reason)})


def load_trims(data_dir):
    """Trims with their state resolved: voided batches dropped, closed_at set
    from the earliest close record. Deduped by id, file order kept."""
    return load_trims_from(_read_jsonl(os.path.join(str(data_dir), TRIMS_FILE)))


def load_trims_from(rows):
    """load_trims over in-memory records (trim / close / void)."""
    voided = {r.get("batch") for r in rows if r.get("kind") == "void" and r.get("batch")}
    closes = {}
    for r in rows:
        if r.get("kind") == "close" and r.get("trim_id"):
            e = to_epoch(r.get("ts"))
            if e is not None and (r["trim_id"] not in closes or e < closes[r["trim_id"]][0]):
                closes[r["trim_id"]] = (e, r.get("ts"), r.get("reason"))
    out, seen = [], set()
    for r in rows:
        if r.get("kind") != "trim" or r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        if r.get("batch") and r["batch"] in voided:
            continue
        t = dict(r)
        t["ts_epoch"] = to_epoch(t.get("ts"))
        if t["ts_epoch"] is None:
            continue
        c = closes.get(t["id"])
        t["closed_at"] = c[1] if c else None
        t["closed_epoch"] = c[0] if c else None
        t["close_reason"] = c[2] if c else None
        t["tokens"] = int(t["tokens_before"]) - int(t["tokens_after"])
        out.append(t)
    return out


def latest_view(data_dir):
    """Latest known tokens per (component, scope): {(comp, scope): (ts, items)}."""
    view = {}
    for r in _read_jsonl(os.path.join(str(data_dir), HISTORY_FILE)):
        if r.get("schema") != SCHEMA:
            continue  # a new schema starts its own series, never offsets the old
        for comp, entry in (r.get("components") or {}).items():
            view[(comp, entry.get("scope"))] = (r.get("ts"), entry.get("items") or {})
    return view


def last_history_epoch(data_dir):
    rows = _read_jsonl(os.path.join(str(data_dir), HISTORY_FILE))
    return to_epoch(rows[-1].get("ts")) if rows else None


# ---------------------------------------------------------------- evidence

def backup_evidence(claude_dir, component, item, scope=""):
    """Path of a TO-written backup that holds this item, or "" when none does."""
    base = os.path.join(str(claude_dir), "_backups")
    try:
        dirs = sorted(os.listdir(base), reverse=True)
    except OSError:
        return ""
    names = [item, item + ".md", item + ".SKILL.md"]
    if component == "memory_md" and scope.startswith("project:"):
        names = ["MEMORY-" + scope[len("project:"):] + ".md"]
    for d in dirs:
        if not d.startswith(TO_BACKUP_PREFIXES):
            continue
        root = os.path.join(base, d)
        for sub in ("", "skill-descriptions", "commands"):
            for n in names:
                p = os.path.join(root, sub, n)
                if os.path.exists(p) or os.path.islink(p):
                    return p
    return ""


# ---------------------------------------------------------------- detection

def detect_trims(prev_view, new_snapshot, ts, claude_dir, open_keys=()):
    """Trim events for items that shrank between the latest view and a new
    snapshot. Only itemized components, only matching (component, scope)
    pairs (a project component missing because the cwd changed is not a
    trim), never a rename (an item gone while a same-size item appeared)."""
    out = []
    open_keys = set(open_keys)
    for comp, entry in new_snapshot.items():
        scope = entry.get("scope")
        prev = prev_view.get((comp, scope))
        if not prev:
            continue
        old_items, new_items = prev[1], entry.get("items") or {}
        if UNITEMIZED in old_items or UNITEMIZED in new_items:
            continue
        appeared = [v for k, v in new_items.items() if k not in old_items]
        for item, before in old_items.items():
            after = int(new_items.get(item, 0))
            before = int(before)
            if before - after < MIN_TRIM_TOKENS or (comp, item, scope) in open_keys:
                continue
            if item not in new_items and any(abs(v - before) <= max(10, before // 10) for v in appeared):
                continue  # renamed, not trimmed
            ev = backup_evidence(claude_dir, comp, item, scope)
            src = "audit_action" if ev else "detected"
            out.append(make_trim(comp, item, scope, before, after, ts, src, evidence=ev,
                                 origin="snapshot_diff"))
    return out


def regrown(trim, items):
    """True when the trimmed item came back: at least half the trim regrew."""
    if trim.get("item") == UNITEMIZED or trim.get("item") not in items:
        return False
    return int(items[trim["item"]]) >= int(trim["tokens_after"]) + max(1, trim["tokens"] // 2)


# ---------------------------------------------------------------- tick

def capture_install_baseline(data_dir, snapshot, ts):
    """Write the per-component install baseline once. Never recaptured: an
    existing baseline is left untouched and False is returned (HISTORY rule 8)."""
    path = os.path.join(str(data_dir), BASELINE_FILE)
    os.makedirs(str(data_dir), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"schema": SCHEMA, "ts": ts, "components": snapshot, "immutable": True}, f)
    return True


def tick(data_dir, snapshot, claude_dir, ts=None, force=False):
    """Record one itemized snapshot: capture the install baseline when none
    exists, append to history when the prefix changed, open trims for items
    that shrank, close trims whose item came back. Returns a summary dict."""
    ts = ts or now_iso()
    data_dir = str(data_dir)
    res = {"baseline_captured": False, "appended": False, "opened": [], "closed": []}
    res["baseline_captured"] = capture_install_baseline(data_dir, snapshot, ts)
    last = last_history_epoch(data_dir)
    if not force and last is not None and (to_epoch(ts) or time.time()) - last < SNAPSHOT_INTERVAL_S:
        return res
    view = latest_view(data_dir)
    h = snapshot_hash(snapshot)
    trims = load_trims(data_dir)
    open_trims = [t for t in trims if not t["closed_at"]]
    for t in open_trims:
        entry = snapshot.get(t["component"])
        if entry and entry.get("scope") == t["scope"] and regrown(t, entry.get("items") or {}):
            close_trim(data_dir, t["id"], ts, "item restored")
            res["closed"].append(t["id"])
    keys = {(t["component"], t["item"], t["scope"]) for t in open_trims if t["id"] not in res["closed"]}
    for t in detect_trims(view, snapshot, ts, claude_dir, open_keys=keys):
        if record_trim(data_dir, t):
            res["opened"].append(t["id"])
    rows = _read_jsonl(os.path.join(data_dir, HISTORY_FILE))
    if not rows or rows[-1].get("hash") != h:
        _append(os.path.join(data_dir, HISTORY_FILE),
                {"schema": SCHEMA, "ts": ts, "hash": h, "components": snapshot})
        res["appended"] = True
    return res


def facade_snapshot(f=None):
    """Itemized snapshot of the live setup through the product's own audit."""
    f = f or _compat.facade()
    comps = f.measure_components()
    return itemize(comps, f.CLAUDE_DIR, f.estimate_tokens_from_frontmatter)


def session_start_tick(f=None, force=False):
    """SessionStart / audit hook: throttled, never raises. Claude Code only."""
    f = f or _compat.facade()
    try:
        if f.detect_runtime() != "claude":
            return None
        last = last_history_epoch(f.SNAPSHOT_DIR)
        have_base = os.path.exists(os.path.join(str(f.SNAPSHOT_DIR), BASELINE_FILE))
        if have_base and not force and last is not None and time.time() - last < SNAPSHOT_INTERVAL_S:
            return None
        return tick(f.SNAPSHOT_DIR, facade_snapshot(f), f.CLAUDE_DIR, force=force)
    except Exception:
        return None


def record_skill_action(action, name, path, f=None):
    """Called by the skill archive/restore action at the moment it succeeds."""
    f = f or _compat.facade()
    try:
        ts = now_iso()
        if action == "archive":
            skill_md = os.path.join(str(path), "SKILL.md")
            marker = os.path.join(str(path), f.SYMLINK_TARGET_MARKER)
            if not os.path.isfile(skill_md) and os.path.isfile(marker):
                with open(marker, encoding="utf-8") as fh:
                    skill_md = os.path.join(fh.read().strip(), "SKILL.md")
            before = int(f.estimate_tokens_from_frontmatter(skill_md))
            if before >= 1:
                record_trim(f.SNAPSHOT_DIR, make_trim("skills", name, "global", before, 0, ts,
                                                      "audit_action", evidence=str(path),
                                                      origin="skill_archive"))
        elif action == "restore":
            for t in load_trims(f.SNAPSHOT_DIR):
                if t["component"] == "skills" and t["item"] == name and not t["closed_at"]:
                    close_trim(f.SNAPSHOT_DIR, t["id"], ts, "skill restored")
    except Exception:
        pass
