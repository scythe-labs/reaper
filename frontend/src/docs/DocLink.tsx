// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one way anything in the app opens the in-app docs. A second caller copying this button
// would duplicate it instead of sharing it, so it lives here rather than inside any one
// page. Its two styles (`.doc-help`, `.doc-learn`) live in the shared stylesheet rather than
// in a policy-editor block.

import type { ReactNode } from "react";

import { useDocs } from "./DocsContext";
import { getDoc } from "./registry";

/** The question mark that rides in a `.doc-help` button. Shared for the same reason the
 *  button is shared: a second caller would otherwise copy this SVG's path data to match it. */
export function HelpIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="15"
      height="15"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4" />
      <path d="M12 17h.01" />
    </svg>
  );
}

/** A button that opens the in-app docs to a page, and optionally a section within it. The policy
 *  header wears it as "Help", each policy section as a "Learn more" that lands on the matching
 *  part of the guide, and a degraded scan's notice as the page for what went wrong. */
export function DocLink({
  doc,
  anchor,
  className,
  children,
}: {
  doc: string;
  anchor?: string;
  className?: string;
  children: ReactNode;
}) {
  const { openDoc } = useDocs();
  return (
    <button type="button" className={className} onClick={() => openDoc(doc, anchor)}>
      {children}
    </button>
  );
}

/** The help page a degraded scan points at, as the button its notice wears. Renders nothing when
 *  the scan named no page, which is the ordinary case, and nothing again when it named one this
 *  build does not have: a scan is stored, so it outlives the version that wrote it, and a button
 *  opening an empty modal is worse than no button.
 *
 *  Its label is the page's own title rather than a phrase written here, so the button and the
 *  thing it opens cannot disagree. */
export function DegradedDocLink({ doc }: { doc: string | null }) {
  const page = doc ? getDoc(doc) : undefined;
  if (!page) return null;
  return (
    <DocLink doc={page.id} className="doc-help">
      <HelpIcon />
      {page.title}
    </DocLink>
  );
}
