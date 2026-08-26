---
paths:
  - "frontend/src/components/ReviewQueue*.ts*"
  - "frontend/src/components/OverrideControls*.tsx"
  - "frontend/src/components/ShowPanel*.tsx"
  - "frontend/src/components/SeasonList*.tsx"
  - "frontend/src/components/reviewFate*.ts"
---

# The review queue: fate, overrides, and the two-level spare

Blockers, not suggestions. **Rule numbers are permanent** (tests and comments cite them); where
two overlap, the more specific governs. Split out of `.claude/rules/frontend.md` because these
seven bind one cluster — the queue, its override controls, the show panel, and the `reviewFate`
helpers they share — and were loading on every Settings, Policy, Plex, and Logs session for
nothing. The SPA's general UI grammar stays in `frontend.md`; rules binding every file are in
the root `CLAUDE.md`. Holds 48–50, 120–123.

**Read this with `frontend.md`, not instead of it.** Rule 51 governs the row layout these
controls sit in and stays there, because the stylesheet cites it eight times.

**48. Reap is dropped wherever the item is already condemned; keep-first colors the pair.** A
hand Reap does nothing to an already-condemned item, so it is hidden in every surface carrying
`OverrideControls` (card, panel, season list, bulk bar) via `hideReap`. `hideReap` judges the
item's OWN verdict (`verdict === "condemn"`), never the tab's, so a mixed season expansion drops
Reap on exactly the condemned rows. The bulk bar is the one exception and keys on the tab verdict
(a heterogeneous selection is not one item). Never reimplement that test inline. Spare is never a
no-op and is never hidden. "Reap now" (the real deletion) is a different control and is never
hidden. Spare invites in green, Reap stays the quiet gray of a plain button until hovered, and a
chosen decision is the solid hand-decision chip.
- **A whole show is not atomic, so it uses its own no-op test.** A movie/season on the Condemned
  lane is fully condemned. A show is on that lane because *some* season is, and a whole-show Reap
  still takes the seasons the scan kept, so both buttons stay until *every* season is condemned.
  That test is `showReapIsNoop` (`components/reviewFate.ts`, re-exported by `ReviewQueue.tsx`),
  the one place it lives. The show card's whole-show control and `ShowPanel` both call it, never
  a fourth inline copy.
- **Every whole-show `hideReap` computation runs over the whole show, every lane.**
  `showReapIsNoop` and `groupReapEffective` take `group.seasons` in the panel and the page's
  per-show rollup (the strip marks, held as `showSeasons`) on the card, never the tab-filtered
  page. The Condemned lane's tab-filtered page holds only the show's condemned seasons, and
  using it would hide the one control that reaps the show's kept seasons. The whole-show
  control's *lit* state is a separate question and is never an aggregate: it reads the show's
  OWN `show_override` (rule 50). `ShowPanel` carries the whole-show Spare/Reap in its own bottom
  `.why-actions` footer, the placement the movie/season panel uses.

**49. A fate-bearing cell colors by the item's fate, never by the scan verdict alone.** The score
badge (`Score`) and the season strip square (`SeasonStrip`) both route color through the one
`handFate` helper (`components/reviewFate.ts`): a hand spare or an *effective* hand reap paints
SOLID ("you chose this"); a reap the engine *can't honor yet* reads **dashed red** (`--condemn`
on `--condemn-soft`, dashed border, never solid) and on the strip also carries a small scythe
corner-mark (`.strip-mark`), so it reads as YOUR ask and never blends into the plain condemned
outline beside it; an untouched cell keeps its scan verdict. **Amber (`--unknown`) means exactly
one thing — "left for you to decide" (the abstain `status-look` chip) — and never a held reap.** A
held reap must never wear the solid red that means "removed," and a hand decision must never leave
the number the color the scan first gave it. Held-reap treatment stays consistent across movies
and seasons through the `.score-refused` / `.strip-ov-reap-refused` / `.status-reap-held` /
`.chip-reap-refused` classes. Never recolor these cells by `verdict` inline:
add the surface to `handFate`, and its class after the scan-verdict classes so it wins.

