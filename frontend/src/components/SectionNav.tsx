import type { ReactElement } from "react";

import { PolicyIcon, ReapIcon, ReviewIcon, ScalesIcon, SettingsIcon } from "./navIcons";
import type { View } from "../navIntent";
import { useSafety } from "../useSafety";

const NAV: { id: View; label: string; Icon: () => ReactElement }[] = [
  { id: "review", label: "Review", Icon: ReviewIcon },
  { id: "policy", label: "Policy", Icon: PolicyIcon },
  { id: "reap", label: "Reap", Icon: ReapIcon },
  { id: "fairness", label: "Scales", Icon: ScalesIcon },
  { id: "settings", label: "Settings", Icon: SettingsIcon },
];

/** The section nav. One element in two shapes: a rail sitting on the masthead's own bottom
 *  border on a wide screen, and the phone's bottom bar under 900px, where the labels give way
 *  to the icons. The labels are never dropped from the accessibility tree -- the 900px block in
 *  styles/10-layout.css clips `.view-label` to a 1px box rather than hiding it, so it still names its
 *  button -- which is why none of these carries an `aria-label` of its own.
 *
 *  Reap carries the safety state as a dot. That is the same fact `SafetyBanner` states in words
 *  directly below, and the banner renders on every view, which is why the dot is `aria-hidden`:
 *  the sentence is already in the tree, and the decoration would only say it twice. */
export function SectionNav({ view, onChange }: { view: View; onChange: (next: View) => void }) {
  const { data, isLoading, isError } = useSafety();
  // No dot means "not armed", so a failed read must never fall through to no dot (rule 17/36):
  // it wears the amber "we could not look" mark instead, the tone the banner uses for the same
  // state. Only the very first fetch draws nothing, because it genuinely knows nothing yet.
  const safety: "armed" | "unknown" | null = isLoading
    ? null
    : isError || !data
      ? "unknown"
      : data.destructive_enabled
        ? "armed"
        : null;

  return (
    <nav className="views" aria-label="Sections">
      {NAV.map((n) => (
        <button
          key={n.id}
          className={view === n.id ? "view-tab active" : "view-tab"}
          // Reserve the bold (active) width so switching sections never shifts the rail. The
          // phone bar drops the strut with the labels; see the 900px block in styles/10-layout.css.
          data-label={n.label}
          // The view you are on is stated, not just colored.
          aria-current={view === n.id ? "page" : undefined}
          onClick={() => onChange(n.id)}
        >
          {/* One positioned box around whichever of the two is showing, so the safety dot
              anchors to the icon on a phone and to the word on a wide screen without being
              placed twice. */}
          <span className="view-mark">
            <n.Icon />
            <span className="view-label">{n.label}</span>
            {n.id === "reap" && safety !== null && (
              <span className={`view-armed view-armed-${safety}`} aria-hidden="true" />
            )}
          </span>
        </button>
      ))}
    </nav>
  );
}
