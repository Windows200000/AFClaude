import assert from "node:assert/strict";
import { test } from "node:test";
import { h } from "preact";
import render from "preact-render-to-string";
import { ROUTES, type NavigationEnv } from "./router.ts";
import { PLACEHOLDER_STATE, Shell } from "./shell.ts";
import { isVerboseTooltips, setVerboseTooltips, Tooltip } from "./components/tooltip.ts";

function fakeEnv(path = "/"): NavigationEnv {
  return {
    pathname: () => path,
    pushState: () => {},
    replaceState: () => {},
    onPopState: () => () => {},
  };
}

test("the shell renders one nav link per route and the top bar's placeholders", () => {
  const html = render(h(Shell, { env: fakeEnv("/") }));
  for (const route of ROUTES) {
    assert.ok(html.includes(`>${route.name}<`), `missing nav link for ${route.name}`);
  }
  assert.ok(html.includes("status-ok"), "the placeholder automation dot should render ok");
  assert.ok(html.includes(String(PLACEHOLDER_STATE.inboxCount)), "the inbox badge should render its count");
});

test("an unknown path renders the not_found view, not a crash", () => {
  const html = render(h(Shell, { env: fakeEnv("/nope") }));
  assert.ok(html.includes("Not found."));
});

test("a known path renders its view placeholder", () => {
  const html = render(h(Shell, { env: fakeEnv("/stalled") }));
  assert.ok(html.includes('data-view="stalled"'));
});

test("the tooltip renders its short text by default and swaps to verbose when toggled", () => {
  setVerboseTooltips(false);
  const short = render(h(Tooltip, { short: "short text", verbose: "much longer explanation", children: "x" }));
  assert.ok(short.includes("short text"));
  assert.ok(!short.includes("much longer explanation"));

  setVerboseTooltips(true);
  assert.equal(isVerboseTooltips(), true);
  const verbose = render(h(Tooltip, { short: "short text", verbose: "much longer explanation", children: "x" }));
  assert.ok(verbose.includes("much longer explanation"));
  setVerboseTooltips(false);   // leave global state as found for later tests in this process
});

test("a tooltip with no verbose text keeps the short one even when the toggle is on", () => {
  setVerboseTooltips(true);
  const html = render(h(Tooltip, { short: "only text", children: "x" }));
  assert.ok(html.includes("only text"));
  setVerboseTooltips(false);
});
