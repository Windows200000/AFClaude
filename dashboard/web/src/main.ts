// The browser entry point (esbuild's entry, build.mjs): mounts the shell to #app. Never imported
// by a test (those render the Shell directly with preact-render-to-string); this is the one file
// that touches the real DOM.
import { h, render } from "preact";
import { browserEnv } from "./router.ts";
import { Shell } from "./shell.ts";

const root = document.getElementById("app");
if (root === null) {
  throw new Error("index.html is missing #app");
}
render(h(Shell, { env: browserEnv() }), root);
