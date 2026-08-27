// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the simulator's wait says, and to whom.
//
// A rescan is driven by a one-second poll rather than by the operator, so the panel changes
// under them: the heading and the button give way to a paragraph and a progress bar. The bar
// announces nothing on its own, so the sentence spoken when the scan starts is the whole
// signal.
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { Simulation } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import {
  appliesOnNextScan,
  Outcome,
  rescanHeading,
  rescanQueuedLead,
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
    // The heading, the progress bar's name, and the announcement all read from one
    // declaration, so they can never say three different things.
    renderNotice();

    expect(screen.getByRole("heading", { name: rescanHeading() })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: rescanHeading() })).toBeInTheDocument();
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

    expect(screen.getByText(new RegExp(rescanQueuedLead().slice(0, 40)))).toBeInTheDocument();
  });

  it("does not claim a second scan when this one carries the changes", () => {
    // The other direction, so the branch cannot collapse into always warning about a follow-up:
    // a test that passes for both states pins neither.
    renderNotice({ followupQueued: false });

    expect(screen.queryByText(new RegExp(rescanQueuedLead().slice(0, 40)))).toBeNull();
    expect(screen.getByText(/scoring your library under the new policy/i)).toBeInTheDocument();
  });
});

describe("what the panel says when it will not answer", () => {
  // Each refusal carries its own remedy, so the operator can tell them apart at a glance. That
  // is why the server sends a typed kind rather than only a sentence: one shared paragraph
  // naming every cause at once could only be right about one of them.
  const cases = [
    ["gathers_differently", "Needs a fresh scan"],
    ["seasons_not_recorded", "Your season rules need a fresh scan"],
    // Names the control, not a cause: the episode map also goes unread after a scan that ran
    // with the hold on and got no answer from Sonarr, so "turn that on" would be false advice
    // for that operator. `tests/test_scan_pipeline.py` drives both producers.
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
    // The reason id lives only in api/simulate.py's `_refused`, and is composed here from the
    // catalog (`policySim.staleReason.<id>`) rather than restated. So the sentence a reviewer
    // reads and the sentence that ships come from the same catalog entry.
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
    // An unknown id falls back, it never guesses. composeIn's own fallback (why.test.ts) fires
    // here, the same as any other reason id this build has no catalog entry for yet: the raw
    // id, never a blank paragraph.
    renderNotice({
      scanning: false,
      staleKind: "a_refusal_from_the_future" as never,
      staleReason: { k: "a_refusal_from_the_future" },
    });

    expect(screen.getByRole("heading", { name: "Needs a fresh scan" })).toBeInTheDocument();
    expect(screen.getByText("a_refusal_from_the_future")).toBeInTheDocument();
  });
});

// This test is a guard rather than a comment asking the next author to remember. The panel
// shows these sentences and `PolicyEditor` speaks them, from two different files. That is
// exactly the arrangement where a reworded heading leaves the spoken copy behind, since each
// file's own tests stay green when neither can see the other's string.
//
// This check reads the sentence however it is written in the source file, but only while it is
// written out as a whole string. An editor that composed the sentence from pieces would still
// pass, which is why the first assertion confirms the declaration exists at all, rather than
// letting a silent no-op pass.
describe("who is allowed to write the rescan sentences", () => {
  const read = (name: string) =>
    readFileSync(join(dirname(fileURLToPath(import.meta.url)), name), "utf8");

  it("is this panel, and not the editor that announces them", () => {
    // These constants resolve from the catalog, so this check pins the key each one reads: the
    // sentence itself lives in locales/en/ui.json, and the imported constant values checked
    // below are what the catalog served.
    const panel = read("PolicySimulator.tsx");
    expect(panel).toContain(
      `export const rescanHeading = () => i18next.t("policySim.rescanHeading")`,
    );
    expect(panel).toContain(`i18next.t("policySim.rescanQueuedLead")`);
    // The savebar's sentence, now that this panel shows it too. The savebar is at the foot of
    // the left column and this panel is the right one, so a reword reaching only one of them
    // leaves two answers to "when does this take effect" on one screen.
    expect(panel).toContain(
      `export const appliesOnNextScan = () => i18next.t("policySim.appliesOnNextScan")`,
    );

    const editor = read("PolicyEditor.tsx");
    for (const [name, sentence] of [
      ["rescanHeading", rescanHeading()],
      ["rescanQueuedLead", rescanQueuedLead()],
      ["appliesOnNextScan", appliesOnNextScan()],
    ] as const) {
      expect(
        editor.includes(sentence),
        `PolicyEditor.tsx writes out the ${name} sentence instead of importing it. The panel ` +
          `shows that text and the editor speaks it, so a reword in one leaves the other saying ` +
          `the old thing (#177, rules 72 and 144).`,
      ).toBe(false);
    }
    expect(editor).toContain("rescanQueuedLead");
    expect(editor).toContain("appliesOnNextScan");
  });
});

