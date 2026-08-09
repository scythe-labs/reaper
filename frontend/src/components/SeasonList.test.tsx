// SPDX-License-Identifier: AGPL-3.0-or-later
// The all-seasons list under an expanded show card. Two behaviors are load-bearing:
// the open list must say "loading" and "failed" out loud (an open chevron over silence
// reads as broken), and every season is actable in place -- each carries its own Spare/Reap,
// judged by that season's own verdict (rule 51), whatever tab you opened the show from.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Candidate, Chip, Group, ShowStatus, Verdict } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { ReviewQueue } from "./ReviewQueue";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", () => ({ api: apiMock }));

// Three reads the queue makes for the whole list, through hooks no test here names: the
// unmeasured allowance (["profile"]), the expand-seasons and spare-length preferences
// (["general-settings"]), and the genre and library filter choices. Rule 135. The last one
// is called THROUGH an arrow (`() => api.vocabularyValues("genre")`), so leaving it out of
// the mock does not trip the missing-queryFn gate -- the queryFn exists and throws inside,
// and the filter menus quietly render the failed read.
beforeEach(() => {
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
  apiMock.vocabularyValues.mockResolvedValue({ field: "", values: [] });
});

function season(
  id: number,
  n: number,
  verdict: Verdict,
  score: number,
  chip: Chip | null,
  showStatus: ShowStatus | null = null,
): Candidate {
  return {
    id,
    media_key: `sonarr:5:42:${n}`,
    title: `Example Show, Season ${n}`,
    media_type: "season",
    size_bytes: 1024 ** 3,
    verdict,
    score,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2012,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: "sonarr:5:42",
    group_title: "Example Show",
    group_condemned_count: null,
    group_condemned_bytes: null,
    group_unknown_size: null,
    video_resolution: null,
    library: null,
    dormant_for: null,
    reason: null,
    override: null,
    override_own: null,
    show_override: null,
    override_effective: null,
    spare_expires_at: null,
    spare_covers_until: null,
    show_spare_expires_at: null,
    chip,
    show_status: showStatus,
    season_number: n,
    group_seasons: [
      {
        id: 1,
        season: 1,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 2,
        season: 2,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 3,
        season: 3,
        verdict: "abstain",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ],
  };
}

const limboSeason = season(3, 3, "abstain", 82, {
  tone: "look",
  text: "Needs a look, watched more than a season your rule keeps",
});

function renderQueue(
  overrides: {
    onSelect?: (id: number) => void;
    onSelectGroup?: (key: string) => void;
    items?: Candidate[];
  } = {},
) {
  const items = overrides.items ?? [limboSeason];
  apiMock.candidates.mockResolvedValue({
    items,
    total: items.length,
    totalBytes: items.reduce((sum, i) => sum + (i.size_bytes ?? 0), 0),
    unknownSize: items.reduce((n, i) => n + (i.size_bytes === null ? 1 : 0), 0),
    offset: 0,
    snapshotId: 1,
  });
  const queryClient = seedSettings(testQueryClient());
  return renderWithProviders(
    <ReviewQueue
      verdict="abstain"
      onVerdictChange={() => {}}
      selectedId={null}
      selectedGroupKey={null}
      onSelect={overrides.onSelect ?? (() => {})}
      onSelectGroup={overrides.onSelectGroup ?? (() => {})}
    />,
    { client: queryClient },
  );
}

async function expandSeasons() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /3 seasons/ }));
  return user;
}

