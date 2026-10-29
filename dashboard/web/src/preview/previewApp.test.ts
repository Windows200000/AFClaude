import assert from "node:assert/strict";
import { test } from "node:test";
import { h } from "preact";
import render from "preact-render-to-string";
import { clientFromCore, RequestCore } from "../api/client.ts";
import type { NavigationEnv } from "../router.ts";
import { createPreviewTransport } from "./mockTransport.ts";
import { PreviewApp, PreviewBanner } from "./previewApp.ts";

function fakeEnv(path = "/"): NavigationEnv {
  return {
    pathname: () => path,
    pushState: () => {},
    replaceState: () => {},
    onPopState: () => () => {},
  };
}

test("the preview app renders the banner and the shell together, before the mock me() resolves", () => {
  const client = clientFromCore(new RequestCore(createPreviewTransport()));
  const html = render(h(PreviewApp, { client, env: fakeEnv("/") }));
  assert.ok(html.includes("preview-banner"));
  assert.ok(html.includes("Preview build"));
  assert.ok(html.includes("signed out"), "useEffect does not run under render-to-string, so the initial me is null");
  assert.ok(html.includes("shell"), "the real Shell still renders underneath the banner");
});

test("the banner names the fixture's role once me has resolved", () => {
  const html = render(h(PreviewBanner, { me: { user: { id: 1, role: "owner" }, csrf_token: "t", time_zone: "Europe/Berlin" } }));
  assert.ok(html.includes("role shown: owner"));
});

test("the banner says signed out for a null me", () => {
  const html = render(h(PreviewBanner, { me: null }));
  assert.ok(html.includes("signed out"));
});
