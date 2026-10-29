// The owner preview's root component (docs/phase6a_plan.md §3.9, D-211): the real `Shell` behind
// a one-line banner naming the role the mock `me` fixture answers with, so it's never mistaken
// for a live session. Split from preview/main.ts (mirroring shell.ts/main.ts) so it can be
// rendered with preact-render-to-string in a test, never touching the real DOM.
import { Fragment, h } from "preact";
import { useEffect, useState } from "preact/hooks";
import type { ApiClient } from "../api/client.ts";
import type { MeResponse } from "../api/types.gen.ts";
import { browserEnv, type NavigationEnv } from "../router.ts";
import { Shell } from "../shell.ts";

export function PreviewBanner(props: { readonly me: MeResponse | null }) {
  const role = props.me?.user?.role ?? "signed out";
  return h(
    "div",
    { className: "preview-banner", role: "note" },
    `Preview build -- sample data only, not connected to any backend (role shown: ${role}).`,
  );
}

export interface PreviewAppProps {
  readonly client: ApiClient;
  readonly env?: NavigationEnv;
}

export function PreviewApp(props: PreviewAppProps) {
  const [me, setMe] = useState<MeResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    props.client.me(null).then((result) => {
      if (!cancelled && result.ok) {
        setMe(result.value);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [props.client]);
  return h(Fragment, null, [h(PreviewBanner, { me }), h(Shell, { env: props.env ?? browserEnv() })]);
}
