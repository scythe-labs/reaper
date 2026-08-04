// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  lazy,
  type ReactElement,
  Suspense,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { applyAccent } from "./accent";
import { announce, Announcer, useSlowWait } from "./announce";
import { api, ApiError, type AuthUser, type Snapshot, type Verdict } from "./api";
import { BackNavProvider, useBackGuard, useBackNav, useModalOpen } from "./backnav";
import { useUpdateStatus } from "./updateStatus";
import { Login } from "./components/Login";
import { ModalShell } from "./components/ModalShell";
import { PolicyIcon, ReapIcon, ReviewIcon, ScalesIcon, SettingsIcon } from "./components/navIcons";
import { NotInScanPanel } from "./components/NotInScanPanel";
import type { PolicySectionId } from "./components/PolicyEditor";
import { ReapConfirm } from "./components/ReapConfirm";
import { ReviewQueue } from "./components/ReviewQueue";
import { ScalesPanel, ScalesPanelFallback } from "./components/ScalesPanel";
import { BrandMark } from "./brand/BrandMark";
import type { Panel } from "./components/Settings";
import { ShowPanel } from "./components/ShowPanel";
import { WhyPanel } from "./components/WhyPanel";
import { WhyShell } from "./components/WhyShell";
import { DocsProvider } from "./docs/DocsContext";
import { bytes, count, date, souls } from "./format";
import type { Focus, NavIntent, Selection, View } from "./navIntent";
import { usePageScrollLock } from "./pageScrollLock";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "./useMediaQuery";
import { useSafety } from "./useSafety";
import { useScanSettled } from "./useScanSettled";
import { Notice } from "./components/Notice";
import { SafetyBanner } from "./components/SafetyBanner";
import { ScanLine } from "./components/ScanLine";

// The review queue is the landing view and stays in the first chunk. Every other route is
// its own, fetched the first time it is opened: the whole app used to ship as one 551 kB
// script, so a first paint of the queue paid for the policy editor, the simulator, every
// settings panel and the docs before it could draw a single card (P-4). Each is a default
// export from a thin wrapper below, because these modules export more than one thing.
const PolicyEditor = lazy(async () => ({
  default: (await import("./components/PolicyEditor")).PolicyEditor,
}));
const ReapPlan = lazy(async () => ({ default: (await import("./components/ReapPlan")).ReapPlan }));
const Fairness = lazy(async () => ({ default: (await import("./components/Fairness")).Fairness }));
const Settings = lazy(async () => ({ default: (await import("./components/Settings")).Settings }));
const SetupWizard = lazy(async () => ({
  default: (await import("./components/SetupWizard")).SetupWizard,
}));

/** What a route shows while its chunk is on the way. The app's own spinner, and, on a slow
 *  network, a sentence -- so a wait that runs long reads as loading rather than as a blank page.
 *
 *  It used to carry `role="status" aria-live="polite"` of its own, mounted in the same commit as
 *  "Loading…", which is the shape several readers never announce (#332). The picture is markup;
 *  the sentence goes through the always-mounted region in `announce.tsx`, and only once the wait
 *  has actually been one. This component mounts only while the chunk is in flight, so its unmount
 *  is what cancels a fast load's announcement. */
function RouteLoading() {
  useSlowWait("Still loading. Reaper is fetching this page.");
  return (
    <div className="fair-loading">
      <span className="spinner spinner-xl" aria-hidden="true" />
      <p className="fair-loading-lead">Loading…</p>
    </div>
  );
}

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

/** The app-wide reap bar: shown on every screen of the app while a reap runs, so its count and
 *  its Stop are reachable after you close or navigate away from the reap sheet. A reap runs
 *  detached from the request that started it, so this bar (and Stop) survive navigating away
 *  and a tab reload -- it re-attaches by polling the shared status. Not a safety surface (the
 *  always-on one is SafetyBanner), so it shows nothing when idle. Stop is graceful: the run
 *  halts after the item in flight and still tidies Plex, and deletion stays armed.
 *
 *  "Every screen" means every screen of the app, and the setup wizard is not one: `Authed`
 *  returns it *instead of* `Dashboard`, so nothing below here mounts while it is up. That is
 *  reachable during a run -- removing the Tautulli or the last *arr invalidates `["setup"]`,
 *  `scan_ready` goes false, and the wizard takes the page with no reload -- and it lands on the
 *  Connect step, which has Back and Skip, so Stop is two presses away rather than lost.
 *
 *  It mounts, runs and reaches its end -- "Reap failed." included -- and used to do all of it
 *  in silence, which on most screens made it the only sign of a deletion and the only Stop
 *  (#170). It now announces the run's end, and its fill carries `role="progressbar"` the way
 *  `ScanLine` (components/ScanLine.tsx) already did (rule 72). The RUNNING ticks are deliberately not announced
 *  here: the reap sheet throttles and speaks them, and a bar that spoke too would say
 *  everything twice for anyone with the sheet open.
 *
 *  Exported for its tests, the way `WhyPanelFallback` below is: what it owes an
 *  operator is a property of this component, and reaching it through the whole authed `App`
 *  tree would test the login gate instead. */
