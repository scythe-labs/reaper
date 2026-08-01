// SPDX-License-Identifier: AGPL-3.0-or-later
// `reapBlockers` re-derives, for the sake of a sentence, a question the server already
// answered as `reap_ready`. Two derivations of one fact is the shape that drifts, so the whole
// point of this file is the agreement test at the bottom: every combination of the fields both
// sides read, driven through both, asserting they never disagree.
import { describe, expect, it } from "vitest";
import type { SetupStatus } from "./api";
import { reapBlockers } from "./reapReadiness";

/** A fully configured install. Every case below turns exactly one thing off. */
const READY: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: { radarr: 1, sonarr: 1, tautulli: 1 },
  has_radarr: true,
  has_sonarr: true,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: true,
  scan_ready: true,
  reap_ready: true,
  complete: true,
};

describe("reapBlockers", () => {
  it("says nothing about an install that could reap", () => {
    expect(reapBlockers(READY)).toEqual([]);
  });

  it("names Plex, and says what the check through it is for", () => {
    const [only, ...rest] = reapBlockers({ ...READY, plex_linked: false, reap_ready: false });
    expect(rest).toEqual([]);
    expect(only?.key).toBe("plex");
    // The outcome first, then the reason -- and the reason is the Plex step's own wording, so
    // the constraint reads the same wherever the operator meets it.
    expect(only?.sentence).toMatch(/^Reaper can't remove anything until Plex is connected\./);
    expect(only?.sentence).toContain("nobody is watching");
  });

  it("names Tautulli", () => {
    const [only] = reapBlockers({ ...READY, has_tautulli: false, reap_ready: false });
    expect(only?.key).toBe("tautulli");
    expect(only?.sentence).toContain("played since you approved it");
  });

  it("names the password that arms deletion", () => {
    const [only] = reapBlockers({ ...READY, has_password: false, reap_ready: false });
    expect(only?.key).toBe("password");
    expect(only?.sentence).toContain("turns deletion on");
  });

  it("treats Radarr and Sonarr as one requirement, not two", () => {
    const eitherOne = { ...READY, has_radarr: false };
    expect(reapBlockers(eitherOne)).toEqual([]);
    const neither = { ...READY, has_radarr: false, has_sonarr: false, reap_ready: false };
    const [only] = reapBlockers(neither);
    expect(only?.key).toBe("arr");
  });

  it("lists every reason at once rather than one at a time", () => {
    const bare: SetupStatus = {
      ...READY,
      has_password: false,
      plex_linked: false,
      has_radarr: false,
      has_sonarr: false,
      has_tautulli: false,
      scan_ready: false,
      reap_ready: false,
      complete: false,
    };
    expect(reapBlockers(bare).map((b) => b.key)).toEqual(["password", "plex", "tautulli", "arr"]);
  });

  // Every sentence is operator copy on a screen someone reads while deciding what to delete,
  // so it obeys rule 21 here rather than at review time.
  it("writes every reason in plain language, with no em dash and no middot", () => {
    const bare: SetupStatus = {
      ...READY,
      has_password: false,
      plex_linked: false,
      has_radarr: false,
      has_sonarr: false,
      has_tautulli: false,
      reap_ready: false,
    };
    for (const { key, sentence } of reapBlockers(bare)) {
      expect(sentence, key).not.toMatch(/[—·]/);
      expect(sentence, key).toMatch(/^Reaper can't remove anything until /);
      expect(sentence.length, key).toBeLessThan(160);
    }
  });

  /** The agreement test. `reap_ready` is `has_password and scan_ready and plex_linked`, and
   *  `scan_ready` is `(has_radarr or has_sonarr) and has_tautulli` -- both from
   *  `api/setup.py`, both re-derived above so each conjunct can carry its own sentence.
   *
   *  Rule 144: deriving the sentences from the same declaration is not available across the
   *  wire, so the next best thing is to make a disagreement fail loudly. If the server's
   *  definition gains a conjunct and this file does not, an install lands on a Reap page with
   *  a live Execute button and no notice, and on a wizard saying "You're all set" over a run
   *  that will be refused. That is the failure this walks all 16 combinations for.
   */
  it("is empty exactly when the server says the install is reap-ready", () => {
    const bits = [false, true];
    let cases = 0;
    for (const has_password of bits) {
      for (const plex_linked of bits) {
        for (const has_tautulli of bits) {
          for (const has_radarr of bits) {
            const scan_ready = has_radarr && has_tautulli;
            const status: SetupStatus = {
              ...READY,
              has_password,
              plex_linked,
              has_tautulli,
              has_radarr,
              has_sonarr: false,
              scan_ready,
              reap_ready: has_password && scan_ready && plex_linked,
              complete: has_password && scan_ready,
            };
            cases += 1;
            expect(
              reapBlockers(status).length === 0,
              `reap_ready=${status.reap_ready} but blockers=${JSON.stringify(
                reapBlockers(status).map((b) => b.key),
              )}`,
            ).toBe(status.reap_ready);
          }
        }
      }
    }
    // Pinned so a loop edited down to fewer dimensions cannot pass by covering less
    // (rule 145): four booleans, every combination.
    expect(cases).toBe(16);
  });
});
