// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { applyAccent } from "./accent";
import { api, ApiError, type AuthUser, type Snapshot, type Verdict } from "./api";
import { BackNavProvider, useBackGuard, useBackNav, useModalOpen } from "./backnav";
import { Login } from "./components/Login";
import { ModalShell } from "./components/ModalShell";
import { NotInScanPanel } from "./components/NotInScanPanel";
import type { PolicySectionId } from "./components/PolicyEditor";
import { ReapConfirm } from "./components/ReapConfirm";
import { ReviewQueue } from "./components/ReviewQueue";
import { ScalesPanel, ScalesPanelFallback } from "./components/ScalesPanel";
import { BrandMark } from "./brand/BrandMark";
import type { Panel } from "./components/Settings";
import { ShowPanel } from "./components/ShowPanel";
import { WhyClose, WhyPanel } from "./components/WhyPanel";
import { DocsProvider } from "./docs/DocsContext";
import { bytes, count, date, souls } from "./format";
import { usePageScrollLock } from "./pageScrollLock";
import { useMediaQuery } from "./useMediaQuery";
import { useSafety } from "./useSafety";
import { useScanSettled } from "./useScanSettled";

// The review queue is the landing view and stays in the first chunk. Every other route is
// its own, fetched the first time it is opened: the whole app used to ship as one 551 kB
// script, so a first paint of the queue paid for the policy editor, the simulator, every
// settings panel and the docs before it could draw a single card (P-4). Each is a default
// export from a thin wrapper below, because these modules export more than one thing.
const PolicyEditor = lazy(async () => ({ default: (await import("./components/PolicyEditor")).PolicyEditor }));
const ReapPlan = lazy(async () => ({ default: (await import("./components/ReapPlan")).ReapPlan }));
const Fairness = lazy(async () => ({ default: (await import("./components/Fairness")).Fairness }));
const Settings = lazy(async () => ({ default: (await import("./components/Settings")).Settings }));
const SetupWizard = lazy(async () => ({ default: (await import("./components/SetupWizard")).SetupWizard }));

/** What a route shows while its chunk is on the way. The app's own spinner, announced, so a
 *  slow network reads as loading rather than as a blank page. */
function RouteLoading() {
  return (
    <div className="fair-loading" role="status" aria-live="polite">
      <span className="spinner spinner-xl" aria-hidden="true" />
      <p className="fair-loading-lead">Loading…</p>
    </div>
  );
}

type View = "review" | "policy" | "reap" | "fairness" | "settings";

/** What the review screen's side panel is showing: one item's reasoning, one whole
 *  show, or nothing. A single slot -- opening either closes the other. */
type Selection = { kind: "item"; id: number } | { kind: "group"; key: string } | null;

const NAV: { id: View; label: string }[] = [
  { id: "review", label: "Review" },
  { id: "policy", label: "Policy" },
  { id: "reap", label: "Reap" },
  { id: "fairness", label: "Scales" },
  { id: "settings", label: "Settings" },
];

/** The safety state, stated permanently and without euphemism.
 *
 *  While destructive actions are disabled, Reaper *structurally cannot* delete: the
 *  GuardedTransport refuses every mutating HTTP request, so this is a fact about the
 *  process rather than a promise about the UI. The banner says which regime you are in,
 *  always, because "can this thing delete my library right now?" should never require
 *  reading a settings page to answer. */
