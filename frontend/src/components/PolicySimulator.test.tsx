// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the simulator's wait says, and to whom.
//
// A rescan is driven by a one-second poll rather than by the operator, so the panel changes
// under them: the heading and the button give way to a paragraph and a progress bar. The bar
// announces nothing on its own, so the sentence said when the scan starts is the whole signal
// -- and it was one string for two different pieces of news (#177).
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { Simulation } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import {
  APPLIES_ON_NEXT_SCAN,
  Outcome,
  RESCAN_HEADING,
  RESCAN_QUEUED_LEAD,
  StaleNotice,
} from "./PolicySimulator";

function renderNotice(props: Partial<Parameters<typeof StaleNotice>[0]> = {}) {
  return render(
    <StaleNotice
      scanning
      followupQueued={false}
      starting={false}
      startError={null}
      onScan={() => {}}
      percent={40}
      detail="Scoring"
      staleKind="gathers_differently"
      staleReason={{ k: "gathers_differently", p: { media_type: "movie" } }}
      {...props}
    />,
  );
}

describe("the wait the simulator shows while a rescan runs", () => {
  it("names itself the same way on the heading and on the progress bar", () => {
    // Both read from one declaration, which is also what the announcement uses. Three copies of
    // one sentence is what this replaced, and the third was the one nobody had noticed (rule
    // 144).
    renderNotice();

    expect(screen.getByRole("heading", { name: RESCAN_HEADING })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: RESCAN_HEADING })).toBeInTheDocument();
  });

  it("has no accessibility violations in either of its two states", async () => {
    // Both, because this notice is really two screens sharing a container: a waiting state with
    // a progress bar and no control, and a resting one whose only content is the button that
    // starts the scan.
    const waiting = renderNotice();
    await expectNoA11yViolations(waiting.container);
    waiting.unmount();

    const resting = renderNotice({ scanning: false });
    await expectNoA11yViolations(resting.container);
  });

  it("says the changes are in a SECOND scan when one was already running", () => {
    // The bar in front of the operator then belongs to a scan that started before they saved,
    // so it is scoring the old policy. This is the one fact about the wait that is not visible
    // from the bar itself.
    renderNotice({ followupQueued: true });

    expect(screen.getByText(new RegExp(RESCAN_QUEUED_LEAD.slice(0, 40)))).toBeInTheDocument();
  });

  it("does not claim a second scan when this one carries the changes", () => {
    // The other direction, so the branch cannot collapse into always warning about a follow-up
    // (rule 118): a test that passes for both states pins neither.
    renderNotice({ followupQueued: false });

    expect(screen.queryByText(new RegExp(RESCAN_QUEUED_LEAD.slice(0, 40)))).toBeNull();
    expect(screen.getByText(/scoring your library under the new policy/i)).toBeInTheDocument();
  });
});

