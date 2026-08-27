// SPDX-License-Identifier: AGPL-3.0-or-later

import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { applyAccent } from "./accent";
import { Announcer, useSlowWait } from "./announce";
import { api, ApiError, type AuthUser, type Verdict } from "./api";
import { BackNavProvider, useBackGuard, useBackNav, useModalOpen } from "./backnav";
import { Login } from "./components/Login";
import { ModalShell } from "./components/ModalShell";
import { NotInScanPanel } from "./components/NotInScanPanel";
import type { PolicySectionId } from "./components/PolicyEditor";
import { ReapBar } from "./components/ReapBar";
import { ReapConfirm } from "./components/ReapConfirm";
import { ScanFreshness } from "./components/ScanFreshness";
import { SectionNav } from "./components/SectionNav";
import { UserMenu } from "./components/UserMenu";
import { WhyPanelFallback } from "./components/WhyPanelFallback";
import { ReviewQueue } from "./components/ReviewQueue";
import { ScalesPanel, ScalesPanelFallback } from "./components/ScalesPanel";
import { BrandMark } from "./brand/BrandMark";
import type { Panel } from "./components/Settings";
import { ShowPanel } from "./components/ShowPanel";
import { WhyPanel } from "./components/WhyPanel";
import { DocsProvider } from "./docs/DocsContext";
import type { Focus, NavIntent, Selection, View } from "./navIntent";
import { readLanding, sectionUrl, writeUrl } from "./navUrl";
import { usePageScrollLock } from "./pageScrollLock";
import { useGeneralSettings, useSeedLanguage } from "./useGeneralSettings";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "./useMediaQuery";
import { useScanSettled } from "./useScanSettled";
import { Notice } from "./components/Notice";
import { SafetyBanner } from "./components/SafetyBanner";
import { ScanLine } from "./components/ScanLine";

// The review queue loads in the first chunk, since it is the app's landing view. Every
// other route loads its own chunk the first time someone opens it, keeping the first paint
// small. Each wrapper below turns a named export into a default one, since lazy() needs a
// default export and these modules export more than one thing.
const PolicyEditor = lazy(async () => ({
  default: (await import("./components/PolicyEditor")).PolicyEditor,
}));
const ReapPlan = lazy(async () => ({ default: (await import("./components/ReapPlan")).ReapPlan }));
const Fairness = lazy(async () => ({ default: (await import("./components/Fairness")).Fairness }));
const Settings = lazy(async () => ({ default: (await import("./components/Settings")).Settings }));
const SetupWizard = lazy(async () => ({
  default: (await import("./components/SetupWizard")).SetupWizard,
}));

/** What a route shows while its chunk loads: the app's spinner, plus a sentence on a slow
 *  network so a long wait reads as loading rather than a blank page.
 *
 *  The sentence goes through the always-mounted region in `announce.tsx` instead of a
 *  `role="status"` region declared here, because a screen reader can miss a live region
 *  created in the same commit as its own text. `useSlowWait` only speaks once the wait has
 *  actually run long. This component mounts only while the chunk is loading, so its unmount
 *  cancels the announcement for a fast load. */
function RouteLoading() {
  const { t } = useTranslation();
  useSlowWait(t("shell.app.routeLoadingSlow"));
  return (
    <div className="fair-loading">
      <span className="spinner spinner-xl" aria-hidden="true" />
      <p className="fair-loading-lead">{t("common.loading")}</p>
    </div>
  );
}

/** Reopens the reap sheet for a run by id, from the app-wide bar's View button on any screen.
 *  Fetches the run and hands it to the same ReapConfirm the review queue uses, which
 *  re-attaches to the live status and shows progress or the finished report. */
