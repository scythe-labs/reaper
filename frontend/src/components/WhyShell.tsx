// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one `.why` panel. Six surfaces render this shell: the movie/season why panel, the
// show panel, the Scales person panel, the not-in-scan panel, and the loading/error
// fallbacks of the first and third. It owns what all six owe a keyboard operator, written
// once so a seventh cannot ship without it. The last two are `PanelFallback` at the foot of
// this file, which takes three strings and renders one shared loading/error shape.
//
// It is a dialog wherever it covers the list and a side panel where it does not, and that
// is not a detail: styles/10-layout.css makes `main.split .why` a right-hand sheet floated
// over the cards at 1100px and `inset: 0; z-index: 50` at 900px, so from 1100px down this
// covers cards the operator can still reach, and under 900px it covers the entire
// application. Claiming `role="dialog"` at every width would be false above 1100px, where
// the panel really does sit beside the list in its own grid column and both are usable.
// Not claiming it while it overlays leaves a screen reader browsing the covered page
// underneath. So the dialog contract is conditional on PANEL_OVERLAY_QUERY, the 1100px
// boundary the CSS overlay block uses, read from the one declaration both sides share.
//
// The boundary is 1100, not `NARROW_SCREEN_QUERY`'s 900. Keying this on 900 would leave a
// 200px band of viewport width where the panel floats over the right of the cards with no
// focus move and no Tab trap, so a keyboard operator could tab into cards hidden underneath
// and press Spare or Reap on an item they cannot see. The two numbers must stay apart: 900
// is the stored meaning of the operator's Mobile/Desktop choice and must never follow this
// one.
//
// Escape lives here too, in exactly one place, so every panel using this shell gets it and
// a future one cannot ship without it.

import { useEffect, useId, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useModalOpen } from "../backnav";
import { useSlowWait } from "../announce";
import { useDialogFocus } from "../focus";
import { PANEL_OVERLAY_QUERY, useMediaQuery } from "../useMediaQuery";
import { trapTab } from "./ModalShell";
import { Notice } from "./Notice";

/** Every `.why` panel's close: a media-sheet disc floated over the hero art (see .why-close),
 *  never a boxed glyph in the title row, where it read as a stray form control. A stroked X
 *  to match the panel's round-capped glyphs, and not the scythe: closing is not reaping.
 *  Over a title with no art it rests on the surface top-right the same way. Rendered first
 *  by the shell so it is the panel's first tab stop. Its z-index clears the hero art and
 *  fade. */
function WhyClose({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <button type="button" className="why-close" onClick={onClose} aria-label={t("common.close")}>
      <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
        <path
          d="M4 4l8 8M12 4l-8 8"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    </button>
  );
}

