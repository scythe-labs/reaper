# The app-wide screen-reader sweep — archived

> **FROZEN 2026-07-29.** The narrative of how Reaper became usable by ear, moved out of
> `docs/STATUS.md` when that file hit its 200-line budget. It is history, not state: what is
> still OPEN lives in `STATUS.md` and in the open `Kind/Accessibility` issues, and what landed
> is in the code. Never edit this file to bring it up to date.
>
> The sweep began as #132 and #147 — one nameless control each, found by hand — and became a
> whole-frontend audit when every hand pass afterwards missed a sibling. The issue numbers below
> are the record of the order things were found in.

## What the sweep landed

#132 and #147 were each one nameless control found by hand, and each hand sweep afterwards
missed a sibling, so the third pass audited the whole frontend rather than the file named in
the issue. Landed: every control that could not
be told apart by ear now names itself, and **the app can speak at all** — there were 109
hand-rolled `.notice` blocks against seven live regions app-wide, none of which was a notice,
so nothing Reaper said after a press was ever announced. All of them now render through one
`Notice` that owns `role="alert"`; `standing` is the declared opt-out for text that is page
furniture rather than a reaction. #155's two draft-refusal twins collapsed into one
`SwitchConfirm` that also moves focus, keyed on a nonce because a repeat press changes no
state and an effect watching the value would not re-fire. A number with a fixed unit now
carries that unit (#176) as the box's *description*, pointing at the suffix already on screen,
so the word exists once and cannot drift and the eleven call sites whose name already says the
unit do not stutter it. **Success is audible (#175)**, which failure already was: Reaper
signalled a save, an add or a connect by something *disappearing* — the savebar unmounting
under the focused button, the modal closing, the composer's boxes emptying — and an absence
cannot be perceived by ear, so a save and a dead button were the same event. `announce()`
(`announce.tsx`) speaks into a region mounted once above every route; it is two alternating
regions because saving twice must say so twice, and a text node that does not change is not
announced. **A refusal says which box it is about (#174)**: `aria-invalid` and
`aria-describedby` appeared zero times app-wide, so a control that would not save had no route
to the sentence explaining it. `WarnBlock` emits the id `fb87ecb` deferred,
`warningsDescribing()` names both ends of the association, and severity moved out of the color
into a word in `Notice`. Bound at the three policy anchors, and at the hex, webhook, password,
ramp and external-URL boxes; the five anchors that warn about a *list* keep ids only, since
binding every child reads the whole card at each of them. `TestBadge` and the job flash chip
carry a word for pass/fail rather than a glyph and a color, but neither sits in a live region:
read when reached, not announced. **The `.why` panels are landed too**, and that sweep found
six rather than the four the issue named. All six now render one `WhyShell` owning the dialog
contract, conditional on `PANEL_OVERLAY_QUERY` (1100px), where the panel stops being a
split-view side column and starts riding over the cards. **#184 closed**: keyed on
`NARROW_SCREEN_QUERY`'s 900px at first, so for 200px of width the panel floated over the side
of the cards the Spare and Reap buttons are on with no focus move and no Tab trap — #171
surviving in a band. The constants stay apart because 900 is the stored meaning of the
operator's Mobile/Desktop choice; a stub answering per query pins which one the shell reads.
Escape moved into the shell, keeping App's
`INPUT/TEXTAREA/SELECT` bail scoped to fields the panel does not own; popovers over a panel
stop the key rather than letting both layers close on one press. **The menus are landed too**:
the spare-length menu, the ＋ Filter menu and the filter chips' value picker each claimed
`role="menu"` or `role="listbox"` and implemented neither contract, so the role told operators
to press arrow keys that did nothing. All three are now plain disclosures with `aria-controls`
and a focus return to their trigger; the chip picker marks the value in force with
`aria-current`, and the portaled spare menu takes focus and keeps Tab inside itself. Each
return is aimed at a control that survives the press closing the menu — the spare caret waits
for its own mutation to settle, the ＋ Filter button hands off to the chip its press created —
since a disabled or unmounted target drops focus to `<body>`. An outside click restores
nothing, on purpose. Scales' "Not in the last scan" tile lost an `aria-expanded` pointing at a
panel App renders in a different subtree.
**#169 closed**: the queue's four card containers were each `role="button"`, whose Children
Presentational pruned every chip, reason, score and season mark inside them out of the
accessibility tree and left one label — and that pruned content is the case for deleting the
file. Movie card, show-card head, season row and Scales person card are plain containers again
and open through one shared `CardOpen` on the title, so a fifth cannot ship without it; the
whole-card click stays as the mouse affordance. It also ends the `nested-interactive`
violation (Spare and Reap were real buttons inside a `role="button"`) and gives the season list
its `listitem`s back. The Enter/Space `stopPropagation` guards on the strip squares, the season
pill and `OverrideControls` went with it: they only ever existed to survive the container
handler that is now gone.
Still filed rather than fixed: the deletion path announces nothing at any stage; a connection
test and a hand-run job say their result to no one; and **116 controls unmount or disable
themselves on their own press**, dropping focus to `<body>` — the app-wide count behind #173.
**The guard is the durable half.** Measured against the real pre-#132 tree,
`eslint-plugin-jsx-a11y` catches none of the filed bugs and costs 112 lockfile entries, an
`overrides` entry to survive `npm ci` on eslint 10, and two audit advisories, so it is not
installed. The gate is `test_repo_hygiene`'s ban on hand-rolled notices with the population
pinned, plus tests that reach controls by the name an operator can hear. No new dependency.
The ban reads the whole `className` value, literal or expression: its first form missed a
ternary and a template literal, so the sweep's own count was short by `ReapPlan`'s plan
loader, which shipped mute while the test read green (rule 145).