function ReapSheetLoader({ runId, onClose }: { runId: number; onClose: () => void }) {
  const { t } = useTranslation();
  const {
    data: run,
    isPending,
    error,
  } = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId) });
  if (run) return <ReapConfirm run={run} onClose={onClose} />;
  // This component must always render something. The query retries once with no
  // refetch-on-focus, so a failed fetch settles into an error state, and a null render then
  // would leave the bar's View button permanently dead: useBackGuard keys on `reapSheetRun`,
  // not on what this renders, so a Back press would silently close an invisible sheet. Show
  // a loading line or a plain error, both routed through ModalShell's own close button.
  //
  // The error text here must never tell the operator to reload the page. This sheet renders
  // outside `<main>`, over a mounted review queue, unlike the app's other error screens. The
  // queue keeps its selection only in component state (its filters are the only part that
  // persists), and "Select everything matching" can build that selection by paging through
  // thousands of rows. A reload would drop it with no warning, since nothing in the app
  // confirms before a page unload. Point the operator at the close button this modal already
  // has instead.
  return (
    <ModalShell title={t("shell.app.reapReportTitle")} onClose={onClose}>
      <div className="service-form">
        {isPending ? (
          <p className="help">{t("shell.app.loadingReap")}</p>
        ) : (
          <Notice tone="error">
            {error instanceof ApiError && error.status === 404
              ? t("shell.app.reapUnavailable")
              : t("shell.app.reapLoadFailed")}
          </Notice>
        )}
      </div>
    </ModalShell>
  );
}