export function ReapBar({ onView }: { onView: (runId: number) => void }) {
  const queryClient = useQueryClient();
  const [dismissed, setDismissed] = useState<number | null>(null);
  // Idle still polls, slowly. A reap can be started from a phone or a second tab, and this
  // bar carries the only Stop on most screens: going silent when nothing is running here
  // would leave an open tab dark through someone else's deletion (the scan line idle-polls
  // at 15s for the same reason).
  const { data: status } = useQuery({
    queryKey: ["reapStatus"],
    queryFn: api.reapStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 15000),
  });
  const stop = useMutation({
    mutationFn: (id: number) => api.stopRun(id),
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
  });

  // A finished reap invalidates half the app -- the queue lists titles that are gone, the
  // ledger promises to remove them, the snapshot's reclaimable figure counts them. That
  // refresh belongs HERE, on the component a reap does not unmount: the confirmation sheet
  // is explicitly designed to be closed mid-run, and everything it invalidated went with it.
  // Fired once, on the running-to-ended edge of a run this mount actually saw running, so a
  // page opened after the fact does not re-invalidate what it just fetched.
  const ranRef = useRef<number | null>(null);
  const settledRef = useRef<number | null>(null);
  useEffect(() => {
    if (!status || status.run_id == null) return;
    if (status.running) {
      ranRef.current = status.run_id;
      return;
    }
    if (ranRef.current !== status.run_id || settledRef.current === status.run_id) return;
    settledRef.current = status.run_id;
    // Say how it ended. The same edge, for the same reason: a run that finished before this tab
    // opened must not be announced as news. The sentence carries the outcome first and the
    // figures after, matching the bar's own text below (rule 144), and it is said HERE rather
    // than in the reap sheet because closing the sheet does not take this bar with it -- the
    // sheet is meant to be closed mid-run, and an operator who closed it would otherwise hear
    // nothing at all about a deletion finishing.
    announce(
      status.phase === "error"
        ? `Reap failed. ${souls(status.deleted_items)} removed before it stopped.`
        : `${status.phase === "aborted" ? "Reap stopped." : "Reap finished."} ${souls(status.deleted_items)} removed, ${bytes(status.deleted_bytes)} freed.`,
    );
    // ["run"] as well as ["runs"]: the plan surface reads one run by id, and that key does
    // not match the list's.
    for (const key of [
      ["runs"],
      ["run"],
      ["candidates"],
      ["reap-breakdown"],
      ["snapshot"],
      ["fairness"],
    ]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  }, [status, queryClient]);

  if (!status || status.run_id == null) return null;
  const runId = status.run_id;
  const running = status.running;
  // Every terminal phase counts as ended -- including "error", so a reap that crashed after
  // removing files still surfaces here (the one always-visible fallback) instead of vanishing.
  const ended =
    !running &&
    (status.phase === "complete" || status.phase === "aborted" || status.phase === "error");
  if (!running && !(ended && runId !== dismissed)) return null;

  if (ended) {
    const errored = status.phase === "error";
    return (
      <div className={errored ? "reap-bar errored" : "reap-bar done"}>
        <span className="banner-dot" aria-hidden="true" />
        <span className="reap-bar-text">
          <b>{errored ? "Reap failed." : status.phase === "aborted" ? "Stopped." : "Reaped."}</b>{" "}
          <span className="reap-bar-sub">
            {souls(status.deleted_items)} removed, {bytes(status.deleted_bytes)} freed
            {errored && status.error ? `. ${status.error}` : ""}
          </span>
        </span>
        <span className="reap-bar-actions">
          <button className="link" onClick={() => onView(runId)}>
            View report
          </button>
          <button className="sm" onClick={() => setDismissed(runId)}>
            Dismiss
          </button>
        </span>
      </div>
    );
  }

  const pct = status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;
  return (
    <div className="reap-bar">
      <span className="banner-dot" aria-hidden="true" />
      {/* The role goes on the TEXT, never on the bar. `progressbar` carries ARIA's Children
          Presentational: True, so a role on the container prunes everything inside it -- and
          this container holds View, Stop, and the `role="alert"` that reports a Stop that
          failed. A reader watching a live deletion heard the percentage and had no way to halt
          it. That is the same pruning `CardOpen` exists to undo on the queue's four cards, so
          the bar nearest deletion was the one sibling the sweep missed (rule 72); the three
          sibling bars and `ScanLine` all carry the role on an element holding only a fill.
          The text is the right anchor here: it is the visible readout, it is never empty, and
          it holds no control -- where the fill is 0px wide at 0% and can drop out of the tree.
          `aria-valuetext` says what a person would say, so nobody is left reading out "62". */}
      <span
        className="reap-bar-text"
        role="progressbar"
        aria-label="Reaping"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={`${pct}%, ${count(status.done)} of ${count(status.total)} removed`}
      >
        {status.stopping ? (
          <b>Stopping after the current one…</b>
        ) : (
          <>
            <b>
              Reaping, {count(status.done)} of {count(status.total)}
            </b>
            <span className="reap-bar-sub">, {bytes(status.deleted_bytes)} freed</span>
          </>
        )}
      </span>
      <span className="reap-bar-actions">
        <button className="link" onClick={() => onView(runId)}>
          View
        </button>
        <button
          className="stop-btn"
          disabled={status.stopping || stop.isPending}
          onClick={() => stop.mutate(runId)}
        >
          {status.stopping ? "Stopping…" : "Stop"}
        </button>
      </span>
      {/* A Stop that failed must say so. Swallowed, it reads as a run that is halting while
          it keeps deleting -- and this is the only Stop on every screen but the sheet. */}
      {stop.error && (
        <Notice tone="error" className="reap-bar-error">
          Reaper couldn't stop the reap: {stop.error.message}
        </Notice>
      )}
      <span className="reap-bar-fill" style={{ width: `${pct}%` }} />
    </div>
  );
}

/** Reopen the reap sheet for a run by id -- the bar's View, from any screen. Fetches the run,
 *  then hands it to the same ReapConfirm the review queue uses, which re-attaches to the live
 *  status and shows progress or the finished report. */
function ReapSheetLoader({ runId, onClose }: { runId: number; onClose: () => void }) {
  const {
    data: run,
    isPending,
    error,
  } = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId) });
  if (run) return <ReapConfirm run={run} onClose={onClose} />;
  // Never render nothing here. This sheet is the app-wide reap bar's View, and the query
  // defaults to one retry with no refetch-on-focus, so a failed fetch settles in error and a
  // null render would leave the View button dead forever. Worse, useBackGuard keys on
  // reapSheetRun, not this render, so a Back press would silently close an invisible sheet. Show
  // a loading line or a plain error, both with ModalShell's own working close (PR-1, rule 36).
  //
  // It does NOT say to reload (#225). This sheet is rendered outside `<main>` and gated on
  // `reapSheetRun`, which the bar's View sets without touching `view` -- so unlike the other
  // three sites that still carry the advice, this one opens OVER a mounted review queue. The
  // queue's `selected` is component state with no storage behind it (only the filters persist)
  // and "Select everything matching" pages the whole list to build it, so a reload here drops a
  // selection that may have cost thousands of rows, with nothing asking first (`frontend/src`
  // has no `beforeunload`). The close this modal already has keeps it, so the line points there.
  return (
    <ModalShell title="Reap report" onClose={onClose}>
      <div className="service-form">
        {isPending ? (
          <p className="help">Loading the reap…</p>
        ) : (
          <Notice tone="error">
            {error instanceof ApiError && error.status === 404
              ? "That reap is no longer available."
              : "Reaper couldn't load this reap. Close this and try View again."}
          </Notice>
        )}
      </div>
    </ModalShell>
  );
}

