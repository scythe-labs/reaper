# Splitting `index.css`: a stylesheet that outgrew one document

> **PROPOSED — 0 of 7 stages landed.** Nothing here has been executed. Stage 1 is mechanical and
> provably safe; every later stage is optional and independently gated. Approve or amend before
> anyone starts. When Stage 1 lands, this file gets its `STATUS.md` open-work line and its row in
> `docs/README.md`'s map moves from *proposed* to *live*.

Written 2026-07-29 against `frontend/src/index.css` at 9,075 lines.

## 1. Why now

`index.css` is not decayed. It is one of the better-kept files in the tree: **one** `!important`,
**zero** duplicated selectors, twenty-nine selectors at four-or-more compound parts, and a
comment on nearly every non-obvious rule. Nothing here is a rescue.

The problem is size, and the shape of the growth:

| Measure | Value |
| --- | --- |
| Lines | 9,075 (3,109 on 2026-07-15 — **+425/day** over 14 days) |
| Lifetime churn | +10,325 / −1,250 — **89% of all edits are additions** |
| Commits touching it | 109 of 625 — about **one commit in six** |
| Rule blocks | 1,259 |
| `@media` blocks | 39, all inline beside the rule they modify |
| Documented source-order dependencies | **18** (see §5) |

The accretion figure is the one that matters. Nothing ever leaves this file, which is precisely
the failure `docs/README.md` already recorded for the old living plan: *"adding a note meant
first reading enough of it to find where the note went, and that cost grew every day."* That
document was 3,508 lines when it broke. This one is 9,075 and adds another 425 a day.

The second-order costs are already visible in the file itself. Review-queue styling sits in
**two non-adjacent regions** 2,800 lines apart (1470–1744 and 4591–6426, 2,111 lines combined).
A banner reading `Setup wizard` covers **1,179 lines of five unrelated features**, including the
shared modal shell, the app-wide reap bar, and the entire in-app documentation viewer. `.notice`
— the app's most-rendered primitive, at 109 call sites — has its base rule at line 3351, inside
the *simulator* section. `.qty`, the control rule 40 names as one of two ways to render a number,
is filed under the setup wizard at line 8041. None of these are bugs. They are what happens when
the only available filing cabinet is "append."

## 2. What best practice says, and what it says here

Four mainstream options, and why three of them are wrong for this codebase specifically:

**Utility-first (Tailwind) — rejected.** The file's own preamble is a charter against it:
*"Semantic classes, not utilities. The point of this file is that the visual language carries
meaning the copy would otherwise have to repeat"* — condemn is red, protect is green, and a
protection that could **not** be checked is amber and dashed, *"because 'we could not look' is not
'we looked and it was fine', and rendering them alike is the exact bug that gets libraries
deleted during an API outage."* Utilities dissolve exactly the distinction that paragraph exists
to protect. This is a safety argument, not a taste one.

**CSS Modules — rejected as a wholesale move.** Local scoping is the opposite of this app's
contract. Rules 18, 40, 41 and 44 mandate a *shared* control grammar, and the sharing is real:
`.notice` renders at 109 sites, `.qty` at 34, `.segmented` at 10. Worse, the shared vocabulary is
funnelled through wrapper components (`Notice.tsx`, `QuantityInput.tsx`, `Segmented.tsx`), so
"one CSS file per component" would file the design system under whichever component happens to
own the wrapper. And there is **no bare `.btn` class** — `.primary`, `.ghost`, `.danger`, `.sm`
are modifiers on a plain `<button>`, so the element selector carries the base for all 49
components. Modules would force `:global` on the interesting half of the stylesheet, touch every
component, and produce no user-visible gain.

**CSS-in-JS — rejected.** A new runtime dependency on an app that ships in one container and
loads nothing from a CDN.

**Plain CSS, cut into an ordered stack behind one barrel of `@import` — recommended.** Zero new
dependencies, no component touched, and — the decisive property — **provably identical output**.

### `@layer`: considered, deferred

`@layer` is the modern answer to §5's fragility and would retire it outright. It is still the
wrong first move. Layers beat specificity across layer boundaries, so introducing them to 1,259
hand-tuned rules changes the resolution of every conflict at once, and the failure mode is a
*silent* visual regression on the UI that authorizes deletions. Revisit per-file once the split
has settled, where the blast radius of getting a layer wrong is one feature rather than the app.

## 3. The property that makes this safe

**Vite 8 inlines `@import` in declaration order, byte for byte.** Verified, not assumed — built a
three-file fixture through this repo's own pinned Vite with a base rule, a media-query override,
and a later same-specificity winner:

```
.probe { color: red; }
@media (max-width: 640px) { .probe { color: blue; } }
.probe { color: green; }
```

Output is the exact concatenation in import order. So a cut that preserves order preserves
**every** cascade relationship by construction, including all 18 in §5, and the proof is
mechanical: build before, build after, diff the emitted CSS. Stage 1 is done when that diff is
empty.

This is why Stage 1 does no reordering at all. Every judgment call — regrouping, hoisting
`.notice`, merging the two queue regions — is deferred to a later stage where it is the *only*
thing changing and can be argued on its own.

## 4. Target shape

`index.css` stays the entry `main.tsx` imports, and becomes a table of contents: the design
charter (which every author should still read first) followed by nothing but `@import`s. Numeric
prefixes because a directory listing must not imply that order is negotiable.

```
frontend/src/
  index.css              # charter (lines 1-20 today) + 31 @imports, order load-bearing
  styles/
    00-tokens.css        320   :root, 3 theme blocks, box-sizing reset
    01-base.css          133   html/body/headings/code/a, .muted .sr-only .blurb, :focus-visible
    02-masthead.css      163
    03-banners.css       101
    04-buttons.css       148
    05-user-menu.css      74
    06-spinner.css        32
    07-auth.css          198   login
    08-sheet.css          81   bottom sheet (local login)
    09-scanline.css       78   scan progress bar
    10-layout.css        120
    11-queue-chrome.css  275   tabs, scan nudge/toast, .score/.num verdict tokens
    12-why-panel.css     595
    13-receipt.css       267   the scoring receipt
    14-policy-editor.css 415
    15-policy-shell.css  294   the policy workspace shell
    16-simulator.css     216   ← also holds .notice's base rule today
    17-reap-plan.css     234
    18-scales.css        337
    19-scales-panel.css  488
    20-queue-cards.css  1041   cards, posters, grouped shows, filters
    21-overrides.css     422   the paired Spare / Reap toggle
    22-select-mode.css   373   tap-or-drag to pick cards
    23-settings.css      663
    24-settings-rows.css 638   the settings standard: switches and rows
    25-settings-log.css  169   General and Logs
    26-setup.css         109   setup wizard proper
    27-password.css       35   security / admin password form
    28-qty.css            60   ← rule 40's shared control, currently filed under setup
    29-modals-reap.css   365   modal shell, reap sheet, running reap, reap bar, result
    30-docs.css          610   in-app docs viewer
```

Thirty-one files summing to 9,054, plus the charter left in the barrel: largest 1,041 lines,
median 234, and eleven under 120. Small files are not the problem being solved — one 9,075-line
file was. Every boundary is a banner the file already draws, so Stage 1 requires no judgment:
the cut points are read off, not chosen. Three of them (26/27/28) split the mislabeled
`Setup wizard` banner, which is the single clearest naming win available.

## 5. The constraint every stage answers to

Eighteen places carry a comment saying source order or specificity is load-bearing. Two are
representative:

- 1608 (`.scan-toast`): *"This override MUST sit after the base rule, not up in the 900px block…
  Written in that block first, it lost to the `bottom: 1.1rem` above and did nothing."*
- 6161 (`.bulk-bar`): *"a media query adds no specificity and the later declaration wins. Sitting
  up in the 900px block instead, it was inert…"*

Most are same-specificity pairs inside one section, which an order-preserving cut cannot disturb.
Two span sections and are worth naming because they constrain file order forever: `main.split
.why` is declared at 1375 (layout) and again at 2213 (why panel), and the second must stay later;
`.card:hover, .fair-card.clickable:hover, .fair-stat-btn:hover` at 4823 deliberately extends a
selector whose other halves are defined back at 3849 and 3918. Both hold under Stage 1 and both
break under a casual reordering, which is the whole argument for the barrel carrying a comment
that says so.

Note the ones that are *not* fragile: `.card .poster` (4720) and its neighbors carry the `.card`
prefix specifically so they win on specificity rather than position. Those survive reordering.

## 6. Stages

### Stage 1 — the pure cut *(mechanical, no behavior change)*

Cut at the 31 boundaries above; write the barrel; move nothing. Comments travel with the rules
they annotate, which keeps `test_repo_hygiene.py`'s rule-citation, American-English and
docs-path sweeps satisfied — they glob `frontend/src/**/*.css`, so new files are covered with no
test change.

**Gate:** `npm --prefix frontend run build` before and after, then diff the emitted
`dist/assets/*.css`. **Must be byte-identical.** Nothing else in this plan proceeds until it is.

### Stage 2 — the three tests that read the stylesheet *(the delicate part)*