describe("what the panel says when it will not answer", () => {
  // Nine season controls, the keep tags and the popularity window used to share one
  // paragraph naming every cause at once, so the sentence could only be right about one of
  // them (#495). Each refusal now carries its own remedy, and the operator has to be able to
  // tell them apart at a glance -- which is the whole reason the server sends a typed kind
  // rather than only a sentence.
  const cases = [
    ["gathers_differently", "Needs a fresh scan"],
    ["seasons_not_recorded", "Your season rules need a fresh scan"],
    // Names the control, not a cause: the episode map is also unread after a scan that ran
    // WITH the hold on and got no answer from Sonarr, so "turning that on" was false for
    // that operator (`tests/test_scan_pipeline.py` drives both producers).
    ["in_progress_not_read", "Your partway-through rule needs a fresh scan"],
  ] as const;

  it("gives each refusal its own heading", () => {
    const seen = new Set<string>();
    for (const [kind, heading] of cases) {
      const view = renderNotice({ scanning: false, staleKind: kind });
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
      seen.add(heading);
      view.unmount();
    }
    // Three distinct headings, not one string reached three ways: a lookup that fell back
    // to the general heading for every kind would satisfy the assertions above one at a
    // time and tell the operator to fix the wrong thing.
    expect(seen.size).toBe(cases.length);
  });

  it("renders the composed reason rather than a copy of its own", () => {
    // The reason id lives in api/simulate.py's `_refused` alone, and is composed here from
    // the catalog (`policySim.staleReason.<id>`) rather than restated -- so the sentence a
    // reviewer reads and the sentence that ships are one catalog entry (rule 144).
    renderNotice({
      scanning: false,
      staleKind: "seasons_not_recorded",
      staleReason: { k: "seasons_not_recorded" },
    });

    expect(
      screen.getByText(
        "The last scan didn't record what your season rules need, so there are no numbers " +
          "to show. Run a scan, then this becomes exact again.",
      ),
    ).toBeInTheDocument();
  });

  it("still says something when the server sends a kind this build does not know", () => {
    // Rule 66: an unknown id falls back, it never guesses. composeIn's own fallback
    // (why.test.ts) fires here, the same as any other reason id this build has no catalog
    // entry for yet: the raw id, never a blank paragraph.
    renderNotice({
      scanning: false,
      staleKind: "a_refusal_from_the_future" as never,
      staleReason: { k: "a_refusal_from_the_future" },
    });

    expect(screen.getByRole("heading", { name: "Needs a fresh scan" })).toBeInTheDocument();
    expect(screen.getByText("a_refusal_from_the_future")).toBeInTheDocument();
  });
});

// Rule 144, as a guard rather than a comment asking the next author to remember. The panel shows
// these sentences and `PolicyEditor` says them, from two different files -- which is exactly the
// arrangement where a reworded heading leaves the spoken copy behind, and each file's own tests
// stay green because neither can see the other one's string.
//
// Bounded, per rule 147: this reads the sentence however it is spelled around it, but only while
// it is spelled out at all. An editor composing it from pieces would pass, which is why the
// first assertion anchors the declaration rather than letting the walk quietly empty.
describe("who is allowed to write the rescan sentences", () => {
  const read = (name: string) =>
    readFileSync(join(dirname(fileURLToPath(import.meta.url)), name), "utf8");

  it("is this panel, and not the editor that announces them", () => {
    // The declarations resolve from the catalog since Stage 4, so the anchor pins the
    // key each constant reads: the sentence itself lives in locales/en/ui.json, and the
    // imported constant values checked below are what the catalog served.
    const panel = read("PolicySimulator.tsx");
    expect(panel).toContain(`export const RESCAN_HEADING = i18next.t("policySim.rescanHeading")`);
    expect(panel).toContain(`i18next.t("policySim.rescanQueuedLead")`);
    // The savebar's sentence, now that this panel shows it too. The savebar is at the foot of
    // the left column and this panel is the right one, so a reword reaching only one of them
    // leaves two answers to "when does this take effect" on one screen.
    expect(panel).toContain(
      `export const APPLIES_ON_NEXT_SCAN = i18next.t("policySim.appliesOnNextScan")`,
    );

    const editor = read("PolicyEditor.tsx");
    for (const [name, sentence] of [
      ["RESCAN_HEADING", RESCAN_HEADING],
      ["RESCAN_QUEUED_LEAD", RESCAN_QUEUED_LEAD],
      ["APPLIES_ON_NEXT_SCAN", APPLIES_ON_NEXT_SCAN],
    ] as const) {
      expect(
        editor.includes(sentence),
        `PolicyEditor.tsx writes out the ${name} sentence instead of importing it. The panel ` +
          `shows that text and the editor speaks it, so a reword in one leaves the other saying ` +
          `the old thing (#177, rules 72 and 144).`,
      ).toBe(false);
    }
    expect(editor).toContain("RESCAN_QUEUED_LEAD");
    expect(editor).toContain("APPLIES_ON_NEXT_SCAN");
  });
});