/** A slim freshness line on the Review screen: when the queue was last built, and a loud
 *  note if that scan came back incomplete (the scan control itself now lives in Settings →
 *  Jobs). Without this, the queue gives no sense of how stale it might be.
 *
 *  Missing data is not the same as "no scan exists". `/api/snapshots/latest` answers 404
 *  only for the genuine first-boot case; every other failure also arrives with no data, and
 *  reading that as "no scan has run yet" turns a dropped request into a confident claim and
 *  silently drops the incomplete-scan warning, the one staleness signal on this screen.
 *  Exported for its own test; the app renders it only from Dashboard. */
export function ScanFreshness({
  snapshot,
  isPending,
  error,
  onGoToJobs,
}: {
  snapshot: Snapshot | undefined;
  isPending: boolean;
  error: unknown;
  onGoToJobs: () => void;
}) {
  if (isPending) {
    return <p className="scan-freshness muted">Checking the last scan…</p>;
  }
  if (!snapshot) {
    if (error instanceof ApiError && error.status === 404) {
      return (
        <p className="scan-freshness muted">
          No scan has run yet.{" "}
          <button className="link" onClick={onGoToJobs}>
            Run one from Settings → Jobs
          </button>{" "}
          to fill the queue.
        </p>
      );
    }
    // No "reload to try again" (#195): this line renders above a working review queue, where
    // `selected` is component state -- only the filters persist -- so a reload drops a bulk
    // selection that "Select everything matching" may have walked thousands of rows to build,
    // and nothing anywhere in the app asks first.
    return (
      // `standing`: this line is the queue's age, so it is part of the page whenever the read
      // behind it will not answer. It arrives that way on a first load, and again whenever
      // `useScanSettled` invalidates `["snapshot"]` off the shell's 15s poll -- a scheduled scan
      // finishing, with nothing pressed. It sits directly above the queue it describes.
      <Notice tone="error" className="scan-freshness" standing>
        Couldn't read the last scan, so Reaper can't say how old this queue is.
      </Notice>
    );
  }
  return (
    <p className="scan-freshness muted">
      Last scanned {date(snapshot.created_at)}, {count(snapshot.item_count)} items
      {snapshot.degraded && (
        <span className="freshness-warn">
          {". "}
          That scan came back incomplete, so Reaper won't act on it
        </span>
      )}
    </p>
  );
}

/** The signed-in identity, with a panel to sign out.
 *
 *  A disclosure, not an ARIA menu: it is a button that shows and hides a small panel, and
 *  it behaves like one (click or Tab away to dismiss, Escape to close). It used to claim
 *  role="menu", which promises arrow-key navigation between menu items that was never
 *  implemented, on a panel whose first child is a heading rather than an item. The honest
 *  simpler role is the one whose keyboard contract this actually keeps. */
export function UserMenu({ user, onGoToAbout }: { user: AuthUser; onGoToAbout: () => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // The light and the menu item render only while an update actually exists; every
  // could-not-answer state renders neither, so the chip never nags on a guess. The
  // words ride the chip's accessible name -- the light itself is decoration.
  const update = useUpdateStatus();
  const updateAvailable = update.data?.update_available === true;

  // A mutation, not a fire-and-forget async onClick: a sign-out that fails must say so.
  // The session would still be live, and a swallowed error leaves the menu open with the
  // user still signed in and nothing to explain why.
  const signOut = useMutation({
    mutationFn: () => api.logout(),
    // A sign-out that WORKED is written, not asked about -- the same call `main.tsx` makes for
    // every other 401, for the same reason. `noteAuthFailure` (api.ts) exempts the whole
    // `/api/auth/` PREFIX, which is seven routes and this key's own read among them, so refetching
    // it here answers a dead session with a query ERROR while React Query still holds the last
    // good user beside it, and the gate below reads that as still signed in. Asking left the
    // operator on their own dashboard until an unrelated poll happened to 401.
    onSuccess: () => queryClient.setQueryData(["me"], null),
    // A failure is the opposite question, and the refetch is the right way to ask it: the session
    // may well still be live, and the answer is whatever `/api/auth/me` says.
    onError: () => void queryClient.invalidateQueries({ queryKey: ["me"] }),
  });

  // While the sign-out is running or has failed, the panel stays put. Disabling the focused
  // Sign out button moves focus off it, which some browsers report as focus leaving the
  // whole menu -- closing the panel would then throw away the only place the failure is
  // ever shown, and it would come back stale the next time the menu opened.
  const keepOpen = signOut.isPending || signOut.isError;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (keepOpen) return;
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open, keepOpen]);

  // Clicking away closed it; tabbing away did not, which left the panel hanging open over
  // a page the keyboard had already moved on from.
  const onBlur = (e: React.FocusEvent<HTMLDivElement>) => {
    if (keepOpen) return;
    if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
  };
  // Escape closes and hands focus back to the chip, so the keyboard is where it started.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "Escape" || !open) return;
    setOpen(false);
    triggerRef.current?.focus();
  };

  // Opening starts clean: a failure from a previous attempt is history, not news.
  const toggle = () => {
    if (!open) signOut.reset();
    setOpen((v) => !v);
  };

  // Back closes the menu instead of leaving Reaper. Held open while a sign-out is pending or
  // failed, matching the outside-click guard, so the failure message is never yanked away.
  useBackGuard(open && !keepOpen, () => setOpen(false));

  const initial = user.username.slice(0, 1).toUpperCase();

  return (
    <div className="user-menu" ref={ref} onBlur={onBlur} onKeyDown={onKeyDown}>
      <button
        className="user-chip"
        ref={triggerRef}
        onClick={toggle}
        aria-expanded={open}
        aria-label={updateAvailable ? `${user.username}, update available` : undefined}
      >
        {user.thumb_url ? (
          <img src={user.thumb_url} alt="" className="user-avatar" />
        ) : (
          <span className="user-avatar user-avatar-fallback">{initial}</span>
        )}
        <span className="user-name">{user.username}</span>
        {updateAvailable && <span className="update-light" aria-hidden="true" />}
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="chevron">
          <path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <div className="user-dropdown">
          <div className="user-dropdown-head">
            <div className="user-name">{user.username}</div>
            <div className="muted user-provider">
              {user.provider === "plex" ? "Plex account" : "Local account"}
            </div>
          </div>
          {updateAvailable && (
            <button
              className="user-dropdown-item user-dropdown-update"
              onClick={() => {
                setOpen(false);
                onGoToAbout();
              }}
            >
              Update available
              <span className="update-light" aria-hidden="true" />
            </button>
          )}
          <button
            className="user-dropdown-item"
            onClick={() => signOut.mutate()}
            disabled={signOut.isPending}
          >
            {signOut.isPending ? "Signing out…" : "Sign out"}
          </button>
          {signOut.isError && (
            <Notice tone="error" inline>
              Couldn't sign you out. Try again.
            </Notice>
          )}
        </div>
      )}
    </div>
  );
}

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

