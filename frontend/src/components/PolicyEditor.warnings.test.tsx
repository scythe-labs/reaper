// SPDX-License-Identifier: AGPL-3.0-or-later
// Rule 42, proven rather than argued: a warning renders beside the control that fixes it, and
// a field an anchor claims lands at that anchor's block or in the bottom catch-all, exactly once,
// in every page state the editor can be in.
//
// Its own file because it is the longest walk in the suite: nine anchors, each driven through the
// state its guard requires and through every branch it does not name. It shares the page boot
// with `PolicyEditor.test.tsx` through `src/test/policyEditor.tsx`.
import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PolicyBody, PolicyWarning } from "../api";
import { body, pace, policyEditorKit } from "../test/policyEditor";
import type { WarningAnchor, WarningAnchorId, WarningGuard } from "./PolicyEditor";
import { WARNING_ANCHORS, anchorClaims } from "./PolicyEditor";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

const { renderEditor, renderTvEditor } = policyEditorKit(apiMock);

// Seeded here for the same reason `PolicyEditor.test.tsx` seeds it: the probe under each signal's
// range fires on a 250ms debounce, and rule 135's gate only catches a queryFn that actually ran.
beforeEach(() => {
  apiMock.probePolicy.mockResolvedValue({ points: 0.8 });
});