describe("the show card", () => {
  it("wears its one status chip and the whole-show strip", async () => {
    renderQueue();
    expect(
      await screen.findByText("Needs a look, watched more than a season your rule keeps"),
    ).toBeInTheDocument();
    // The strip marks every season across every lane, not just this tab's.
    expect(screen.getByTitle("Season 1: kept. Open for its full reasoning.")).toBeInTheDocument();
    expect(
      screen.getByTitle("Season 2: would be removed. Open for its full reasoning."),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle("Season 3: left alone. Open for its full reasoning."),
    ).toBeInTheDocument();
  });

  it("marks an ended show, says so when it couldn't check, and stays quiet otherwise", async () => {
    // Three shows, one per state. The card names only the two worth reading: no chip is
    // how "still going" reads, so a bare row must carry no status word at all.
    const ended = season(11, 1, "abstain", 80, null, "ended");
    const going = { ...season(12, 1, "abstain", 80, null, "continuing"), group_key: "sonarr:5:43" };
    const unread = { ...season(13, 1, "abstain", 80, null, "unknown"), group_key: "sonarr:5:44" };
    renderQueue({ items: [ended, going, unread] });

    expect(await screen.findByTitle("This show has ended")).toHaveTextContent("Ended");
    expect(screen.getByTitle("We couldn't check whether this show has ended")).toHaveTextContent(
      "Status unknown",
    );
    // The still-going show wears nothing: one card, one chip, and neither is its.
    expect(screen.queryByText("Still going")).not.toBeInTheDocument();
    expect(screen.getAllByText("Ended")).toHaveLength(1);
    expect(screen.getAllByText("Status unknown")).toHaveLength(1);
  });

  it("opens a season's own reasoning when its strip square is clicked", async () => {
    const onSelect = vi.fn();
    const onSelectGroup = vi.fn();
    renderQueue({ onSelect, onSelectGroup });
    const user = userEvent.setup();
    // Season 2's square carries the candidate id 2; clicking it opens that season, not
    // the show. The square sits inside the card head (which opens the show), so the click
    // must stop there: onSelect fires with the season, onSelectGroup never does.
    await user.click(
      await screen.findByRole("button", { name: "Open Season 2, would be removed" }),
    );
    expect(onSelect).toHaveBeenCalledWith(2);
    expect(onSelectGroup).not.toHaveBeenCalled();
  });
});