function Dashboard({ user }: { user: AuthUser }) {
  const [view, setView] = useState<View>("review");
  const [verdict, setVerdict] = useState<Verdict>("condemn");
  const [selected, setSelected] = useState<Selection>(null);
  // Which Scales person has their panel open. Kept here (not in Fairness) so the panel is a
  // sibling of the list inside `main.split`, exactly as the why-panel sits beside the queue.
  const [scalesUser, setScalesUser] = useState<string | null>(null);
  // Whether the board's "not in the last scan" panel is open. Only one Scales panel shows at
  // a time, so opening either closes the other.
  const [scalesUnmatched, setScalesUnmatched] = useState(false);
  const openScalesPerson = (identity: string) => {
    setScalesUnmatched(false);
    setScalesUser(identity);
  };
  const openScalesUnmatched = () => {
    setScalesUser(null);
    setScalesUnmatched(true);
  };
  // The reap sheet reopened from the app-wide bar's View, by run id, on any screen.
  const [reapSheetRun, setReapSheetRun] = useState<number | null>(null);

  // A side panel is open: the why panel, the show panel, or one of the Scales panels, all of
  // which render as `main.split`'s second column beside their list.
  const splitOpen =
    (selected !== null && view === "review") ||
    ((scalesUser !== null || scalesUnmatched) && view === "fairness");
  // The two list views the split rides on. Off these, there is no list scroll to keep.
  const listView = view === "review" || view === "fairness";
  // A phone shows the panel as a full-screen sheet over the list (`main.split .why` below 900px
  // in styles/10-layout.css); a wider screen keeps the list visible beside it. That is what decides whether
  // the window scroll still tracks the list while a panel is open (used just below). The width
  // must match that full-screen-sheet breakpoint, so it comes from the one declaration both
  // readers share (rule 67).
  const fullSheet = useMediaQuery(NARROW_SCREEN_QUERY);

  // Keep the reviewer's place when a panel opens or closes. Opening turns the list into the
  // side-by-side split (the cards make room for the panel) and closing takes it back; that
  // reflow drops the window scroll in some browsers -- Safari all the way to the top -- and on a
  // phone the panel is a full-screen sheet whose close landed back at the top too. An operator
  // pages deep into thousands of items, so losing that place is real work to redo. Remember where
  // the list is scrolled, then put it back the instant the layout toggles -- in a layout effect,
  // before the browser paints -- so the list never jumps. scrollRestoration is `manual`
  // (BackNavProvider), so the Back sentinel it parks on open can't fight this.
  const listScrollRef = useRef(0);
  useEffect(() => {
    // Track the list's scroll so a close lands where the reviewer is NOW -- even after they
    // scrolled and opened a different card with the panel already up. Frozen only while a
    // full-screen sheet covers the list (a phone): the list is not being scrolled then, so any
    // movement is the page drifting behind the sheet, which must not overwrite the place we
    // return to. On wider screens the list stays visible beside the panel, so keep tracking.
    if (!listView || (splitOpen && fullSheet)) return;
    const onScroll = () => {
      listScrollRef.current = window.scrollY;
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [splitOpen, fullSheet, listView]);
  // Restore only on a genuine open/close within one list view -- never when the change is a page
  // navigation (which owns its own scroll), which the view guard below distinguishes.
  const splitPrevRef = useRef({ open: splitOpen, view });
  useLayoutEffect(() => {
    const prev = splitPrevRef.current;
    const toggledInPlace = prev.view === view && prev.open !== splitOpen;
    splitPrevRef.current = { open: splitOpen, view };
    if (toggledInPlace) window.scrollTo(0, listScrollRef.current);
  }, [splitOpen, view]);

  // Freeze the list while a panel covers the whole screen on a phone, so a touch drag scrolls
  // the panel's own overflow instead of the list underneath it. Only the full-screen sheet
  // (`splitOpen && fullSheet`, matching the 900px block in styles/12-why-panel.css): on a wider screen the
  // list stays visible beside the panel and is meant to scroll, so it is left alone. The freeze
  // parks and restores window.scrollY through the same ref-counted lock the modal shell uses,
  // and it returns to the exact place the restore above keeps (both hold the pre-open offset),
  // so the two never fight.
  usePageScrollLock(splitOpen && fullSheet);

  // The browser Back button steps back through the UI instead of leaving Reaper: open panels
  // and menus register themselves (useBackGuard, below and in their own components), and a tab
  // or section change records how to undo it here. `pushNav` captures the CURRENT location so a
  // later Back restores it; the undo calls the raw setter, never these wrappers, so it never
  // records itself.
  const { pushNav } = useBackNav();

  // What each view is currently aimed at, held here rather than inside the view: `App` outlives
  // the mount, so a jump can name a destination for a page that is not on screen yet. Each is
  // acted on once, counted by its nonce -- revisiting the page later must not replay the jump
  // that first brought you there. Rule 72: three of these now, and a fourth belongs in the same
  // three places (declared here, cleared on a plain tab visit, handed to the view).
  const [policyFocus, setPolicyFocus] = useState<Focus<{ section: PolicySectionId }> | null>(null);
  const [settingsFocus, setSettingsFocus] = useState<Focus<{ panel: Panel }> | null>(null);
  const [reviewFocus, setReviewFocus] = useState<Focus<{ search: string }> | null>(null);
  const clearFocus = () => {
    setPolicyFocus(null);
    setSettingsFocus(null);
    setReviewFocus(null);
  };

  // A destination dies with the visit it aimed at. Clearing on a nav click (below) is not enough,
  // because that is not the only way a view is left: a Back press restores `view` through the raw
  // setter and runs no handler at all. The queue unmounts on the way out, so its once-per-nonce
  // ref goes with it, and the next mount seeds the search box from a jump the operator had
  // already backed out of -- they asked to return to the Review list they started from and got a
  // one-title list with a chip they never typed. Clearing a focus as its view goes off screen
  // also covers the search they cleared BY HAND, which the box would otherwise refill on the way
  // back. Keyed on `view`, so clicking the tab you are already on still changes nothing (B-23).
  // Rule 72: all three focuses, not just the one that was shown to replay.
  useEffect(() => {
    if (view !== "review") setReviewFocus(null);
    if (view !== "policy") setPolicyFocus(null);
    if (view !== "settings") setSettingsFocus(null);
  }, [view]);

  // Every jump in the app, in one place. The caller names a whole destination (navIntent.ts) and
  // this applies it; nothing else calls the raw setters, so a new destination is a new call site
  // rather than a new function with its own idea of what a jump resets.
  //
  // Three things it has to get right, each of which was a bug in one of the four hand-written
  // jumps this replaced:
  //
  // ONE BACK STEP for the whole jump, restoring the view AND the lane together. The lane is not
  // a place the operator visited on its own, so recording it separately would spend a Back press
  // landing them on a list they never saw. `view` and `verdict` are this render's values, so the
  // undo closes over where the operator actually was, and it calls the raw setters, so it never
  // records itself.
  //
  // THE LANE COMES FROM THE CALLER, never re-derived here. Only the caller knows which lane it
  // means: a show sits in every lane one of its seasons does, so there is no single answer to
  // derive for a group, while Scales means the lane of the seasons that person asked for.
  // `reviewFate.laneOf` answers it for one item, and Scales sends the same override-aware lane
  // the queue filters on (rule 77). A jump that opened an item without its lane left the panel
  // open above a list the item is not in: no card to find, and the two affordances that would
  // have led back to it -- the scroll to the open card, and the j/k step through the list --
  // both quietly do nothing when the open item is off-lane.
  //
  // OMITTED IS NOT EMPTY. Every optional here is three-state, and the middle state is what makes
  // one function serve both a section-nav click and a targeted jump: `select: undefined` leaves
  // an open panel alone (arriving on Review from the nav), `select: null` closes it (a lane tab,
  // whose new list does not hold the open card). Same for `search` -- a jump from inside Review
  // leaves the operator's own search text where it is.
  const goTo = (intent: NavIntent) => {
    const lane = intent.view === "review" ? (intent.lane ?? verdict) : verdict;
    if (intent.view !== view || lane !== verdict) {
      pushNav(() => {
        setView(view);
        setVerdict(verdict);
      });
    }
    if (intent.view === "review") {
      setVerdict(lane);
      if (intent.select !== undefined) setSelected(intent.select);
      if (intent.search !== undefined) setReviewFocus({ search: intent.search, nonce: Date.now() });
    } else if (intent.view === "policy") {
      if (intent.section !== undefined)
        setPolicyFocus({ section: intent.section, nonce: Date.now() });
    } else if (intent.view === "settings") {
      if (intent.panel !== undefined) setSettingsFocus({ panel: intent.panel, nonce: Date.now() });
    }
    setView(intent.view);
  };

  const goToPolicySection = (section: PolicySectionId) => goTo({ view: "policy", section });
  const goToSettingsPanel = (panel: Panel) => goTo({ view: "settings", panel });
  // A lane tab: the new list does not hold whatever card is open, so the panel closes with it.
  const changeVerdict = (next: Verdict) => goTo({ view: "review", lane: next, select: null });
  // Open one item's reasoning, on the lane it lives in -- an item on its own card, a whole show
  // on its group panel. `search` is optional and the caller decides: a jump from another section
  // arrives at an untouched queue and seeds the box with the title it is opening, so the list
  // behind the panel is the one title rather than the whole lane. A jump from INSIDE Review
  // (ShowPanel's season list) sends none, because the operator's own search is already in that
  // box and seeding over it would throw away what they typed.
  const goToItemReasons = (candidateId: number, lane: Verdict, search?: string) =>
    goTo({ view: "review", lane, select: { kind: "item", id: candidateId }, search });
  const goToGroupReasons = (key: string, lane: Verdict, search?: string) =>
    goTo({ view: "review", lane, select: { kind: "group", key }, search });

  const selectedId = selected?.kind === "item" ? selected.id : null;
  const selectedGroupKey = selected?.kind === "group" ? selected.key : null;

  const {
    data: snapshot,
    isPending: snapshotPending,
    error: snapshotError,
  } = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    // A 404 means no scan has run. That is a normal first-boot state, not an error, and
    // retrying it on a loop would be noise. Every *other* failure is a real error, which is
    // why ScanFreshness is handed the error itself rather than just the missing data.
    retry: false,
  });

  // The browser tab wears the install's chosen name (Settings → General), so two
  // Reapers stay tellable-apart. The default title is baked into index.html; only a
  // non-default name changes it, and a failed read changes nothing.
  const { data: generalSettings } = useQuery({
    queryKey: ["general-settings"],
    queryFn: api.general,
    staleTime: 60_000,
  });
  useEffect(() => {
    const name = generalSettings?.application_name;
    if (name && document.title !== name) document.title = name;
  }, [generalSettings?.application_name]);

  // The install's chosen accent color (Settings → General), applied to the whole UI. Saved
  // on the server, so it follows the install, not the browser. index.html pre-paints it from
  // a cache to avoid a flash; this keeps it in step after a save or on another device.
  useEffect(() => {
    applyAccent(generalSettings?.accent_color);
  }, [generalSettings?.accent_color]);

  // The background-scan cue, polled from the shell so it lights up on every screen. Fast
  // while a scan runs; a gentle idle poll so a scan started elsewhere (the scheduler,
  // another device) still surfaces here without a manual start or a tab refocus -- that
  // is what a global "something is running" line is for. Shares the ["scanStatus"] cache
  // with the scan bar, so the two never disagree.
  const { data: scanStatus } = useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    refetchInterval: (query) => (query.state.data?.running ? 1000 : 15000),
  });
  // ...and when one ends, refresh what the new snapshot changed. Here, off the shell's own
  // poll, for the same reason the finished-reap refresh above is here: a scan started from
  // the Reap page, the scheduler or another device must refresh the screen the operator is
  // on, not only the one screen that happens to mount the scan bar.
  useScanSettled(scanStatus?.running ?? false, scanStatus?.error ?? null);

  const { data: detail, isError: detailError } = useQuery({
    queryKey: ["candidate", selectedId],
    queryFn: () => api.candidate(selectedId!),
    enabled: selectedId !== null,
  });

  const { data: groupDetail, isError: groupError } = useQuery({
    queryKey: ["group", selectedGroupKey],
    queryFn: () => api.group(selectedGroupKey!),
    enabled: selectedGroupKey !== null,
  });

  const { data: personDetail, isError: personError } = useQuery({
    queryKey: ["fairness", "person", scalesUser],
    queryFn: () => api.person(scalesUser!),
    enabled: scalesUser !== null,
  });

  // The board report, for the "not in the last scan" panel. Same query key as the Fairness
  // list, so React Query serves it from one fetch -- no second network call. Only fetched on
  // the Scales screen, where the list already needs it.
  const {
    data: fairnessReport,
    isPending: fairnessPending,
    isError: fairnessError,
  } = useQuery({
    queryKey: ["fairness"],
    queryFn: api.fairness,
    enabled: view === "fairness",
  });

  // Reviewing is a loop: read the reasoning, decide, move to the next one. The queue owns
  // the order the cards are actually in (this tab, these filters, this sort), so it hands
  // back a way to walk that order instead of this component guessing at it.
  const stepRef = useRef<((delta: 1 | -1) => void) | null>(null);
  const hasSelection = selected !== null;
  const modalOpen = useModalOpen();
  useEffect(() => {
    if (view !== "review" || !hasSelection) return;
    const onKey = (e: KeyboardEvent) => {
      // Browser and OS shortcuts keep their meaning, and typing in a field is typing: `j` and
      // `k` are letters before they are queue steps. Escape used to be caught here too and
      // took this same bail with it, so Escape from inside one of the panel's own fields did
      // nothing at all. It belongs to the panel, and `WhyShell` owns it for all six of them.
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) {
        return;
      }
      // While a modal is up it owns the keyboard: its own Escape closes it, and the panel
      // behind it must not move underneath. Read from state (ModalShell says so on mount),
      // not probed for in the DOM on every keypress (H-2).
      if (modalOpen) return;
      const delta =
        e.key === "ArrowDown" || e.key === "j" ? 1 : e.key === "ArrowUp" || e.key === "k" ? -1 : 0;
      if (delta === 0) return;
      e.preventDefault();
      stepRef.current?.(delta);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, hasSelection, modalOpen]);

  // Back-button layers for the side panels and the app-wide reap sheet (the tab and section
  // changes are recorded by `goTo` above). Each is gated on the view that actually shows
  // it, so a panel whose state lingers off-screen is not a phantom Back step.
  useBackGuard(selected !== null && view === "review", () => setSelected(null));
  useBackGuard(scalesUser !== null && view === "fairness", () => setScalesUser(null));
  useBackGuard(scalesUnmatched && view === "fairness", () => setScalesUnmatched(false));
  useBackGuard(reapSheetRun !== null, () => setReapSheetRun(null));

  return (
    <div className="app">
      <ScanLine running={scanStatus?.running ?? false} percent={scanStatus?.percent ?? 0} />
      <header className="masthead">
        <div className="brand">
          <BrandMark className="brand-mark sm" />
          <div className="brand-text">
            {/* The authenticated app's only `h1`, and it had none: every view opened at `h2`,
                so heading navigation (H, 1) had no top-level landing point at all. `Login.tsx`
                already promoted the same class this way and the pattern was not carried into
                the shell (#177). No level is skipped anywhere below it -- this was a missing
                root, not a broken outline.
                "Only" is a claim about the whole authenticated tree, and it was false the day
                it was written: the docs pane rendered its title as a second `h1`, so opening
                Help put two on the page. That is why it is `h3` there now, under the `h2`
                `ModalShell` gives the dialog (rule 7/24 -- a comment naming a property is
                checked, not assumed). `SetupWizard` and `Login` keep their own `h1` because
                each REPLACES this shell rather than rendering inside it. */}
            <h1 className="brand-word">Reaper</h1>
            <span className="muted brand-sub">Grave decisions, clearly explained</span>
          </div>
        </div>

        <SectionNav
          view={view}
          onChange={(next) => {
            // A plain tab visit must not replay an old cross-page jump -- but clicking the
            // tab you are ALREADY on is not a visit, and clearing the focus there changes
            // the key Settings is mounted under, remounting the whole subtree and throwing
            // away whatever is typed into it (B-23). Arriving from a "Settings → Jobs" link,
            // typing a name, then clicking Settings in the nav used to silently discard it.
            if (next !== view) clearFocus();
            // Leaving Scales (or re-entering it) closes any open Scales panel, so the
            // split view never lingers on a tab that has no panel to show. Both the person
            // panel and the "not in the last scan" panel are cleared, matching the
            // mutual-exclusion pairing the open handlers use (U-5).
            setScalesUser(null);
            setScalesUnmatched(false);
            // A section visit and nothing more: no lane, no selection, no search. The queue
            // keeps whatever the operator left open on it, which is what "go back to Review"
            // means when it is the nav that says it.
            goTo({ view: next });
          }}
        />

        <UserMenu user={user} onGoToAbout={() => goToSettingsPanel("about")} />
      </header>

      {/* These three speak for the whole app rather than for the view below, and they sat
          between the header and `<main>`, which is to say inside no landmark at all. A screen
          reader user moving by landmarks (the normal way to skim a page) reached the nav and
          the main content and never these -- so "deletion is armed", a reap in flight, and a
          stale scan were exactly the three facts that navigation could not reach. Naming the
          section is what makes it a landmark; `.app` is plain block flow with no child or
          sibling selectors, so the wrapper moves nothing on screen. Caught by the page-level
          axe audit in `AppStaleRead.test.tsx`, which is the only test that mounts the shell. */}
      <section className="app-status" aria-label="Status">
        <SafetyBanner onGoToDeletion={() => goToPolicySection("deletion")} />
        <ReapBar onView={(runId) => setReapSheetRun(runId)} />
        {view === "review" && (
          <ScanFreshness
            snapshot={snapshot}
            isPending={snapshotPending}
            error={snapshotError}
            onGoToJobs={() => goToSettingsPanel("jobs")}
          />
        )}
      </section>

      <main className={splitOpen ? "split" : ""}>
        {/* One boundary for every route: the queue below is already in this chunk, so only
            a first visit to another view ever shows the fallback. */}
        <Suspense fallback={<RouteLoading />}>
          {view === "review" ? (
            <>
              <ReviewQueue
                verdict={verdict}
                onVerdictChange={changeVerdict}
                focus={reviewFocus}
                selectedId={selectedId}
                selectedGroupKey={selectedGroupKey}
                // No lane to carry on these two (unlike the jumps above): the row was picked
                // out of the lane's own list, so it is already on the lane behind the panel.
                onSelect={(id) => setSelected({ kind: "item", id })}
                onSelectGroup={(key) => setSelected({ kind: "group", key })}
                // Show latest closes an open why-panel: its candidate id belongs to the OLD
                // snapshot, so a refetch could only return a stale row. The show panel is keyed on
                // a stable group key and refreshes in place, so only the item selection is cleared
                // (B-7).
                onClearItemSelection={() => setSelected((s) => (s?.kind === "item" ? null : s))}
                stepRef={stepRef}
                // The newest completed scan, from the shell's status poll. When it moves past
                // the snapshot the queue is showing, the queue offers (or quietly takes) the
                // fresher one instead of leaving the reviewer on a stale list.
                latestScanSnapshotId={scanStatus?.snapshot_id ?? null}
              />
              {selectedId !== null &&
                (detail ? (
                  <WhyPanel
                    item={detail}
                    onClose={() => setSelected(null)}
                    // Also no lane: a show is listed on every lane one of its seasons is on,
                    // and the season this panel is open on is on the current one, so its show
                    // is already in the list behind.
                    onShowGroup={(key) => setSelected({ kind: "group", key })}
                  />
                ) : (
                  <WhyPanelFallback error={detailError} onClose={() => setSelected(null)} />
                ))}
              {selectedGroupKey !== null &&
                (groupDetail ? (
                  <ShowPanel
                    group={groupDetail}
                    // This one DOES carry a lane: the panel lists every season the show has,
                    // whatever Reaper decided, so the season picked here is regularly not on
                    // the lane the queue behind it is showing (rule 72 -- the same jump as
                    // Scales', reached from inside Review).
                    onOpenSeason={goToItemReasons}
                    onClose={() => setSelected(null)}
                  />
                ) : (
                  <WhyPanelFallback error={groupError} onClose={() => setSelected(null)} />
                ))}
            </>
          ) : view === "policy" ? (
            <PolicyEditor focus={policyFocus} />
          ) : view === "reap" ? (
            <ReapPlan
              onGoToDeletion={() => goToPolicySection("deletion")}
              onGoToPlexSettings={() => goToSettingsPanel("plex")}
              onGoToReview={() => goTo({ view: "review", select: null })}
            />
          ) : view === "fairness" ? (
            <>
              <Fairness
                selectedIdentity={scalesUser}
                onSelectPerson={openScalesPerson}
                onOpenUnmatched={openScalesUnmatched}
                unmatchedSelected={scalesUnmatched}
              />
              {scalesUser !== null &&
                (personDetail ? (
                  <ScalesPanel
                    detail={personDetail}
                    onClose={() => setScalesUser(null)}
                    onOpenItem={goToItemReasons}
                    onOpenGroup={goToGroupReasons}
                  />
                ) : (
                  <ScalesPanelFallback error={personError} onClose={() => setScalesUser(null)} />
                ))}
              {scalesUnmatched && (
                <NotInScanPanel
                  items={fairnessReport?.unmatched ?? []}
                  isPending={fairnessPending}
                  error={fairnessError}
                  onClose={() => setScalesUnmatched(false)}
                />
              )}
            </>
          ) : (
            <Settings
              key={settingsFocus?.nonce ?? "settings"}
              initialPanel={settingsFocus?.panel}
              // The Lists rows' policy-use links land on the keep-rules card's section.
              onGoToPolicy={() => goToPolicySection("kept")}
            />
          )}
        </Suspense>
      </main>
      {reapSheetRun !== null && (
        <ReapSheetLoader runId={reapSheetRun} onClose={() => setReapSheetRun(null)} />
      )}
    </div>
  );
}

