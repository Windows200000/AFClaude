#!/usr/bin/env node
// Builds dashboard/web for production (docs/phase6a_plan.md §3.8): one hashed JS bundle and one
// hashed CSS file under dist/assets/ (immutable, security.py's cache rule), and dist/index.html
// from the template with no inline script or style (the CSP forbids both). Needs the pinned
// Node (tools/node.py); run through `npm run build` or test_dashboard_web.py.
import { build } from "esbuild";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.join(here, "dist");

async function main() {
  await rm(outdir, { recursive: true, force: true });
  const result = await build({
    entryPoints: [path.join(here, "src/main.ts"), path.join(here, "src/styles.css")],
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
  const template = await readFile(path.join(here, "src/index.template.html"), "utf8");
  const html = template.replace("__APP_CSS__", asUrl(css)).replace("__APP_JS__", asUrl(js));
  if (html.includes("__APP_")) {
    throw new Error("src/index.template.html has an unfilled placeholder");
  }
  await mkdir(outdir, { recursive: true });
  await writeFile(path.join(outdir, "index.html"), html, "utf8");
  console.log(`built ${asUrl(js)} and ${asUrl(css)}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