function SafetyBanner({ onGoToDeletion }: { onGoToDeletion: () => void }) {
  // The same authenticated query the deletion toggle invalidates, so arming or disarming
  // updates this banner in the same render pass -- and polled, so arming it somewhere else
  // reaches this tab too (useSafety says why). (/api/health is a bare liveness probe now;
  // it deliberately says nothing about the armed state.)
  const { data, isLoading, isError } = useSafety();

  // On the very first fetch we genuinely know nothing yet -- stay quiet rather than flash a
  // state we might immediately contradict.
  if (isLoading) return null;

  // The banner's whole promise is that the regime is stated *always*. If the safety state
  // can't be read and React Query has no last-known value to show, we must not just
  // disappear -- an absent banner reads as "nothing to worry about". Say the state is
  // unknown, in the amber "we could not look" tone, so it never reads as safe.
  if (isError || !data) {
    return (
      <div className="banner banner-unknown">
        <span className="banner-dot" aria-hidden="true" />
        <span>
          <strong>Safety state unknown.</strong> Reaper couldn't reach the server to confirm
          whether deletion is on. Until it can, treat this as armed and{" "}
          <button className="link" onClick={onGoToDeletion}>
            check Policy → Deletion
          </button>
          .
        </span>
      </div>
    );
  }

  if (!data.destructive_enabled) {
    return (
      <div className="banner banner-safe">
        <span className="banner-dot" aria-hidden="true" />
        <span>
          <strong>Read-only.</strong> Reaper can look but can't remove anything.{" "}
          <button className="link" onClick={onGoToDeletion}>
            Turn deletion on in Policy → Deletion
          </button>{" "}
          when you're ready.
        </span>
      </div>
    );
  }

  return (
    <div className="banner banner-armed">
      <span className="banner-dot" aria-hidden="true" />
      <span>
        <strong>Deletion is on.</strong> Reaper can remove media you approve, through Sonarr and
        Radarr.
      </span>
    </div>
  );
}

/** The app-wide reap bar: shown on every screen while a reap runs, so its count and its Stop
 *  are reachable after you close or navigate away from the reap sheet. A reap runs detached
 *  from the request that started it, so this bar (and Stop) survive navigating away and a tab
 *  reload -- it re-attaches by polling the shared status. Not a safety surface (the always-on
 *  one is SafetyBanner), so it shows nothing when idle. Stop is graceful: the run halts after
 *  the item in flight and still tidies Plex, and deletion stays armed. */
