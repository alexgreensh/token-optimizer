"""One-time, explicit, reversible import of a past audit into the trim ledger.

Measures what a Token Optimizer backup holds against the live setup, with the
same estimators the audit uses, and proposes one audit_action trim per item
that is smaller (or gone) today. Dry run by default: nothing is written
without --apply. Every written trim carries one batch id; --revert <batch>
appends a void record, so an import is undone without deleting a line.

Inputs (all optional except the backup dir):
  backup dir       token-optimizer-<date>-<time>/: CLAUDE.md, MEMORY-<proj>.md,
                   skill-descriptions/<skill>.SKILL.md, commands/
  skills archive   <name>/SKILL.md for each archived skill
  commands archive *.md and <ns>/*.md for each archived command

MEMORY.md is measured as Claude Code loads it (first 200 lines, at most 25 KB,
cut at a line), not as a whole file: a longer file was never sent in full.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from . import structural_ledger as ledger

MEMORY_MAX_LINES = 200
MEMORY_MAX_BYTES = 25 * 1024


def loaded_memory_chars(path):
    """Characters of MEMORY.md that Claude Code injects."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")[:MEMORY_MAX_LINES]
    except OSError:
        return 0
    kept, size = [], 0
    for line in lines:
        b = len(line.encode("utf-8")) + 1
        if size + b > MEMORY_MAX_BYTES:
            break
        kept.append(line)
        size += b
    return len("\n".join(kept))


def _newest_readme_ts(*dirs):
    best = None
    for d in dirs:
        p = os.path.join(d, "README.txt") if d else ""
        if p and os.path.isfile(p):
            m = os.path.getmtime(p)
            best = m if best is None or m > best else best
    if best is None:
        return None
    return datetime.fromtimestamp(best, timezone.utc).isoformat(timespec="seconds")


def _md_items(root):
    """{item: path} for *.md and <ns>/*.md, named like the audit names them."""
    out = {}
    if not root or not os.path.isdir(root):
        return out
    for fn in sorted(os.listdir(root)):
        p = os.path.join(root, fn)
        if fn.endswith(".md") and os.path.isfile(p):
            out[fn[:-3]] = p
        elif os.path.isdir(p):
            for sub in sorted(os.listdir(p)):
                if sub.endswith(".md"):
                    out[fn + "/" + sub[:-3]] = os.path.join(p, sub)
    return out


def propose(backup_dir, skills_archive, commands_archive, claude_dir, ts, est_fm, est_file, cpt=4.0):
    """List of trim dicts (not written)."""
    claude_dir = str(claude_dir)
    live_skills = os.path.join(claude_dir, "skills")
    live_cmds = os.path.join(claude_dir, "commands")
    out = []

    def add(comp, item, scope, before, after, evidence):
        if before - after >= ledger.MIN_TRIM_TOKENS:
            out.append(ledger.make_trim(comp, item, scope, before, after, ts, "audit_action",
                                        evidence=evidence, origin="seed_import"))

    # archived skills
    if skills_archive and os.path.isdir(skills_archive):
        for name in sorted(os.listdir(skills_archive)):
            src = os.path.join(skills_archive, name, "SKILL.md")
            if not os.path.isfile(src):
                continue
            live = os.path.join(live_skills, name, "SKILL.md")
            after = int(est_fm(live)) if os.path.isfile(live) else 0
            add("skills", name, "global", int(est_fm(src)), after, os.path.join(skills_archive, name))
    # rewritten skill descriptions
    desc = os.path.join(backup_dir, "skill-descriptions")
    if os.path.isdir(desc):
        for fn in sorted(os.listdir(desc)):
            if not fn.endswith(".SKILL.md"):
                continue
            name = fn[: -len(".SKILL.md")]
            live = os.path.join(live_skills, name, "SKILL.md")
            if os.path.isfile(live):
                add("skills", name, "global", int(est_fm(os.path.join(desc, fn))), int(est_fm(live)),
                    os.path.join(desc, fn))
    # archived commands, then rewritten commands still live
    archived = _md_items(commands_archive)
    for item, path in archived.items():
        live = os.path.join(live_cmds, item + ".md")
        add("commands", item, "global", int(est_fm(path)), int(est_fm(live)) if os.path.isfile(live) else 0, path)
    for item, path in _md_items(os.path.join(backup_dir, "commands")).items():
        live = os.path.join(live_cmds, item + ".md")
        if item not in archived and os.path.isfile(live):
            add("commands", item, "global", int(est_fm(path)), int(est_fm(live)), path)
    # global CLAUDE.md
    bk = os.path.join(backup_dir, "CLAUDE.md")
    if os.path.isfile(bk):
        add("claude_md_global", "CLAUDE.md", "global", int(est_file(bk)),
            int(est_file(os.path.join(claude_dir, "CLAUDE.md"))), bk)
    # per-project MEMORY.md (only loads in its own project's sessions)
    for fn in sorted(os.listdir(backup_dir)):
        if not (fn.startswith("MEMORY-") and fn.endswith(".md")):
            continue
        proj = fn[len("MEMORY-"):-3]
        live = os.path.join(claude_dir, "projects", proj, "memory", "MEMORY.md")
        before = int(loaded_memory_chars(os.path.join(backup_dir, fn)) / cpt)
        after = int(loaded_memory_chars(live) / cpt) if os.path.isfile(live) else 0
        add("memory_md", "MEMORY.md", "project:" + proj, before, after, os.path.join(backup_dir, fn))
    return out


def run(a, f):
    """CLI body for `structural.py seed`."""
    data = f.SNAPSHOT_DIR
    if a.revert:
        if a.apply:
            ledger.void_batch(data, a.revert, ledger.now_iso(), "seed import reverted")
            return {"reverted": a.revert}
        return {"dry_run": True, "would_revert": a.revert}
    bdir = os.path.abspath(os.path.expanduser(a.backup_dir))
    if not os.path.isdir(bdir):
        return {"error": "backup dir not found: %s" % bdir}
    sk = os.path.abspath(os.path.expanduser(a.skills_archive)) if a.skills_archive else ""
    cm = os.path.abspath(os.path.expanduser(a.commands_archive)) if a.commands_archive else ""
    ts = a.ts or _newest_readme_ts(sk, cm) or datetime.fromtimestamp(
        os.path.getmtime(bdir), timezone.utc).isoformat(timespec="seconds")
    trims = propose(bdir, sk, cm, f.CLAUDE_DIR, ts, f.estimate_tokens_from_frontmatter,
                    f.estimate_tokens_from_file, f.CHARS_PER_TOKEN)
    batch = "seed-" + hashlib.sha1(("|".join(sorted(t["id"] for t in trims))).encode()).hexdigest()[:12]
    for t in trims:
        t["batch"] = batch
    summary = {"dry_run": not a.apply, "batch": batch, "ts": ts, "trims": len(trims),
               "tokens_per_call_by_scope": {}}
    for t in trims:
        k = t["scope"]
        summary["tokens_per_call_by_scope"][k] = summary["tokens_per_call_by_scope"].get(k, 0) + \
            t["tokens_before"] - t["tokens_after"]
    summary["items"] = [{k: t[k] for k in ("component", "item", "scope", "tokens_before", "tokens_after", "evidence")}
                        for t in trims]
    if a.apply:
        summary["written"] = sum(1 for t in trims if ledger.record_trim(data, t))
        summary["revert_with"] = "structural.py seed --backup-dir X --revert %s --apply" % batch
    return summary
