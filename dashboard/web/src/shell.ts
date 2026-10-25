// The app shell: layout and top bar (docs/phase6a_plan.md §3.8, design §3/§3.1). Real data
// (automation state, runner dot, tonight's window, inbox badge) is 6c's; this is the scaffold
// with placeholders, over the typed router. Built with Preact's `h()` directly (no JSX/.tsx).
import { h } from "preact";
import { useEffect, useState } from "preact/hooks";
import { Tooltip, VerboseToggle } from "./components/tooltip.ts";
import { ROUTES, Router, type NavigationEnv, type ViewName } from "./router.ts";
import { statusClass } from "./theme.ts";

/** Placeholders until 6c wires real data in (design §3.1). */
export interface ShellState {
  readonly automationOk: boolean;
  readonly runnerActive: boolean;
  readonly tonightsWindow: string | null;
  readonly inboxCount: number;
}

export const PLACEHOLDER_STATE: ShellState = {
  automationOk: true,
  runnerActive: false,
  tonightsWindow: null,
  inboxCount: 0,
};

function TopBar(props: { readonly state: ShellState }) {
  const { state } = props;
  return h("header", { className: "topbar" }, [
    h(
      Tooltip,
      {
        short: state.automationOk ? "Automation is running normally." : "Automation is paused.",
        verbose: state.automationOk
          ? "No pause is set; the runners pick up work on their own schedule."
          : "An install-wide or per-project pause is set (failstop.py); `tasks.py automation resume` clears it.",
        children: h("span", {
          className: `dot ${statusClass(state.automationOk ? "ok" : "error")}`,
          "aria-label": state.automationOk ? "automation ok" : "automation paused",
        }),
      },
    ),
    h(
      Tooltip,
      {
        short: state.runnerActive ? "A session is running now." : "No session is running.",
        verbose: "The watcher's live state (real data from 6c; this is the W3 scaffold).",
        children: h("span", {
          className: `dot ${statusClass(state.runnerActive ? "info" : "ok")}`,
          "aria-label": state.runnerActive ? "runner active" : "runner idle",
        }),
      },
    ),
    h(
      Tooltip,
      {
        short: state.tonightsWindow ?? "No window configured.",
        verbose: "The next session-window start (design §9.5); set per weekday in Settings.",
        children: h("span", { className: "window" }, state.tonightsWindow ?? "–"),
      },
    ),
    h(
      Tooltip,
      {
        short: `${state.inboxCount} waiting for you`,
        verbose: "Open questions and blocked tasks across every project you can see (F1).",
        children: h("span", { className: "badge" }, String(state.inboxCount)),
      },
    ),
    h(VerboseToggle, {}),
  ]);
}

function ViewOutlet(props: { readonly view: ViewName }) {
  if (props.view === "not_found") {
    return h("main", { className: "view" }, h("p", null, "Not found."));
  }
  // every F-view is a placeholder route until 6c builds it over the real read endpoints
  return h("main", { className: "view", "data-view": props.view }, h("p", null, `${props.view} (not built yet)`));
}

export interface ShellProps {
  readonly env: NavigationEnv;
  readonly state?: ShellState;
}

export function Shell(props: ShellProps) {
  const [router] = useState(() => new Router(props.env));
  const [view, setView] = useState<ViewName>(() => router.current());
  useEffect(() => router.subscribe(setView), [router]);
  const state = props.state ?? PLACEHOLDER_STATE;
  return h("div", { className: "shell" }, [
    h(TopBar, { state }),
    h(
      "nav",
      { className: "nav", "aria-label": "views" },
      ROUTES.map((route) =>
        h(
          "a",
          {
            key: route.name,
            href: route.path,
            className: "nav-link",
            "aria-current": view === route.name ? "page" : undefined,
            onClick: (event: MouseEvent) => {
              event.preventDefault();
              router.navigate(route.name);
            },
          },
          route.name,
        ),
      ),
    ),
    h(ViewOutlet, { view }),
  ]);
}
