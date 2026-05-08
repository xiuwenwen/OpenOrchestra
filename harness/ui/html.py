from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_STATIC_DIR = Path(__file__).with_name("static")


@lru_cache(maxsize=1)
def render_html() -> str:
    template = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    return template.replace("{{ OPENORCHESTRA_CSS }}", css).replace("{{ OPENORCHESTRA_JS }}", js)
