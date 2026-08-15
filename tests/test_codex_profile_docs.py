"""Keep Codex profile documentation aligned with the installer."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_codex_docs_name_the_installer_default_profile():
    """A profile-default change must update the public Codex installation guide."""
    docs = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    installer = ast.parse(
        (ROOT / "skills" / "token-optimizer" / "scripts" / "codex_install.py").read_text(encoding="utf-8")
    )
    profile_default = next(
        keyword.value.value
        for node in ast.walk(installer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and any(isinstance(arg, ast.Constant) and arg.value == "--profile" for arg in node.args)
        for keyword in node.keywords
        if keyword.arg == "default" and isinstance(keyword.value, ast.Constant)
    )

    assert f"The default profile is `{profile_default}`." in docs
    assert "quiet/balanced" in docs
    assert "telemetry/aggressive" in docs
    assert "1-6 hook event types" in docs
