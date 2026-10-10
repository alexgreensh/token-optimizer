"""Per-pattern anchor gating in credential_patterns.redact_credentials.

Why this exists: the first H-8 attempt gated the whole regex suite on one
global prefix scan. Real command output almost always contains "https://",
so the scan fired and every pattern still ran, and the three new M-12
patterns (case-insensitive alternations scanned at every position) made the
hot path ~2x slower than before the "fix". Each pattern now carries literal
anchors; a pattern whose anchors are absent is skipped.

These tests pin: anchors are only declared for known patterns; anchors are sufficient (a
pattern can never match text lacking all of its anchors); gating never
changes the result versus running every pattern unconditionally; and the
AWS secret pattern actually matches the common ``aws_secret_access_key``
spelling (the first version's ``\\b`` could never match it).
"""
from __future__ import annotations

import os
import random
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "token-optimizer", "scripts"))

import credential_patterns as cp  # noqa: E402


def _ungated(text: str) -> str:
    """Reference: run every pattern unconditionally (old two-phase loop)."""
    for label, pat in cp.CREDENTIAL_PATTERNS:
        if "keep" in pat.groupindex:
            text = pat.sub(rf"\g<keep>[CREDENTIAL REDACTED: {label}]", text)
        else:
            text = pat.sub(f"[CREDENTIAL REDACTED: {label}]", text)
    return text


def _j(*parts: str) -> str:
    """Join fixture fragments at runtime so no literal in this file matches a
    real-secret shape (GitHub push protection scans test files too)."""
    return "".join(parts)


_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ALNUM = _LETTERS + "0123456789"

