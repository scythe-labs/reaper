// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one line a settings panel shows when its own read failed but its form is still up.

import { useTranslation } from "react-i18next";
import i18next from "../i18n";
import { Notice } from "./Notice";

/** Says the values on screen may be out of date, for a panel whose read failed after a good
 *  load. A panel trades its form for "couldn't load" only when nothing ever landed. Once
 *  something has loaded, a later failed refetch keeps showing the last good data and warns
 *  with this line instead, because the operator may have an unsaved draft sitting in it.
 *  This one component covers every such surface: settings panels, the review queue, the
 *  Scales board, and several others.
 *
 *  Never suggest reloading the page here, and never build a retry step into this line. A
 *  reload drops any unsaved draft, such as a typed password, a staged restore, or a pasted
 *  webhook secret, since nothing in the app warns before a reload. A caller that wants a
 *  retry control puts one beside its own content instead (see `LogsPanel`, `PlexPanel`).
 *
 *  `what` names the thing on the page, since this line covers more than settings now.
 *  `inline` only changes spacing, for a notice living inside the row it describes rather
 *  than at the top of a panel. */
export function StaleReadNotice({ what, inline = false }: { what?: string; inline?: boolean }) {
  const { t } = useTranslation();
  return (
    <Notice tone="warn" inline={inline}>
      {staleReadLine(what ?? t("shell.staleRead.defaultWhat"))}
    </Notice>
  );
}

/** One read a panel groups under a single stale line: the noun it would use, and whether it is
 *  in the state that draws one. */
export type StaleRead = { readonly what: string; readonly stale: boolean };

/** A panel's decision about which of its stale-read lines actually draw.
 *
 *  Ask it per slot. It answers with the noun to use there, or null. */
export type StaleReadPlan = { readonly at: (slot: string) => string | null };

/** Several reads failing at once are one line, naming the panel.
 *
 *  A panel's reads fail together far more often than apart. `PlexPanel`'s four are
 *  refetched by one `invalidateAllPlex`, so a single server switch against an unreachable
 *  Plex drew four near-identical amber lines down the page. The notice carries
 *  `role="alert"`, so that is four announcements as well as four paragraphs, and this copy
 *  is scanned while deciding what to delete: the repetition is what gets read.
 *
 *  Collapse on how many lines would draw, not on which invalidation caused them. React
 *  Query does not expose the latter, and it would be the wrong rule anyway: the Jobs
 *  panel's two reads are independent polls that fail apart, so an invalidation-group rule
 *  would collapse nothing on the panel this was written for.
 *
 *  One failure keeps its own words, in its own slot: "the library list" tells the operator
 *  more than "the Plex settings", and the panel noun is only worth reaching for once it
 *  speaks for several reads. Two or more draw one line in the first read's slot, which
 *  every caller puts above the groups it covers, because the sentence says what's below may
 *  be out of date and a panel is plain block flow.
 *
 *  A caller passes only reads in the failed-refetch state (`isError` with data still held).
 *  A read that never landed is a different claim, "there is nothing here" rather than "this
 *  may be stale", and keeps its own never-loaded notice per group. It must not be counted
 *  here, or it would collapse a panel into a line about staleness on behalf of a group
 *  showing red. */
export function collapseStaleReads(panelWhat: string, reads: readonly StaleRead[]): StaleReadPlan {
  const failed = reads.filter((read) => read.stale);
  const only = failed.length === 1 ? failed[0] : undefined;
  if (only !== undefined) return { at: (slot) => (only.what === slot ? only.what : null) };
  if (failed.length === 0) return { at: () => null };
  const first = reads[0];
  return { at: (slot) => (first !== undefined && first.what === slot ? panelWhat : null) };
}

/** One slot's stale line, drawn only when the panel's plan says this slot speaks.
 *
 *  Every collapsing call site reads the same way, so which slot is currently carrying the line is
 *  the plan's business rather than each panel's. */
export function StaleReadSlot({
  plan,
  slot,
  inline = false,
}: {
  plan: StaleReadPlan;
  slot: string;
  inline?: boolean;
}) {
  const what = plan.at(slot);
  return what === null ? null : <StaleReadNotice what={what} inline={inline} />;
}

/** The sentence itself, for a surface whose own grammar is not a `.notice`.
 *
 *  The review queue's expanded season list speaks in `.season-list-note` lines, the grammar
 *  its loading and failed states already use, inside a card where a full-width notice block
 *  does not belong. Without this, it would hand-write this claim on its own, which is the
 *  drift a shared sentence is supposed to prevent. The wording lives here and the
 *  presentation varies. A caller that can render a notice uses the component above rather
 *  than this, as the queue itself does one screen out.
 *
 *  This is not required by the action-failure convention that puts failures in
 *  `.notice.notice-error` and lets bare red `.error` survive in the review surfaces: that
 *  convention permits `.error` there, it does not ban `.notice`. Nothing forbids a notice in
 *  a review surface, and `ReviewQueue` renders one. */
export function staleReadLine(what: string): string {
  return i18next.t("shell.staleRead.line", { what });
}