**50. An override control reflects and acts on its OWN level; the effective (inherited) decision
colors the row but never lights a control.** The whitelist keeps a decision at two levels — a
whole show (its show key) or a single season (its own key), the season's winning — so three views
ride on every candidate, built once in `_candidate_out` / `GroupOut` (`api/review.py`) from the
one `whitelist.effective_override` + `show_key`, never recomputed as a client-side aggregate:
- `override` — the decision *in effect* (own or inherited); colors the chip, score, and strip.
- `override_own` — the item's own decision, and the ONLY value a Spare/Reap control passes to
  `OverrideControls` (a movie's `override_own` equals its `override`).
- `show_override` — the show's own decision, which lights the whole-show control (card +
  `ShowPanel`).

Each control clears the key it lit: a season control clears the season key, and a whole-show
control clears the show key. So it can only ever reverse what it showed. Lighting a control from
effective/aggregate state it *cannot* clear was the dead toggle this rule exists to prevent. When
a whole-show decision keeps or reaps a season, `KeptByShowNote`
(`components/OverrideControls.tsx`) names it beside that season's control. Its wording turns on
whether the season's own decision is absent, the same, or opposite. A season-level clear NEVER
silently un-decides the whole show: that strips protection from every other season, which is
fail-open and forbidden. The grace clock follows the same
effective set (`_sync_grace_clocks` in `api/whitelist.py`), so a scan-condemned item the owner
spares and later un-spares re-enters on a FRESH window, never a spent one (rule 4/71).

## The two-level spare

How a season row reads a spare when its own and its show's overlap.

**120. Precedence answers which decision is read; it never answers what will happen.** A surface
that COLORS a row or asserts its fate reads the *covering* spare (`spare_covers_until`, from
`whitelist.covering_spare_expiry`: own or show, whichever runs longer, forever winning outright).
A control reads the spare in force by precedence (`spare_expires_at`,
`effective_spare_expiry`), because that is the key it toggles and clears. Reading one field for
both jobs drew dashed "expired" over a file a show spare keeps forever, and promised "then Reaper
judges it again" about a re-judgment that changes nothing. A level must be *spared* to contribute
cover, so a season spare lapsing under a show set to REAP still reads expired: there the file
really is handed back. Derive it server-side and put it on every shape that colors,
`GroupSeasonMark` included — threading a show's decision down to each strip square is the
`showReapReaches` bug waiting to happen.

**121. A control that stops being a toggle stops looking like one, in all three signals at
once.** When a press no longer undoes the state shown — a spent spare, whose press now sets a
fresh one — the fill, `aria-pressed`, and the click handler move together off one `pressed` flag.
Never leave a pressed-looking button whose press does something else. The undo it displaced moves
to a surface with room to name it (the length menu's "Clear this spare"), and never just
disappears. A count is how much is LEFT, so "0d" is not a smaller "27d" and must never sit in a
lit button: it read as an active decision with none of itself remaining.

**122. A control that knows only its own level never asserts the item's fate.** The Spare
button's tooltip states what happened to *its* spare and what a press does
(`spareRemaining().expiredOn`), never `note`'s "still kept until the next scan judges it again,"
which is false wherever a show-level spare outlasts it. What is still keeping the file is the
covering spare's question, answered beside it by the row's chip and `KeptByShowNote`. Same reason
a spent spare draws no resting mark: the mark is a decision in force, and that one no longer is.

**123. Every branch a control can clear names what clearing does, in both directions.**
`KeptByShowNote` told the operator "clearing this one won't remove it" when clearing was harmless,
and said nothing when clearing dropped the file onto the reap list. Warning only on the safe side
is backwards for a codebase whose every ambiguity resolves toward keeping the file: a new branch
ships its consequence clause, and the destructive one ships it first.
