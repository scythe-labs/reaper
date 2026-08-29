// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The safety state, stated permanently and without euphemism.
//
// Its own module so the app shell and the first-run wizard both read the regime from one
// declaration, the way `ScanLine` does for scan progress. Keeping it in one place stops the
// two surfaces from ever disagreeing about whether deletion is armed.

import { useState, type MouseEvent, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { useSafety } from "../useSafety";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "../useMediaQuery";

// The copy lives in `locales/en/ui.json` under `safetyBanner.*`, one message per rendered
// sentence. The linked and link-less variants are separate whole messages, not a shared stem
// with a bolted-on tail, because word order is the translator's to choose.

/** The banner shell every state renders through, so the tone class and the mobile behavior are
 *  written once. On a phone the notice collapses to one line and expands on tap, so the safety
 *  state stops eating three lines at the top of every screen. The state lead comes first in
 *  every message, so the regime still reads while collapsed. The whole line is a convenience tap
 *  target, and the chevron is the real, keyboard-reachable toggle, so a link inside the sentence
 *  (Policy → Deletion) stays reachable rather than pruned as a button's presentational child.
 *  Desktop renders the notice in full, as it always did. */
function Banner({ tone, children }: { tone: string; children: ReactNode }) {
  const { t } = useTranslation();
  const narrow = useMediaQuery(NARROW_SCREEN_QUERY);
  const [open, setOpen] = useState(false);

  if (!narrow) {
    return (
      <div className={`banner ${tone}`}>
        <span className="banner-dot" aria-hidden="true" />
        <span>{children}</span>
      </div>
    );
  }

  const toggle = () => setOpen((o) => !o);
  return (
    <div className={`banner ${tone} collapsible${open ? " open" : ""}`}>
      <span className="banner-dot" aria-hidden="true" />
      {/* The chevron below is the real toggle; this makes the whole line a tap target too. A
          link inside the sentence stops its own click here, so tapping it navigates rather than
          toggling. */}
      <span className="banner-body" onClick={toggle}>
        <span className="banner-text">{children}</span>
      </span>
      <button
        type="button"
        className="banner-chev"
        aria-expanded={open}
        aria-label={open ? t("safetyBanner.collapse") : t("safetyBanner.expand")}
        onClick={toggle}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path
            d="M6 4l4 4-4 4"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </div>
  );
}

/** The safety state, stated permanently and without euphemism.
 *
 *  While destructive actions are disabled, Reaper cannot delete anything. The
 *  GuardedTransport refuses every mutating HTTP request, so this is a fact about the
 *  process, not a promise about the UI. The banner always says which regime you are in,
 *  because "can this thing delete my library right now?" should never need a trip to the
 *  settings page to answer.
 *
 *  `onGoToDeletion` is optional because the wizard renders above the app's navigation. There
 *  is no Policy page to send anyone to until setup finishes. Where the link is absent, the
 *  state still renders in full and only the link is dropped, so no branch says less. */
export function SafetyBanner({ onGoToDeletion }: { onGoToDeletion?: () => void }) {
  // The same authenticated query the deletion toggle invalidates, so arming or disarming
  // updates this banner in the same render pass. It also polls, so arming from another tab
  // reaches this one too (see useSafety for why). /api/health is only a liveness probe now
  // and carries no armed state.
  const { data, isLoading, isError } = useSafety();

  // On the very first fetch, nothing is known yet. Stay quiet rather than flash a state
  // that might be wrong a moment later.
  if (isLoading) return null;

  // The banner's whole point is that it states the regime every time it renders. When the
  // safety state can't be read and React Query has no last-known value, show an unknown
  // state instead of nothing: an absent banner reads as "nothing to worry about". Use the
  // amber "could not check" tone so it never reads as safe.
  if (isError || !data) {
    return (
      <Banner tone="banner-unknown">
        {onGoToDeletion ? (
          <Trans
            i18nKey="safetyBanner.unknown"
            components={{ btn: <button className="link" onClick={linkClick(onGoToDeletion)} /> }}
          />
        ) : (
          <Trans i18nKey="safetyBanner.unknownNoLink" />
        )}
      </Banner>
    );
  }

  // This check runs before the read-only branch below. Recovery mode also reports
  // `destructive_enabled: false`, so checking read-only first would call it plain read-only
  // and send the operator to a switch that refuses them: the arm route answers 409 while
  // recovery is on. Recovery is also the only one of the three states with something the
  // operator can still do, and this is the branch that says what.
  //
  // Never link to Policy, Deletion from this branch. The control there would refuse the
  // operator, and offering a dead end is worse than offering nothing. What actually fixes
  // recovery mode is a file on the host and a restart, which no button here can reach.
  if (data.recovery_mode) {
    return (
      <Banner tone="banner-recovery">
        <Trans i18nKey="safetyBanner.recovery" />
      </Banner>
    );
  }

  if (!data.destructive_enabled) {
    return (
      <Banner tone="banner-safe">
        {onGoToDeletion ? (
          <Trans
            i18nKey="safetyBanner.readOnly"
            components={{ btn: <button className="link" onClick={linkClick(onGoToDeletion)} /> }}
          />
        ) : (
          <Trans i18nKey="safetyBanner.readOnlyNoLink" />
        )}
      </Banner>
    );
  }

  return (
    <Banner tone="banner-armed">
      <Trans i18nKey="safetyBanner.armed" />
    </Banner>
  );
}

/** A banner link's click, which must not also toggle the collapsed banner on a phone: it stops
 *  the click from reaching the line's tap handler, then navigates. */
function linkClick(go: () => void) {
  return (e: MouseEvent) => {
    e.stopPropagation();
    go();
  };
}
