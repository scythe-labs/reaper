---
paths:
  # Both spellings: the repo root form, and the form seen when a session is
  # launched from inside frontend/ (a common working directory here).
  - "frontend/src/**/*.{ts,tsx,css}"
  - "frontend/index.html"
  - "src/**/*.{ts,tsx,css}"
  - "index.html"
---

# Frontend blockers for `frontend/src/`

Every rule here is a blocker. Rule numbers are permanent, because existing tests and the
archived review passes in `docs/history/` cite them. Where two rules overlap, the more specific
one governs. A code comment states its constraint in plain language and does not cite a
rule number. Rules that bind every file are in the root `CLAUDE.md`. The backend rules are in
`.claude/rules/backend.md`, and the review queue's rules are in `.claude/rules/review-queue.md`.
Holds 17–20, 36, 39–45, 47, 53, 54, 60–62, 66, 67, 69, 79, 80, 85, 86, 138, 139, 146.

## React correctness

**17 / 36. Gating and always-on UI handles both `isPending` and `error` explicitly.** Render an
explicit unknown or error fallback for safety indicators and setup gates. A component whose
contract is "always visible" never returns `null` on a missing or failed query. Every async
onClick is a mutation with a rendered error state.

**19. Stable keys, stable effect dependencies.** A list key comes from a stable server id,
never a display name (rule 63). Use `useRef` for a mutable flag that lives across renders, and
reset state with `useEffect` when a prop changes identity. `src/test/setup.ts` fails a run on
duplicate sibling keys, and eslint `react-hooks/exhaustive-deps` covers effect dependencies.

**20. Independent bulk operations use `Promise.allSettled`, never `Promise.all`.** After the
batch settles, reconcile UI state even when part of it failed. Invalidate the queries, and clear
or retain the selection.

**39. Drafts and dirty checks compare canonical forms.** Never compare serialized state with raw
`JSON.stringify` across the frontend/backend boundary. After a save, re-seed from the server
response.

