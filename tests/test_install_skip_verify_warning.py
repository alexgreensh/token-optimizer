"""The installer's verification bypass must announce itself on stderr.

TOKEN_OPTIMIZER_SKIP_VERIFY=1 is a legitimate escape hatch for air-gapped
installs: it skips both the release-tag pin and the out-of-band checksum
verification. But if a user is socially engineered into setting it, a silent
or stdout-only skip leaves no visible trace in terminal logs (which capture
stderr separately from program output). The skip must print a prominent
warning to stderr so it appears in places a victim would actually check.

This test reads install.sh source and verifies:
  1. The SKIP_VERIFY warning is directed to stderr (>&2).
  2. The warning mentions the risk (not just "skipping").
  3. The warning is emitted at BOTH the clone-unpinned point and the
     checksum-skip point, so neither phase is silently unguarded.

Run: python3 -m pytest tests/test_install_skip_verify_warning.py -v
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _src():
    return INSTALL_SH.read_text(encoding="utf-8")


def _first_skip_verify_else_block(src):
    """Extract the else-branch of the first `if verification_enabled` guard.

    This is the clone-stage skip point (~line 960). The else branch is where
    the SKIP_VERIFY warning lives.
    """
    guard_pos = src.index("if verification_enabled")
    after_guard = src[guard_pos:]
    else_pos = after_guard.index("\nelse\n")
    fi_pos = after_guard.index("\nfi\n", else_pos)
    return after_guard[else_pos:fi_pos + 4]


def test_skip_verify_warning_goes_to_stderr():
    """The skip-verify announcement must print to stderr, not stdout."""
    src = _src()
    block = _first_skip_verify_else_block(src)

    assert ">&2" in block, (
        "The TOKEN_OPTIMIZER_SKIP_VERIFY=1 warning must print to stderr "
        "(>&2), not stdout. A stdout-only warn() is invisible in terminal "
        "logs that capture stderr separately from program output."
    )


def test_skip_verify_warning_mentions_risk():
    """The warning must say WHAT is being skipped, not just 'skipping'."""
    src = _src()
    block = _first_skip_verify_else_block(src)

    # Must mention the env var name and the risk (no checksum / no verification).
    assert "TOKEN_OPTIMIZER_SKIP_VERIFY" in block
    assert "checksum" in block.lower() or "integrity" in block.lower() or "verification" in block.lower()


def test_skip_verify_warning_is_prominent():
    """The warning must be multi-line and visible, not a single quiet line."""
    src = _src()
    block = _first_skip_verify_else_block(src)

    # A single printf/warn line is not prominent enough for a supply-chain
    # bypass. Require multiple output lines directed to stderr.
    stderr_lines = [l for l in block.split("\n") if ">&2" in l]
    assert len(stderr_lines) >= 3, (
        f"The skip-verify warning should be prominent: at least 3 stderr "
        f"lines, got {len(stderr_lines)}. A user who was social-engineered "
        f"into setting the env var needs to SEE it."
    )


def test_checksum_skip_also_warns_on_stderr():
    """The checksum-verification skip (line ~1122) must also warn on stderr.

    There are two skip points: the clone-unpinned point (~line 965) and the
    checksum-verification skip (~line 1122). Both must announce on stderr.
    The checksum block is `if verification_enabled; then ... fi`; after the
    fix it must have an else that warns on stderr.
    """
    src = _src()
    # Locate the Integrity Verification section by its comment marker.
    marker = "# ── Integrity Verification"
    iv_section = src[src.index(marker):]
    # Find the `if verification_enabled; then` guard in this section.
    guard_start = iv_section.index("if verification_enabled")
    guard_block = iv_section[guard_start:]
    # Find the matching `fi` that closes the OUTER guard. There is an inner
    # if/fi pair; we need the outer one. Count nesting.
    depth = 0
    end = 0
    for i, line in enumerate(guard_block.split("\n")):
        stripped = line.strip()
        if stripped.startswith("if ") or stripped == "if":
            depth += 1
        elif stripped == "fi":
            depth -= 1
            if depth == 0:
                end = sum(len(l) + 1 for l in guard_block.split("\n")[:i + 1])
                break
    guard_block = guard_block[:end]

    # The outer guard must have an else branch that warns on stderr.
    assert "else" in guard_block, (
        "The checksum-verification guard must have an else branch so a "
        "SKIP_VERIFY=1 user sees that checksums were not checked."
    )
    # The else branch (the last else in the block, belonging to the outer if)
    # must direct to stderr.
    assert ">&2" in guard_block, (
        "The checksum-verification skip else-branch must warn on stderr."
    )
