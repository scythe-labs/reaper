---
paths:
  - "frontend/src/components/ReviewQueue*.ts*"
  - "frontend/src/components/OverrideControls*.tsx"
  - "frontend/src/components/ShowPanel*.tsx"
  - "frontend/src/components/SeasonList*.tsx"
  - "frontend/src/components/reviewFate*.ts"
  - "frontend/src/styles/24-overrides.css"
  - "frontend/src/styles/25-select-mode.css"
---

# The review queue: fate, overrides, and the two-level spare

Every rule here is a blocker. Rule numbers are permanent, because existing tests and the
archived review passes in `docs/history/` cite them. Where two rules overlap, the more specific
one governs. A code comment states its constraint in plain language and does not cite a
rule number. These rules bind the queue, its override controls and their stylesheets, the show
panel, and the `reviewFate` helpers they share. The SPA's general UI grammar is in
`.claude/rules/frontend.md`. Rules that bind every file are in the root `CLAUDE.md`.
Holds 46, 48–51, 120–123.

Read this file with `frontend.md`, never instead of it. Card hover (rule 47) stays in
`frontend.md`.

**48. Reap is dropped wherever the item is already condemned, and keep-first colors the pair.**
A hand Reap does nothing to an already-condemned item. So `hideReap` hides it in every surface
that carries `OverrideControls` (card, panel, season list, bulk bar). `hideReap` comes from
`reapIsNoop` (`components/reviewFate.ts`). It is true when the item's own verdict condemns it
and the item is not spared. It never judges the tab's verdict, so a mixed season expansion drops
Reap on exactly the condemned, unspared rows. The bulk bar is the one exception. It keys on the
tab verdict, because a heterogeneous selection is not one item. Never reimplement that test
inline. Spare is never a no-op and is never hidden. "Reap now" (the real deletion) is a
different control and is never hidden. Spare invites in green. Reap stays the quiet gray of a
plain button until hovered. A chosen decision is the solid hand-decision chip.
- **A whole show is not atomic, so it uses its own no-op test.** A movie or season on the
  Condemned lane is fully condemned. A show is on that lane because some season is, and a
  whole-show Reap still takes the seasons the scan kept. So both buttons stay until every season
  is condemned. That test is `showReapIsNoop` (`components/reviewFate.ts`, re-exported by
  `ReviewQueue.tsx`), and it lives in that one place. The show card's whole-show control and
  `ShowPanel` both call it. Never write a fourth inline copy.
- **Every whole-show `hideReap` computation runs over the whole show, every lane.**
  `showReapIsNoop` and `groupReapEffective` take `group.seasons` in the panel. On the card they
  take the page's per-show rollup (the strip marks, held as `showSeasons`). They never take the
  tab-filtered page. The Condemned lane's tab-filtered page holds only the show's condemned
  seasons, so using it hides the one control that reaps the show's kept seasons. The whole-show
  control's lit state is a separate question and is never an aggregate. It reads the show's own
  `show_override` (rule 50). `ShowPanel` carries the whole-show Spare/Reap in its own bottom
  `.why-actions` footer, the placement the movie and season panel uses.

**49. A fate-bearing cell colors by the item's fate, never by the scan verdict alone.** The score
badge (`Score`) and the season strip square (`SeasonStrip`) both route color through the one
`handFate` helper (`components/reviewFate.ts`). A hand spare or an effective hand reap paints
solid, meaning "you chose this." A reap the engine cannot honor yet reads dashed red, with
`--condemn` on `--condemn-soft` and a dashed border, never solid. On the strip it also carries a
small scythe corner-mark (`.strip-mark`). So it reads as the operator's own ask and never blends
into the plain condemned outline beside it. An untouched cell keeps its scan verdict. On a
fate-bearing cell, amber (`--unknown`) means "left for you to decide" (the abstain `status-look`
chip). It never marks a held reap. A held reap never wears the solid red that means
"removed." A hand decision never leaves the number in the color the scan first gave it. The
`.score-refused`, `.strip-ov-reap-refused`, `.status-reap-held`, and `.chip-reap-refused`
classes keep the held-reap treatment consistent across movies and seasons. Never recolor these
cells inline by `verdict`. Add the surface to `handFate`, and place its class after the
scan-verdict classes so it wins.

**50. An override control reflects and acts on its own level, and the effective (inherited)
decision colors the row but never lights a control.** The whitelist keeps a decision at two
levels, a whole show (its show key) or a single season (its own key). The season's decision
wins. Every candidate carries three views of it. `_candidate_out` and `GroupOut`
(`api/review.py`) build them once from the one `whitelist.effective_override` and `show_key`.
They are never recomputed as a client-side aggregate.
- `override` is the decision in effect, own or inherited. It colors the chip, score, and strip.
- `override_own` is the item's own decision. It is the only value a Spare/Reap control passes to
  `OverrideControls`. A movie's `override_own` equals its `override`.
- `show_override` is the show's own decision. It lights the whole-show control (card and
  `ShowPanel`).