// What the panel says about an edit that changes titles without moving the headline numbers.
//
// A keep rule can redraw the histogram while the two headline numbers stay exactly the same,
// since the delta rows count threshold crossings while the headline rows are absolute totals
// with nothing to compare against. So a protection edit that moves a title from spared to not
// judged can leave every number on the panel holding still, even though something changed.
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
      <Outcome
        simulation={{ ...BASE, ...sim }}
        threshold={62}
        pace={null}
        edited={edited}
        mediaType="movie"
      />,
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
    // A sentence contrasting two equal numbers reads as broken: "Your last scan flags 412.
    // This draft flags 412." This checks that sentence never appears.
    expect(screen.queryByText(/This draft flags/)).not.toBeInTheDocument();
  });

  it("compares nothing at all until something has been edited", () => {
    // The panel simulates on mount, before anything is touched. Keying the inert sentence on
    // the count alone would tell the operator their untouched policy does nothing. Keying the
    // comparison on nothing at all would contrast a number with itself ("Your last scan flags
    // 412. This draft flags 412."). With no draft there is nothing to compare, and the
    // headline above already says what the policy flags.
    renderOutcome({ changed_titles: 0 }, false);

    expect(screen.queryByText(INERT)).not.toBeInTheDocument();
    expect(screen.queryByText(/This draft flags/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Your last scan flags/)).not.toBeInTheDocument();
    // And says nothing about a scan either, for the same reason: there is no save pending
    // for one to follow, and these numbers already describe the scan that has run.
    expect(screen.queryByText(appliesOnNextScan())).not.toBeInTheDocument();
  });

  it.each([
    ["a draft that moves titles", 65],
    ["a draft that moves none", 0],
  ])("says a scan applies it, on %s", (_case, changed_titles) => {
    // The panel previews a keep rule exactly, off frozen evidence, so it answers instead of
    // asking for a scan. Without this line, an operator watching these numbers move would have
    // nothing telling them the review queue hasn't moved with them yet, until a scan re-scores
    // it. Both branches are tested, since an inert edit still saves and still starts a scan.
    renderOutcome({ changed_titles }, true);

    expect(screen.getByText(appliesOnNextScan())).toBeInTheDocument();
  });

  it("reads the last scan's count off the server rather than off the two deltas", () => {
    // The fixture is deliberately inconsistent: `no_longer_condemned` here is 0, so deriving
    // the last scan's count as `condemned - newly_condemned + no_longer_condemned` gives 14,
    // the same as the draft's own count. Reading `condemned_before` directly, instead of
    // deriving it from the deltas, is what keeps the sentence correct here.
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
    // A protection edit takes 65 titles from spared to not judged. Nothing crosses the
    // threshold, so both deltas are correctly zero and both headline numbers correctly hold.
    // Without the summary row, this screen would look identical to the one above where nothing
    // happened at all, on the one surface whose job is to show how much a change affects
    // before it's saved.
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

// "Why titles were spared" tallies the gate ids the server sends, and renders each one through
// the browser's own copy rather than the raw id. An id with no copy must never fall through to
// a title-cased slug: the season guard and every keep rule the operator wrote would otherwise
// read as "Season Progression" and "Custom" beside their counts, on the panel someone reads
// while deciding what to delete.
describe("what the spared-by list calls each protection", () => {
  function renderSpared(
    protected_by: { gate: string; count: number }[],
    mediaType: "movie" | "tv" = "movie",
  ) {
    return render(
      <Outcome
        simulation={{ ...BASE, protected_by }}
        threshold={62}
        pace={null}
        edited={false}
        mediaType={mediaType}
      />,
    );
  }

  /** The tally beside one reason, read through its row. Never by searching the page for the
   *  number: the histogram's axis labels are numbers too, so a bare text query answers from
   *  a bar chart that has nothing to do with this list. */
  function tally(label: string): string {
    const row = screen.getByText(label).parentElement;
    return row?.querySelector("dd")?.textContent ?? "";
  }

  // The engine's id, then the words it should map to. Written from what `gateMeta` intends to
  // say, not from its source code, so a label edited to match engine vocabulary fails here.
  //
  // `season_progression` is deliberately vague, and pinned that way on purpose: the same id
  // tallies a season held because the guard could not be answered (a watch mirror shallower
  // than the partway-through hold, the ordinary state of a new install). A label naming the
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
    // Neither spelling should leak through: the raw id, or a title-cased slug made from it.
    // Neither is something a person would say.
    expect(screen.queryByText(slug)).not.toBeInTheDocument();
    expect(screen.queryByText(gate)).not.toBeInTheDocument();
    expect(tally(label)).toBe("40");
  });

  it("picks the rewatch-odds label's movie or TV wording off the simulator's own policy", () => {
    renderSpared([{ gate: "rewatch_odds", count: 12 }], "movie");
    expect(
      screen.getByText("Keep titles most likely to be rewatched above a percentage"),
    ).toBeInTheDocument();

    renderSpared([{ gate: "rewatch_odds", count: 12 }], "tv");
    expect(
      screen.getByText("Keep shows most likely to be rewatched above a percentage"),
    ).toBeInTheDocument();
  });

  it("still says something true about an id it has no copy for", () => {
    // The fallback for a server newer than the browser. Those titles really were kept by
    // something, so the row stays and says only what it can know: never a blank, and never
    // the id.
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