/** Once signed in, a fresh install goes to the setup wizard until it is configured and has
 *  scanned once; after that (or if the owner skips) it is the dashboard. */
function Authed({ user }: { user: AuthUser }) {
  const [skipped, setSkipped] = useState(false);
  // Latched, because the wizard's own last step is what makes `complete` true: finishing the
  // first scan flips `has_scanned`, and deriving this gate live then unmounted the wizard in
  // the same commit that its finish panel would have rendered in. That panel is where an
  // install that skipped Plex is told a real reap would still be refused (#383), so the screen
  // reporting the result of setup was the one screen setup could never show. Once the wizard
  // has been needed it stays until the operator leaves it, and every step past the password
  // carries a way out.
  const [wasNeeded, setWasNeeded] = useState(false);
  const {
    data: setup,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["setup"],
    queryFn: api.setupStatus,
  });

  // Above the early return, so the hook order holds whichever branch renders (rule 146).
  useSlowWait(isLoading ? "Still loading Reaper." : null);

  if (isLoading) {
    return (
      // `role="status"` with no `aria-live` at all used to sit here, which is the markup
      // `Notice.tsx`'s own comment documents as reading correct and staying silent (#332). The
      // sr-only word stays: it is read in document order like any other text, and the sentence
      // for a wait that runs long goes through `announce.tsx`.
      <div className="auth-screen">
        {/* The spinner is the whole screen, and ARIA does not expose a name on a plain span, so
            the aria-label this used to carry reached nobody: Reaper's first screen announced as
            an empty page. The word goes in the tree instead of on the element. */}
        <span className="spinner spinner-lg" aria-hidden="true" />
        <span className="sr-only">Loading Reaper…</span>
      </div>
    );
  }

  // Treat an UNREAD setup status as "setup still needed": if the status call has never landed we
  // cannot prove the install is configured, and dropping a genuinely-fresh install onto an empty
  // Dashboard (with no way back to the wizard) is the worse failure. The owner can still skip
  // past it.
  //
  // That argument stops applying the moment a status HAS landed, and the undivided `isError` it
  // used to be went on applying it anyway. React Query keeps the last good value through a failed
  // refetch and raises `isError` beside it, so a blinked read on a configured install rendered the
  // setup wizard over the whole Dashboard -- the split #140 made in the settings panels, at the
  // one gate that sits above everything (#181). Every trigger is an ordinary in-app action that
  // invalidates `["setup"]`: linking or unlinking Plex, saving a service, saving general settings.
  //
  // Both gates keep their surface WITHOUT a `StaleReadNotice`, which every surface that keeps
  // its content pairs with it, and the departure is deliberate: these two
  // reads route, they do not render. Nothing on the Dashboard is derived from `setup.complete`,
  // and a stale `["me"]` at worst shows a yesterday's username in the menu, so an app-wide amber
  // banner would state a staleness the operator cannot see the effect of (rule 21).
  // The blast radius is why it is worth the divided test here: everything below unmounts,
  // including Settings, whose unsaved-edits guard then never runs, because the unmount comes from
  // above the panel holding the draft (rule 146).
  const needsSetup = setup === undefined ? isError : !setup.complete;
  // Adjusting state during render, which React allows for exactly this: it re-renders before
  // committing, so the wizard never flashes out and back. A configured install never sets the
  // latch, so a blinked read still holds the Dashboard, which is the split above.
  if (needsSetup && !wasNeeded) setWasNeeded(true);
  if ((needsSetup || wasNeeded) && !skipped) {
    return (
      <Suspense fallback={<RouteLoading />}>
        <SetupWizard onSkip={() => setSkipped(true)} />
      </Suspense>
    );
  }
  return (
    <BackNavProvider>
      <DocsProvider>
        <Dashboard user={user} />
      </DocsProvider>
    </BackNavProvider>
  );
}

