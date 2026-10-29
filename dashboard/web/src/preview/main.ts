// The owner preview build's entry point (docs/phase6a_plan.md §3.9, D-211). Mounts `PreviewApp`
// wired to the mock transport (mockTransport.ts) instead of a real session. Never imported by a
// test (previewApp.test.ts renders PreviewApp directly with preact-render-to-string) and never
// bundled into the production build -- `npm run build:preview` (build.mjs --preview) is the only
// thing that uses this file as its entry point, and it never imports ../main.ts or the real
// `browserFetch` in ../api/client.ts.
import { h, render } from "preact";
import { clientFromCore, RequestCore } from "../api/client.ts";
import { PreviewApp } from "./previewApp.ts";
import { createPreviewTransport } from "./mockTransport.ts";

const client = clientFromCore(new RequestCore(createPreviewTransport()));

const root = document.getElementById("app");
if (root === null) {
  throw new Error("index.html is missing #app");
}
render(h(PreviewApp, { client }), root);
