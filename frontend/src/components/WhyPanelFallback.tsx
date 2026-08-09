import { useId } from "react";

import { useSlowWait } from "../announce";
import { Notice } from "./Notice";
import { WhyShell } from "./WhyShell";

/** What the why-panel's column shows while the reasoning is loading, or when it could not
 *  be loaded at all. The column is already reserved the moment an item is selected, so
 *  leaving it blank would read as "the app hung"; and it must keep its own close button,
 *  or a failed fetch would strand the reader in split view. */
export function WhyPanelFallback({ error, onClose }: { error: boolean; onClose: () => void }) {
  const headingId = useId();
  // Above the branch, and null on the failure arm: that arm reaches `Notice`'s `role="alert"`,
  // which speaks on its own, so a wait sentence arriving beside it would say two things about
  // one state (rule 146 -- what this reports has to be re-read in every state it renders).
  useSlowWait(error ? null : "Still loading what Reaper saw about this item.");
  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      {error ? (
        <>
          <div className="why-head">
            <h2 id={headingId}>Something went wrong</h2>
          </div>
          <Notice tone="error">
            Couldn't load the reasons for this item. The item itself is unaffected. Close this panel
            and click the item to try again.
          </Notice>
        </>
      ) : (
        // No live region here any more: it was mounted in the same commit as its text, which is
        // the shape several readers never announce (#332). The sentence goes through the shared
        // region in `announce.tsx`, once the wait has run long.
        <div className="why-loading">
          <span className="spinner spinner-lg" aria-hidden="true" />
          {/* No heading in this branch, so the lead carries the panel's name. */}
          <p className="why-loading-lead" id={headingId}>
            Fetching what Reaper saw…
          </p>
        </div>
      )}
    </WhyShell>
  );
}
