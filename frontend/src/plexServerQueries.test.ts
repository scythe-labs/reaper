// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// One declaration of "of the linked server", and nothing open-coding a subset of it.
//
// The keys that mean "of the linked server" are declared once, in `OF_THE_LINKED_SERVER`, and
// no component may invalidate two or more of them by hand. A component that wrote its own
// partial copy of that list could look complete while actually being out of date.

import { readFileSync } from "node:fs";

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { OF_THE_LINKED_SERVER, invalidateAllPlex } from "./plexServerQueries";
import { shippedSource, sourceText, srcRelative } from "./test/sources";

describe("invalidateAllPlex", () => {
  it("drops every key that means 'of the linked server', and only those", () => {
    const client = new QueryClient();
    const dropped: unknown[] = [];
    vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
      dropped.push(filters?.queryKey);
      return Promise.resolve();
    });

    invalidateAllPlex(client);

    expect(dropped).toEqual(OF_THE_LINKED_SERVER.map((key) => [...key]));
    // `["setup"]` is the caller's, never the helper's: only some of the five paths change
    // whether the install is configured, and folding it in would make the count above wrong.
    expect(dropped).not.toContainEqual(["setup"]);
  });

  it("names six keys, reconciled by hand against the SPA's readers", () => {
    // Pinned rather than derived, because every assertion here is a comparison against this
    // same list, so a key silently dropped from the module would move both sides at once and
    // prove nothing. Six: plex, plex-resources, plex-libraries, leaving-soon-settings,
    // plexTrash, watch-evidence.
    expect(OF_THE_LINKED_SERVER).toHaveLength(6);
    expect(new Set(OF_THE_LINKED_SERVER.map((k) => k.join("/"))).size).toBe(6);
  });
});

describe("no component open-codes a subset of it", () => {
  /** The key of every hand-written `invalidateQueries({ queryKey: ["thing"] })`, with its line.
   *
   *  Anchored on `invalidateQueries` rather than on `queryKey:`, which also spells every
   *  `useQuery` READ in the file. Anchoring on `queryKey:` instead would falsely report
   *  `PlexPanel` as open-coding eight keys when it invalidates only one; a read of `["plex"]`
   *  is what the panel is for. */
  function handWrittenKeys(source: string): { key: string; line: number }[] {
    const call = /invalidateQueries\(\{\s*queryKey:\s*\[\s*"([a-zA-Z-]+)"\s*\]/g;
    return [...source.matchAll(call)].flatMap((m) =>
      m[1] === undefined ? [] : [{ key: m[1], line: source.slice(0, m.index).split("\n").length }],
    );
  }

  /** Owned keys dropped together by one handler, as runs of adjacent invalidation calls.
   *
   *  **A run, not a file.** Per-file would be wrong: `PlexPanel` holds four unrelated one-key
   *  invalidations (saving the web address, saving the certificate check, saving a connection,
   *  forgetting watch evidence) in four different mutations, and counting a whole file together
   *  would flag the panel for doing nothing wrong. What "open-codes a subset" means is ONE
   *  handler naming several of the set in consecutive statements. Three lines of tolerance,
   *  since an `await` line or a comment can sit between calls that belong to the same
   *  handler. */
  function subsetRuns(source: string, owned: Set<string>): string[][] {
    const runs: string[][] = [];
    let current: string[] = [];
    let previousLine = -99;
    for (const { key, line } of handWrittenKeys(source)) {
      if (line - previousLine > 3) {
        if (current.length) runs.push(current);
        current = [];
      }
      if (owned.has(key)) current.push(key);
      previousLine = line;
    }
    if (current.length) runs.push(current);
    return runs.filter((run) => new Set(run).size >= 2);
  }

  it("no handler invalidates two or more of the keys by hand", () => {
    const owned = new Set(OF_THE_LINKED_SERVER.map((key) => key[0]));
    const offenders: string[] = [];

    for (const path of shippedSource()) {
      if (path.endsWith("plexServerQueries.ts")) continue; // the declaration itself
      for (const run of subsetRuns(readFileSync(path, "utf8"), owned)) {
        offenders.push(`${srcRelative(path)}: ${run.join(", ")}`);
      }
    }

    // Two is the threshold, not one. Invalidating `["plex"]` alone is a legitimate thing to do
    // -- both `setConnection` mutations save a new ADDRESS for the same server, which leaves
    // every other row about that server still true. Reaching for a SECOND key in the same
    // breath is the moment a caller has started restating the set, which belongs in the helper.
    expect(
      offenders,
      `open-coded subsets of OF_THE_LINKED_SERVER: ${offenders.join("; ")}`,
    ).toEqual([]);
  });

  it("catches a subset when one is written, so the check above is not vacuous", () => {
    const owned = new Set(OF_THE_LINKED_SERVER.map((key) => key[0]));

    // A shape that really open-codes a subset, proving the matcher reads the actual spelling
    // components use, rather than just the absence of a match.
    expect(
      subsetRuns(
        `const refreshPlex = async () => {
           await queryClient.invalidateQueries({ queryKey: ["setup"] });
           await queryClient.invalidateQueries({ queryKey: ["plex"] });
           await queryClient.invalidateQueries({ queryKey: ["plex-resources"] });
           await queryClient.invalidateQueries({ queryKey: ["plex-libraries"] });
         };`,
        owned,
      ),
    ).toEqual([["plex", "plex-resources", "plex-libraries"]]);

    // The three spellings that must stay legal, each for its own reason.
    const legal = [
      // One key: a new address for the same server.
      `onSuccess: () => queryClient.invalidateQueries({ queryKey: ["plex"] })`,
      // Keys that are not ours, however many.
      `await queryClient.invalidateQueries({ queryKey: ["setup"] });
       await queryClient.invalidateQueries({ queryKey: ["me"] });`,
      // Two of ours in one FILE but different handlers, which is `PlexPanel` today. Four lines
      // apart, so the run breaks -- the case that made the per-file version wrong.
      `const a = useMutation({
         onSuccess: () => queryClient.invalidateQueries({ queryKey: ["plex"] }),
       });

       const b = useMutation({
         onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watch-evidence"] }),
       });`,
    ];
    for (const source of legal) expect(subsetRuns(source, owned)).toEqual([]);
  });

  it("both components that change which server is linked call the helper", () => {
    for (const file of ["components/PlexPanel.tsx", "components/SetupPlexStep.tsx"]) {
      const source = sourceText(file);
      expect(source, `${file} should import the shared helper`).toContain(
        'from "../plexServerQueries"',
      );
    }
  });
});
