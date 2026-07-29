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
the backend's are in `.claude/rules/backend.md`. Holds 17–20, 36, 39–47, 51, 53, 54, 60–62, 66, 67, 69, 79, 80, 85, 86, 138, 139, 146.

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
their rule (`WARNING_ANCHORS` + `WarnBlock` in `PolicyEditor`); adding a warning field means
adding an anchor, or knowingly letting it fall through to the bottom stack. **Claiming a field is
a promise to RENDER it, because claiming is exactly what excludes it from that stack** — which
therefore catches only what no anchor claims, and can never catch an anchor. So a `WarnBlock`
inside a conditional subtree takes its warning off the page altogether on the branch that subtree
does not mount, rather than down to the bottom, and its anchor names that condition as its
`guard` so it claims only while the condition holds. The warning lost that way was the one about
a setting that lets deletions past the size caps (#145). Both directions are proven in
`PolicyEditor.test.tsx`, never argued here: every anchor is driven through the state its guard
requires, and through every branch it does not name, so a guard that is missing fails as loudly
as one that is wrong (#167). Action failures everywhere are `.notice.notice-error` with a
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
covering two controls, and never help detached from the row it explains.

**138. An anchored popover is measured against the viewport before it is drawn.** Absolutely
placing a popover at `left: 0` inside its anchor is right only while that anchor is far enough
from the right edge — and on a phone it is not: the toolbar wraps, so `＋ Filter` and the last
chip of a row both end up flush against it. What runs past the edge is not clipped and cannot be
scrolled to, because the page has no horizontal scroll, so half a menu is simply unreachable, and
the operator picks from the half they can see. Every anchor-aligned popover therefore takes its
offset from `usePopoverShift` (`components/popoverFit.ts`) and reads it back as `--pop-shift`, and
caps its width so one long value cannot make it wider than the screen. **Measure on every render,
not once on open:** a list that refilters as you type changes width while it is open. The pull is
leftward only and stops at the near gutter — a popover pushed right of its anchor loses the line
back to the control that opened it, and one dragged off the left loses the side every label starts
on. Two popovers answer this another way and are correct as they are: `.user-dropdown` is
`right: 0` in a corner it never leaves, and the spare-length menu clamps its own `position: fixed`
coordinates (`OverrideControls.toggleMenu`). A new popover left-aligned to an anchor with no fit
pass is a blocker, and so is fixing one of these and leaving its twin (rule 72).

**139. Text the operator did not choose is given a break opportunity.** A requester handle, a
title, a path, a host — anything arriving from a portal, a file system, or someone else's
keyboard — can be one long unbroken string, and on a phone it paints straight through the box
holding it. Where the page can scroll, the layout slides sideways; where the container clips (a
side panel, a sheet), the tail is simply unreachable, which is rule 138's failure reached by a
different route. So an element rendering text from outside the app carries `overflow-wrap:
anywhere`, the idiom already at a dozen sites in `index.css`, and the fix lands on every surface
rendering that same value (rule 72): the Scales card's `.fair-name` and the person panel's
`.scales-head-id h2` are one name in two places. **Wrap, do not truncate** — two handles
differing only in their tail truncate to the same string, and the operator reading them is
deciding whose files to delete.

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

**146. A dirty/blocked signal a component reports UPWARD is two claims, and both are checked
in every state the component can render.** Lifting "this panel is holding something" into a
parent so the parent can guard on it asserts *there is something to lose* AND *you can still
get to it*. They are separate facts, they are computed in different places, and one PR broke
them in opposite directions at once. A panel that reported the bar's contents went quiet about
a proxy list its own bar drops on purpose, so the field walked out with no confirm; the same
panel, on a failed refetch, went on reporting a draft while every early return above the render
had replaced the form with one error paragraph, so the guard demanded a discard for edits with
no box, no bar and no Discard on screen. Neither was visible from the diff, because the signal
and the surface read correct on their own lines. So **a hook that reports state upward is
declared above every early return, and every early return is then re-read as a state the report
still fires in** — say what the parent is told while the component renders "loading", while it
renders its failure branch, and after it unmounts. Where the reported set and the acting surface
are deliberately different (a bar must not name a control the operator cannot reach), compute
them apart and say why in the same breath, rather than deriving one from the other and leaving a
comment claiming they cannot disagree. A guard whose signal outlives the surface that satisfies
it is not a guard, it is a trap: the only exit it leaves is the destructive button.
