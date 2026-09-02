"""Docs claims: every absolute zero-network claim is qualified now that the
Teams edition adds an admin-enabled telemetry channel.

Run: python3 -m pytest tests/test_teams_telemetry_docs.py -v
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ABSOLUTE_PHRASES = [
    "nothing leaves",
    "zero network",
    "No data is transmitted",
    "never leaves the machine",
    "telemetry-none",
]

QUALIFIER_CONTEXT = ("admin", "fleet", "by default", "Teams", "default")


def _qualified(text, idx, phrase_len):
    window = text[max(0, idx - 200):idx + phrase_len + 200].lower()
    return any(q.lower() in window for q in QUALIFIER_CONTEXT)


def test_no_unqualified_absolute_claims():
    offenders = []
    for pattern in ("*.md", "*.mdx"):
        for path in ROOT.rglob(pattern):
            s = str(path)
            if any(x in s for x in (".sprint-scratch", "node_modules", ".git/",
                                    "sessions/")):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for phrase in ABSOLUTE_PHRASES:
                for m in re.finditer(re.escape(phrase), text):
                    if not _qualified(text, m.start(), len(phrase)):
                        offenders.append(f"{path}:{text[:m.start()].count(chr(10)) + 1}: {phrase}")
    assert not offenders, "unqualified absolute claims:\n" + "\n".join(offenders)


def test_privacy_md_names_the_mechanics():
    text = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    for needle in ("fleet.json", "hash_key", "TO_FLEET_DISABLE", "--dry-run",
                   "user_label", "billing_mode"):
        assert needle in text, needle


def test_gitignore_lists_fleet_files():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for needle in ("fleet.json", "fleet-outbox.jsonl", "fleet-cursor.json", "fleet.log"):
        assert needle in text, needle


def test_teams_doc_exists_and_covers_admin_flow():
    text = (ROOT / "docs" / "teams-telemetry.md").read_text(encoding="utf-8")
    for needle in ("--enable", "add-org", "serve", "OTEL", "rotate-token",
                   "user-level", "dashboard"):
        assert needle in text, needle
