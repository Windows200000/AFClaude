#!/usr/bin/env node
// Builds dashboard/web for production (docs/phase6a_plan.md §3.8): one hashed JS bundle and one
// hashed CSS file under dist/assets/ (immutable, security.py's cache rule), and dist/index.html
// from the template with no inline script or style (the CSP forbids both). Needs the pinned
// Node (tools/node.py); run through `npm run build` or test_dashboard_web.py.
//
// `--preview` (npm run build:preview, docs/phase6a_plan.md §3.9, D-211) builds the owner preview
// instead: entry point src/preview/main.ts (wired to a mock transport, never the real
// `browserFetch`) and src/preview/index.template.html (connect-src 'none'), output to
// dist-preview/ so it can never be confused with (or accidentally served as) the real build.
// Deploying dist-preview/ to /private is the task-manager's (§3.9); this script only builds it.
import { build } from "esbuild";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const isPreview = process.argv.includes("--preview");
const outdir = path.join(here, isPreview ? "dist-preview" : "dist");
const entry = path.join(here, isPreview ? "src/preview/main.ts" : "src/main.ts");
const templatePath = path.join(here, isPreview ? "src/preview/index.template.html" : "src/index.template.html");

async function main() {
  await rm(outdir, { recursive: true, force: true });
  const result = await build({
    entryPoints: [entry, path.join(here, "src/styles.css")],
    bundle: true,
    format: "esm",
    target: "es2022",
    minify: true,
    sourcemap: false,
    outdir,
    entryNames: "assets/app-[hash]",
    metafile: true,
    logLevel: "silent",
  });
  const outputs = Object.keys(result.metafile.outputs);
  const js = outputs.find((file) => file.endsWith(".js"));
  const css = outputs.find((file) => file.endsWith(".css"));
  if (js === undefined || css === undefined) {
    throw new Error(`esbuild did not produce both a JS and a CSS asset: ${outputs.join(", ") || "(none)"}`);
  }
  const asUrl = (outputPath) => `/${path.relative(outdir, outputPath)}`;
  const template = await readFile(templatePath, "utf8");
  const html = template.replace("__APP_CSS__", asUrl(css)).replace("__APP_JS__", asUrl(js));
  if (html.includes("__APP_")) {
    throw new Error(`${templatePath} has an unfilled placeholder`);
  }
  await mkdir(outdir, { recursive: true });
  await writeFile(path.join(outdir, "index.html"), html, "utf8");
  console.log(`built ${asUrl(js)} and ${asUrl(css)}${isPreview ? " (preview)" : ""}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
