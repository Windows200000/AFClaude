import assert from "node:assert/strict";
import { test } from "node:test";
import { ROUTES, Router, href, resolve, type NavigationEnv, type ViewName } from "./router.ts";

function fakeEnv(startPath: string): NavigationEnv & { path: string; fireBack(path: string): void } {
  const popListeners = new Set<() => void>();
  const env = {
    path: startPath,
    pathname(): string {
      return env.path;
    },
    pushState(path: string): void {
      env.path = path;
    },
    replaceState(path: string): void {
      env.path = path;
    },
    onPopState(listener: () => void): () => void {
      popListeners.add(listener);
      return () => popListeners.delete(listener);
    },
    fireBack(path: string): void {
      env.path = path;
      for (const listener of popListeners) listener();
    },
  };
  return env;
}

test("every route resolves from its own path and back", () => {
  for (const route of ROUTES) {
    assert.equal(resolve(route.path), route.name);
    assert.equal(href(route.name), route.path);
  }
});

test("an unknown path resolves to not_found, which has no href", () => {
  assert.equal(resolve("/does/not/exist"), "not_found");
  assert.throws(() => href("not_found" as unknown as Parameters<typeof href>[0]));
});

test("navigate updates the path and notifies subscribers", () => {
  const env = fakeEnv("/");
  const router = new Router(env);
  const seen: ViewName[] = [];
  router.subscribe((view) => seen.push(view));
  router.navigate("stalled");
  assert.equal(env.path, "/stalled");
  assert.deepEqual(seen, ["stalled"]);
});

test("navigate with replace never pushes a new history entry", () => {
  const env = fakeEnv("/");
  let pushed = 0;
  let replaced = 0;
  const wrapped: NavigationEnv = {
    ...env,
    pushState: (p) => { pushed += 1; env.pushState(p); },
    replaceState: (p) => { replaced += 1; env.replaceState(p); },
  };
  const router = new Router(wrapped);
  router.navigate("settings", { replace: true });
  assert.equal(pushed, 0);
  assert.equal(replaced, 1);
});

test("the browser's back/forward (popstate) is reflected without calling navigate", () => {
  const env = fakeEnv("/");
  const router = new Router(env);
  const seen: ViewName[] = [];
  router.subscribe((view) => seen.push(view));
  env.fireBack("/queue");
  assert.deepEqual(seen, ["queue"]);
  assert.equal(router.current(), "queue");
});

test("an unsubscribed listener stops receiving navigations", () => {
  const env = fakeEnv("/");
  const router = new Router(env);
  const seen: ViewName[] = [];
  const unsubscribe = router.subscribe((view) => seen.push(view));
  router.navigate("docs");
  unsubscribe();
  router.navigate("audit");
  assert.deepEqual(seen, ["docs"]);
});
