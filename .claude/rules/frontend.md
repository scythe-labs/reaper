---
paths:
  # Both spellings: the repo root form, and the form seen when a session is
  # launched from inside frontend/ (a common working directory here).
  - "frontend/src/**/*.{ts,tsx,css}"
  - "frontend/index.html"
  - "src/**/*.{ts,tsx,css}"
  - "index.html"
---

# Frontend blockers — `frontend/src/`

Blockers, not suggestions. **Rule numbers are permanent** (tests cite them); where two
overlap, the more specific governs. Rules binding every file are in the root `CLAUDE.md`;
the backend's are in `.claude/rules/backend.md`. Holds 17–20, 36, 39–51, 53–54, 60–62,
66–67, 69, 79–80, 85–86.

## React correctness

**17 / 36. Gating and always-on UI handles `isPending` AND `error` explicitly.** Render an
explicit unknown/error fallback for safety indicators and setup gates. `return null` on a
missing or failed query, for a component whose contract is "always visible," is a blocker — and
every async onClick is a mutation with a rendered error state.

**19. Stable keys, stable effect dependencies.** List keys unique among siblings and drawn
from a stable server id (never a display name, rule 63), memoized arrays, `useRef` for
cross-render mutable flags, and `useEffect` resets on identity-changing props. Never key on a
value shared by sibling rows, or depend an effect on a freshly-allocated array.

**20. `Promise.allSettled`, not `Promise.all`, for independent bulk operations,** then
reconcile UI state (invalidate queries, clear/retain selection) regardless of partial failure.

**39. Drafts and dirty checks compare canonical forms.** Never compare serialized state with
raw `JSON.stringify` across the frontend/backend boundary; re-seed from the server response
after a save.

**60. Interactive children of a keyboard-handling row stop Enter/Space propagation.** Any
control nested inside a row or card with its own Enter/Space handler either stops propagation
(the `SeasonStrip` guard is the model) or the container checks
`e.target === e.currentTarget`. Adding a control to such a row without this is a blocker.

**79. A cache-invalidation helper claiming completeness is grep-verified against every query
key, and a detail panel keyed on a row id is closed or re-resolved when its snapshot is
replaced.** Invalidation alone is insufficient when the key itself points at superseded data.

**80. Every close affordance runs the modal's close guard.** Browser Back, gestures, and any
new dismissal path honor the same `canClose` the scrim/Escape/✕ honor; a back-layer close that
bypasses a declared guard is a blocker.

## Honesty of what the UI asserts

**53. A rendered limit checks its enable switch.** Any UI string, summary clause, or simulator
note stating a cap, budget, or bound branches on the setting that enables enforcement. Showing
the stored figure while the switch is off is a blocker.

**54. A preset that promises enforcement stages the enabling switch too.** Applying a preset
(`components/policyPresets.ts`) sets every switch its help text implies, not just the values
behind the switch.

**61. Prose about a removal consults the effective decision, not just the scan verdict.** Any
note, chip, or sentence asserting an item "will be removed" or "will be kept" branches on
`override_effective`, including held reaps and opposing season-level decisions. (Rule 49 is the
same obligation for color; rule 77 for backend aggregation.)

**62. Every number on the Reap page derives from the planner's exact set.** Headline, ledger,
and per-line counts consult the same branches the planner does, including the unknown-size
allowance via `useHoldsBackUnmeasured()`, and every stored override state — held reaps included
— appears in the ledger or is explicitly summarized.

**66. Server-defined lists render from the server response.** A hardcoded frontend copy of a
backend id list (jobs, phases, states) is a blocker when the server already returns the list;
fallback copy handles unknown ids only.

**85. Success copy fires on settled state.** A toast, timestamp, or "done" indicator is set only
after the operation it describes has actually completed (refetch settled, final chunk streamed),
never at issuance.

**86. Copy describing a clock, zone, or schedule renders the effective stored setting** — the
setting that governs it, not a static guess about the deployment.

## UI grammar

The whole UI speaks one control grammar. These came from an audit that found the same job done
three or four ways on one page. A new variant of any of them is a blocker, not a style choice.

**18. Reuse the existing shared component / token / pattern** — tabs, segmented controls,
notices, loading affordances, form-field labels, confirmation dialogs, CSS success/accent
colors, modal sizing (`dvh` on mobile). Never introduce a parallel one-off implementation, an
undefined CSS variable, a native `confirm()`, or white-on-`--accent` text that fails WCAG AA.

