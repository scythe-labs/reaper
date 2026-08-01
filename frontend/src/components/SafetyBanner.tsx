// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The safety state, stated permanently and without euphemism.
//
// Its own module so the app shell and the first-run wizard state the regime from one
// declaration, the way `ScanLine` already does for scan progress (rule 18, rule 144). The
// wizard used to hand-write its own sentence instead, in this component's green tone and
// reading no query at all, so a host armed by `REAPER_DESTRUCTIVE_ACTIONS_ENABLED` was told
// deletion was off on the one screen that was saying anything about it.

import { useSafety } from "../useSafety";

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
          <strong>Safety state unknown.</strong> Reaper couldn't reach the server to confirm whether
          deletion is on. Until it can, treat this as armed and{" "}
          {onGoToDeletion ? (
            <>
              <button className="link" onClick={onGoToDeletion}>
                check Policy → Deletion
              </button>
              .
            </>
          ) : (
            "check Policy → Deletion once you're in the app."
          )}
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
          {onGoToDeletion ? (
            <>
              <button className="link" onClick={onGoToDeletion}>
                Turn deletion on in Policy → Deletion
              </button>{" "}
              when you're ready.
            </>
          ) : (
            "You turn deletion on later, in Policy → Deletion."
          )}
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