**60. Interactive children of a keyboard-handling row stop Enter/Space propagation.** A control
nested in a row or card that has its own Enter/Space handler either stops propagation, or the
container checks `e.target === e.currentTarget`. Adding a control to such a row without one of
these is a blocker. Prefer a design with no such row. A `role="button"` container whose key handler
has no such guard cancels every button inside it (#169). A queue card is a plain element with a real control on
its title (`components/CardOpen.tsx`). That control needs no guard, because nothing above it can
cancel it. This rule is for a row that must own a key. It is never permission to build one.

**79. A cache-invalidation helper that claims completeness is grep-verified against every query
key.** A detail panel keyed on a row id is closed or re-resolved when its snapshot is replaced.
Invalidation alone fails there, because the key itself points at superseded data.

**80. Every close affordance runs the modal's close guard.** Browser Back, gestures, and any new
dismissal path honor the same `canClose` that the scrim, Escape, and ✕ honor. A back-layer close
that bypasses a declared guard is a blocker.

**146. A dirty or blocked signal that a component reports upward is two claims, and both are
checked in every state the component can render.** A component lifts "this panel is holding
something" into a parent so the parent can guard on it. That report asserts two facts. There is
something to lose, and the operator can still reach it. The two facts are computed in different
places. Re-read every early return as a state in which the report still fires. State what the
parent is told while the component renders "loading", while it renders its failure branch, and
after it unmounts. Sometimes the reported set and the acting surface differ by design, for
example when a bar must not name a control the operator cannot reach. Then compute them
separately and state why where you compute them. Never derive one from the other and leave a
comment that claims they cannot disagree. A guard whose signal outlives the surface that
satisfies it does not guard. Its only exit is the destructive button.

## Honesty of what the UI asserts

**53. A rendered limit checks its enable switch.** Any UI string, summary clause, or simulator
note that states a cap, budget, or bound branches on the setting that enables enforcement.
Showing the stored figure while the switch is off is a blocker.

**54. A preset that promises enforcement stages the enabling switch too.** Applying a preset
(`components/policyPresets.ts`) sets every switch its help text implies, along with the values
behind each switch.

**61. Prose about a removal consults the effective decision, never the scan verdict alone.** Any
note, chip, or sentence that asserts an item "will be removed" or "will be kept" branches on
`override_effective`. That includes held reaps and opposing season-level decisions. Rule 49 is
the same obligation for color, and rule 77 is the same obligation for backend aggregation.

**62. Every number on the Reap page derives from the planner's exact set.** The headline, the
ledger, and the per-line counts consult the same branches the planner does. That includes the
unknown-size allowance through `useHoldsBackUnmeasured()`. Every stored override state, held
reaps included, appears in the ledger or is explicitly summarized.

**66. Server-defined lists render from the server response.** A hardcoded frontend copy of a
backend id list (jobs, phases, states) is a blocker when the server already returns the list.
Fallback copy handles unknown ids only. A map from those ids to operator copy is the same
mirror, and rule 103's drift guard binds it. Rule 103 lives in `.claude/rules/backend.md`, which
never loads for the file that carries the copy, so the obligation is stated here too. Derive the
map's keys from one declaration, or make a test fail when the set changes. `gateMeta` is the
worked case. `policyMeta.ts` types it off a `GateId` union, and `test_api_type_mirror.py` pins
that union against the enum. `jobMeta` (`JobsPanel.tsx`) is guarded by `test_repo_hygiene.py`,
and `REPAIR_NOTICES` (`PolicyEditor.tsx`) by `test_policy_repairs.py`. Do not count on the
fallback here. The fallback is what makes a missing member silent (#551). `signalMeta`,
`RAMP_SPECS` (`signalRamp.ts`) and `BUILTIN_SIGNAL_IDS` have the same shape and have no guard
yet. `phaseLabel` (`ScanBar.tsx`) mirrors the phase list in `src/reaper/api/scan.py` and has no
guard either.

**85. Success copy fires on settled state.** A toast, timestamp, or "done" indicator is set only
after the operation it describes has completed (refetch settled, final chunk streamed). It is
never set when the operation is issued.

**86. Copy describing a clock, zone, or schedule renders the effective stored setting.** That is
the setting that governs it, never a static guess about the deployment.

## UI grammar

The whole UI uses one control grammar, so each job is done one way. A new variant of any control
below is a blocker.

**18. Reuse the existing shared component, token, or pattern.** This includes tabs, segmented
controls, notices, loading affordances, form-field labels, confirmation dialogs, CSS success and
accent colors, and modal sizing (`dvh` on mobile). Never introduce a parallel one-off
implementation, an undefined CSS variable, a native `confirm()`, or white-on-`--accent` text
that fails WCAG AA.

**40. A number with a unit is always one of two components.** A changeable unit uses
`QuantityInput`. A fixed unit ("days", "people", "seasons", "/ 10", "%", "+ votes") uses
`FixedQuantity`, with the unit as a suffix inside the same box. Both live in
`components/QuantityInput.tsx` and share the `.qty` chrome. Never put a bare
`<input type="number">` beside loose unit text. Never add a new input size. Every text, number,
and select box follows the one control standard in `styles/00-tokens.css`, which sets
`--control-pad` padding, `--border-strong`, `--radius-sm`, the `--bg` fill, and the accent focus
ring. Width is the only thing that may vary.

**41. A choice between two visible options is the shared `Segmented`**
(`components/Segmented.tsx`). A `<select>` is only for open lists (rating sources, fields,
units, servers, log levels). A segmented control that grows past three options may become a
select. A binary choice never hides inside a dropdown. `Switch` stays the one on/off control. A
settings-bearing group renders its sub-controls only while its toggle is on. While the toggle is
off they are hidden, never disabled, matching the gates.

**42. A warning renders beside the control that fixes it.** Policy warnings anchor by `field` to
their rule, through `WARNING_ANCHORS` and `WarnBlock` in `PolicyEditor`. A new warning field
gets an anchor, or you let it fall through to the bottom stack on purpose. An anchor that claims
a field must render it, because the claim is what removes the field from the bottom stack. That
stack catches only what no anchor claims, so it never catches a warning an anchor claimed and
did not render. A `WarnBlock` inside a conditional subtree therefore takes its warning off the
page entirely on any branch where that subtree does not mount. Its anchor names that condition
as its `guard`, so it claims the field only while the condition holds (#145).
`PolicyEditor.warnings.test.tsx` proves both directions. It drives every anchor through the
state its guard requires and through every branch the guard does not name, so a missing guard
fails as loudly as a wrong one (#167).
Action failures render `<Notice tone="error">` (`components/Notice.tsx`) with a plain-language
lead ("The scan didn't start: …"). A hand-written `.notice` is banned, and
`test_every_notice_goes_through_the_one_component_that_announces_it` fails on one. Bare red
`.error` text stays only in the review surfaces and in the simulator's dedicated failure panel.

**43. One save affordance per page.** The Policy editor's sticky `.savebar` is that page's one
save affordance. It names what is dirty, states when each part takes effect, saves everything
with one click, and offers Discard. General settings has its own `.savebar`
(`components/GeneralPanel.tsx`). New savable state joins its page's bar. Never add a second
Save button beside one.

**44. Settings-bearing groups are cards, and plain toggles are rows.** A protection or group
with sub-configuration is a `.rules-card` with its `Switch` in `.card-head`, as the rating floor
and the rewatch keep cards in `PolicyEditor.tsx` are. The season card shares the container. An
on/off with nothing else stays a bare `.rule-row`. Rows of repeated per-item controls align in
one grid with a shared label column, never one boxed well per row.

**45. Help text binds to exactly one control, directly beneath it.** Never write one help
paragraph that covers two controls. Never detach help from the row it explains.

**138. An anchored popover is measured against the viewport before it is drawn.** A popover
placed absolutely at `left: 0` inside its anchor fits only while the anchor sits far enough from
the right edge. On a phone the toolbar wraps until `＋ Filter` sits flush against that edge.
Whatever runs past the edge is unreachable, because the page has no horizontal scroll. Every
anchor-aligned popover takes its offset from `usePopoverShift` (`components/popoverFit.ts`) and
reads it back as `--pop-shift`. It also caps its width, so one long value cannot make it wider
than the screen. Measure on every render, never only once on open, because a list that
refilters as you type changes width while it is open. The shift pulls leftward only and stops at
the near gutter. A popover pushed right of its anchor loses its visual link to the control that
opened it. A popover dragged off the left edge loses the side every label starts on. Two
popovers use another method and are correct as they are. `.user-dropdown` is `right: 0` in a
corner it never leaves. The spare-length menu and the collection picker both clamp their own
`position: fixed` coordinates through the shared `useFixedMenu` hook
(`components/popoverFit.ts`). A new popover left-aligned to an anchor with no fit pass is a
blocker. Fixing one of these popovers and leaving its twin is a blocker too (rule 72).

**139. Text the operator did not choose is given a break opportunity.** A requester handle, a
title, a path, or a host can arrive from a portal, a file system, or someone else's keyboard as
one long unbroken string. On a phone that string paints through the box that holds it. A
scrolling page slides sideways. A clipping container, such as a side panel or a sheet, makes the
tail unreachable, which is rule 138's failure by a different route. An element that renders text
from outside the app carries `overflow-wrap: anywhere`. Wrap the text, never truncate it. Two
handles that differ only in their tail truncate to the same string, and the operator reading
them is deciding whose files to delete. A new surface adds a row to the `SITES` table in
`index-outside-text.test.ts`, which checks every surface listed there. The fix lands on every
surface that renders the same value (rule 72). The Scales card's `.fair-name` and the person
panel's `.scales-head-id h2` are one name in two places.

**67. Values coupled across TSX and CSS derive from one declaration.** A width, gap, or count
that must agree between a component and a stylesheet lives in one custom property that both
read, or both sites carry a cross-reference comment. The `--btns` track (rule 51) is the model,
and the rule covers every such value.

**69. The icon link the app rewrites at runtime is declared last in `index.html`.** Static
fallback icons precede the dynamic one. `test_dynamic_favicon_link_is_declared_last` fails on an
icon link after `#favicon`.

## The queue's action grammar

This rule sets how a queue card shows hover and selection. The Spare/Reap row grammar is in
`.claude/rules/review-queue.md`.

**47. Card hover is the accent, additive on the open card.** A card's hover is the accent edge,
never gray. On hover, the open (`card-selected`) card keeps its accent selection bar and deepens
it. It never trades the bar for the plain hover, which reads as a deselect. Any `:hover` that
can also be `.active` or selected re-asserts the selected treatment at equal or higher
specificity.
