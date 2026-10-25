// Status colour tokens (D-074: purple main accent, vibrant status colours; D-230 keeps them for
// the final design). The actual colour values live in styles.css as CSS custom properties
// (`--status-*`) so there is one source for them; this module only names the fixed set of
// levels a component may use, as a class name, so a typo can't silently fall back to "no colour".
export type StatusLevel = "ok" | "info" | "warn" | "error";

export function statusClass(level: StatusLevel): string {
  return `status-${level}`;
}
