#!/usr/bin/env python3
"""Run the quickview page's JavaScript in real headless browsers against a status.json.

The host has no node or browser; this runs inside the Playwright image (throwaway container):

  docker run --rm -v "$PWD":/w -w /w mcr.microsoft.com/playwright/python:v1.48.0-jammy \\
    sh -c 'pip install -q playwright==1.48.0 && python3 tools/quickview_render_check.py \\
           [--page quickview/AFClaude.html] [--status data/quickview/status.json] [--browsers chromium,firefox,webkit]'

The page is served at /private/AFClaude.html as on ovm3, status.json and an empty inbox under
/private/AFClaude-data/ (the CDN scripts load for real when there is network, else the page's
plain-text fallback). Fails (exit 1) on any page error, a load error shown on the page, or a
Session / Week tile without a percentage while status.json has the numbers. Exit 2 = could not run.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = {"messages": [], "reset_hours": 5, "max_chars": 4000}


def check(pw, browser, html, status):
    errors = []
    b = getattr(pw, browser).launch()
    try:
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(f"page error: {e}"))
        pg.on("console", lambda m: m.type == "error" and errors.append(f"console.error: {m.text}"))

        def route(r):
            u = r.request.url
            if u.endswith("/private/AFClaude.html"):
                return r.fulfill(body=html, content_type="text/html")
            if "/private/AFClaude-data/status.json" in u:
                return r.fulfill(body=status, content_type="application/json")
            if "/private/AFClaude-data/api/inbox" in u:
                return r.fulfill(body=json.dumps(INBOX), content_type="application/json")
            if u.startswith("https://cdnjs.cloudflare.com/"):
                return r.continue_()
            return r.fulfill(status=404, body="not found")

        pg.route("**/*", route)
        pg.goto("http://quickview.test/private/AFClaude.html")
        pg.wait_for_function("document.getElementById('updated').textContent.indexOf('exported') === 0 || "
                             "!document.getElementById('error').hidden", timeout=15000)
        pg.wait_for_timeout(500)
        if not pg.is_hidden("#error"):
            errors.append("load error on the page: " + pg.inner_text("#error"))
        usage = pg.inner_text("#usage")
        k = json.loads(status).get("keepalive") or {}
        for label, key in (("SESSION", "session"), ("WEEK", "weekly")):
            if (k.get(key) or {}).get("percent") is None:
                continue
            want = f"{round(k[key]['percent'])}%"
            if not re.search(rf"^{label}\s*\n\s*{re.escape(want)}\s*$", usage, re.M | re.I):
                errors.append(f"the {label.title()} tile does not show {want}: {usage[:200]!r}")
    finally:
        b.close()
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--page", default=os.path.join(HERE, "quickview", "AFClaude.html"))
    ap.add_argument("--status", default=os.path.join(HERE, "data", "quickview", "status.json"))
    ap.add_argument("--browsers", default="chromium,firefox,webkit")
    a = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: run this in the Playwright image (see the module doc)")
        return 2
    with open(a.page, encoding="utf-8") as fh:
        html = fh.read()
    with open(a.status, encoding="utf-8") as fh:
        status = fh.read()
    failed = False
    with sync_playwright() as pw:
        for browser in a.browsers.split(","):
            errors = check(pw, browser, html, status)
            print(f"{browser}: {'FAIL' if errors else 'PASS'}")
            for e in errors:
                print("  " + e)
            failed = failed or bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
