import assert from "node:assert/strict";
import { test } from "node:test";
import { RequestCore, createClient, stateForError, type Transport } from "./client.ts";
import type { JsonValue } from "./types.gen.ts";

function fakeResponse(status: number, body: JsonValue): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function at(responses: readonly Response[], i: number): Response {
  const response = responses[Math.min(i, responses.length - 1)];
  if (response === undefined) {
    throw new Error("recordingTransport: no response configured");
  }
  return response;
}

function recordingTransport(responses: readonly Response[]): { transport: Transport; calls: RequestInit[] } {
  const calls: RequestInit[] = [];
  let i = 0;
  const transport: Transport = {
    fetch: async (_path, init) => {
      calls.push(init);
      const response = at(responses, i);
      i += 1;
      return response;
    },
  };
  return { transport, calls };
}

test("stateForError maps every documented code", () => {
  assert.equal(stateForError("unauthenticated"), "login");
  assert.equal(stateForError("step_up_required"), "reauth");
  assert.equal(stateForError("conflict"), "reload");
  assert.equal(stateForError("forbidden"), "error");
  assert.equal(stateForError("internal"), "error");
});

test("a GET carries no CSRF header, content-type or idempotency key", async () => {
  const { transport, calls } = recordingTransport([fakeResponse(200, { status: "ok", db: "ok" })]);
  const core = new RequestCore(transport);
  core.setCsrfToken("tok");
  await core.send("GET", "/api/v1/health", null, { idempotent: false });
  const headers = calls[0]?.headers as Record<string, string>;
  assert.equal(headers["x-csrf-token"], undefined);
  assert.equal(headers["content-type"], undefined);
  assert.equal(headers["idempotency-key"], undefined);
});

test("a write carries the CSRF header once it is set, never before", async () => {
  const { transport, calls } = recordingTransport([fakeResponse(200, { ok: true })]);
  const core = new RequestCore(transport);
  await core.send("POST", "/api/v1/task/add", {}, { idempotent: false });
  assert.equal((calls[0]?.headers as Record<string, string>)["x-csrf-token"], undefined);

  core.setCsrfToken("the-token");
  await core.send("POST", "/api/v1/task/add", {}, { idempotent: false });
  assert.equal((calls[1]?.headers as Record<string, string>)["x-csrf-token"], "the-token");
});

test("an idempotent write reuses one key across a failing retry, then a new one for the next action", async () => {
  const { transport, calls } = recordingTransport([
    fakeResponse(503, { error: { code: "unavailable", message: "try again", details: null } }),
    fakeResponse(200, { ok: true }),
    fakeResponse(200, { ok: true }),
  ]);
  const core = new RequestCore(transport);
  const first = await core.send("POST", "/api/v1/task/add", {}, { idempotent: true });
  assert.equal(first.ok, false);
  const second = await core.send("POST", "/api/v1/task/add", {}, { idempotent: true });
  assert.equal(second.ok, true);
  const keyAttempt1 = (calls[0]?.headers as Record<string, string>)["idempotency-key"];
  const keyRetry = (calls[1]?.headers as Record<string, string>)["idempotency-key"];
  assert.ok(keyAttempt1);
  assert.equal(keyAttempt1, keyRetry, "a retry of the same action reuses its key");

  const third = await core.send("POST", "/api/v1/task/add", {}, { idempotent: true });
  assert.equal(third.ok, true);
  const keyNextAction = (calls[2]?.headers as Record<string, string>)["idempotency-key"];
  assert.notEqual(keyNextAction, keyAttempt1, "a new action after a success gets a fresh key");
});

test("a non-JSON or network failure maps to an error result, never a throw", async () => {
  const brokenTransport: Transport = { fetch: async () => { throw new Error("offline"); } };
  const core = new RequestCore(brokenTransport);
  const result = await core.send("GET", "/api/v1/health", null, { idempotent: false });
  assert.equal(result.ok, false);
  assert.equal(!result.ok && result.code, "unavailable");
});

test("createClient exposes one function per endpoint name", async () => {
  const { transport } = recordingTransport([fakeResponse(200, { status: "ok", db: "ok" })]);
  const client = createClient(transport);
  const result = await client.api.health(null);
  assert.equal(result.ok, true);
  assert.equal(result.ok && result.value.status, "ok");
});

test("an error response is surfaced with its code, message and details", async () => {
  const { transport } = recordingTransport([
    fakeResponse(401, { error: { code: "unauthenticated", message: "sign in", details: null } }),
  ]);
  const client = createClient(transport);
  const result = await client.api.health(null);
  assert.equal(result.ok, false);
  assert.equal(!result.ok && result.code, "unauthenticated");
  assert.equal(!result.ok && result.message, "sign in");
});