describe("the all-seasons list", () => {
  // Every row here carries its own Spare and Reap for one season of one show. A row whose buttons
  // a reader cannot tie back to their season number is how an operator reaps season 2 while
  // meaning to reap season 3.
  it("has no accessibility violations", async () => {
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 3 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: limboSeason.chip,
      show_override: null,
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [
        season(1, 1, "protect", 34, { tone: "kept", text: "Kept, someone is partway through" }),
        season(2, 2, "condemn", 88, null),
        limboSeason,
      ],
    } as Group);
    const { container } = renderQueue();
    await expandSeasons();
    await screen.findByText("Kept, someone is partway through");
    await expectNoA11yViolations(container);
  });

  it("strips the show's name from a season row however old the stored title is", async () => {
    // A title is FROZEN into the snapshot that scanned it, so a library scanned before the
    // separator changed still holds the older shape, and will until someone rescans. Every
    // other fixture in this file now uses the comma `season_scan.py` writes today, which left
    // both legacy branches of `seasonName` with no cover at all -- one refactor from silently
    // gone (rule 118). What their loss looks like is not subtle: the show's name doubled on
    // every season row of every older library, on the rows a per-season reap is chosen from.
    const titled = (n: number, title: string) => ({ ...season(n, n, "condemn", 88, null), title });
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 3 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: null,
      show_override: null,
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [
        titled(1, "Example Show, Season 1"), // what a scan writes now
        titled(2, "Example Show · Season 2"), // before the screen-reader pass
        titled(3, "Example Show — Season 3"), // older still
      ],
    } as Group);
    renderQueue();
    await expandSeasons();

    for (const n of [1, 2, 3]) {
      expect(await screen.findByText(`Season ${n}`)).toBeInTheDocument();
    }
  });

  it("says it failed rather than sitting silent under an open chevron", async () => {
    apiMock.group.mockRejectedValue(new Error("boom"));
    renderQueue();
    await expandSeasons();
    expect(await screen.findByText(/Couldn't load the seasons/)).toBeInTheDocument();
  });

  it("lists every lane and lets you act on each, dropping Reap only where it is a no-op", async () => {
    const group: Group = {
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 3 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: limboSeason.chip,
      show_override: null,
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [
        season(1, 1, "protect", 34, { tone: "kept", text: "Kept, someone is partway through" }),
        season(2, 2, "condemn", 88, null),
        limboSeason,
      ],
    };
    apiMock.group.mockResolvedValue(group);
    renderQueue();
    await expandSeasons();

    // Every lane is present: the kept season's chip, the condemned row's constant
    // mark, and the limbo row (whose chip also sits on the card head).
    expect(await screen.findByText("Kept, someone is partway through")).toBeInTheDocument();
    expect(screen.getByText("Would be removed")).toBeInTheDocument();

    // Every season is actable in place now, not just this tab's. Scope to the season list
    // (the card head's whole-show control and the strip squares are also buttons).
    const list = screen.getByRole("list");
    const rows = within(list).getAllByRole("button", { name: /Season \d/ });
    expect(rows).toHaveLength(3);
    // Spare is on every row; it is never a no-op.
    expect(within(list).getAllByRole("button", { name: /^Spare$/ })).toHaveLength(3);
    // Reap shows only where it would change something -- the kept season and the limbo one,
    // never the already-condemned row (reaping it changes nothing).
    expect(within(list).getAllByRole("button", { name: /^Reap$/ })).toHaveLength(2);
  });

  it("wears a season-level held reap in the truncating status-chip family, not the card .chip", async () => {
    // The pill overran the fixed button column because a season-level OverrideChip used the
    // default `.chip` family (a card's meta line), which never clamps. It must use the season
    // list's own `.status-chip` family -- exactly like ShowPanel's SeasonPill and the row's
    // other chips -- so a long "Reap requested" line ellipsizes in place instead (rule 51).
    const held: Candidate = {
      ...season(3, 3, "protect", 90, { tone: "kept", text: "Kept, playing right now" }),
      override: "reap",
      override_own: "reap",
      override_effective: false,
    };
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 1024 ** 3,
      reason: null,
      library: null,
      chip: limboSeason.chip,
      show_override: null,
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [held],
    } as Group);
    renderQueue();
    await expandSeasons();

    const chip = await screen.findByText(/Reap requested, kept for now/);
    expect(chip.className).toContain("status-chip");
    expect(chip.className).toContain("status-reap-held");
    // NOT the non-clamping card family; a bare "chip" token here is the regression.
    expect(chip.className).not.toMatch(/(^|\s)chip(\s|$)/);
  });

  it("says a season's size is unknown rather than showing it as empty", async () => {
    // The server sends null when Sonarr would not report a size. "0 B" here would be a
    // false statement sitting beside Spare and Reap.
    const unsized = { ...season(2, 2, "condemn", 88, null), size_bytes: null };
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 1024 ** 3,
      reason: null,
      library: null,
      chip: limboSeason.chip,
      show_override: null,
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [unsized, limboSeason],
    } as Group);
    renderQueue();
    await expandSeasons();

    // The click is awaited, the read it starts is not, so the list arrives a beat later
    // (rule 137).
    const list = await screen.findByRole("list");
    expect(await within(list).findByText("Size unknown")).toBeInTheDocument();
    expect(within(list).queryByText("0 B")).not.toBeInTheDocument();
    // The season that did report one still reads as a size.
    expect(within(list).getByText("1.0 GiB")).toBeInTheDocument();
  });

  it("states a whole-show reap once and lets only divergent seasons speak", async () => {
    // A whole-show reap covers every season. The header says so once; a plainly-inheriting
    // season then carries no per-row note at all (the wall of repeated sentences is gone), and
    // only a held or against-the-show season adds a chip and a reason.
    const withShow = (id: number, n: number, extra: Partial<Candidate>): Candidate => ({
      ...season(id, n, "abstain", 80, null),
      show_override: "reap",
      ...extra,
    });
    const inherited = withShow(21, 1, {
      override: "reap",
      override_own: null,
      override_effective: true,
    });
    const held = withShow(22, 2, {
      override: "reap",
      override_own: null,
      override_effective: false,
      // `text` and `why` deliberately say different things: the held-reap reason is the
      // chip's own `why` field, which the server words beside the text (H-1). A frontend
      // that went back to parsing `text` would render the wrong sentence here and fail,
      // which is what this batch's predecessor could not do -- it transcribed the backend's
      // prose on both sides of the contract, so drift was invisible (I-2).
      chip: {
        tone: "look",
        text: "Some checks couldn't run",
        why: "a rating source didn't answer",
      },
    });
    const sparedAgainst = withShow(23, 3, {
      override: "spare",
      override_own: "spare",
      override_effective: null,
    });
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 3 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: null,
      show_override: "reap",
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [inherited, held, sparedAgainst],
    } as Group);
    renderQueue();
    await expandSeasons();

    // The inherited fate is stated once, at the top of the list.
    expect(await screen.findByText(/The whole show is set to reap\./)).toBeInTheDocument();
    // A plainly-inheriting season says nothing extra: no per-row "Reaped by hand" pill.
    expect(screen.queryByText(/Reaped by hand/)).not.toBeInTheDocument();
    // The held season says it is kept for now, and why.
    expect(screen.getByText("Kept for now")).toBeInTheDocument();
    expect(screen.getByText("A rating source didn't answer.")).toBeInTheDocument();
    // The season spared against the show says it goes its own way. ("Spared" is also the active
    // Spare button's label, so target the chip.)
    expect(screen.getByText("Spared", { selector: ".status-chip" })).toBeInTheDocument();
    expect(screen.getByText("Kept even though the whole show is set to reap.")).toBeInTheDocument();
  });

  it("qualifies the whole-show reap banner when the engine holds every season", async () => {
    // The show is set to reap, but the engine can honor it on no season (all held). The banner
    // must not assert removal; it says the reap is noted and the seasons are kept for now (U-2).
    const withShow = (id: number, n: number, extra: Partial<Candidate>): Candidate => ({
      ...season(id, n, "abstain", 80, null),
      show_override: "reap",
      ...extra,
    });
    const held1 = withShow(31, 1, {
      override: "reap",
      override_own: null,
      override_effective: false,
    });
    const held2 = withShow(32, 2, {
      override: "reap",
      override_own: null,
      override_effective: false,
    });
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 2 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: null,
      show_override: "reap",
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [held1, held2],
    } as Group);
    renderQueue();
    await expandSeasons();

    expect(await screen.findByText(/the seasons are kept for now/i)).toBeInTheDocument();
    // It must not claim blanket removal when nothing is actually going.
    expect(screen.queryByText(/Every season below is removed/i)).not.toBeInTheDocument();
  });
  it("describes the whole-show spare in force, not the shape of a spare in general", async () => {
    // The banner's mark and words come from the spare actually on the show. A fixed ∞ said
    // "kept forever" directly above a control counting down, and went on saying it after the
    // clock passed -- the same bug the Spare button had. Expired is its own wording: the
    // seasons really are still kept until a scan judges them, so it must not read as a
    // removal (rule 61).
    const spared = (id: number, n: number): Candidate => ({
      ...season(id, n, "abstain", 80, null),
      show_override: "spare",
      override: "spare",
      override_own: null,
    });
    const group = (showSpareExpiresAt: string | null) =>
      ({
        group_key: "sonarr:5:42",
        title: "Example Show",
        year: 2012,
        poster_url: null,
        summary: null,
        size_bytes: 1024 ** 3,
        unknown_size_seasons: 0,
        reason: null,
        library: null,
        chip: null,
        show_override: "spare",
        show_spare_expires_at: showSpareExpiresAt,
        links: {} as Group["links"],
        show_status: null,
        seasons: [spared(41, 1)],
      }) as Group;

    // Forever: ∞, and the plain claim.
    apiMock.group.mockResolvedValue(group(null));
    const forever = renderQueue();
    await expandSeasons();
    expect(await screen.findByText(/The whole show is spared\./)).toBeInTheDocument();
    expect(document.querySelector(".show-inherit-mark .mk-inf")).not.toBeNull();
    forever.unmount();

    // Timed: no ∞, and the banner carries how long is left.
    apiMock.group.mockResolvedValue(group(new Date(Date.now() + 27 * 86_400_000).toISOString()));
    const timed = renderQueue();
    await expandSeasons();
    expect(await screen.findByText(/The whole show is spared, 27 days left\./)).toBeInTheDocument();
    expect(document.querySelector(".show-inherit-mark .mk-inf")).toBeNull();
    timed.unmount();

    // Expired: says the spare ran out, and that the seasons are still kept.
    apiMock.group.mockResolvedValue(group(new Date(Date.now() - 3 * 86_400_000).toISOString()));
    renderQueue();
    await expandSeasons();
    expect(await screen.findByText(/The whole show's spare has run out\./)).toBeInTheDocument();
    expect(
      screen.getByText(/still kept until the next scan judges them again/),
    ).toBeInTheDocument();
    expect(document.querySelector(".show-inherit-mark .mk-inf")).toBeNull();
  });
  it("marks a season whose own spare ran out, so its row does not disagree with itself", async () => {
    // The row already draws the third state twice -- a dashed-green score badge and a dashed
    // strip square (rule 49). The chip beside them said a solid "Spared", so one row claimed
    // both "your decision holds" and "your decision ran out". The season is kept either way,
    // which is why the sentence still leads with that; what it adds is the spare running out
    // and a scan being what ends it (rule 61).
    const withShow = (id: number, n: number, extra: Partial<Candidate>): Candidate => ({
      ...season(id, n, "abstain", 80, null),
      show_override: "reap",
      ...extra,
    });
    const liveUntil = new Date(Date.now() + 30 * 86_400_000).toISOString();
    const spentAt = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const live = withShow(51, 1, {
      override: "spare",
      override_own: "spare",
      spare_expires_at: liveUntil,
      spare_covers_until: liveUntil,
    });
    const spent = withShow(52, 2, {
      override: "spare",
      override_own: "spare",
      // The show above is set to REAP, so it contributes no cover and the spent season spare
      // is still the last word: this row must go on reading expired.
      spare_expires_at: spentAt,
      spare_covers_until: spentAt,
    });
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 2 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: null,
      show_override: "reap",
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [live, spent],
    } as Group);
    renderQueue();
    await expandSeasons();

    // The live spare keeps the solid chip and the plain sentence.
    const solid = await screen.findByText("Spared", { selector: ".status-chip" });
    expect(solid.className).toContain("status-hand-spare");
    expect(solid.className).not.toContain("status-spare-expired");
    // The spent one says so, on the dashed fill, and adds what happens next.
    const spentChip = screen.getByText("Spare expired", { selector: ".status-chip" });
    expect(spentChip.className).toContain("status-spare-expired");
    expect(
      screen.getByText(/Your spare ran out, so the next scan judges it again\./),
    ).toBeInTheDocument();
  });
});