export function WhyShell({
  headingId,
  onClose,
  children,
}: {
  /** The id the panel's own `<h2>` carries. That heading is the panel's accessible name, so
   *  it is pointed at rather than copied into an `aria-label`: a duplicate string is one
   *  more place for the spoken name and the visible one to drift apart. Each of the six is
   *  pinned to its name by a test, six hand-written tests, one per panel, which is a record
   *  of the six and not a gate on a seventh: nothing fails if a new call site passes an id
   *  it never renders, and the panel is then unnamed on a phone. Turning this into a gate
   *  means enumerating the `<WhyShell` call sites and asserting each has such a test, the
   *  shape `test_repo_hygiene.py` uses on the other side of the tree. */
  headingId: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const modal = useMediaQuery(PANEL_OVERLAY_QUERY);

  useDialogFocus(panelRef, modal);

  // Escape closes. A modal, if one is up, owns the key first.
  //
  // The listener is on `window`, so it hears every press on the page, and Escape already
  // means something inside a text box: it clears a `type="search"` field natively. The
  // queue's search box sits beside this panel in split view, so an unscoped handler would
  // close the reasoning the operator was reading when they only meant to clear their
  // search. This bails on INPUT/TEXTAREA/SELECT for that reason, scoped so a field the
  // panel itself owns still closes it. (None of the six render one today. The scope is
  // what lets a seventh, and what keeps this honest about which presses are ours.)
  const modalOpen = useModalOpen();
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (modalOpen) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      const inAField =
        tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable;
      if (inAField && !panelRef.current?.contains(target)) return;
      closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [modalOpen]);

  return (
    <div
      ref={panelRef}
      className="why"
      // One element in both bands, naming its own role in each: a side region beside the
      // list above the boundary, a modal dialog over it below. This cannot be an <aside>
      // taking `role="dialog"` on the lower band: browsers and screen readers honor that,
      // but ARIA in HTML does not permit it, since a sectioning element cannot be a dialog.
      //
      // A <div> is required for a second reason too: an <aside> scopes the
      // `<header class="why-head">` each panel renders inside it. A <header> is the page's
      // banner unless a sectioning element encloses it, and `role` does not scope it, only
      // the element does. An <aside> here would make `.why-head` a banner on all six
      // panels, nested inside a landmark. The panels answer that by not being headers: that
      // row is a plain div now, which is all it ever was in the accessibility tree, since a
      // scoped <header> maps to `generic`. A seventh panel bringing its own <header> back
      // is caught by the axe audit each panel already carries, not by this comment.
      role={modal ? "dialog" : "complementary"}
      aria-modal={modal || undefined}
      aria-labelledby={headingId}
      // 0, not -1, and the difference is the whole desktop band. The reasoning body
      // scrolls, and -1 is programmatic focus only: below the boundary `useDialogFocus`
      // moves focus here, so it is scrollable by keyboard there. Above it, in split view
      // with `role="complementary"` and nobody moving focus, Tab could not reach the
      // container at all otherwise (WCAG 2.1.1).
      //
      // This is not cleared just because the panel "holds focusable children". It does not
      // always: `WhyPanelFallback` and `ScalesPanelFallback` render a Notice or a spinner
      // with no control in them, and where the rich panels do have buttons those cluster at
      // the top rather than running down the body, so tabbing to one scrolls away from the
      // text being read. `trapTab` reads descendants only and already handles
      // `active === panel`, so the dialog band is unaffected.
      tabIndex={0}
      onKeyDown={modal ? (e) => trapTab(e, panelRef.current) : undefined}
    >
      <WhyClose onClose={onClose} />
      {children}
    </div>
  );
}

/** What a panel's column shows while its read is in flight, or once it has failed. The column is
 *  reserved the moment the operator picks something, so leaving it blank reads as a hang. It
 *  keeps its own close, or a failed read strands the reader in split view.
 *
 *  `waiting` goes to `useSlowWait`. `loading` is the lead in the loading branch, which has no
 *  heading of its own, so it carries the panel's name. `failure` is the error notice. */
export function PanelFallback({
  error,
  onClose,
  waiting,
  loading,
  failure,
}: {
  error: boolean;
  onClose: () => void;
  waiting: string;
  loading: string;
  failure: string;
}) {
  const { t } = useTranslation();
  const headingId = useId();
  // Above the branch, and null on the failure arm: that arm reaches `Notice`'s
  // `role="alert"`, which speaks on its own, so a wait sentence arriving beside it would
  // say two things about one state.
  useSlowWait(error ? null : waiting);
  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      {error ? (
        <>
          <div className="why-head">
            <h2 id={headingId}>{t("why.panel.shell.errorHeading")}</h2>
          </div>
          <Notice tone="error">{failure}</Notice>
        </>
      ) : (
        // No live region here: text mounted in the same commit as its own DOM node is a
        // shape several screen readers never announce. The sentence goes through the
        // shared region in `announce.tsx` instead, once the wait has run long.
        <div className="why-loading">
          <span className="spinner spinner-lg" aria-hidden="true" />
          <p className="why-loading-lead" id={headingId}>
            {loading}
          </p>
        </div>
      )}
    </WhyShell>
  );
}
