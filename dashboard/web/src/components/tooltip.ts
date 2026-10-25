// Custom tooltip + the "?" verbose toggle (D-230, D-249, docs/phase6a_plan.md §3.8). Built with
// Preact's `h()` directly (no JSX/.tsx): keeps the toolchain to tsc + esbuild, nothing else.
import { h, type ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

// One in-memory toggle for verbose tooltips (D-249): no web storage (design §9.8), so it resets
// on reload; 7b turns it into a per-user setting instead. A module-level value plus a tiny
// listener set keeps every open Tooltip in sync without a state-management package.
let verboseTooltips = false;
const verboseListeners = new Set<() => void>();

export function isVerboseTooltips(): boolean {
  return verboseTooltips;
}

export function setVerboseTooltips(value: boolean): void {
  verboseTooltips = value;
  for (const listener of verboseListeners) {
    listener();
  }
}

function useVerboseTooltips(): boolean {
  const [value, setValue] = useState(verboseTooltips);
  useEffect(() => {
    const listener = (): void => setValue(verboseTooltips);
    verboseListeners.add(listener);
    return () => {
      verboseListeners.delete(listener);
    };
  }, []);
  return value;
}

export function VerboseToggle() {
  const verbose = useVerboseTooltips();
  return h(
    "button",
    {
      type: "button",
      className: verbose ? "verbose-toggle verbose-toggle--on" : "verbose-toggle",
      "aria-pressed": verbose ? "true" : "false",
      "aria-label": verbose ? "Switch tooltips to short" : "Switch tooltips to detailed",
      onClick: () => setVerboseTooltips(!verboseTooltips),
    },
    "?",
  );
}

function hasHover(): boolean {
  // no `window` under node:test (no DOM at all): treat that, and a browser with no hover
  // pointer, as "tap to show" rather than "hover to show".
  return typeof window !== "undefined" && window.matchMedia !== undefined
    && window.matchMedia("(hover: hover)").matches;
}

export interface TooltipProps {
  readonly short: string;
  readonly verbose?: string;
  readonly children: ComponentChildren;
}

/**
 * Instant, custom hover/focus/tap tooltip (D-230): no show delay; the short fade/scale on the
 * way in is CSS only (`prefers-reduced-motion` drops it there, not here, styles.css). The "?"
 * toggle (D-249) swaps in the longer text where one is given. Class names and data attributes
 * only -- an inline `style` would be a CSP violation (style-src 'self', security.py's CSP).
 */
export function Tooltip(props: TooltipProps) {
  const [open, setOpen] = useState(false);
  const verbose = useVerboseTooltips();
  const text = verbose && props.verbose !== undefined ? props.verbose : props.short;
  const ref = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!open || hasHover()) {
      return undefined;
    }
    const onOutside = (event: Event): void => {
      if (ref.current !== null && event.target instanceof Node && !ref.current.contains(event.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onOutside);
    return () => document.removeEventListener("pointerdown", onOutside);
  }, [open]);

  return h(
    "span",
    {
      ref,
      className: "tooltip",
      "data-tooltip-open": open ? "true" : undefined,
      onMouseEnter: () => { if (hasHover()) setOpen(true); },
      onMouseLeave: () => { if (hasHover()) setOpen(false); },
      onFocusIn: () => setOpen(true),
      onBlur: () => setOpen(false),
      onClick: () => { if (!hasHover()) setOpen((value) => !value); },
      tabIndex: 0,
    },
    [props.children, h("span", { className: "tooltip-bubble", role: "tooltip" }, text)],
  );
}