describe("what a screen reader hears on a season row", () => {
  // The row's name is the name of the `CardOpen` control on its season title, and that control
  // is what makes the rest of the row readable at all. The row USED to be a `role="button"`,
  // which made its own name the whole of what was announced -- so with no `aria-label` it read
  // out as its score, its chip, the nested Spare/Reap group label and its size run together, and
  // with one it read out as the label and nothing else. #169 took the role off every card; what
  // these still pin is that each row names itself for the season it opens.
  //
  // The existing lane test reaches these rows with `{ name: /Season \d/ }`, which matched the
  // old computed name just as well as the new one -- the name-blind shape that let this hide.
  // This asserts the exact string instead.
  it("names the row for the season it opens, not for everything inside it", async () => {
    const group: Group = {
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 3 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: limboSeason.chip,
      show_override: null,
      show_spare_expires_at: null,
      links: {} as Group["links"],
      show_status: null,
      seasons: [
        season(1, 1, "protect", 34, { tone: "kept", text: "Kept, someone is partway through" }),
        season(2, 2, "condemn", 88, null),
        limboSeason,
      ],
    };
    apiMock.group.mockResolvedValue(group);
    renderQueue();
    await expandSeasons();

    // Rule 145: the population is every row this group renders, counted, not just the one
    // looked up. A fourth season that opted out of naming itself would fail the length check
    // even though the three named rows still answer.
    const list = screen.getByRole("list");
    const named = within(list).getAllByRole("button", { name: /^Why Season \d+ scored \d+$/ });
    expect(named).toHaveLength(group.seasons.length);
    expect(
      within(list).getByRole("button", { name: "Why Season 1 scored 34" }),
    ).toBeInTheDocument();
    expect(
      within(list).getByRole("button", { name: "Why Season 2 scored 88" }),
    ).toBeInTheDocument();
  });
});