**40. A number with a unit is one of two components, always.** A changeable unit is
`QuantityInput`; a fixed unit ("days", "people", "seasons", "/ 10", "%", "+ votes") is
`FixedQuantity` with the unit as a suffix in the same box (both in
`components/QuantityInput.tsx`, sharing the `.qty` chrome). Never a bare `<input type="number">`
beside loose unit text, and never a new input size: every text, number, and select box sits on
the one control standard documented at the top of `index.css` (`0.42rem 0.6rem` padding,
`--border-strong`, `--radius-sm`, `--bg` fill, accent focus ring). Width is the only thing that
may vary.

**41. A choice between two visible options is the shared `Segmented`**
(`components/Segmented.tsx`). A `<select>` is only for open lists (rating sources, fields,
units, servers, log levels); growing a segmented past three options may turn it into a select,
but hiding a binary inside a dropdown is never allowed. `Switch` stays the one on/off control,
and a settings-bearing group's sub-controls render only while its toggle is on — hidden, not
disabled, matching the gates.

**42. A warning renders beside the control that fixes it.** Policy warnings anchor by `field` to
their rule (the anchor list + `WarnBlock` in `PolicyEditor`); adding a warning field means adding
an anchor, or knowingly letting it fall through to the bottom stack, which exists so no field is
ever silently dropped. Action failures everywhere are `.notice.notice-error` with a
plain-language lead ("The scan didn't start: …"); bare red `.error` text survives only in the
review surfaces and the simulator's dedicated failure panel.

**43. One save affordance per page.** The policy editor's sticky `.savebar` is the only save UI
on that page: it names what is dirty, states when each part takes effect, saves everything with
one click, and offers Discard. New savable state on that page joins the bar; never add a second
Save button beside it.

**44. Settings-bearing groups are cards; plain toggles are rows.** A protection or group with
sub-configuration is a `.rules-card` with its `Switch` in `.card-head` (rating bars, keep-tags;
the season card shares the container); an on/off with nothing else stays a bare `.rule-row`.
Rows of repeated per-item controls align in one grid with a shared label column, never one boxed
well per row.

**45. Help text binds to exactly one control, directly beneath it.** Never one help paragraph
covering two controls, and never help detached from the row it explains. *Known deferred
exception:* the `.warn` banner (ScanBar + the review card) merges into `.notice-warn` whenever
the review UI is next touched.

**67. Values coupled across TSX and CSS derive from one declaration.** A width, gap, or count
that must agree between a component and a stylesheet lives in one custom property both read, or
both sites carry a cross-reference comment. The `--btns` track (rule 51) is the model, not the
only case.

**69. The icon link the app rewrites at runtime is declared last in `index.html`.** Static
fallback icons precede the dynamic one; adding an icon link after `#favicon` is a blocker.

## The queue's action grammar

How a queue row presents its Spare/Reap decision, settled over a run of approved mockups and
driven end-to-end in a real browser — which is where the bug behind rule 46 surfaced, in Safari
alone.

**46. Row actions reveal on hover; a decided row rests as its icon.** The per-row Spare/Reap
(`OverrideControls`) is hidden until the row is hovered or keyboard-focused, kept in flow with
`visibility`, never `display`, so nothing reflows. A row carrying a hand override rests as a
small icon of it (`OverrideMark`: ∞ spared, scythe reaped) in the buttons' slot, faded out by the
same hover. Never park the full buttons on every row at rest, and never show the icon and the
buttons at once. Give the toggling buttons a **fixed width** so a label change (Spare↔Spared,
Reap↔Reaping) never resizes them: a shrinking button left a red ghost in the region it vacated,
in Safari only. A **spent** spare is not a decision at rest and draws no mark at all (rule
122): the row rests bare, and its button offers a fresh spare.

**47. Card hover is the accent, additive on the open card.** A card's hover is the accent edge,
not gray; the open (`card-selected`) card keeps its accent selection bar on hover and deepens it,
never trading it for the plain hover, which reads as a deselect. Any `:hover` that can also be
`.active`/selected re-asserts the selected treatment at equal-or-higher specificity.

**48. Reap is dropped wherever the item is already condemned; keep-first colors the pair.** A
hand Reap does nothing to an already-condemned item, so it is hidden in every surface carrying
`OverrideControls` (card, panel, season list, bulk bar) via `hideReap`, judged by the item's OWN
verdict (`verdict === "condemn"`), never the tab's — so a mixed season expansion drops Reap on
exactly the condemned rows. The bulk bar is the one exception and keys on the tab verdict (a
heterogeneous selection is not one item). Never reimplement that test inline. Spare is never a
no-op and is never hidden; "Reap now" (the real deletion) is a different control and is never
hidden. Spare invites in green, Reap stays the quiet gray of a plain button until hovered, and a
chosen decision is the solid hand-decision chip.
- **A whole show is not atomic, so it uses its own no-op test.** A movie/season on the Condemned
  lane is fully condemned. A show is on that lane because *some* season is, and a whole-show Reap
  still takes the seasons the scan kept, so both buttons stay until *every* season is condemned.
  That test is `showReapIsNoop` (`components/ReviewQueue.tsx`), the one place it lives; the show
  card's whole-show control and `ShowPanel` both call it, never a fourth inline copy.