/** The gate. Nothing renders until we know who (if anyone) is signed in. */
export function App() {
  const { data: user, isLoading } = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    // A 401 is the normal logged-out state, not something to retry into a storm.
    retry: false,
    staleTime: 0,
  });

  useSlowWait(isLoading ? "Still loading Reaper." : null);

  // One expression rather than the three early returns this used to be, so `Announcer` is a
  // sibling of every branch instead of three copies of itself (rule 72). It has to sit above
  // the whole gate: a polite region only speaks reliably when it was already in the DOM before
  // the message arrived, so it must outlive a route change, a Suspense fallback and a logout.
  //
  // `!user` alone, never `isError || !user` (#181). A read that never landed leaves `user`
  // undefined and lands on Login by this same test, and a signed-out answer reaches the gate as
  // DATA rather than as an error, because every 401 outside `/api/auth/` writes `["me"] = null`
  // (`main.tsx`) and a sign-out that worked writes it directly (`signOut.onSuccess`). So the
  // `isError` arm only ever covered the TRANSIENT case: a refetch that failed while React Query
  // still held the signed-in user, answered by showing the login screen to somebody who is signed
  // in. A sign-out that failed on a flaky network reached it and signed the operator out of the
  // UI anyway, which is the opposite of what that failure means.
  //
  // What this test canNOT see is a 401 on `["me"]` itself: the handler exempts the whole
  // `/api/auth/` prefix, so this key's own read arrives as an error with the last good user still
  // held. A writer that means to convey SIGNED OUT therefore has to put that state in, never
  // refetch to discover it -- which is why the sign-out above writes on success. Invalidating is
  // still right where the question is open rather than answered: `signOut.onError`, where the
  // session may well still be live, and `Login`'s `onAuthed`, where the refetch is the sign-in.
  return (
    <>
      <Announcer />
      {isLoading ? (
        <div className="auth-screen">
          <div className="auth-aurora" aria-hidden="true" />
          {/* Same as the setup gate above, live region dropped and all (rule 72, #332). The
              sentence reaches `Announcer` even here, where it is this branch's SIBLING rather
              than its ancestor: both mount in the same commit, and the wait is only spoken
              `SLOW_WAIT_MS` later, by which time the region has been in the DOM the whole time.
              Pre-existing the message is the property that matters, not pre-existing the
              spinner. */}
          <span className="spinner spinner-lg" aria-hidden="true" />
          <span className="sr-only">Loading Reaper…</span>
        </div>
      ) : !user ? (
        <Login />
      ) : (
        <Authed user={user} />
      )}
    </>
  );
}
