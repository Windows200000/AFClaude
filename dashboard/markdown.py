"""Markdown to HTML on the server (docs/phase6c_plan.md §0, §2, R2; design §9.8): the only producer
of api_types.SanitizedHtml. Questions, docs, hand-off texts and review reports reach the browser
through render(); prompts and decision words stay plain text (the frontend shows them as text).

Two lines, each enough on its own for what it checks:
  1. markdown-it-py (CommonMark plus tables and strikethrough): raw HTML off, so `<script>` in the
     text stays text; images off (no remote fetches; CSP allows none anyway); a link only with
     http(s), mailto or no scheme (relative), others stay text.
  2. nh3 (the Rust ammonia sanitiser) over its output, with a fixed tag and attribute allowlist,
     the same URL schemes, and rel="noopener noreferrer" on every link. Inline styles are dropped
     (the CSP has no 'unsafe-inline'), so a table's column alignment is not kept.

markdown-it-py and nh3 are pinned in requirements-dashboard.txt (Python 3.12, the .venv and the image).
"""
from __future__ import annotations

import re

import nh3
from markdown_it import MarkdownIt

from dashboard.api_types import SanitizedHtml

LINK_SCHEMES = frozenset({"http", "https", "mailto"})
TAGS = frozenset({"a", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "s", "code", "pre",
                  "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td"})
ATTRIBUTES: dict[str, frozenset[str]] = {"a": frozenset({"href", "title"}), "ol": frozenset({"start"})}
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")


class _Markdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        """A link destination (already normalised, i.e. percent-encoded) is kept only without a
        scheme (relative) or with one of LINK_SCHEMES."""
        m = _SCHEME_RE.match(url)
        return m is None or m.group(1).lower() in LINK_SCHEMES


_MD = _Markdown("commonmark", {"html": False}).enable(["table", "strikethrough"]).disable("image")
_CLEANER = nh3.Cleaner(tags=set(TAGS), attributes={k: set(v) for k, v in ATTRIBUTES.items()},
                       url_schemes=set(LINK_SCHEMES), link_rel="noopener noreferrer", strip_comments=True)


def render(text: str) -> SanitizedHtml:
    """`text` (markdown) as sanitised HTML for a SanitizedHtml response field."""
    return SanitizedHtml.model_construct(_CLEANER.clean(_MD.render(text)))