- **Every whole-show `hideReap` computation runs over the whole show, every lane.**
  `showReapIsNoop` and `groupReapEffective` take `group.seasons` in the panel and `group_seasons`
  (the strip marks, held as `showSeasons`) on the card, never the tab-filtered page — which on the
  Condemned lane holds only the show's condemned seasons, and would hide the one control that
  reaps the show's kept seasons. The whole-show control's *lit* state is a separate question and
  is never an aggregate: it reads the show's OWN `show_override` (rule 50). `ShowPanel` carries
  the whole-show Spare/Reap in its own bottom `.why-actions` footer, the placement the
  movie/season panel uses.

**49. A fate-bearing cell colors by the item's fate, never by the scan verdict alone.** The score
badge (`Score`) and the season strip square (`SeasonStrip`) both route color through the one
`handFate` helper (`components/ReviewQueue.tsx`): a hand spare or an *effective* hand reap paints
SOLID ("you chose this"); a reap the engine *can't honor yet* reads **dashed red** (`--condemn`
on `--condemn-soft`, dashed border, never solid) and on the strip also carries a small scythe
corner-mark (`.strip-mark`), so it reads as YOUR ask and never blends into the plain condemned
outline beside it; an untouched cell keeps its scan verdict. **Amber (`--unknown`) means exactly
one thing — "left for you to decide" (the abstain `status-look` chip) — and never a held reap.** A
held reap must never wear the solid red that means "removed," and a hand decision must never leave
the number the color the scan first gave it. Held-reap language stays consistent across movies and
seasons: a movie carries the scythe via its resting `OverrideMark`, the strip square via the
corner-mark, and both wear the dashed-red `.score-refused` / `.strip-ov-reap-refused` /
`.status-reap-held` / `.chip-reap-refused` classes. Never recolor these cells by `verdict` inline:
add the surface to `handFate`, and its class after the scan-verdict classes so it wins.

**50. An override control reflects and acts on its OWN level; the effective (inherited) decision
colors the row but never lights a control.** The whitelist keeps a decision at two levels — a
whole show (its show key) or a single season (its own key), the season's winning — so three views
ride on every candidate, built once in `_candidate_out` / `GroupOut` (`api/routes.py`) from the
one `whitelist.effective_override` + `show_key`, never recomputed as a client-side aggregate:
- `override` — the decision *in effect* (own or inherited); colors the chip, score, and strip.
- `override_own` — the item's own decision, and the ONLY value a Spare/Reap control passes to
  `OverrideControls` (a movie's `override_own` equals its `override`).
- `show_override` — the show's own decision, which lights the whole-show control (card +
  `ShowPanel`).

Each control clears the key it lit — a season control the season key, a whole-show control the
show key — so it can only ever reverse what it showed. Lighting a control from effective/aggregate
state it *cannot* clear was the dead toggle this rule exists to prevent. When a whole-show decision
keeps or reaps a season, `KeptByShowNote` (`components/ReviewQueue.tsx`) names it beside that
season's control, its wording turning on whether the season's own decision is absent, the same, or
opposite. A season-level clear NEVER silently un-decides the whole show: that strips protection
from every other season, which is fail-open and forbidden. The grace clock follows the same
effective set (`_sync_grace_clocks` in `api/whitelist.py`), so a scan-condemned item the owner
spares and later un-spares re-enters on a FRESH window, never a spent one (rule 4/71).

**51. Row actions align in fixed columns; the size holds still; every season is actable in
place.** In the expanded season list (`SeasonList`) Spare keeps the left button column and Reap
the right, both to the LEFT of a right-pinned size in its own column, so buttons and sizes read as
straight columns whatever a row's button count. The button track and the size track are FIXED
width, never `auto`: each `.season-row` is its own grid, so an `auto` track sizes to that row's own
text and drifts the buttons row to row. The button track is the `--btns` custom property, set once
per list by `SeasonList` from whether any season there can show Reap (any non-condemned one), so a
show condemned top to bottom reserves no empty Reap slot; `.season-row .override-controls` is
`justify-self: start` so a lone Spare lands in the same column as a paired row's Spare. Every
season row is actable from here — the old read-only "other-lane" row and its edge marker are gone —
each with its own `OverrideControls` keyed to `override_own` (rule 50) and `hideReap` from that
season's own verdict (rule 48). A per-tab `hideReap` on a list row, a read-only season row, or an
`auto` button/size track that lets the columns drift is a regression.

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