function ReapBar({ onView }: { onView: (runId: number) => void }) {
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
  // refresh belongs HERE, on the one component a reap cannot unmount: the confirmation sheet
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
            {souls(status.deleted_items)} removed · {bytes(status.deleted_bytes)} freed
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
      <span className="reap-bar-text">
        {status.stopping ? (
          <b>Stopping after the current one…</b>
        ) : (
          <>
            <b>
              Reaping · {count(status.done)} of {count(status.total)}
            </b>{" "}
            <span className="reap-bar-sub">· {bytes(status.deleted_bytes)} freed</span>
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
        <p className="notice notice-error reap-bar-error">
          Reaper couldn't stop the reap: {stop.error.message}
        </p>
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
  return (
    <ModalShell title="Reap report" onClose={onClose}>
      <div className="service-form">
        {isPending ? (
          <p className="help">Loading the reap…</p>
        ) : (
          <p className="notice notice-error">
            {error instanceof ApiError && error.status === 404
              ? "That reap is no longer available."
              : "Reaper couldn't load this reap. Reload the page to try again."}
          </p>
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
    return (
      <p className="notice notice-error scan-freshness">
        Couldn't read the last scan, so Reaper can't say how old this queue is. Reload the
        page to try again.
      </p>
    );
  }
  return (
    <p className="scan-freshness muted">
      Last scanned {date(snapshot.created_at)} · {count(snapshot.item_count)} items
      {snapshot.degraded && (
        <span className="freshness-warn">
          {" "}
          · that scan came back incomplete, so Reaper won't act on it
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
export function UserMenu({ user }: { user: AuthUser }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  // A mutation, not a fire-and-forget async onClick: a sign-out that fails must say so.
  // The session would still be live, and a swallowed error leaves the menu open with the
  // user still signed in and nothing to explain why.
  const signOut = useMutation({
    mutationFn: () => api.logout(),
    // onSettled, not onSuccess: either way the gate must re-evaluate. On success /me now
    // 401s and the login screen returns; on failure the refetch confirms we are still in.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["me"] }),
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
      >
        {user.thumb_url ? (
          <img src={user.thumb_url} alt="" className="user-avatar" />
        ) : (
          <span className="user-avatar user-avatar-fallback">{initial}</span>
        )}
        <span className="user-name">{user.username}</span>
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
          <button
            className="user-dropdown-item"
            onClick={() => signOut.mutate()}
            disabled={signOut.isPending}
          >
            {signOut.isPending ? "Signing out…" : "Sign out"}
          </button>
          {signOut.isError && (
            <p className="notice notice-error notice-inline">Couldn't sign you out. Try again.</p>
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
function WhyPanelFallback({ error, onClose }: { error: boolean; onClose: () => void }) {
  return (
    <aside className="why">
      <WhyClose onClose={onClose} />
      {error ? (
        <>
          <header className="why-head">
            <h2>Something went wrong</h2>
          </header>
          <p className="notice notice-error">
            Couldn't load the reasons for this item. The item itself is unaffected. Close this
            panel and click the item to try again.
          </p>
        </>
      ) : (
        <div className="why-loading" role="status" aria-live="polite">
          <span className="spinner spinner-lg" aria-hidden="true" />
          <p className="why-loading-lead">Fetching what Reaper saw…</p>
        </div>
      )}
    </aside>
  );
}

/** A thin accent line pinned to the very top of the window while a scan runs in the
 *  background, filling to the scan's real percent and gone the moment it finishes. Ambient
 *  by design: it only says "a scan is working"; the phase, counts and controls live on the
 *  scan bar (Settings → Jobs), which is where you go to actually read them.
 *
 *  Unlike SafetyBanner this is not a safety surface, so it may show nothing when it knows
 *  nothing: an absent line reads as "idle", the calm and correct default, and a dropped
 *  status poll must not paint a scan that may not be running. (The armed-state banner is the
 *  surface that must never fail quiet.) Kept mounted so it can fade rather than pop, and
 *  aria-hidden while idle so a screen reader hears it only when there is activity. */
export function ScanLine({ running, percent }: { running: boolean; percent: number }) {
  const pct = Math.max(0, Math.min(100, percent));
  return (
    <div
      className={running ? "scanline on" : "scanline"}
      role="progressbar"
      aria-label="Scanning your library"
      aria-hidden={!running}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={running ? Math.round(pct) : undefined}
    >
      <div className="scanline-fill" style={{ width: `${pct}%` }} />
    </div>
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
  // in index.css); a wider screen keeps the list visible beside it. That is what decides whether
  // the window scroll still tracks the list while a panel is open (used just below). 900px must
  // match that full-screen-sheet breakpoint (rule 67).
  const fullSheet = useMediaQuery("(max-width: 900px)");

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
  // (`splitOpen && fullSheet`, matching the 900px block in index.css): on a wider screen the
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
  const changeView = (next: View) => {
    if (next !== view) pushNav(() => setView(view));
    setView(next);
  };
  const changeVerdict = (next: Verdict) => {
    if (next !== verdict) pushNav(() => setVerdict(verdict));
    setVerdict(next);
    setSelected(null);
  };

  // Cross-page jumps: "Turn it on in Policy → Deletion" from the Reap page lands on
  // the Deletion section, "Settings → Plex" lands on the Plex panel. The nonce makes
  // each jump fire once; plain tab clicks clear the focus so revisiting a page never
  // replays an old jump.
  const [policyFocus, setPolicyFocus] = useState<{
    section: PolicySectionId;
    nonce: number;
  } | null>(null);
  const [settingsFocus, setSettingsFocus] = useState<{ panel: Panel; nonce: number } | null>(
    null,
  );

  const goToPolicySection = (section: PolicySectionId) => {
    setPolicyFocus({ section, nonce: Date.now() });
    changeView("policy");
  };
  const goToSettingsPanel = (panel: Panel) => {
    setSettingsFocus({ panel, nonce: Date.now() });
    changeView("settings");
  };

  // Scales lists titles without saying why each one is where it is. Opening one lands on its
  // reasoning, which lives on the review screen beside the queue -- an item on its own card,
  // a whole show on its group panel.
  const goToItemReasons = (candidateId: number) => {
    setSelected({ kind: "item", id: candidateId });
    changeView("review");
  };
  const goToGroupReasons = (key: string) => {
    setSelected({ kind: "group", key });
    changeView("review");
  };

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
  // another device) still surfaces here without a manual start or a tab refocus -- the
  // whole point of a global "something is running" line. Shares the ["scanStatus"] cache
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
  useScanSettled(scanStatus?.running ?? false);

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
      // Browser and OS shortcuts keep their meaning, and typing in a field is typing.
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
      if (e.key === "Escape") {
        setSelected(null);
        return;
      }
      const delta =
        e.key === "ArrowDown" || e.key === "j"
          ? 1
          : e.key === "ArrowUp" || e.key === "k"
            ? -1
            : 0;
      if (delta === 0) return;
      e.preventDefault();
      stepRef.current?.(delta);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, hasSelection, modalOpen]);

  // Back-button layers for the side panels and the app-wide reap sheet (the tab changes are
  // recorded by changeView/changeVerdict above). Each is gated on the view that actually shows
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
            <span className="brand-word">Reaper</span>
            <span className="muted brand-sub">explainable pruning for Plex</span>
          </div>
        </div>

        <nav className="views" aria-label="Sections">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "tab active" : "tab"}
              // Reserve the bold (active) width so switching sections never shifts the nav.
              data-label={n.label}
              // The view you are on is stated, not just colored.
              aria-current={view === n.id ? "page" : undefined}
              onClick={() => {
                // A plain tab visit must not replay an old cross-page jump -- but clicking the
                // tab you are ALREADY on is not a visit, and clearing the focus there changes
                // the key Settings is mounted under, remounting the whole subtree and throwing
                // away whatever is typed into it (B-23). Arriving from a "Settings → Jobs" link,
                // typing a name, then clicking Settings in the nav used to silently discard it.
                if (n.id !== view) {
                  setPolicyFocus(null);
                  setSettingsFocus(null);
                }
                // Leaving Scales (or re-entering it) closes any open Scales panel, so the
                // split view never lingers on a tab that has no panel to show. Both the person
                // panel and the "not in the last scan" panel are cleared, matching the
                // mutual-exclusion pairing the open handlers use (U-5).
                setScalesUser(null);
                setScalesUnmatched(false);
                changeView(n.id);
              }}
            >
              {n.label}
            </button>
          ))}
        </nav>

        <UserMenu user={user} />
      </header>

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

      <main className={splitOpen ? "split" : ""}>
        {/* One boundary for every route: the queue below is already in this chunk, so only
            a first visit to another view ever shows the fallback. */}
        <Suspense fallback={<RouteLoading />}>
        {view === "review" ? (
          <>
            <ReviewQueue
              verdict={verdict}
              onVerdictChange={changeVerdict}
              selectedId={selectedId}
              selectedGroupKey={selectedGroupKey}
              onSelect={(id) => setSelected({ kind: "item", id })}
              onSelectGroup={(key) => setSelected({ kind: "group", key })}
              // Show latest closes an open why-panel: its candidate id belongs to the OLD
              // snapshot, so a refetch could only return a stale row. The show panel is keyed on
              // a stable group key and refreshes in place, so only the item selection is cleared
              // (B-7).
              onClearItemSelection={() =>
                setSelected((s) => (s?.kind === "item" ? null : s))
              }
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
                  onShowGroup={(key) => setSelected({ kind: "group", key })}
                />
              ) : (
                <WhyPanelFallback error={detailError} onClose={() => setSelected(null)} />
              ))}
            {selectedGroupKey !== null &&
              (groupDetail ? (
                <ShowPanel
                  group={groupDetail}
                  onOpenSeason={(id) => setSelected({ kind: "item", id })}
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
            onGoToReview={() => {
              setSelected(null);
              changeView("review");
            }}
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
  const { data: setup, isLoading, isError } = useQuery({
    queryKey: ["setup"],
    queryFn: api.setupStatus,
  });

  if (isLoading) {
    return (
      <div className="auth-screen">
        <span className="spinner spinner-lg" aria-label="Loading" />
      </div>
    );
  }

  // Treat an unreadable setup status as "setup still needed": if the status call errors we
  // cannot prove the install is configured, and dropping a genuinely-fresh install onto an
  // empty Dashboard (with no way back to the wizard) is the worse failure. The owner can
  // still skip past it. Only a status we could read *and* that says complete lands on the
  // Dashboard directly.
  const needsSetup = isError || (setup !== undefined && !setup.complete);
  if (needsSetup && !skipped) {
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
  const { data: user, isLoading, isError } = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    // A 401 is the normal logged-out state, not something to retry into a storm.
    retry: false,
    staleTime: 0,
  });

  if (isLoading) {
    return (
      <div className="auth-screen">
        <div className="auth-aurora" aria-hidden="true" />
        <span className="spinner spinner-lg" aria-label="Loading" />
      </div>
    );
  }

  if (isError || !user) return <Login />;

  return <Authed user={user} />;
}
