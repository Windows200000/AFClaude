// Source checks (docs/phase6a_plan.md §3.8): no explicit `any` in src/api/, and no raw-HTML or
// code-from-string sink anywhere in src/. Plain text scans, deliberately independent of the
// TypeScript compiler (a type-checker bug or a future relaxed tsconfig should not silently
// reopen either hole) and of any lint package (keeps the pinned toolchain small, D-292).
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const SRC = path.dirname(fileURLToPath(import.meta.url));

async function tsFiles(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...await tsFiles(full));
    } else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".test.ts") && !entry.name.endsWith(".gen.ts")) {
      // .gen.ts (types.gen.ts) is codegen.py's output, not hand-written here: its doc comments
      // use "any" in English prose ("any 4xx/5xx"), and it is covered by its own staleness test
      files.push(full);
    }
  }
  return files;
}

// a word-boundary match on "any" as a type, not inside "ApiClient" / "many" / a comment's prose;
// this is deliberately conservative (it also flags "any" in a comment) -- it must only ever find
// fewer problems to review, never more code it quietly accepts
const ANY_RE = /\bany\b/;
const FORBIDDEN = [/\binnerHTML\b/, /\bdangerouslySetInnerHTML\b/, /\beval\s*\(/, /\bnew\s+Function\s*\(/];

test("no explicit `any` in src/api/", async () => {
  const files = await tsFiles(path.join(SRC, "api"));
  assert.ok(files.length > 0, "expected at least one file under src/api/");
  const offenders: string[] = [];
  for (const file of files) {
    const text = await readFile(file, "utf8");
    for (const [i, line] of text.split("\n").entries()) {
      if (ANY_RE.test(line)) {
        offenders.push(`${path.relative(SRC, file)}:${i + 1}: ${line.trim()}`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});

test("no innerHTML/dangerouslySetInnerHTML/eval/new Function anywhere in src/", async () => {
  const files = await tsFiles(SRC);
  const offenders: string[] = [];
  for (const file of files) {
    const text = await readFile(file, "utf8");
    for (const [i, line] of text.split("\n").entries()) {
      if (FORBIDDEN.some((re) => re.test(line))) {
        offenders.push(`${path.relative(SRC, file)}:${i + 1}: ${line.trim()}`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});
