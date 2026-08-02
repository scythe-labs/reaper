// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The site's callout, drawn from the same three tones `DocBody` uses: tip reads as a protection
// (green), note as ordinary emphasis (accent), caution as something unknown or costly (amber).
// The tones are Reaper's verdict language, so they carry meaning and are not decoration.

import type { ReactNode } from "react";

export type CalloutTone = "tip" | "note" | "caution";

/** One icon per tone, matching the glyphs `DocBody` draws so the same paragraph reads the same
 *  in both places. `aria-hidden`, because the tone is already carried by the visible label. */
function Icon({ tone }: { tone: CalloutTone }) {
  const common = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2.2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (tone === "tip") {
    return (
      <svg {...common}>
        <path d="m9 12 2 2 4-4" />
        <circle cx="12" cy="12" r="9" />
      </svg>
    );
  }
  if (tone === "caution") {
    return (
      <svg {...common}>
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
        <path d="M12 9v4M12 17h.01" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
  );
}

const LABEL: Record<CalloutTone, string> = {
  tip: "Tip",
  note: "Note",
  caution: "Caution",
};

export function Callout({ tone = "note", children }: { tone?: CalloutTone; children: ReactNode }) {
  return (
    <aside className={`rp-callout rp-callout--${tone}`}>
      <div className="rp-callout__head">
        <span className="rp-callout__ic">
          <Icon tone={tone} />
        </span>
        {LABEL[tone]}
      </div>
      <div className="rp-callout__body">{children}</div>
    </aside>
  );
}
