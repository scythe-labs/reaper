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

import { RESCAN_HEADING, RESCAN_QUEUED_LEAD, StaleNotice } from "./PolicySimulator";

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
    const panel = read("PolicySimulator.tsx");
    expect(panel).toContain(`export const RESCAN_HEADING = "${RESCAN_HEADING}"`);
    expect(panel).toContain(`"${RESCAN_QUEUED_LEAD}"`);

    const editor = read("PolicyEditor.tsx");
    for (const [name, sentence] of [
      ["RESCAN_HEADING", RESCAN_HEADING],
      ["RESCAN_QUEUED_LEAD", RESCAN_QUEUED_LEAD],
    ] as const) {
      expect(
        editor.includes(sentence),
        `PolicyEditor.tsx writes out the ${name} sentence instead of importing it. The panel ` +
          `shows that text and the editor speaks it, so a reword in one leaves the other saying ` +
          `the old thing (#177, rules 72 and 144).`,
      ).toBe(false);
    }
    expect(editor).toContain("RESCAN_QUEUED_LEAD");
  });
});