// What the panel says about an edit that legitimately moves nothing (#488).
//
// A keep rule redraws the histogram while the two headline numbers cannot move, and an operator
// read the frozen outcome as a broken preview. The delta rows were correct throughout, which is
// the trap: they count threshold crossings, and the two rows beside them are absolute totals
// with no before to read them against, so a protection edit that moves a title from spared to
// not judged leaves every number on the panel holding still.
/** One simulation payload, shared by the two panels below: what changed, and what spared. */
const BASE: Simulation = {
  exact: true,
  stale_kind: null,
  stale_reason: null,
  condemned: 412,
  protected: 388,
  abstained: 2669,
  reclaimable_bytes: 2_090_000_000_000,
  unknown_size_items: 0,
  newly_condemned: 0,
  no_longer_condemned: 0,
  condemned_before: 412,
  changed_titles: 0,
  histogram: [180, 402, 611, 688, 590, 430, 292, 168, 78, 30],
  examples_newly_condemned: [],
  protected_by: [],
};

describe("the outcome panel on an edit that changes no title", () => {
  const INERT = "Your changes leave every title as it is.";

  function renderOutcome(sim: Partial<Simulation>, edited: boolean) {
    return render(
      <Outcome simulation={{ ...BASE, ...sim }} threshold={62} pace={null} edited={edited} />,
    );
  }

  /** The count beside the summary row, read through the row rather than by position. */
  function changed(): string {
    const row = screen.getByText("Titles that change").parentElement;
    return row?.querySelector("dd")?.textContent ?? "";
  }

  it("says so, in place of a comparison between two identical numbers", () => {
    renderOutcome({ changed_titles: 0 }, true);

    expect(screen.getByText(INERT)).toBeInTheDocument();
    // The line it replaces, which is the one that read as broken: "Your last scan flags
    // 412. This draft flags 412." is a sentence built to contrast two numbers that are equal.
    expect(screen.queryByText(/This draft flags/)).not.toBeInTheDocument();
  });

  it("compares nothing at all until something has been edited", () => {
    // The panel simulates on mount, before anything is touched. The inert sentence keyed on
    // the count alone would open by telling the operator their untouched policy does nothing;
    // the comparison keyed on nothing at all opened by contrasting a number with itself
    // ("Your last scan flags 412. This draft flags 412."). With no draft there is no
    // comparison to draw, and the headline above already says what the policy flags.
    renderOutcome({ changed_titles: 0 }, false);

    expect(screen.queryByText(INERT)).not.toBeInTheDocument();
    expect(screen.queryByText(/This draft flags/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Your last scan flags/)).not.toBeInTheDocument();
    // And says nothing about a scan either, for the same reason: there is no save pending
    // for one to follow, and these numbers already describe the scan that has run.
    expect(screen.queryByText(APPLIES_ON_NEXT_SCAN)).not.toBeInTheDocument();
  });

  it.each([
    ["a draft that moves titles", 65],
    ["a draft that moves none", 0],
  ])("says a scan applies it, on %s", (_case, changed_titles) => {
    // The panel previews a keep rule exactly, off frozen evidence, so it answers instead of
    // asking for a scan -- and an operator who watched these numbers move had nothing here
    // telling them the list they review had not moved with them until a scan re-scored it.
    // Both branches, because an inert edit still saves and still starts a scan (rule 118).
    renderOutcome({ changed_titles }, true);

    expect(screen.getByText(APPLIES_ON_NEXT_SCAN)).toBeInTheDocument();
  });

  it("reads the last scan's count off the server rather than off the two deltas", () => {
    // The fixture is deliberately inconsistent: it is the payload the server sent while
    // `no_longer_condemned` was broken, where the old derivation
    // (condemned - newly + gone) collapsed to the draft's own 14 and printed it on both
    // sides of the sentence. A delta bug must not be able to reach this line again.
    renderOutcome(
      { condemned: 14, condemned_before: 281, newly_condemned: 0, changed_titles: 813 },
      true,
    );

    const line = screen.getByText(/This draft flags/);
    expect(line).toHaveTextContent("Your last scan flags 281. This draft flags 14.");
  });

  it("keeps the comparison when titles do move", () => {
    renderOutcome({ changed_titles: 65 }, true);

    expect(screen.queryByText(INERT)).not.toBeInTheDocument();
    expect(screen.getByText(/This draft flags/)).toBeInTheDocument();
  });

  it("counts the moves both deltas are blind to", () => {
    // The case from #488's thread: a protection edit takes 65 titles from spared to not judged.
    // Nothing crosses the threshold, so both deltas are correctly zero and both headline numbers
    // correctly hold. Without the summary row this screen is identical to the one above where
    // nothing happened at all -- two outcomes, one picture, on the surface whose job is to show
    // blast radius before saving.
    renderOutcome(
      {
        changed_titles: 65,
        newly_condemned: 0,
        no_longer_condemned: 0,
        protected: 323,
        abstained: 2734,
      },
      true,
    );

    expect(changed()).toBe("65");
    expect(screen.getByText("+0")).toBeInTheDocument();
    expect(screen.getByText("−0")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderOutcome({ changed_titles: 0 }, true);
    await expectNoA11yViolations(container);
  });
});

// "Why titles were spared" is a tally of gate ids the server sends, rendered through the
// browser's own copy for each one. Until #551 an id with no copy fell through to a title-cased
// slug, so the two the engine emits on ordinary scans -- the season guard, and every keep rule
// the operator wrote -- read as "Season Progression" and "Custom" beside their counts, in the
// panel someone reads while deciding what to delete (rule 21).
describe("what the spared-by list calls each protection", () => {
  function renderSpared(protected_by: { gate: string; count: number }[]) {
    return render(
      <Outcome simulation={{ ...BASE, protected_by }} threshold={62} pace={null} edited={false} />,
    );
  }

  /** The tally beside one reason, read through its row. Never by searching the page for the
   *  number: the histogram's axis labels are numbers too, so a bare text query answers from
   *  a bar chart that has nothing to do with this list. */
  function tally(label: string): string {
    const row = screen.getByText(label).parentElement;
    return row?.querySelector("dd")?.textContent ?? "";
  }

  // The engine's id, then the words. Written from `GATE_META`'s intent rather than from its
  // source, so a label edited into engine vocabulary fails here (rule 119).
  //
  // `season_progression` is deliberately vague and pinned that way: the same id tallies a
  // season held because the guard could not be ANSWERED (a watch mirror shallower than the
  // partway-through hold, the ordinary state of a new install), so a label naming the
  // operator's season rules would send them to controls that cannot move the number.
  // `api/review.py`'s `_kept_phrase` refuses the same sentence for the same rows.
  it.each([
    ["season_progression", "A season check", "Season Progression"],
    ["custom", "A rule you wrote", "Custom"],
    ["min_dormancy", "Give every title time to be rewatched", "Min Dormancy"],
    ["hand_spare", "Spared by hand", "Hand Spare"],
  ])("names %s in the operator's words", (gate, label, slug) => {
    renderSpared([{ gate, count: 40 }]);

    expect(screen.getByText(label)).toBeInTheDocument();
    // Both spellings of the leak: the raw id, and the title-cased slug the old fallback made
    // of it. Neither is a thing a person would say (rule 21).
    expect(screen.queryByText(slug)).not.toBeInTheDocument();
    expect(screen.queryByText(gate)).not.toBeInTheDocument();
    expect(tally(label)).toBe("40");
  });

  it("still says something true about an id it has no copy for", () => {
    // Rule 66's fallback, for a server newer than the browser. Those titles really were kept
    // by something, so the row stays and says only what it can know -- never a blank, and
    // never the id.
    renderSpared([{ gate: "brand_new_gate", count: 7 }]);

    expect(screen.getByText("Another protection")).toBeInTheDocument();
    expect(tally("Another protection")).toBe("7");
    expect(screen.queryByText("Brand New Gate")).not.toBeInTheDocument();
    expect(screen.queryByText("brand_new_gate")).not.toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderSpared([
      { gate: "min_dormancy", count: 120 },
      { gate: "season_progression", count: 40 },
    ]);
    await expectNoA11yViolations(container);
  });
});
