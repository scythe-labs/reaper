// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one line a settings panel shows when its own read failed but its form is still up.

import { Notice } from "./Notice";

/** Says the values on screen may be out of date, for a panel whose read failed after a good load.
 *
 *  A settings panel only trades its form for "couldn't load these settings" when NOTHING ever
 *  landed (`!data`). A refetch that fails afterwards must keep the form, because React Query
 *  still holds the last good row and the operator may have a draft typed into it that the panel
 *  is reporting upward: replacing the form there would take the boxes, the Save and the Discard
 *  off screen while the guard went on demanding a discard for edits with nowhere left to put
 *  them (rule 146). Keeping the form silently is the other half of that mistake, though, since
 *  the panel then presents state it knows is stale as current. Rule 17/36 wants both the pending
 *  and the failed read handled explicitly, so this is what the failed one says.
 *
 *  One component for every surface, so the wording cannot drift (rule 144). It began as a
 *  settings line and is not one any more: #190 brought the review queue, the expanded season
 *  list, the Scales board, the not-in-the-last-scan panel, the services list and the service
 *  editor's two mapping grids here too. A surface that starts keeping its content takes this
 *  line with it rather than writing its own.
 *
 *  **No count lives in this comment**, deliberately: one did, and it stood at six while
 *  `NotificationsPanel` already was the seventh, which is how a gap gets written down as future
 *  work and then reads as an assurance that the sweep is finished (rule 7/24). Grep for the
 *  component to see who calls it.
 *
 *  **Several reads failing at once are ONE line (#198).** A panel that groups its reads through
 *  `collapseStaleReads` below says it once, naming the panel, rather than once per read; a lone
 *  failure keeps its own noun in its own slot. `PlexPanel` and `JobsPanel` do this. The rest carry
 *  a single read each and pass no plan.
 *
 *  Surfaces, not files: `PlexPanel` carries this line at several places (#166) and so does
 *  `ServiceModal`. `PlexPanel`'s status read took it in #140; its library grid and its Leaving Soon
 *  group were still trading the whole grid, and both shelf switches, for an error paragraph over
 *  a list React Query was still holding. A file entering this set does not always do it once, so
 *  finding one branch that tests a bare `isError` is a reason to read the rest of the file.
 *
 *  Security is the one whose clock always runs: `useSafety` refetches every 15 seconds whatever
 *  the app is doing, so its form met this state without the operator doing anything at all. Jobs
 *  has a clock too and it is the conditional one -- 1500ms, only while something is running
 *  (`JobsPanel.tsx`) -- which is why the paragraph below says "most callers" rather than "all but
 *  Security". Backup is the one where the
 *  never-loaded sentence was actively harmful rather than merely wrong: it says to reload, and a
 *  reload does not run the restore card's unmount cleanup, so the staged archive is orphaned by
 *  an operator doing what the page told them.
 *
 *  **So this line does not say to reload, and offers no recovery step at all.** It said "Reload to
 *  try again." for its whole life, which is the harm the paragraph above says was fixed by taking
 *  the never-loaded sentence off Backup -- reintroduced by the line that replaced it, on more
 *  panels than the original reached. It is the destructive advice precisely where there is
 *  something to destroy: three typed password boxes on Security, a staged restore on Backup, a
 *  pasted webhook on Notifications, a typed address on Plex and General. There is no
 *  `beforeunload` handler anywhere in `frontend/src` (grep: zero), so a reload takes the draft
 *  with no ask. Nothing honest was available to put in its place: `main.tsx` sets
 *  `refetchOnWindowFocus: false` app-wide, and the only interval anywhere behind one of these
 *  reads is Security's, which runs always (`useSafety`, 15s), and Jobs', which runs only while a
 *  job is running (`JobsPanel.tsx`, 1500ms) -- so a line promising Reaper keeps trying would be
 *  false on most callers and, on Jobs, false exactly half the time, which is worse than plainly
 *  wrong. (The flat "only Security has an interval" this used to say was simply untrue; the
 *  conclusion it was arguing for still holds. #196.) A retry the operator can press without
 *  losing the draft is the real answer and is not built INTO THIS LINE -- three exist elsewhere
 *  in the tree, two inside a failure notice (`LogsPanel`) and one beside a failure line
 *  (`PlexPanel`'s server list), so the pattern is available to a caller that wants it. Saying
 *  nothing beats saying the one thing that costs them their work.
 *
 *  Three different reasons brought panels here, and they are worth keeping straight. The first
 *  four keep their surface to protect a DRAFT (rule 146). About and Jobs hold none, and keep
 *  theirs because the last good row is still the best answer available and the never-loaded
 *  sentence is simply false above a page that is right there; Jobs reads worst stale, since its
 *  rows carry next-run times and a running flag. Notifications had no early return to fix at all:
 *  its form was always kept, so the failed read printed "couldn't check whether Discord is
 *  connected" directly above three controls derived from that very answer -- the "leave blank to
 *  keep the current webhook" placeholder, an enabled Remove, and a Send test that fires at the
 *  stored webhook -- each of them asserting it HAD been checked. Its old sentence also told the
 *  operator to reload, which throws away a pasted webhook secret shown nowhere ever again.
 *
 *  `what` names the thing on the page, because this line sits over more than settings now.
 *  A noun slot rather than a second sentence somewhere else: the claim is written once here, and
 *  a caller can only vary what it is about, never what it says.
 *
 *  `inline` is spacing, not a second voice: `.notice-inline` is the margin a notice takes when it
 *  lives inside the thing it describes rather than at the top of a panel, which is where the
 *  Leaving Soon row puts it, beside that row's own `notice-inline` error (rule 18). */
