"""Inline renderer modules into the dashboard template.

The dashboard ships as one self-contained page (its CSP allows inline scripts
only), so renderer code kept in its own file under assets/ is inlined where the
template carries a ``/*@include name.js*/`` marker. A missing file leaves the
marker as a comment; the page then falls back to the layout that does not need
the module.
"""
import re

_INCLUDE = re.compile(r"/\*@include ([A-Za-z0-9_.-]+\.js)\*/")


def inline(template, assets_dir):
    def _sub(m):
        try:
            src = (assets_dir / m.group(1)).read_text(encoding="utf-8")
        except OSError:
            return m.group(0)
        return src.replace("</script", "<\\/script")
    return _INCLUDE.sub(_sub, template)
