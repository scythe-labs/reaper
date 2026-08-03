// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What still stops a real run, in the operator's words.
//
// The server answers the question as one boolean (`SetupStatus.reap_ready`), which is the
// right shape for a gate and the wrong shape for a sentence: "not ready" tells nobody what to
// go and do. So the boolean decides *whether* to speak and this decides *what to say*, from
// the same fields the server used.
//
// It lives here, above `components/`, because it is read from two screens that are otherwise
// unrelated: the wizard's last step, where the operator is being told they are finished, and
// the Reap page, where they are standing in front of the button. Both used to say nothing at
// all -- the only place the requirement appeared was the 409 that fires after the whole
// confirmation phrase has been typed (#383) -- and two hand-written copies of a refusal are
// exactly the pair rule 144 says will drift, because each is written by someone reading the
// other screen.
//
// The sentences are deliberately the wording the Plex step already uses ("checks nobody is
// watching ... runs through Plex"), so the constraint reads the same wherever it is met.

import type { SetupStatus } from "./api";

/** One reason a real run cannot go ahead. */
export interface ReapBlocker {
  /** Stable id: the list key, and what a consumer branches on to offer a way to fix it. */
  key: "password" | "plex" | "tautulli" | "arr";
  /** The whole message, outcome first. Rendered as-is in a warn notice. */
  sentence: string;
}

/** Everything standing between this install and a real run, in the order it is worth fixing.
 *
 *  Empty exactly when `setup.reap_ready` is true: these four conditions are that boolean,
 *  re-derived here only so each one can carry a sentence.
 *
 *  Two tests hold that, and it takes both. `reapReadiness.test.ts` walks all 16 combinations to
 *  pin the sentences against the payload's own `reap_ready`, which proves this module is
 *  self-consistent and nothing more: the expected side is a hand-written copy of the Python, in
 *  the same file as the assertion. The tie to the server is
 *  `tests/test_repo_hygiene.py::test_the_frontend_reap_blockers_read_the_fields_the_server_builds_reap_ready_from`,
 *  which parses `reap_ready` out of `src/reaper/api/setup.py` and fails if the fields read below
 *  are not exactly its conjuncts. Adding one there and not here is what that catches, and it
 *  went uncaught until it was tried: the claim that it could not was written here first
 *  (rule 144).
 */
export function reapBlockers(setup: SetupStatus): ReapBlocker[] {
  const blockers: ReapBlocker[] = [];
  if (!setup.has_password) {
    blockers.push({
      key: "password",
      sentence:
        "Reaper can't remove anything until you set a password. It's what turns deletion on.",
    });
  }
  if (!setup.plex_linked) {
    blockers.push({
      key: "plex",
      sentence:
        "Reaper can't remove anything until Plex is connected. It checks nobody is watching " +
        "first, and that check runs through Plex.",
    });
  }
  if (!setup.has_tautulli) {
    blockers.push({
      key: "tautulli",
      sentence:
        "Reaper can't remove anything until Tautulli is connected. It checks nothing was " +
        "played since you approved it, and that check runs through Tautulli.",
    });
  }
  if (!setup.has_radarr && !setup.has_sonarr) {
    blockers.push({
      key: "arr",
      sentence:
        "Reaper can't remove anything until Radarr or Sonarr is connected. It removes files " +
        "through them, never on its own.",
    });
  }
  return blockers;
}
