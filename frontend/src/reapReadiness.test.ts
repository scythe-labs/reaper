// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
// `reapBlockers` recomputes, in order to write a sentence, the same question the server already
// answers as `reap_ready`. Computing one fact two ways is exactly how two sides drift apart, so
// the test at the bottom drives every combination of the fields both sides read and checks they
// never disagree.
//
// That test only proves the two sides agree with each other: `reap_ready` here is built by this
// same file, not read from the server. The separate check in `tests/test_repo_hygiene.py`,
// named at the bottom, is what confirms this file's fields still match the server's real
// definition.
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
    // States the outcome first, then the reason. The reason uses the Plex step's own wording,
    // so the same explanation reads the same wherever the operator sees it.
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

  // Every sentence here is operator copy shown on a screen while someone decides what to
  // delete, so this test checks its plain-language rules directly rather than at review time.
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
   *  `scan_ready` is `(has_radarr or has_sonarr) and has_tautulli`, both from `api/setup.py`
   *  and both re-derived above so each condition can carry its own sentence.
   *
   *  The frontend and the server cannot share one declaration across the network boundary. If
   *  the server's definition gains a condition and `reapReadiness.ts` does not, an install
   *  reaches the Reap page with a live Execute button and no warning, and the wizard says
   *  "You're all set" about a run the server will actually refuse.
   *
   *  This test alone cannot catch that: `reap_ready` below is a hand transcription of the
   *  Python, in the same file as the assertions, so all 16 combinations only prove the
   *  sentences agree with that transcription. Whether the transcription itself still matches
   *  the server is checked separately by
   *  `tests/test_repo_hygiene.py::test_the_frontend_reap_blockers_read_the_fields_the_server_builds_reap_ready_from`,
   *  which parses `src/reaper/api/setup.py`. Both checks are needed: that one confirms the set
   *  of fields matches, this one confirms each field turns into the right sentence.
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
    // Pinned at 16 so a loop edited down to fewer dimensions cannot pass while covering less:
    // four booleans, every combination.
    expect(cases).toBe(16);
  });
});