function Dashboard({ user }: { user: AuthUser }) {
  const { t } = useTranslation();
  // `view` and `verdict` are read from the URL once, at mount (see navUrl.ts), and never
  // again. The URL decides where a cold load lands and nothing after that: `backnav` owns
  // Back, and its undo sets these two state variables directly, so re-reading the URL later
  // would fight that undo.
  const [view, setView] = useState<View>(() => readLanding().view);
  const [verdict, setVerdict] = useState<Verdict>(() => readLanding().lane);
  // Settings and Policy are the two sections with their own sub-navigation, and their state
  // lives here for the same reason `verdict` does: the address bar names where the operator
  // is inside the section, and this component writes the URL. `settingsPanel`, `policyMedia`
  // and `policySection` are the whole of that state; `Settings` and `PolicyEditor` render
  // whatever they are handed and report a click back up, rather than keeping their own copy.
  const [settingsPanel, setSettingsPanel] = useState<Panel>(() => readLanding().panel);
  // Policy needs its own media type here, not inside `PolicyEditor`, because Movies and TV
  // are separate policies with separate caps, byte budgets and weights. Reloading a page on
  // the policy section URL must reopen with the right media type's numbers, not just the
  // right section.
  const [policyMedia, setPolicyMedia] = useState<"movie" | "tv">(() => readLanding().policyMedia);
  const [policySection, setPolicySection] = useState<PolicySectionId>(
    () => readLanding().policySection,
  );
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
  // The only two views that render the split layout. Off them, there is no list scroll to keep.
  const listView = view === "review" || view === "fairness";
  // On a phone, the panel covers the list as a full-screen sheet (`main.split .why` below
  // 900px in styles/10-layout.css); a wider screen keeps the list visible beside it. This
  // decides whether the window scroll below still tracks the list while a panel is open. The
  // breakpoint here must match that CSS rule, so both read the same shared value.
  const fullSheet = useMediaQuery(NARROW_SCREEN_QUERY);

  // Remembers where the reviewer scrolled the list, so opening or closing a panel does not
  // lose their place. Opening turns the list into the side-by-side split and closing reverts
  // it; that layout change resets the window scroll to the top in some browsers (Safari), and
  // on a phone closing the full-screen panel does the same. An operator can page through
  // thousands of items, so losing that place costs real time to redo. The saved position is
  // restored in a layout effect, before the browser paints, so the list never visibly jumps.
  // `BackNavProvider` sets `scrollRestoration` to `manual`, so its own Back sentinel cannot
  // fight this restore.
  const listScrollRef = useRef(0);
  useEffect(() => {
    // Tracks the list's scroll position continuously, so a close lands where the reviewer is
    // now, even if they scrolled and opened a different card while the panel was already
    // open. Stops tracking only while a full-screen sheet covers the list on a phone: the
    // list itself is not scrolling then, so any movement is the page drifting behind the
    // sheet and must not overwrite the saved position. On a wider screen the list stays
    // visible beside the panel, so tracking continues.
    if (!listView || (splitOpen && fullSheet)) return;
    const onScroll = () => {
      listScrollRef.current = window.scrollY;
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [splitOpen, fullSheet, listView]);
  // Restores scroll only on a genuine open or close within one list view. A page navigation
  // owns its own scroll instead, and the view guard below tells the two apart.
  const splitPrevRef = useRef({ open: splitOpen, view });
  useLayoutEffect(() => {
    const prev = splitPrevRef.current;
    const toggledInPlace = prev.view === view && prev.open !== splitOpen;
    splitPrevRef.current = { open: splitOpen, view };
    if (toggledInPlace) window.scrollTo(0, listScrollRef.current);
  }, [splitOpen, view]);

  // Freezes the list while a panel covers the whole screen on a phone (`splitOpen &&
  // fullSheet`, matching the 900px block in styles/12-why-panel.css), so a touch drag scrolls
  // the panel's own content instead of the list underneath it. On a wider screen the list
  // stays visible beside the panel and is meant to keep scrolling, so the freeze is left off.
  // It parks and restores `window.scrollY` through the same ref-counted lock the modal shell
  // uses, returning to the same pre-open offset the restore above keeps, so the two never
  // conflict.
  usePageScrollLock(splitOpen && fullSheet);

  // The browser Back button steps back through the app's own UI instead of leaving Reaper.
  // Open panels and menus register themselves with `useBackGuard` (below, and in their own
  // components); a tab or section change records its own undo here. `pushNav` captures the
  // current location so a later Back can restore it. The undo calls the raw state setter
  // directly, never these wrapper functions, so restoring never records a further undo.
  const { pushNav } = useBackNav();

  // `focus` names the app's current jump target. It lives in `Dashboard`, above the view
  // components, so a jump can name a destination on a page that is not mounted yet. Each
  // focus fires once, tracked by its `nonce`: revisiting the page later must not replay the
  // jump that first brought the operator there.
  //
  // A cold load on `/policy/tv/deletion` seeds one, because the policy page's sections are
  // places on one long page rather than separate panels, and landing on one means scrolling
  // to it, which `PolicyEditor` already knows how to do for a jump. The media type needs no
  // seed: it is a prop the editor reads, not a place on the page. Settings needs none either,
  // since its panel is the whole of what it shows.
  const [focus, setFocus] = useState<Focus | null>(() => {
    const landing = readLanding();
    return landing.view === "policy"
      ? { view: "policy", section: landing.policySection, nonce: Date.now() }
      : null;
  });
  // Each view reads only the focus that names it, and this is not a formality: `goTo` sets
  // the focus and the view in one commit, and the effect below only drops a stale focus after
  // that commit runs. So a single render can briefly hold the previous view's focus while the
  // new view is already on screen.
  const reviewFocus = focus?.view === "review" ? focus : null;
  const policyFocus = focus?.view === "policy" ? focus : null;
  const settingsFocus = focus?.view === "settings" ? focus : null;

  // A focus dies the moment its view is left, whether by a nav click or a Back press: a
  // Back press restores `view` through the raw setter, running no handler here at all. The
  // review queue unmounts when its view is left, so if this effect did not clear a stale
  // focus, returning to Review later would re-seed the search box from an old jump, even one
  // the operator has since backed out of or cleared by hand.
  //
  // The effect keys on `view` alone, not on any specific view, so clicking the tab you are
  // already on changes nothing. Settings has no focus and no key, so this effect cannot
  // discard anything there; the guard only matters for Review, where a stale focus would
  // replay a search the operator already cleared.
  //
  // Naming no specific view means adding a future destination needs no change here.
  useEffect(() => {
    setFocus((f) => (f?.view === view ? f : null));
  }, [view]);

  // The address bar names the current section, and, where the section has its own
  // sub-navigation, the panel open inside it. Review is the one section this effect does not
  // write: its URL also carries the lane and the filters, both of which live in
  // `ReviewQueue`, so the queue writes that whole URL itself. Each part of the URL has one
  // writer.
  //
  // A side panel is never put in the URL. A candidate id belongs to one snapshot, and the
  // next scan would leave a saved link pointing at a row that no longer exists. Scales' person
  // panel is keyed on a stable identity rather than a snapshot row, so that reason does not
  // apply there, but it stays out of the URL anyway: a person's own identifier is the one
  // thing on these screens that names them, and a URL is the part of the app people paste
  // into a chat.
  useEffect(() => {
    if (view !== "review") {
      writeUrl(sectionUrl(view, { panel: settingsPanel, policyMedia, policySection }));
    }
  }, [view, settingsPanel, policyMedia, policySection]);

  // Every jump in the app goes through this one function. The caller names a whole
  // destination (see navIntent.ts) and `goTo` applies it; nothing else calls the raw state
  // setters, so a new destination is a new call site here rather than a new function with its
  // own idea of what a jump resets.
  //
  // Three things it has to get right:
  //
  // ONE BACK STEP for the whole jump, restoring the view and the lane together. The lane is
  // not a place the operator visited on its own, so undoing it as a separate step would land
  // them on a list they never actually saw. `view` and `verdict` are read at the moment of the
  // jump, so the undo restores where the operator actually was, and it calls the raw setters
  // directly, so restoring never records a further undo.
  //
  // THE LANE COMES FROM THE CALLER, never re-derived here, because only the caller knows which
  // lane it means: a show can sit in every lane one of its seasons does, so there is no single
  // lane to derive for a group, while Scales means the lane of the seasons that person asked
  // about. `reviewFate.laneOf` answers this for one item, and Scales sends the same
  // override-aware lane the queue itself filters on. A jump that opens an item without its
  // lane leaves the panel open above a list that does not contain that item: there is no card
  // to find, and neither the scroll-to-card nor the j/k list navigation can do anything about
  // an item that is off-lane.
  //
  // OMITTED IS NOT EMPTY. Every optional argument here is three-state, and the middle state
  // (omitted) is what lets one function serve both a plain section-nav click and a targeted
  // jump: `select: undefined` leaves an already-open panel alone, as when arriving on Review
  // from the nav; `select: null` closes it, as a lane tab does, since its new list does not
  // hold the open card. `search` works the same way: a jump from inside Review omits it, so
  // the operator's own search text stays where they left it.
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
      // A collection jump sets a focus even with no search text, since a focus is the only
      // one-shot channel that reaches the queue from `WhyPanel`, a sibling component. A
      // cross-section jump's search text rides the same channel.
      if (intent.search !== undefined || intent.collection !== undefined)
        setFocus({
          view: "review",
          search: intent.search ?? "",
          collection: intent.collection,
          nonce: Date.now(),
        });
    } else if (intent.view === "policy") {
      if (intent.section !== undefined) {
        setPolicySection(intent.section);
        // Setting the focus alongside the section is what scrolls the page there. The editor
        // can already be mounted when this jump arrives, since the safety banner's link is on
        // every screen, so only a fresh nonce can trigger a second jump to the same section.
        setFocus({ view: "policy", section: intent.section, nonce: Date.now() });
      } else {
        // A plain nav click names no section, so it lands on the top of the page. Using the
        // last-persisted section instead would put the rail's `aria-current` and the address
        // bar on a section the operator is not actually looking at, until a later scroll-spy
        // read corrects it. Settings needs no equivalent: each of its panels has its own
        // scroll position, so whichever one it was left on is still what it shows.
        setPolicySection("flags");
      }
    } else if (intent.view === "settings") {
      if (intent.panel !== undefined) {
        // Already on Settings, the currently open panel may hold unsaved edits, so this asks
        // for the new panel through a focus rather than setting it directly, letting the
        // confirm inside Settings refuse the switch. Arriving from another view there is
        // nothing mounted yet to lose, so the panel is set directly instead of painting the
        // old one for a frame first.
        if (view === "settings")
          setFocus({ view: "settings", panel: intent.panel, nonce: Date.now() });
        else setSettingsPanel(intent.panel);
      }
    }
    setView(intent.view);
  };

  const goToPolicySection = (section: PolicySectionId) => goTo({ view: "policy", section });
  const goToSettingsPanel = (panel: Panel) => goTo({ view: "settings", panel });
  // A lane tab: the new list does not hold whatever card is open, so the panel closes with it.
  const changeVerdict = (next: Verdict) => goTo({ view: "review", lane: next, select: null });
  // Opens one item's reasoning on the lane it lives in: an item on its own card, or a whole
  // show on its group panel. `search` is optional and the caller decides whether to send it.
  // A jump from another section arrives at an untouched queue and seeds the search box with
  // the title being opened, so the list behind the panel shows only that title rather than
  // the whole lane. A jump from inside Review (the season list in `ShowPanel`) sends no
  // search, since the operator's own search text is already in that box and seeding over it
  // would overwrite what they typed.
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
    // A 404 means no scan has run yet, a normal first-boot state rather than an error, so
    // retrying it on a loop would just be noise. Any other failure is a real error, which is
    // why `ScanFreshness` is handed the error itself, not just the missing data.
    retry: false,
  });

  // The browser tab shows the install's chosen name (Settings → General), so two Reaper
  // installs stay easy to tell apart. The default title is baked into index.html; only a
  // non-default name changes it, and a failed read leaves the title alone.
  const { data: generalSettings } = useGeneralSettings();
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

  // The background-scan indicator, polled from the app shell so it shows on every screen.
  // Polls fast while a scan runs, and slowly while idle, so a scan started elsewhere (the
  // scheduler, another device) still shows up here without a manual refresh or a tab
  // refocus. That is what a global "something is running" indicator is for. It shares the
  // ["scanStatus"] cache with the scan bar, so the two can never disagree.
  //
  // This poll is declared directly here, rather than through `useScanStatus` (which every
  // other reader uses), because that hook only polls fast during a scan and stays silent
  // otherwise. This one has to notice a scan nobody on this screen started, which needs the
  // slow idle poll too.
  const { data: scanStatus } = useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    refetchInterval: (query) => (query.state.data?.running ? 1000 : 15000),
  });
  // When a scan ends, refresh whatever the new snapshot changed. This runs off the shell's
  // own poll, for the same reason the code above does: a scan started from the Reap page,
  // the scheduler, or another device must refresh whichever screen the operator is actually
  // on, not only the one screen that happens to mount the scan bar.
  useScanSettled(scanStatus?.running ?? false, scanStatus?.error_reason ?? null);

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
  // list, so React Query serves it from one fetch, with no second network call. Only fetched on
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
      // Browser and OS shortcuts keep their meaning, and typing in a field is typing: `j`
      // and `k` are letters before they are queue steps. Escape belongs to the panel instead
      // of this handler; `WhyShell` owns it for all six of the panel types.
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) {
        return;
      }
      // While a modal is open it owns the keyboard: its own Escape closes it, and the panel
      // behind it must not move under it. `modalOpen` is read from state, which `ModalShell`
      // sets on mount, rather than probed for in the DOM on every keypress.
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
            {/* The authenticated app's only `h1`, so a screen reader jumping by heading level always has
                a top-level landing point. No heading level is skipped anywhere below it.
                "Only" is a claim about the whole authenticated tree, and it can go stale: the docs pane
                once rendered its own title as a second `h1` when Help opened, so it now renders `h3`
                there instead, nested under the `h2` `ModalShell` gives the dialog. Check this claim
                again before adding a heading here. `SetupWizard` and `Login` keep their own `h1` because
                each replaces this shell instead of rendering inside it. */}
            <h1 className="brand-word">Reaper</h1>
            <span className="muted brand-sub">{t("shell.app.brandTagline")}</span>
          </div>
        </div>

        <SectionNav
          view={view}
          onChange={(next) => {
            // No focus handling here: a plain tab visit must not replay an old cross-page
            // jump. The effect above already drops it, since it keys on `view` alone, so a
            // tab change clears it and clicking the tab you are already on leaves it alone.
            //
            // Leaving Scales, or re-entering it, closes any open Scales panel, so the split
            // view never lingers on a tab with no panel to show. Both the person panel and
            // the "not in the last scan" panel are cleared here, the same pair the open
            // handlers keep mutually exclusive.
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

      {/* This section wraps three facts that apply to the whole app, not just the view below:
          whether deletion is armed, a reap in progress, and a stale scan. Naming it as a
          landmark section lets a screen reader user browsing by landmark (the normal way to
          skim a page) reach these facts, the same way they reach the nav and the main content.
          `.app` is plain block flow with no child or sibling selectors, so this wrapper moves
          nothing on screen. `AppStaleRead.test.tsx` is the only test that mounts the shell, and
          it runs a page-level axe audit that catches a landmark like this going missing. */}
      <section className="app-status" aria-label={t("shell.app.statusLabel")}>
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
                // These two carry no lane, unlike the jumps above: the row was picked out of
                // the lane's own list, so it is already on the lane behind the panel.
                onSelect={(id) => setSelected({ kind: "item", id })}
                onSelectGroup={(key) => setSelected({ kind: "group", key })}
                // "Show latest" closes an open why-panel, since its candidate id belongs to
                // the old snapshot and a refetch could only return a stale row. The show panel
                // is keyed on a stable group key and refreshes in place instead, so only the
                // item selection is cleared here.
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
                    // Also no lane: a show appears on every lane one of its seasons is on,
                    // and the season this panel is open on is already on the current lane, so
                    // its show is already in the list behind.
                    onShowGroup={(key) => setSelected({ kind: "group", key })}
                    // The panel is a sibling of the queue (both mount here, in `App`), so
                    // opening a collection from its chip crosses that boundary the same way
                    // any other cross-component jump does, through `goTo`. `goTo` never
                    // touches `select`, so the panel stays open on the same item behind the
                    // swapped list.
                    onOpenCollection={(name) => goTo({ view: "review", collection: name })}
                    collectionSizes={snapshot?.collection_sizes ?? null}
                  />
                ) : (
                  <WhyPanelFallback error={detailError} onClose={() => setSelected(null)} />
                ))}
              {selectedGroupKey !== null &&
                (groupDetail ? (
                  <ShowPanel
                    group={groupDetail}
                    // This one does carry a lane: the panel lists every season a show has,
                    // whatever Reaper decided about each one, so the season picked here is
                    // often not on the lane the queue behind it is showing. The same jump
                    // Scales uses, reached here from inside Review.
                    onOpenSeason={goToItemReasons}
                    onClose={() => setSelected(null)}
                  />
                ) : (
                  <WhyPanelFallback error={groupError} onClose={() => setSelected(null)} />
                ))}
            </>
          ) : view === "policy" ? (
            <PolicyEditor
              focus={policyFocus}
              mediaType={policyMedia}
              onMediaTypeChange={setPolicyMedia}
              section={policySection}
              onSectionChange={setPolicySection}
            />
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
              panel={settingsPanel}
              onPanelChange={setSettingsPanel}
              jump={settingsFocus}
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
  const { t } = useTranslation();
  const [skipped, setSkipped] = useState(false);
  // `wasNeeded` latches the gate open, because the wizard's own last step is what makes
  // `complete` true. Finishing the first scan flips `has_scanned`, and reading this gate live
  // would unmount the wizard in the very commit that renders its finish panel. That panel is
  // where an install that skipped Plex is told a real reap would still be refused, so it is
  // the one screen setup could never show without the latch. Once the wizard has been needed
  // it stays until the operator leaves it, and every step past the password carries its own
  // way out.
  const [wasNeeded, setWasNeeded] = useState(false);
  const {
    data: setup,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["setup"],
    queryFn: api.setupStatus,
  });

  // Declared above the early return below, so hook order stays the same whichever branch
  // renders.
  useSlowWait(isLoading ? t("shell.app.stillLoadingReaper") : null);
  // The first signed-in moment is the earliest one where a browser that knows its own preferred
  // languages is talking to a server that cannot detect them. Here rather than in the wizard,
  // because an install past setup never runs the wizard again.
  useSeedLanguage();

  if (isLoading) {
    return (
      // The sr-only word here is read in document order like any other text; it is not a
      // live region. The sentence for a wait that runs long is announced separately, through
      // `announce.tsx`.
      <div className="auth-screen">
        {/* The spinner is the whole screen. ARIA exposes no name from a plain span, so an
            aria-label on this element would reach nobody; the word is placed in the tree as
            visible text instead. */}
        <span className="spinner spinner-lg" aria-hidden="true" />
        <span className="sr-only">{t("shell.app.loadingReaperEllipsis")}</span>
      </div>
    );
  }

  // An unread setup status (`setup === undefined`) is treated as "setup still needed": if the
  // status call has never landed there is no proof the install is configured, and dropping a
  // genuinely fresh install onto an empty Dashboard with no way back to the wizard is the worse
  // mistake. The owner can still skip past it.
  //
  // Once a status HAS landed, this reads `!setup.complete`, never `isError` alone. React Query
  // keeps the last good value through a failed refetch while still raising `isError` beside it,
  // so treating `isError` as "needs setup" would render the wizard over an already-configured
  // Dashboard on any transient refetch failure. An ordinary in-app action refetches this:
  // linking or unlinking Plex, saving a service, or saving general settings all invalidate
  // `["setup"]`.
  //
  // Both this gate and the `["me"]` gate in `App` render without a `StaleReadNotice`, unlike
  // every other surface that keeps its content on a stale read. That is deliberate: these two
  // reads decide which screen to show, they do not render data from it. Nothing on the
  // Dashboard reads `setup.complete` directly, and a stale `["me"]` at worst shows yesterday's
  // username in the menu, so an app-wide staleness banner would tell the operator about a
  // problem they cannot see any effect of.
  // Getting this right matters because a wrong flip unmounts everything below, including
  // Settings, whose unsaved-edits guard then never runs: the unmount happens above the panel
  // holding the draft, not inside it.
  const needsSetup = setup === undefined ? isError : !setup.complete;
  // Sets state during render, which React allows for exactly this: it re-renders before
  // committing, so the wizard never flashes in and back out. A configured install never sets
  // the latch, so a single stale read still shows the Dashboard, per the split above.
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
  const { t } = useTranslation();
  const { data: user, isLoading } = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    // A 401 is the normal logged-out state, not something to retry into a storm.
    retry: false,
    staleTime: 0,
  });

  useSlowWait(isLoading ? t("shell.app.stillLoadingReaper") : null);

  // `Announcer` sits above every branch of this gate, as one shared component rather than a
  // copy inside each branch, because a polite region only reliably speaks if it already
  // existed in the DOM before the message arrived. It must outlive a route change, a Suspense
  // fallback, and a logout.
  //
  // The gate below reads `!user` alone, never `isError || !user`. A read that has not landed
  // yet leaves `user` undefined and lands on Login the same way a real logout does, and a
  // signed-out answer reaches this gate as DATA rather than as an error: every 401 outside
  // `/api/auth/` writes `["me"] = null` (see `main.tsx`), and a successful sign-out writes it
  // directly (`UserMenu`'s `signOut.onSuccess`). Adding `isError` here would only ever catch
  // the transient case, a refetch that failed while React Query still held the signed-in
  // user, and answer it by showing Login to someone who is still signed in. A sign-out that
  // failed on a flaky network would reach that same branch and sign the operator out of the
  // UI anyway, the opposite of what that failure means.
  //
  // A 401 on `["me"]` itself is the one case `!user` cannot see: the handler exempts the
  // whole `/api/auth/` prefix, so this key's own read arrives as an error while the last good
  // user is still held. Conveying SIGNED OUT therefore means writing that state in directly,
  // never refetching to discover it, which is why `UserMenu`'s sign-out writes on success.
  // Invalidating `["me"]` is still right where the answer is genuinely open rather than
  // known: `UserMenu`'s `signOut.onError`, where the session may still be live, and `Login`'s
  // `onAuthed`, where the refetch IS the sign-in.
  return (
    <>
      <Announcer />
      {isLoading ? (
        <div className="auth-screen">
          <div className="auth-aurora" aria-hidden="true" />
          {/* Same reasoning as the setup gate above: no live region here. The sentence still
              reaches `Announcer` even though it is this branch's sibling rather than its
              ancestor, because both mount in the same commit and the wait is only spoken
              `SLOW_WAIT_MS` later, by which time the region has been in the DOM the whole
              time. What matters is that the region exists before the message, not before the
              spinner. */}
          <span className="spinner spinner-lg" aria-hidden="true" />
          <span className="sr-only">{t("shell.app.loadingReaperEllipsis")}</span>
        </div>
      ) : !user ? (
        <Login />
      ) : (
        <Authed user={user} />
      )}
    </>
  );
}
