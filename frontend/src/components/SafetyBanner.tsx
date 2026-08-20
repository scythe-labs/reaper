// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The safety state, stated permanently and without euphemism.
//
// Its own module so the app shell and the first-run wizard state the regime from one
// declaration, the way `ScanLine` already does for scan progress (rule 18, rule 144). The
// wizard used to hand-write its own sentence instead, in this component's green tone and
// reading no query at all, so a host armed by `REAPER_DESTRUCTIVE_ACTIONS_ENABLED` was told
// deletion was off on the one screen that was saying anything about it.

import { Trans } from "react-i18next";
import { useSafety } from "../useSafety";

// The copy lives in `locales/en/ui.json` under `safetyBanner.*`, one message per rendered
// sentence: the linked and link-less variants are separate whole messages, never a shared
// stem with a bolted-on tail, because word order is the translator's to choose.

/** The safety state, stated permanently and without euphemism.
 *
 *  While destructive actions are disabled, Reaper *structurally cannot* delete: the
 *  GuardedTransport refuses every mutating HTTP request, so this is a fact about the
 *  process rather than a promise about the UI. The banner says which regime you are in,
 *  always, because "can this thing delete my library right now?" should never require
 *  reading a settings page to answer.
 *
 *  `onGoToDeletion` is optional because the wizard renders above the app's navigation: there
 *  is no Policy page to send anyone to until they leave setup. Where it is absent the state
 *  is still stated in full and only the link is dropped, so no branch gets quieter (rule 25). */
export function SafetyBanner({ onGoToDeletion }: { onGoToDeletion?: () => void }) {
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
          {onGoToDeletion ? (
            <Trans
              i18nKey="safetyBanner.unknown"
              components={{ btn: <button className="link" onClick={onGoToDeletion} /> }}
            />
          ) : (
            <Trans i18nKey="safetyBanner.unknownNoLink" />
          )}
        </span>
      </div>
    );
  }

  // Before the read-only branch, because recovery mode reports `destructive_enabled: false`
  // too and would otherwise be told as plain read-only -- which sends the operator to a
  // switch that refuses them (the arm route answers 409 while recovery is on). It is also
  // the only one of the three states with something left to DO, so it says what.
  //
  // No link to Policy, Deletion here, deliberately: rule 25 the other way round. Offering the
  // control that cannot help is worse than offering none, and the thing that does help is a
  // file on the host plus a restart, which no button in this app can reach.
  if (data.recovery_mode) {
    return (
      <div className="banner banner-recovery">
        <span className="banner-dot" aria-hidden="true" />
        <span>
          <Trans i18nKey="safetyBanner.recovery" />
        </span>
      </div>
    );
  }

  if (!data.destructive_enabled) {
    return (
      <div className="banner banner-safe">
        <span className="banner-dot" aria-hidden="true" />
        <span>
          {onGoToDeletion ? (
            <Trans
              i18nKey="safetyBanner.readOnly"
              components={{ btn: <button className="link" onClick={onGoToDeletion} /> }}
            />
          ) : (
            <Trans i18nKey="safetyBanner.readOnlyNoLink" />
          )}
        </span>
      </div>
    );
  }

  return (
    <div className="banner banner-armed">
      <span className="banner-dot" aria-hidden="true" />
      <span>
        <Trans i18nKey="safetyBanner.armed" />
      </span>
    </div>
  );
}