describe("PolicyEditor warning anchors", () => {
  /** Puts the page in one state and hands it `warnings` back from /policy/validate, which is
   *  the only route a warning reaches the page by. */
  type Drive = (warnings: PolicyWarning[]) => Promise<void>;

  const unguarded: Drive = async (warnings) => {
    renderEditor({ body: body() }, pace, null, warnings);
  };

  const ratingBody = (enabled: boolean): PolicyBody => ({
    ...body(),
    gates: [{ gate: "rating_floor", enabled, threshold: 70, window_days: 365 }],
  });

  /** The rating card holding two bars, on the two scales it draws differently: IMDb is the
   *  ten-point scale with a vote floor beside it, Rotten Tomatoes critics is a bare
   *  percentage. Two rather than one because the complaint this binding exists to stop is a
   *  bar reading its NEIGHBOUR's -- a card with one bar cannot fail that way. */
  const barsBody: PolicyBody = {
    ...ratingBody(true),
    keep_rating_rules: [
      { source: "imdb", floor: 65, min_votes: 5000 },
      { source: "rotten_tomatoes_critic", floor: 75, min_votes: 0 },
    ],
  };

  /** The four protections the server warns about, each in the switch position its warning
   *  actually fires in: the two thresholds are warned about only while their gate is ON (a
   *  gate that is off holds nothing, and its box is not rendered), and the two switches only
   *  while theirs is OFF. So this is not an arrangement chosen to make boxes appear -- it is
   *  the one page state all four warnings can reach an operator in at once.
   *
   *  The default fixture ships no gates at all, which is why the `gates` binding needs its
   *  own state rather than the unguarded one: the block under the list renders either way,
   *  but the rows carrying its boxes do not. */
  const gatesBody: PolicyBody = {
    ...body(),
    gates: [
      { gate: "min_dormancy", enabled: true, threshold: 1095, window_days: 365 },
      { gate: "server_popularity", enabled: true, threshold: 2, window_days: 365 },
      { gate: "streaming_now", enabled: false, threshold: 0, window_days: 365 },
      { gate: "data_horizon", enabled: false, threshold: 0, window_days: 365 },
    ],
  };

  /** The two page states each guard is checked in, plus the accessible name of a control that
   *  exists on the held branch ONLY. That control is what pins a guard's name to the mount
   *  condition it claims to be: without it these cases would assert a guard against itself.
   *  Exhaustive over `WarningGuard` by its type, so a guard added to the editor does not
   *  compile until the states that hold and drop it are written here (rule 145). */
  const GUARDS: Record<WarningGuard, { control: string; held: Drive; absent: Drive }> = {
    pace: {
      control: "Items with an unknown size",
      held: unguarded,
      // A profile read that FAILED, which is how #145 reached an operator: the whole pace
      // section is one error paragraph, and the box this warning sits under is gone with it.
      absent: async (warnings) => {
        renderEditor({ body: body() }, new Error("boom"), null, warnings);
      },
    },
    tv: {
      control: "Newest seasons to always keep",
      held: async (warnings) => {
        await renderTvEditor({}, undefined, warnings);
      },
      // The movies policy the editor opens on: the season card is not rendered at all.
      absent: unguarded,
    },
    ratingGate: {
      control: "Add a rating source",
      held: async (warnings) => {
        renderEditor({ body: ratingBody(true) }, pace, null, warnings);
      },
      // The card is on the page either way. Its bars, and everything warned about them, live
      // under its own switch.
      absent: async (warnings) => {
        renderEditor({ body: ratingBody(false) }, pace, null, warnings);
      },
    },
  };

  /** A probe fixture's own text, back out of its `legacy` reason (`why.ts`'s `composeIn`
   *  renders a legacy reason's `p.text` verbatim). This walk cares only about WHERE a
   *  warning lands, never what it says, so every fixture here carries a made-up sentence
   *  with no catalog entry rather than a real backend id. */
  const probeText = (w: PolicyWarning): string => String(w.reason.p?.text ?? "");

  /** One warning per field the anchor claims. The claimed fields ARE the probe fields, since
   *  both read the same array in the declaration. */
  const probes = (anchor: WarningAnchor): PolicyWarning[] =>
    anchor.fields.map((field) => ({
      field,
      severity: "warn",
      reason: { k: "legacy", p: { text: `anchor probe: ${field}` } },
    }));

  /** The same probe, one per field the binding table below names -- which for a `prefix`
   *  anchor is wider than the anchor's own `fields`, and for every other one is the same list
   *  by the assertion in that case. Same message shape either way, so a probe reads the same
   *  wherever it turns up on the page. */
  const boundProbes = (controls: Record<string, string>): PolicyWarning[] =>
    Object.keys(controls).map((field) => ({
      field,
      severity: "warn",
      reason: { k: "legacy", p: { text: `anchor probe: ${field}` } },
    }));

  /** Claimed by no anchor, so it proves the stack is rendering at all in the same render.
   *  Without it, a page showing no warnings whatsoever would fail the cases below for a
   *  reason none of their messages name. */
  const catchAll: PolicyWarning = {
    field: "some_field_no_anchor_claims",
    severity: "warn",
    reason: { k: "legacy", p: { text: "anchor probe: the catch-all stack" } },
  };

  it("declares the nine anchors these cases walk", () => {
    // Pinned because the walk is flag-shaped: an anchor deleted from the list takes its own
    // case away with it, and every assertion that remains still passes (rule 145). Move this
    // number in the same commit that adds or removes an anchor.
    expect(WARNING_ANCHORS).toHaveLength(9);
  });

  for (const anchor of WARNING_ANCHORS) {
    it(`renders every warning the ${anchor.id} anchor claims, exactly once`, async () => {
      const mine = probes(anchor);
      // The fields it claims are the fields it is probed with -- one list in the declaration,
      // so a claim cannot quietly go unprobed.
      for (const w of mine) expect(anchorClaims(anchor, w.field)).toBe(true);

      await (anchor.guard ? GUARDS[anchor.guard].held : unguarded)([...mine, catchAll]);

      expect(await screen.findByText(probeText(catchAll))).toBeInTheDocument();
      if (anchor.guard) {
        expect(screen.getByLabelText(GUARDS[anchor.guard].control)).toBeInTheDocument();
      }
      for (const w of mine) {
        // Claiming took this out of the catch-all, so anything found here came from the
        // anchor's own block -- and finding nothing means it rendered nowhere at all.
        expect(await screen.findByText(probeText(w))).toBeInTheDocument();
        // Exactly once: claiming AND still reaching the catch-all is the other way to be
        // wrong, and it prints the same sentence twice on one page.
        expect(screen.getAllByText(probeText(w))).toHaveLength(1);
      }
    });
  }

  // The anchors whose warnings each have ONE control that fixes them, and that control's
  // accessible name. SIX of the nine are here; the three that are not warn about a LIST or a
  // card -- the signal sliders and the two rule editors -- where there is no single box to
  // point at and binding every child would read the whole card's complaints at each of them.
  // Deliberately partial, and named here so the boundary is a decision on the page rather than
  // an omission (#174).
  //
  // Three of the six earned their place rather than arriving with one obvious box:
  //
  // `in_progress` claims one field and its remedy ("lower this to match your history") names
  // one box, so leaving it unbound would have added an item to the backlog above rather than a
  // considered boundary.
  //
  // `gates` looks like a list and is not one: every warning in that family names one setting
  // of one protection, so each already has the single owning box the other three lack, and
  // binding them needed no judgment about what a group should say (#189).
  //
  // `keep_rating_rules` was the same shape in disguise, and joined on the server rather than
  // here: three of its four producers sit inside the loop over the bars and name one bar each
  // through `source_label(rule.source)`, so giving them a `keep_rating_rules.{source}.floor`
  // field was all they needed (#189). Its fourth is the card's own -- the protection on with no
  // sources -- and that one stays unbound, below.
  //
  // Keyed by FIELD, not by anchor: `keep_last` claims three, and each is fixed from a different
  // control. Bound to the anchor as a whole, the seasons box spoke the scope control's
  // complaint and the scope control said nothing.
  const BOUND: {
    anchor: WarningAnchorId;
    controls: Record<string, string>;
    /** Fields this anchor DECLARES that deliberately reach no single control, with the reason
     *  at the entry. Without this the check below would read a mixed anchor as an unfinished
     *  one, and the only way to quiet it would be to stop claiming the field -- which drops
     *  the warning off the page rather than down to the catch-all (#145, rule 42). Declared
     *  rather than omitted, and the whole population is pinned by the count case below, so a
     *  binding cannot be given up by quietly moving its field into here. */
    unbound?: readonly string[];
    /** The page state these boxes exist in, where the anchor's own guard does not name it.
     *  `gates` is one: its block is unguarded and renders on the default fixture, but that
     *  fixture ships no protections, so the rows holding its boxes are not there. */
    drive?: Drive;
  }[] = [
    { anchor: "condemn_at", controls: { condemn_at: "Put a title on the list once it scores" } },
    {
      anchor: "gates",
      // The four the server sends today, each named by `engine/policy_warnings.py` as
      // `gates.<protection>.<setting>`. `PolicyEditor` binds them generically off the gate id
      // in the served body, so a fifth protection warning about one of these three settings
      // binds with no frontend change -- which is also why this list cannot prove the binding
      // covers every BOX a row draws. The walk below that one does.
      controls: {
        "gates.min_dormancy.threshold": "Give every title time to be rewatched threshold",
        "gates.server_popularity.window_days": "How far back recent plays count",
        "gates.streaming_now.enabled": "Never touch something playing right now",
        "gates.data_horizon.enabled": "Stop if the unwatched time can't be read",
      },
      drive: async (warnings) => {
        renderEditor({ body: gatesBody }, pace, null, warnings);
      },
    },
    {
      anchor: "keep_rating_rules",
      // The three the server sends today all name one bar's number, so each lands on the box
      // holding that number. Named per SOURCE, which is what makes them distinct: `PolicyBody`
      // refuses two rules on one source, so the source keys the row the way the gate id keys a
      // protection row.
      controls: {
        "keep_rating_rules.imdb.floor": "IMDb score out of 10",
        "keep_rating_rules.rotten_tomatoes_critic.floor": "Rotten Tomatoes critics percentage",
      },
      // The card's own complaint, and the one warning here that is genuinely about the card:
      // it fires when the protection is on with no bars at all, so there is no bar row for it
      // to sit on, and the two remedies it offers ("Add a rating source to it, or turn the
      // protection off") are two different controls. It renders in the block under the list,
      // where it is beside both of them.
      unbound: ["keep_rating_rules"],
      drive: async (warnings) => {
        renderEditor({ body: barsBody }, pace, null, warnings);
      },
    },
    {
      anchor: "keep_last",
      controls: {
        keep_last_seasons: "Newest seasons to always keep",
        keep_last_scope: "Keep-last scope",
        // The switch is the third, and it is bound rather than left to the card: the warning
        // it carries (#224) has no remedy this family will recommend, so the one control it
        // can point at is the one whose own help text offers the other way out.
        flag_keep_conflicts: "Ask me first when a removal looks unusual",
      },
    },
    {
      anchor: "max_unmeasured_per_run",
      controls: { max_unmeasured_per_run: "Items with an unknown size" },
    },
    {
      anchor: "in_progress",
      controls: {
        in_progress_hold_days: "Days without watching before a held place is released",
      },
    },
  ];

  it("binds six of the nine anchors, and walks each one it binds", () => {
    // Rule 145: this table is flag-shaped the same way the anchor list is. An entry deleted
    // takes both of its cases away with it and every assertion left still passes, so the count
    // is what says an anchor stopped being bound. Move it in the same commit that binds one or
    // gives one up, and move the sentence above it too -- the three it describes are 9 minus
    // this.
    expect(BOUND).toHaveLength(6);
    expect(new Set(BOUND.map((b) => b.anchor)).size).toBe(BOUND.length);
    // And the exemptions, by name and in full. `unbound` excuses a declared field from the
    // description walk below, so it is the one place a binding could be given up without the
    // count noticing: pinning the whole population is what closes that (rule 145).
    expect(BOUND.flatMap((b) => b.unbound ?? [])).toEqual(["keep_rating_rules"]);
  });

  /** The state a bound anchor's boxes are driven in: its own entry says so where the default
   *  and the guard states do not put them on the page. */
  const driveBound = (entry: (typeof BOUND)[number], anchor: WarningAnchor): Drive =>
    entry.drive ?? (anchor.guard ? GUARDS[anchor.guard].held : unguarded);

  for (const entry of BOUND) {
    const { anchor: id, controls } = entry;
    it(`lets each ${id} control speak the warning that is about IT`, async () => {
      // The warning was rendered beside the box and the box never mentioned it, so reaching it
      // meant leaving the control to go looking. Asserted as the accessible DESCRIPTION, which
      // is what a reader computes: an `aria-describedby` naming an id that is not on the page
      // would satisfy an attribute check and still say nothing.
      const anchor = WARNING_ANCHORS.find((a) => a.id === id)!;
      const mine = boundProbes(controls);

      // Every field this anchor DECLARES is accounted for above -- bound to a control, or
      // named as deliberately card-level -- so widening `fields` without giving the new one
      // somewhere to speak from fails here rather than silently hanging its warning off
      // whichever control was already bound.
      const accounted = [...Object.keys(controls), ...(entry.unbound ?? [])];
      for (const field of anchor.fields) expect(accounted).toContain(field);
      // And nothing here is bound or exempted that the anchor does not claim: a field
      // misspelled above points its box at an id no notice on this page ever carries, which is
      // the failure the description assertions below would report as a missing binding rather
      // than a typo.
      for (const field of accounted) expect(anchorClaims(anchor, field)).toBe(true);
      // Exactly the declared fields, for every anchor that can state them. A `prefix` anchor
      // claims an open family and its `fields` holds one member as a probe, so the table names
      // more of the family than the declaration can (`gates`, four settings across four
      // protections; `keep_rating_rules`, one bar per source); the two checks above are what
      // still holds it to the claim.
      if (anchor.prefix === undefined) {
        expect(accounted.sort()).toEqual([...anchor.fields].sort());
      }

      await driveBound(entry, anchor)([...mine, catchAll]);

      // Wait for the WARNING, not for the box: validation is debounced, so the control is on
      // the page a beat before there is anything for it to describe itself with, and asserting
      // on the box alone would read the empty description every time (rule 137's shape).
      await screen.findByText(probeText(mine[0]!));

      for (const w of mine) {
        const box = screen.getByLabelText(controls[w.field]!);
        expect(box).toHaveAccessibleDescription(new RegExp(probeText(w)));
        // And ONLY that one: a control speaking a sibling field's complaint sends the operator
        // to fix it here, where it cannot be fixed.
        for (const other of mine) {
          if (other.field === w.field) continue;
          expect(box).not.toHaveAccessibleDescription(new RegExp(probeText(other)));
        }
        // Never invalid. A policy warning does not block a save -- the save gate is a 422 from
        // body validation plus the points budget, and `severity` reaches neither -- so a box
        // marked invalid states a refusal that will not happen, which is the same lie as one
        // that hides the complaint.
        expect(box).not.toHaveAttribute("aria-invalid");
      }
    });

    it(`never marks a ${id} control invalid, even when the warning is danger`, async () => {
      // The severity a `warn` probe cannot reach. The first version of #174 read "danger" as
      // "blocks a save" and marked three controls invalid over values the app saves happily,
      // so this arm is what keeps the encoding from coming back.
      const anchor = WARNING_ANCHORS.find((a) => a.id === id)!;
      const blocking = boundProbes(controls).map((w) => ({ ...w, severity: "danger" as const }));
      await driveBound(entry, anchor)([...blocking, catchAll]);
      await screen.findByText(probeText(blocking[0]!));

      for (const name of Object.values(controls)) {
        expect(screen.getByLabelText(name)).not.toHaveAttribute("aria-invalid");
      }
    });
  }

  it("leaves no box in a rating bar's row that its own warning cannot reach", async () => {
    // The `gates` walk below, for the family that joined it (#189). Same reason it is needed:
    // the `keep_rating_rules` entry above names the two fields the SERVER sends on this
    // fixture, and those cannot see a third control added to `RatingBarRow` with no binding,
    // because a field nobody sends yet has no probe to go missing (rule 145).
    //
    // IMDb is the row that draws all of them: the ten-point score, the vote floor beside it,
    // and the × that removes the bar. `min_votes` carries no warning today and is bound
    // anyway, off the same generic helper the score uses, so the walk is over what the row
    // RENDERS rather than over what the server currently complains about.
    const BOXES: Record<string, string | null> = {
      "IMDb score out of 10": "keep_rating_rules.imdb.floor",
      "IMDb vote floor": "keep_rating_rules.imdb.min_votes",
      // Deliberately unbound, and the one exemption on this row. It is the way OUT of the bar,
      // not the way to fix its number, and it already says what it does; a complaint about a
      // score read from the remove button would offer deleting the protection as the remedy.
      "Remove the IMDb bar": null,
    };
    const probed: PolicyWarning[] = Object.values(BOXES)
      .filter((field): field is string => field !== null)
      .map((field) => ({
        field,
        severity: "warn",
        reason: { k: "legacy", p: { text: `anchor probe: ${field}` } },
      }));
    // The neighboring bar's complaint, driven in the same render. This is the misattribution
    // the issue was filed on -- a warning about IMDb spoken by the Rotten Tomatoes row -- so it
    // is probed rather than argued.
    const neighbor: PolicyWarning = {
      field: "keep_rating_rules.rotten_tomatoes_critic.floor",
      severity: "warn",
      reason: { k: "legacy", p: { text: "anchor probe: the neighboring bar" } },
    };

    renderEditor({ body: barsBody }, pace, null, [...probed, neighbor, catchAll]);
    await screen.findByText(probeText(probed[0]!));

    // Typed, unlike the `closest("li")` in the walk below: TypeScript resolves a TAG selector
    // to that tag's element type and a CLASS selector only to `Element`, which `within` will
    // not take.
    const row = screen.getByLabelText("IMDb score out of 10").closest<HTMLElement>(".bar-line")!;
    // A container query rather than a role query, deliberately: the point is to collect boxes
    // this walk does not already know about, and a role query only finds the roles it was told
    // to ask for. `<button>` is in the list for the same reason and matches the × here.
    const boxes = Array.from(row.querySelectorAll("input, select, textarea, button"));
    const named = Object.keys(BOXES).map((name) => within(row).getByLabelText(name));
    expect(boxes).toHaveLength(named.length);
    for (const box of boxes) expect(named).toContain(box);

    for (const [name, field] of Object.entries(BOXES)) {
      const box = within(row).getByLabelText(name);
      if (field === null) {
        expect(box).not.toHaveAccessibleDescription(/anchor probe/);
        continue;
      }
      expect(box).toHaveAccessibleDescription(new RegExp(`anchor probe: ${field}`));
      for (const other of [...probed, neighbor]) {
        if (other.field === field) continue;
        expect(box).not.toHaveAccessibleDescription(new RegExp(probeText(other)));
      }
    }
  });

  it("leaves no box in a protection's row that its own warning cannot reach", async () => {
    // The `gates` entry above names the four fields the SERVER sends today, and rule 145 is
    // exactly why that is not the whole proof: those four cannot see a fifth control added to
    // `GateRow` with no binding, because a field nobody sends yet has no probe to go missing.
    // So the population walked here is the other one -- every box one row draws -- counted and
    // reconciled by hand against what that row renders.
    //
    // `server_popularity` is the row that draws all of them: the switch, a threshold, and the
    // look-back window with its unit picker. The three probes are driven together, which the
    // server never does (each fires on a different fault), because the claim under test is
    // about the row's wiring and not about which faults co-occur.
    //
    // Names, not just a count: a count alone passes when a box is renamed out of one binding
    // and into another's.
    const BOXES: Record<string, string | null> = {
      "Keep what your users actually watch": "gates.server_popularity.enabled",
      "Keep what your users actually watch threshold": "gates.server_popularity.threshold",
      "How far back recent plays count": "gates.server_popularity.window_days",
      // Deliberately unbound, and the one exemption on this row. `QuantityInput` says why: the
      // unit is a control that announces itself and its own value, so binding the pair's
      // complaint to it as well would say the same sentence twice on the way through.
      "How far back recent plays count unit": null,
    };
    const probed: PolicyWarning[] = Object.values(BOXES)
      .filter((field): field is string => field !== null)
      .map((field) => ({
        field,
        severity: "warn",
        reason: { k: "legacy", p: { text: `anchor probe: ${field}` } },
      }));

    renderEditor({ body: gatesBody }, pace, null, [...probed, catchAll]);
    await screen.findByText(probeText(probed[0]!));

    const row = screen.getByLabelText("Keep what your users actually watch").closest("li")!;
    // A container query rather than a role query, deliberately: the point is to collect boxes
    // this walk does not already know about, and a role query only finds the roles it was
    // told to ask for. `<button>` is in the list for the same reason and matches nothing here.
    const boxes = Array.from(row.querySelectorAll("input, select, textarea, button"));
    const named = Object.keys(BOXES).map((name) => within(row).getByLabelText(name));
    expect(boxes).toHaveLength(named.length);
    for (const box of boxes) expect(named).toContain(box);

    for (const [name, field] of Object.entries(BOXES)) {
      const box = within(row).getByLabelText(name);
      if (field === null) {
        expect(box).not.toHaveAccessibleDescription(/anchor probe/);
        continue;
      }
      expect(box).toHaveAccessibleDescription(new RegExp(`anchor probe: ${field}`));
      for (const other of probed) {
        if (other.field === field) continue;
        expect(box).not.toHaveAccessibleDescription(new RegExp(probeText(other)));
      }
    }
  });

  for (const anchor of WARNING_ANCHORS) {
    const guard = anchor.guard;
    if (guard === undefined) continue;
    it(`sends ${anchor.id}'s warnings to the catch-all when its control is not mounted`, async () => {
      // #145 as a property of every guarded anchor rather than of the one that was found:
      // on the branch where the block is not there, claiming has to stop, or the warning is
      // dropped from the page instead of falling to the bottom.
      const mine = probes(anchor);
      await GUARDS[guard].absent(mine);

      for (const w of mine) {
        expect(await screen.findByText(probeText(w))).toBeInTheDocument();
        expect(screen.getAllByText(probeText(w))).toHaveLength(1);
      }
      // Read after the warnings, not before: the control is trivially absent from a page that
      // has not rendered yet, and this has to say something about the settled one (rule 137).
      expect(screen.queryByLabelText(GUARDS[guard].control)).toBeNull();
    });
  }

  // The two walks above prove a guard an anchor DECLARES, and they are blind to one it should
  // have declared and did not -- which is #145 itself, not a variant of it. The first walk
  // drives an unguarded anchor in one state, and `pace` is held there, so a block sitting
  // inside the pace section had a mount to paint its probe on and the failed-read branch was
  // never rendered at all; the second walk skips the anchor for having no guard to drive.
  // Dropping `guard: "pace"` from `max_unmeasured_per_run` therefore put the unknown-size
  // warning back off the page and this file went green (#167) -- at 40 cases rather than 41,
  // because the mutation deletes the one case that mentions it and every case left still
  // passes, which is the flag shape rule 145 is about. The two guards that DO fail on that
  // mutation only do so by luck of the default state being their absent branch, so the walk
  // below is what actually holds this, not them.
  //
  // So every anchor is also driven through every branch it does NOT name, where rule 42's
  // sentence is the whole invariant: a claimed field lands at its own block or in the bottom
  // catch-all, and either way renders EXACTLY ONCE. Zero is the silent drop; twice is a claim
  // that also fell through. This needs no assertion about which of the two it was -- an anchor
  // is free to be unmounted here, and pinning the location would only re-state the declaration.
  const GUARD_NAMES = Object.keys(GUARDS) as WarningGuard[];

  for (const anchor of WARNING_ANCHORS) {
    for (const guard of GUARD_NAMES) {
      // Its own guard's absent branch is the case above, which asserts more than this one.
      if (anchor.guard === guard) continue;
      it(`still renders ${anchor.id}'s warnings, exactly once, with ${guard} not mounted`, async () => {
        const mine = probes(anchor);
        await GUARDS[guard].absent([...mine, catchAll]);

        expect(await screen.findByText(probeText(catchAll))).toBeInTheDocument();
        for (const w of mine) {
          expect(await screen.findByText(probeText(w))).toBeInTheDocument();
          expect(screen.getAllByText(probeText(w))).toHaveLength(1);
        }
      });
    }
  }

  it("renders both of two warnings that are byte-identical", async () => {
    // #146. Two protect conditions on the same movie-only field produce the same sentence,
    // because `ConditionSpec` carries nothing an operator named. Keyed on field+message they
    // collided, and React logged "two children with the same key" -- both still painted under
    // React 19, but the reconciliation guarantee is what rule 19 is about.
    const twin: PolicyWarning = {
      field: "protect_conditions",
      severity: "danger",
      reason: { k: "legacy", p: { text: "anchor probe: one sentence, two rules behind it" } },
    };
    renderEditor({ body: body() }, pace, null, [twin, twin]);

    expect(await screen.findAllByText(probeText(twin))).toHaveLength(2);
  });
});
