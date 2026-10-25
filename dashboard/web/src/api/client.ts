// The typed API client over types.gen.ts (docs/phase6a_plan.md §3.8). One function per
// endpoint name (ApiClient), the CSRF header on every write, an idempotency key per user
// action reused on retry (RequestCore), and error codes mapped to UI states.
import { ENDPOINTS, type EndpointName, type EndpointTypes, type ErrorCode, type ErrorResponse,
         type JsonValue } from "./types.gen.ts";

export interface Transport {
  fetch(input: string, init: RequestInit): Promise<Response>;
}

export type ApiResult<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly code: ErrorCode; readonly message: string;
      readonly details: Readonly<Record<string, JsonValue>> | null };

/** The UI state an error code maps to (design §8; W5/6b add the states that need a session). */
export type ErrorUiState = "login" | "reauth" | "reload" | "error";

export function stateForError(code: ErrorCode): ErrorUiState {
  if (code === "unauthenticated") return "login";
  if (code === "step_up_required") return "reauth";
  if (code === "conflict") return "reload";
  return "error";
}

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/**
 * One request's write-guard and idempotency-key handling (design §3.3, §3.8), kept independent
 * of the concrete endpoint table so it can be unit-tested without a generated write endpoint to
 * call against (W1/W2 only have GETs so far).
 */
export class RequestCore {
  private csrfToken: string | null;
  private readonly transport: Transport;
  private readonly retryKeys: Map<string, string>;

  constructor(transport: Transport) {
    this.transport = transport;
    this.csrfToken = null;
    this.retryKeys = new Map();
  }

  /** The session's CSRF token (W5's `me`); kept in memory only, never in web storage. */
  setCsrfToken(token: string | null): void {
    this.csrfToken = token;
  }

  async send<T>(method: string, path: string, body: JsonValue | null,
                options: { readonly idempotent: boolean; readonly retryKey?: string }): Promise<ApiResult<T>> {
    const headers: Record<string, string> = { accept: "application/json" };
    const init: RequestInit = { method, headers, credentials: "same-origin" };
    const isWrite = method !== "GET" && method !== "HEAD";
    if (isWrite) {
      headers["content-type"] = "application/json";
      if (this.csrfToken !== null) {
        headers["x-csrf-token"] = this.csrfToken;
      }
      if (options.idempotent) {
        headers["idempotency-key"] = this.idempotencyKey(options.retryKey ?? path);
      }
      init.body = JSON.stringify(body ?? {});
    }
    let response: Response;
    try {
      response = await this.transport.fetch(path, init);
    } catch {
      return { ok: false, code: "unavailable", message: "network error", details: null };
    }
    let parsed: unknown;
    try {
      parsed = await response.json();
    } catch {
      return { ok: false, code: "internal", message: "the response body was not JSON", details: null };
    }
    if (!response.ok) {
      const error = (parsed as ErrorResponse).error;
      return { ok: false, code: error.code, message: error.message, details: error.details };
    }
    if (isWrite && options.idempotent) {
      // the action went through: a later retry under this key would be a *new* action
      this.retryKeys.delete(options.retryKey ?? path);
    }
    return { ok: true, value: parsed as T };
  }

  private idempotencyKey(key: string): string {
    const existing = this.retryKeys.get(key);
    if (existing !== undefined) {
      return existing;
    }
    const made = newIdempotencyKey();
    this.retryKeys.set(key, made);
    return made;
  }
}

export type ApiClient = {
  readonly [N in EndpointName]: (request: EndpointTypes[N]["request"]) => Promise<ApiResult<EndpointTypes[N]["response"]>>;
};

export interface Client {
  readonly api: ApiClient;
  setCsrfToken(token: string | null): void;
}

const browserFetch: Transport = { fetch: (input, init) => fetch(input, init) };

export function createClient(transport: Transport = browserFetch): Client {
  const core = new RequestCore(transport);
  const entries = (Object.keys(ENDPOINTS) as EndpointName[]).map((name) => {
    const endpoint = ENDPOINTS[name];
    const call = (request: EndpointTypes[typeof name]["request"]): Promise<ApiResult<EndpointTypes[typeof name]["response"]>> =>
      core.send(endpoint.method, endpoint.path, request as JsonValue | null, { idempotent: endpoint.idempotent });
    return [name, call] as const;
  });
  const api = Object.fromEntries(entries) as ApiClient;
  return { api, setCsrfToken: (token) => core.setCsrfToken(token) };
}
