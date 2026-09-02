"""U12 docs assertions for the Antigravity adapter.

Covers the plan's U12 test scenarios: the README install block and Works-on
sentence, the docs-site sidebar slugs, the existence of both new pages, and
the capability-matrix column / port entry.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_readme_names_antigravity_install_and_works_on():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "install.sh --antigravity" in readme
    assert "**Google Antigravity**" in readme


def test_docs_site_pages_exist():
    for rel in (
        "docs-site/src/content/docs/install/antigravity.mdx",
        "docs-site/src/content/docs/platforms/antigravity.mdx",
        "docs/antigravity.md",
        "antigravity/README.md",
    ):
        assert (REPO / rel).is_file(), rel


def test_sidebar_names_both_new_slugs():
    config = (REPO / "docs-site/astro.config.mjs").read_text(encoding="utf-8")
    assert 'slug: "install/antigravity"' in config
    assert 'slug: "platforms/antigravity"' in config


def test_capability_matrix_has_antigravity_column_and_port():
    matrix = (
        REPO / "docs-site/src/content/docs/reference/capability-matrix.mdx"
    ).read_text(encoding="utf-8")
    assert "Antigravity (beta)" in matrix
    assert "| Antigravity | 24847 |" in matrix
