#!/usr/bin/env python3
"""Tests for dashboard/markdown.py (phase 6c R2, docs/phase6c_plan.md §0, §2): markdown rendered on
the server and sanitised into SanitizedHtml. An XSS corpus (script, event handlers, javascript: /
data: / vbscript: links, SVG and MathML, entity and nesting tricks) must leave nothing executable:
every tag and attribute of the output is on the allowlist and every link has an allowed scheme.
Each of the two lines (markdown-it with raw HTML off, nh3) is also checked on its own.

Needs the project venv (markdown-it-py, nh3, pydantic; Python 3.12):
    .venv/bin/python -m unittest test_dashboard_markdown
"""
import html
import importlib.util
import os
import re
import sys
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_MISSING = [m for m in ("pydantic", "markdown_it", "nh3") if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'markdown-it-py and nh3'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_dashboard_markdown "
        "(.venv/bin/pip install --require-hashes -r requirements-dashboard.txt)"
        ) if _MISSING or sys.version_info < (3, 12) else ""

if not SKIP:
    from dashboard import markdown
    from dashboard.api_types import SanitizedHtml

CORPUS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    "<img src=x onerror=alert(1)>",
    "<svg onload=alert(1)>",
    "<svg><script>alert(1)</script></svg>",
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
    "<iframe src=//evil.example></iframe>",
    "<style>body{display:none}</style>",
    "<details open ontoggle=alert(1)>x</details>",
    "<form action=//evil.example><input name=x></form>",
    '<a href="javascript:alert(1)">x</a>',
    "**<b onmouseover=alert(1)>x</b>**",
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](&#x6A;avascript&colon;alert(1))",
    "[x](javascript&#58;alert(1))",
    "[x]( javascript:alert(1))",
    "[x](<javascript:alert(1)>)",
    "[x](\x00javascript:alert(1))",
    "[x](java\tscript:alert(1))",
    "[x](vbscript:msgbox(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[x](file:///etc/passwd)",
    "<javascript:alert(1)>",
    "<data:text/html,x>",
    "![x](javascript:alert(1))",
    "![x](https://evil.example/pixel.png)",
    "[x][r]\n\n[r]: javascript:alert(1)",
    '[x](https://e.example "a\\" onmouseover=\\"alert(1)")',
    "[<img src=x onerror=alert(1)>](https://e.example)",
    "[x](https://e.example)<script>alert(1)</script>",
    "`<script>alert(1)</script>`",
    "```html\n<script>alert(1)</script>\n```",
    "<!-- <script>alert(1)</script> -->",
    "<![CDATA[<script>alert(1)</script>]]>",
    "> <script>alert(1)</script>",
    "| a |\n|---|\n| <img src=x onerror=alert(1)> |",
    "- [x](javascript:alert(1))\n  - <svg/onload=alert(1)>",
    "<a href=\"https://e.example\" target=_blank>x</a>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "<scr<script>ipt>alert(1)</script>",
    "<div style=\"background:url(javascript:alert(1))\">x</div>",
    "<base href=//evil.example/>",
    "<meta http-equiv=refresh content=0;url=//evil.example>",
    "<object data=//evil.example/x></object><embed src=//evil.example/x>",
]


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    handle_startendtag = handle_starttag


def _scheme(url):
    """The scheme a browser reads from an href (the URL standard: C0 controls and spaces stripped
    at the ends, tabs and newlines removed; a scheme is [a-z][a-z0-9+.-]* before ':'), None = relative."""
    url = re.sub(r"[\t\n\r]", "", html.unescape(url).strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e"
                                                            "\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a"
                                                            "\x1b\x1c\x1d\x1e\x1f \t\n\r"))
    m = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):", url)
    return m.group(1).lower() if m else None


@unittest.skipIf(bool(SKIP), SKIP)
class Sanitised(unittest.TestCase):
    def assertSafe(self, out, src):
        p = _Collect()
        p.feed(out)
        p.close()
        for tag, attrs in p.tags:
            self.assertIn(tag, markdown.TAGS, f"{src!r} -> {out!r}")
            allowed = markdown.ATTRIBUTES.get(tag, frozenset()) | ({"rel"} if tag == "a" else set())
            for name, value in attrs:
                self.assertIn(name, allowed, f"{src!r} -> {out!r}")
                if name == "href":
                    self.assertIn(_scheme(value or ""), {None, *markdown.LINK_SCHEMES}, f"{src!r} -> {out!r}")
            if tag == "a":
                self.assertIn(("rel", "noopener noreferrer"), attrs, f"{src!r} -> {out!r}")
        lowered = out.lower()     # text is escaped, so a tag name after "<" can only be a tag
        for bad in ("<script", "<svg", "<img", "<iframe", "<style", "<math", "<form", "<input", "<object",
                    "<embed", "<base", "<meta"):
            self.assertNotIn(bad, lowered, f"{src!r} -> {out!r}")

    def test_xss_corpus(self):
        for src in CORPUS:
            with self.subTest(src=src):
                out = markdown.render(src)
                self.assertIsInstance(out, SanitizedHtml)
                self.assertSafe(out.root, src)

    def test_each_line_holds_on_its_own(self):
        for src in CORPUS:
            with self.subTest(src=src):
                self.assertSafe(markdown._CLEANER.clean(src), src)             # nh3 over raw HTML
        for src in CORPUS:
            with self.subTest(src=src):
                rendered = markdown._MD.render(src)                             # markdown-it alone
                self.assertNotRegex(rendered.lower(), r"<(script|svg|img|iframe|style|math|a href=\"(javascript|data|vbscript))")

    def test_markdown_renders(self):
        cases = {
            "# T": "<h1>T</h1>",
            "**b** *i* ~~s~~ `c`": "<p><strong>b</strong> <em>i</em> <s>s</s> <code>c</code></p>",
            "[r](/projects/1)": '<a href="/projects/1" rel="noopener noreferrer">r</a>',
            "[x](https://e.example/a?b=1 \"t\")": '<a href="https://e.example/a?b=1" title="t" rel="noopener noreferrer">x</a>',
            "[m](mailto:a@b.example)": '<a href="mailto:a@b.example" rel="noopener noreferrer">m</a>',
            "3. a\n4. b": '<ol start="3">',
            "| a | b |\n|--:|---|\n| 1 | 2 |": "<td>1</td>",
            "> q": "<blockquote>",
            "line one\nline two": "<p>line one\nline two</p>",
        }
        for src, part in cases.items():
            with self.subTest(src=src):
                self.assertIn(part, markdown.render(src).root)
        self.assertEqual(markdown.render("").root, "")

    def test_text_stays_text(self):
        out = markdown.render("a < b && c > d, 'q' \"q\"").root
        self.assertEqual(out, "<p>a &lt; b &amp;&amp; c &gt; d, 'q' \"q\"</p>\n")

    def test_serialises_as_a_string(self):
        from dashboard.api_types import Model

        class R(Model):
            html: SanitizedHtml
        self.assertEqual(R(html=markdown.render("*x*")).model_dump_json(), '{"html":"<p><em>x</em></p>\\n"}')


if __name__ == "__main__":
    unittest.main()
