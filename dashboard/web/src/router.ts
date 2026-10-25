// The typed route table over `history` (docs/phase6a_plan.md §3.8): the F-views (design §2,
// docs/dashboard_design.md F1-F13; F14 is embedded in F1, not its own route) as a fixed list.
// No router package.

export type ViewName =
  | "inbox"       // F1 (home)
  | "stalled"     // F2
  | "queue"       // F3
  | "window"      // F4
  | "status"      // F5
  | "driven"      // F6
  | "decisions"   // F7
  | "prompts"     // F8
  | "docs"        // F9
  | "settings"    // F10
  | "account"     // F11
  | "audit"       // F12
  | "host"        // F13
  | "not_found";

type KnownView = Exclude<ViewName, "not_found">;

export interface RouteEntry {
  readonly name: KnownView;
  readonly path: string;
}

export const ROUTES: readonly RouteEntry[] = [
  { name: "inbox", path: "/" },
  { name: "stalled", path: "/stalled" },
  { name: "queue", path: "/queue" },
  { name: "window", path: "/window" },
  { name: "status", path: "/status" },
  { name: "driven", path: "/driven" },
  { name: "decisions", path: "/decisions" },
  { name: "prompts", path: "/prompts" },
  { name: "docs", path: "/docs" },
  { name: "settings", path: "/settings" },
  { name: "account", path: "/account" },
  { name: "audit", path: "/audit" },
  { name: "host", path: "/host" },
];

const BY_PATH: ReadonlyMap<string, KnownView> = new Map(ROUTES.map((route) => [route.path, route.name]));
const BY_NAME: ReadonlyMap<KnownView, string> = new Map(ROUTES.map((route) => [route.name, route.path]));

export function resolve(pathname: string): ViewName {
  return BY_PATH.get(pathname) ?? "not_found";
}

export function href(name: KnownView): string {
  const path = BY_NAME.get(name);
  if (path === undefined) {
    throw new Error(`no route named ${name}`);
  }
  return path;
}

/** The parts of `window.history`/`window.location` the router needs, injected so tests never
 * touch a real DOM (there isn't one under node:test). */
export interface NavigationEnv {
  pathname(): string;
  pushState(path: string): void;
  replaceState(path: string): void;
  onPopState(listener: () => void): () => void;
}

export function browserEnv(): NavigationEnv {
  return {
    pathname: () => window.location.pathname,
    pushState: (path) => window.history.pushState(null, "", path),
    replaceState: (path) => window.history.replaceState(null, "", path),
    onPopState: (listener) => {
      window.addEventListener("popstate", listener);
      return () => window.removeEventListener("popstate", listener);
    },
  };
}

export class Router {
  private readonly env: NavigationEnv;
  private readonly listeners: Set<(view: ViewName) => void>;
  private unsubscribePop: (() => void) | null;

  constructor(env: NavigationEnv) {
    this.env = env;
    this.listeners = new Set();
    this.unsubscribePop = null;
  }

  current(): ViewName {
    return resolve(this.env.pathname());
  }

  navigate(name: KnownView, options?: { readonly replace?: boolean }): void {
    const path = href(name);
    if (options?.replace === true) {
      this.env.replaceState(path);
    } else {
      this.env.pushState(path);
    }
    this.emit();
  }

  /** Returns an unsubscribe function. The first subscriber wires up the browser's own
   * back/forward navigation (popstate); the last one unsubscribing never un-wires it, since a
   * second subscriber later would otherwise miss it -- it is removed by GC with the env. */
  subscribe(listener: (view: ViewName) => void): () => void {
    this.listeners.add(listener);
    if (this.unsubscribePop === null) {
      this.unsubscribePop = this.env.onPopState(() => this.emit());
    }
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(): void {
    const view = this.current();
    for (const listener of this.listeners) {
      listener(view);
    }
  }
}