SECRETS = [
    _j("AKIA", "IOSFODNN7EXAMPLE"),
    _j("sk-", _ALNUM),
    _j("sk-ant-", _ALNUM),
    _j("ghp_", _ALNUM),
    _j("gho_", _ALNUM),
    _j("ghs_", _ALNUM),
    _j("ghr_", _ALNUM),
    _j("github_pat_", "A" * 82),
    _j("npm_", _ALNUM),
    _j("xoxb-", "123456789012-", _LETTERS[:24]),
    _j("xoxp-", "123456789012-", _LETTERS[:24]),
    _j("xoxa-", "123456789012-", _LETTERS[:24]),
    _j("sk_live_", _LETTERS),
    _j("rk_live_", _LETTERS),
    _j("hf_", "a" * 34),
    _j("glpat-", _ALNUM),
    _j("glrt-", _ALNUM),
    _j("Authorization: Bearer ", "abcdefghijklmnopqrstuvwxyz"),
    _j("AIza", "SyA-", _ALNUM[:35]),
    _j("ya29.", _ALNUM),
    _j("eyJ", "hbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0In0.", "abcdefghijklmnopqrstuvwxyz"),
    _j("-----BEGIN ", "RSA PRIVATE KEY-----"),
    _j("postgres://", "user:s3cr3t@db.example.com:5432/app"),
    _j("https://", "alice:hunter2@example.com/path"),
    _j("curl https://api.example.com/v1?", "access_token=abcdef0123456789abcdef"),
    _j("mysql -u root -p", "S3cretPass -e 'select 1'"),
    _j("PGPASSWORD=", "hunter22 psql -h db"),
    _j("aws_secret_access_key = ", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    _j('"SecretAccessKey": "', "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", '"'),
    _j("psql --password=", "hunter23 -h db"),
    _j("some-tool --auth-token ", "tok3nvalue --verbose"),
    _j("sshpass -p ", "hunter24 ssh box"),
    _j("redis-cli -a ", "hunter25 ping"),
]


def _realistic_lines(n: int) -> str:
    random.seed(7)
    tmpl = [
        "Compiling src/module_{i}.c",
        "   -> Installing package-{i} 1.{j}.0",
        "GET /api/users/{i} 200 {j}ms",
        "-rw-r--r--  1 alex staff  {j} Sep  2 12:{i:02d} file_{i}.py",
        "npm WARN deprecated lib-{i}@{j}.0.0",
        "PASSED tests/test_thing_{i}.py::test_case_{j}",
        "[INFO] worker-{i} processed batch {j} in {j}ms",
        "modified:   src/app/component_{i}.tsx",
        "https://registry.npmjs.org/package-{i}/-/package-{i}-1.{j}.tgz",
        "export PATH=/usr/local/bin:$PATH  # item {i}",
    ]
    return "\n".join(random.choice(tmpl).format(i=k % 97, j=k % 53) for k in range(n))


def test_anchored_patterns_have_lowercase_anchors():
    labels = {label for label, _ in cp.CREDENTIAL_PATTERNS}
    unknown = [label for label in cp._PATTERN_ANCHORS if label not in labels]
    assert not unknown, f"anchors for unknown patterns: {unknown}"
    for label, anchors in cp._PATTERN_ANCHORS.items():
        assert anchors, f"empty anchor tuple for {label}"
        assert all(a == a.lower() for a in anchors), f"anchors must be lowercase: {label}"


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_is_redacted_and_gating_matches_reference(secret):
    line = f"prefix text {secret} suffix"
    gated = cp.redact_credentials(line)
    assert secret not in gated, f"secret survived redaction: {gated!r}"
    assert gated == _ungated(line)


def test_anchor_absent_means_pattern_cannot_match():
    """Every secret sample trips at least one anchor of the pattern that redacts it."""
    for secret in SECRETS:
        lowered = f"x {secret} y".lower()
        matched = [label for label, pat in cp.CREDENTIAL_PATTERNS if pat.search(f"x {secret} y")]
        assert matched, f"no pattern matches sample {secret!r}"
        for label in matched:
            if label not in cp._PATTERN_ANCHORS:
                continue
            assert any(a in lowered for a in cp._PATTERN_ANCHORS[label]), (
                f"{label} matched {secret!r} but none of its anchors are present"
            )


def test_gating_equivalent_on_realistic_output_with_embedded_secrets():
    text = _realistic_lines(2000)
    lines = text.split("\n")
    for k, secret in enumerate(SECRETS):
        lines.insert((k * 71) % len(lines), secret)
    text = "\n".join(lines)
    assert cp.redact_credentials(text) == _ungated(text)


def test_aws_secret_access_key_spelling_matches():
    out = cp.redact_credentials(_j("aws_secret_access_key = ", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"))
    assert "wJalrXUtnFEMI" not in out and "AWS secret key" in out


def test_placeholder_is_idempotent():
    once = cp.redact_credentials("Authorization: Bearer abcdefghijklmnop")
    assert cp.redact_credentials(once) == once


def test_realistic_output_is_not_slower_than_a_generous_bound(monkeypatch):
    """Catastrophic-regression guard only (the first H-8 attempt doubled the
    cost); a tight bound would flake on slow CI runners."""
    text = _realistic_lines(10_000)

    # Deterministic half: count pattern substitutions. A broken anchor gate
    # runs EVERY pattern once per call — the gate must skip at least one, and
    # on this fixture it skips several (measured: 22 of 28 run).
    subs: list = []
    orig = cp._sub_with_placeholder

    def _spy(pat, label, txt):
        subs.append(label)
        return orig(pat, label, txt)

    monkeypatch.setattr(cp, "_sub_with_placeholder", _spy)
    cp.redact_credentials(text)
    monkeypatch.undo()
    assert 0 < len(subs) < len(cp.CREDENTIAL_PATTERNS), (
        f"anchor gate ran {len(subs)}/{len(cp.CREDENTIAL_PATTERNS)} pattern "
        "subs on clean text — ungated runs every pattern"
    )

    # Timing half, scaled by an INDEPENDENT fixed workload (a fixed regex pass
    # over the same input the gate cannot influence): a same-function
    # baseline would scale with the very slowdown being guarded against, and
    # an absolute floor (the old 1.0s) hides a ~2x-uniform or ~15x blowup on
    # any fast machine.
    probe_re = re.compile(r"\berror\b|\bwarning\b|\bfailed\b")
    probe = float("inf")
    for _ in range(3):
        p0 = time.perf_counter()
        probe_re.findall(text)
        probe = min(probe, time.perf_counter() - p0)

    t0 = time.perf_counter()
    cp.redact_credentials(text)
    elapsed = time.perf_counter() - t0
    # The sub-count assert above is the primary discriminator (it fails the
    # instant the gate drops); this bound only catches a pathological UNIFORM
    # slowdown the count can't see. Healthy is ~4-6x the probe idle, ~40x
    # under 4xCPU contention; an ungated pass lands ~90x+ and grows under
    # load, so 60x sits between loaded-healthy and broken.
    assert elapsed < probe * 60.0, (
        f"redact_credentials took {elapsed:.3f}s on 10K clean lines "
        f"(probe: {probe:.4f}s for one fixed regex pass over the same text)"
    )
