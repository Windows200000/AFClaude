// A `Transport` (api/client.ts) that never reaches a network: every request is answered
// synchronously from FIXTURES, matched by method + path against the same `ENDPOINTS` table the
// real client dispatches from (docs/phase6a_plan.md §3.9, D-211). Only `Transport` is imported
// from client.ts, and only as a type (`import type`, erased by the compiler) -- this module never
// references `createClient` or the real `browserFetch`, so the preview bundle built from it has
// no reachable call to the global `fetch` (test_dashboard_web.py checks the built file for that).
import { ENDPOINTS, type EndpointName } from "../api/types.gen.ts";
import type { Transport } from "../api/client.ts";
import { FIXTURES } from "./fixtures.ts";

function endpointFor(method: string, path: string): EndpointName | null {
  for (const name of Object.keys(ENDPOINTS) as EndpointName[]) {
    if (ENDPOINTS[name].method === method && ENDPOINTS[name].path === path) {
      return name;
    }
  }
  return null;
}

/** Every call this preview build makes must hit a known endpoint with a fixture (D-235: a
 * preview that silently shows nothing is worse than one that fails loudly in the console). */
export function createPreviewTransport(): Transport {
  return {
    fetch: async (path, init) => {
      const method = init.method ?? "GET";
      const name = endpointFor(method, path);
      if (name === null) {
        throw new Error(`preview mock transport: no fixture for ${method} ${path}`);
      }
      const body = JSON.stringify(FIXTURES[name]);
      return new Response(body, { status: 200, headers: { "content-type": "application/json" } });
    },
  };
}