Each control clears the key it lit. A season control clears the season key, and a whole-show
control clears the show key. So a control only ever reverses what it showed. Never light a
control from effective or aggregate state that it cannot clear, because a press on it cannot
undo what it shows. When a whole-show decision keeps or reaps a season, `KeptByShowNote`
(`components/OverrideControls.tsx`) names that decision beside the season's control. Its wording
turns on whether the season's own decision is absent, the same, or opposite. A season-level
clear never silently un-decides the whole show. That strips protection from every other season,
which fails open and is forbidden. The grace clock follows the same effective set
(`_sync_grace_clocks` in `api/whitelist.py`). A scan-condemned item that the owner spares and
later un-spares re-enters on a fresh window, never a spent one (rule 4/71).

## The queue's action grammar

These rules set how a queue row presents its Spare/Reap decision.

**46. Row actions reveal on hover, and a decided card rests as its icon.** The per-row
Spare/Reap (`OverrideControls`) stays hidden until the row is hovered or keyboard-focused. It
stays in flow through `visibility`, never `display`, so nothing reflows. A card that carries a
hand override rests as a small icon of it in the buttons' slot (`OverrideMark`). A forever spare
rests as ∞, a timed spare as a clock with its remaining days, and a reap as a scythe. The same
hover fades the icon out. A season row draws no resting mark. Never leave the full buttons on
every row at rest. Never show the icon and the buttons at once. A touch screen (`hover: none`)
has no hover, so there the buttons show at rest and the mark is dropped. Give the toggling
buttons a fixed `min-width` (`--ov-btn-w`), so a label change (Spare↔Spared, Reap↔Reaping) never
resizes them. A shrinking button leaves a red ghost in the region it vacated, in Safari only. A
spent spare is not a decision at rest, so it draws no mark at all (rule 122). The card rests
bare, and its button offers a fresh spare.

**51. Row actions align in fixed columns, the size holds still, and every season is actable in
place.** In the expanded season list (`SeasonList`), Spare keeps the left button column and Reap
keeps the right. Both sit to the left of a right-pinned size in its own column, so buttons and
sizes read as straight columns whatever a row's button count. The button track and the size
track have a fixed width, never `auto`. Each `.season-row` is its own grid, so an `auto` track
sizes to that row's own text and the buttons drift from row to row. The button track is the
`--btns` custom property. `SeasonList` sets it once per list, from whether any season in the
list can show Reap (`!reapIsNoop`, `components/reviewFate.ts`), so a spared condemned season
still counts. A show whose every season is condemned and unspared then reserves no empty Reap
slot. `.season-row .override-controls` is `justify-self: start`, so a lone Spare lands in the
same column as a paired row's Spare. Every season row is actable from this list. Each row has
its own `OverrideControls`, keyed to `override_own` (rule 50), with `hideReap` from that
season's own `reapIsNoop` (rule 48). A per-tab `hideReap` on a list row, a read-only season row,
or an `auto` button or size track that lets the columns drift is a regression.

## The two-level spare

These rules set how a season row reads a spare when its own spare and its show's spare overlap.

**120. Precedence answers which decision is read, and it never answers what will happen.** A
surface that colors a row or asserts its fate reads the covering spare. That is
`spare_covers_until`, from `whitelist.covering_spare_expiry`. It takes the own or the show
spare, whichever runs longer, and a forever spare wins outright. A control reads the spare in
force by precedence (`spare_expires_at`, `effective_spare_expiry`), because that is the key it
toggles and clears. Never read one field for both jobs. Reading one field for both draws dashed "expired" over a
file that a show spare keeps forever. It also promises "then Reaper judges it again" about a
re-judgment that changes nothing. A level must be spared to contribute cover. So a season spare
that lapses under a show set to reap still reads expired, because there the file really is
handed back. Derive the covering spare server-side and put it on every shape that colors,
`GroupSeasonMark` included. Never thread a show's decision down to each strip square. A
client-side copy of the derivation can miss a spare state added later.

**121. A control that stops being a toggle stops looking like one, in all three signals at
once.** When a press no longer undoes the state shown, the fill, `aria-pressed`, and the click
handler move together off one `pressed` flag. A spent spare is that case, because its press now
sets a fresh spare. Never leave a pressed-looking button whose press does something else. The
undo it displaced moves to a surface with room to name it (the length menu's "Clear this
spare"). It never just disappears. A count is how much is left, so "0d" is not a smaller "27d".
Never show "0d" in a lit button. There it reads as an active decision with none of itself
remaining.

**122. A control that knows only its own level never asserts the item's fate.** The Spare
button's tooltip states what happened to its own spare and what a press does
(`spareRemaining().expiredOn`). It never uses `note`'s "still kept until the next scan judges it
again," which is false wherever a show-level spare outlasts the button's spare. The covering
spare answers what still keeps the file. The row's chip and `KeptByShowNote` show that answer
beside the button. For the same reason a spent spare draws no resting mark. The mark shows a
decision in force, and a spent spare is no longer one.

**123. Every branch a control can clear names what clearing does, in both directions.**
`KeptByShowNote` names the harmless branch ("clearing this one won't remove it") and the branch
where clearing drops the file onto the reap list. A warning on the safe side only is backward,
because every ambiguity resolves toward keeping the file. A new branch ships its consequence
clause, and the destructive branch ships it first.