export function StaleReadNotice({
  what = "these settings",
  inline = false,
}: {
  what?: string;
  inline?: boolean;
}) {
  return (
    <Notice tone="warn" inline={inline}>
      {staleReadLine(what)}
    </Notice>
  );
}

/** One read a panel groups under a single stale line: the noun it would use, and whether it is
 *  in the state that draws one. */
export type StaleRead = { readonly what: string; readonly stale: boolean };

/** A panel's decision about which of its stale-read lines actually draw (#198).
 *
 *  Ask it per slot; it answers with the noun to use there, or null. */
export type StaleReadPlan = { readonly at: (slot: string) => string | null };

/** Several reads failing at once are ONE line, naming the panel (#198).
 *
 *  A panel's reads fail together far more often than apart -- `PlexPanel`'s four are refetched by
 *  one `invalidateAllPlex`, so a single server switch against an unreachable Plex drew four
 *  near-identical amber lines down the page. The notice carries `role="alert"`, so that is four
 *  announcements as well as four paragraphs, and rule 21's point is that this copy is SCANNED
 *  while deciding what to delete: the repetition is what gets read.
 *
 *  **Collapse on how many lines would draw, not on which invalidation caused them.** React Query
 *  does not expose the latter, and it would be the wrong rule anyway: the Jobs panel's two reads
 *  are independent polls that fail apart, so an invalidation-group rule would collapse nothing on
 *  the panel the issue was filed about.
 *
 *  **One failure keeps its own words**, in its own slot -- "the library list" tells the operator
 *  more than "the Plex settings", and the panel noun is only worth reaching for once it speaks for
 *  several reads. **Two or more draw one line in the FIRST read's slot**, which every caller puts
 *  above the groups it covers, because the sentence says what's BELOW may be out of date and a
 *  panel is plain block flow.
 *
 *  A caller passes only reads in the failed-REFETCH state (`isError` with data still held). A read
 *  that never landed is a different claim -- "there is nothing here", not "this may be stale" --
 *  and keeps its own never-loaded notice per group, so it must not be counted here or it would
 *  collapse a panel into a line about staleness on behalf of a group showing red. */
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
 *  The review queue's expanded season list speaks in `.season-list-note` lines -- the grammar its
 *  loading and failed states already use, inside a card where a full-width notice block does not
 *  belong -- so it would otherwise hand-write this claim, which is the drift rule 144 describes.
 *  The wording lives here and the presentation varies; a caller that CAN render a notice uses the
 *  component above rather than this, as the queue itself does one screen out.
 *
 *  Rule 42 is NOT the reason, though three comments said it was: it puts action failures in
 *  `.notice.notice-error` everywhere and lets bare red `.error` survive in the review surfaces,
 *  which permits `.error` there rather than banning `.notice`. Nothing forbids a notice in a
 *  review surface, and `ReviewQueue` renders one. */
export function staleReadLine(what: string) {
  return `Couldn't check ${what} just now, so what's below may be out of date.`;
}