`bottom-bar-clearance.test.ts`, `viewport-units.test.ts` and `index-outside-text.test.ts` each
`readFileSync` the single sibling path. Two of them are genuinely order-sensitive:
bottom-bar-clearance exists *because* a lift sat ~4,000 lines above its base rule and was inert,
and index-outside-text asserts nothing after an `overflow-wrap` grant takes it back.

Add `frontend/src/test/stylesheet.ts` exporting (a) the concatenation in the barrel's `@import`
order and (b) an offset → `styles/20-queue-cards.css:431` resolver. The second half is not
cosmetic: both tests print `index.css:${lineOf(offset)}` in their failure messages, and after a
split that line number names a line that does not exist in any file. A confidently wrong
citation in a failure message is worse than none — rule 144's exact shape — so the resolver is a
blocker on this stage, not a follow-up.

Also add a hygiene test that every `styles/*.css` is imported by the barrel exactly once. An
orphaned file is dead CSS that nothing would ever report.

### Stage 3 — the reference sweep *(rule 72, rule 144)*

58 references to `index.css` across 19 files. Live ones must be corrected: `useMediaQuery.ts`
(three breakpoint cross-references), `accent.ts`, `App.tsx`, `WhyShell.tsx`, `ReviewQueue.tsx`,
`navIcons.tsx`, `JobStatus.tsx`, plus `.claude/rules/frontend.md` and
`.claude/rules/review-queue.md`. Frozen files in `docs/history/` are **not** edited. Every
hardcoded `index.css:LINE` citation in live prose goes stale the moment Stage 1 lands; prefer
naming the file and selector over a line number.

### Stage 4 — name the two scales that have none *(no pixels change)*

Both are pure renames, verifiable by the same byte-identical build diff as Stage 1:

- **The control standard.** Rule 40 calls `0.42rem 0.6rem` "the one control standard"; it is
  written out as a literal **11 times**. It should be a custom property, which is rule 67's own
  argument applied inside the stylesheet.
- **Stacking order.** 22 `z-index` uses across **12 distinct unnamed values** (0,1,2,3,5,20,30,
  40,50,55,60,100). The gap between 50, 55 and 60 is where the next overlay bug lives.

### Stage 5 — rehome the misfiled primitives *(reorders; needs a browser pass)*

`.notice` out of the simulator section and `.qty` out of the setup wizard, both into the
primitives band near the top. Merge the two review-queue regions. These change source order, so
the Stage 1 proof no longer applies: each move needs its own specificity argument plus an
end-to-end pass (the `verify` skill), one move per commit.

### Stage 6 — the type and space scales *(changes pixels; needs an approved mockup first)*

The token layer covers color and radius and stops there — 54 custom properties, none for type or
space. The result is drift, not variety:

- **39 distinct font sizes**, including `0.8 / 0.82 / 0.84 / 0.85 / 0.86 / 0.88rem` — six values
  inside 0.08rem, almost certainly three real ones.
- **25 distinct gap values.**

Collapsing these moves text, so `CLAUDE.md`'s golden rule binds: a rendered HTML mockup, approved,
before any CSS edit. This is the only stage that a reviewer must look at rather than diff.

### Stage 7 — dead CSS *(last, and manually)*

Deliberately last, because **96 sites compute their class name** rather than writing it —
`` `score score-${handFate(item)}` ``, `` `strip-sq strip-${mark.verdict}${handClass}` ``,
`levelClass(line.level)` returning one of four strings, `StatusChip`'s family × state lookup
table, `Notice`'s `.filter(Boolean).join(" ")`, and `className={filters.order}` where the class
name *is* a TypeScript union member and appears nowhere as a string. Any automated
unused-selector sweep will confidently delete live styles. Do this by hand, per file, after the
split makes per-file review tractable — or not at all.

### The guard that keeps it split

A hygiene test capping any `styles/*.css` at ~800 lines, on the `STATUS.md` precedent: this repo
has already learned once that a budget nobody enforces is a budget that reads green while the
file it measures grows in the dimension it does not measure.

## 7. What this plan does not do

- **No visual change through Stage 5.** If anything looks different, the change is wrong.
- **No component touched** in Stages 1–4. No `.tsx` edit, no class rename, no new dependency.
- **No renumbered or reused rule numbers**, and no new rule proposed. Stages 4 and 6 are covered
  by rule 67 and the golden rules already; the Stage 2 hygiene test and the Stage 7 cap are gates,
  which is what `CLAUDE.md` asks for ahead of appending prose.
- **Stages 5–7 are genuinely optional.** Stages 1–3 deliver the whole navigational win and are
  the only ones with a mechanical proof of safety. If the appetite runs out after Stage 3, the
  result is complete, not half-done.
