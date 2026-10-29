// Typed sample data for the owner preview build (docs/phase6a_plan.md §3.9, D-211). The mapped
// type below requires exactly one fixture per entry of `EndpointTypes` (types.gen.ts), so a
// schema change that adds, removes or reshapes an endpoint fails this file's typecheck -- the
// build breaks loudly instead of the preview quietly showing a stale or missing response for a
// route it no longer (or not yet) matches. Dummy data only; never a real id, name or secret.
import type { EndpointName, EndpointTypes } from "../api/types.gen.ts";

export const FIXTURES: { readonly [N in EndpointName]: EndpointTypes[N]["response"] } = {
  health: { status: "ok", db: "ok" },
  api_schema: { openapi: "3.1.0", info: {}, paths: {}, components: {} },
  me: {
    user: { id: 1, role: "owner" },
    csrf_token: "preview-sample-csrf-token",
    time_zone: "Europe/Berlin",
  },
};
