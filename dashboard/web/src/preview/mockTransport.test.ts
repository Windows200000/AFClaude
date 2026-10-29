import assert from "node:assert/strict";
import { test } from "node:test";
import { ENDPOINTS, type EndpointName } from "../api/types.gen.ts";
import { FIXTURES } from "./fixtures.ts";
import { createPreviewTransport } from "./mockTransport.ts";

test("every endpoint in ENDPOINTS has a fixture", () => {
  const names = Object.keys(ENDPOINTS) as EndpointName[];
  assert.ok(names.length > 0);
  for (const name of names) {
    assert.ok(name in FIXTURES, `missing fixture for ${name}`);
  }
});

test("the mock transport answers a known endpoint with its fixture, with no network call", async () => {
  const transport = createPreviewTransport();
  const response = await transport.fetch(ENDPOINTS.health.path, { method: ENDPOINTS.health.method });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(body, FIXTURES.health);
});

test("the mock transport answers me() with its fixture", async () => {
  const transport = createPreviewTransport();
  const response = await transport.fetch(ENDPOINTS.me.path, { method: ENDPOINTS.me.method });
  const body = await response.json();
  assert.deepEqual(body, FIXTURES.me);
});

test("an unmatched method/path throws rather than returning a guessed response (D-235)", async () => {
  const transport = createPreviewTransport();
  await assert.rejects(() => transport.fetch("/api/v1/does-not-exist", { method: "GET" }));
});
