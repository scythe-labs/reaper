# UI/UX review: the whole frontend

- **Baseline:** `dev` @ `f750744`, reviewed 2026-07-17.
- **Method:** seven parallel review lenses (visual tokens, component patterns, layout/responsive,
  interaction states, accessibility, copy, fresh-eyes UX) swept all of `frontend/src` plus the
  backend strings that surface in the UI. Every finding was re-verified by an independent
  adversarial agent that re-read the cited code, recomputed contrast ratios, and corrected
  drifted line numbers; the seven highest-impact claims were verified a third time by hand.
  94 raw findings deduplicated to 82.
- **The standard applied** is the codebase's own system (the `index.css` control standard, the
  shared primitives, CLAUDE.md rules 17-21/36/39-45), not generic taste. Sanctioned exceptions
  (the ScanBar/review-card `.warn` banner, bare `.error` in review surfaces, the dark-mode
  `--accent-ink` navy) are not flagged.
- **Severity totals:** 1 critical · 25 high · 31 medium · 25 low.
- **Status: all 82 fixed** (2026-07-18), verified against the full CLAUDE.md gate set plus a
  live pass at 375px and in dark mode. Four things worth remembering, because they are the
  kind of defect a green build does not catch:
  - **A fix can be worse than the bug.** Cp1's first cut mapped Tautulli's bad-API-key reply
    (HTTP 200 with an error envelope) to "Couldn't connect.", which is false: Reaper *did*
    connect. A server that answers and refuses now says so.
  - **Half a sweep is its own defect.** `fields.py` lost its `!r` quoting while `policy.py`'s
    twin validators kept theirs, on the same page. Sweeps finish or they create inconsistency.
  - **Paired findings must land together.** Cp7 shipped ahead of Cp6, so for a while the plan
    surface and the deletion sheet described the same run differently, which is the drift Cp7
    existed to close.
  - **Reworded copy can start lying.** M1's "they are still selected so you can try again" was
    true only while the selection survived; the filter effect wiped it and left the sentence
    standing. Copy that asserts state has to be cleared with that state.

**What is genuinely good** (preserve it while fixing): the safety surfaces (`SafetyBanner`,
`DeletionToggle`, `ReapPlan`) model the correct unknown-state discipline; the token system is
documented in-file with reasoning; `ScanBar` is a model mutation. Several findings below are
"this file already does it right, its sibling doesn't." When a fix names a reference pattern,
copy that pattern, don't invent a new one.

## How to work this document

- Check off a finding (`[x]`) only after the fix passes the relevant verification gates
  (`npm --prefix frontend run lint`, `test`, `build`; the full CLAUDE.md gate set before a commit).
- Line numbers are exact at the baseline commit and will drift as fixes land: re-locate by the
  quoted class/identifier, not the number.
- Follow the **Agent Rules** at the bottom for every change, including changes unrelated to a
  listed finding. They exist to stop these defects from coming back.
- Suggested batch order is at the end. Findings that share one mechanical pattern are cheapest
  fixed together.

---

## 1. Visual consistency

- [x] **V1 [high]** `frontend/src/index.css:3699` · Seven input chromes fork the documented
  form-control standard (padding `0.42rem 0.6rem`, `--bg` fill, `var(--radius-sm)`, accent
  outline focus), including two classes the standard's own comment (lines 65-71) names as
  compliant: `.field-sm` (3697-3711: `0.45rem 0.55rem`, `--surface` fill, box-shadow ring
  focus), `.condition-add` (1728-1737: `0.4rem 0.55rem`, `--surface`, no focus rule),
  `.log-search` (4130-4139: hardcoded 8px radius, `0.45rem 0.7rem`, `--surface`, no focus
  rule), `.search-input` (2584-2597: `--surface` fill, ring focus), `.cron-custom input`
  (3766-3774), `.pw-form input` (4364-4377: `--surface`, ring focus), `.local-form input`
  (857-873). **Fix:** normalize all seven to the documented chrome: `background: var(--bg)`,
  padding `0.42rem 0.6rem`, `border-radius: var(--radius-sm)`,
  `:focus { outline: 2px solid var(--accent); outline-offset: 1px; }`. If any deviation is
  deliberate (the auth card), amend the standard comment in the same change.

- [x] **V2 [medium]** `frontend/src/components/PolicyEditor.tsx:2196` · Rule 40: bare
  `<input type="number">` where FixedQuantity is required, in four spots: the two title-count
  caps (2195-2200, 2213-2219) sit in the same caps grid as three QuantityInputs; the lean
  rule's "full effect at" box (1202-1208) and SuggestInput's numeric fallback (738-746) show
  their unit only as placeholder text that vanishes on input. **Fix:** render the caps as
  `FixedQuantity` with suffix `"titles"` (min 1, width narrow); where a field carries
  `unit_suffix`, render `FixedQuantity` with that suffix (`rating_tenths` keeps step 0.1);
  keep a plain input only for genuinely unitless fields.

- [x] **V3 [medium]** `frontend/src/index.css:3613` · The service kind badges hardcode brand
  hexes that duplicate existing tokens: `.kind-tautulli` uses `#e5a00d`/`#241a02`,
  byte-identical to `--plex`/`--plex-ink`; `.kind-radarr` uses `#f5c518`, identical to
  `--imdb`. `.kind-seerr` (3616) puts `#fff` on `#6366f1`: 4.47:1, just under AA for its
  11.2px bold text. **Fix:** reuse `var(--plex)`/`var(--plex-ink)` for tautulli and mint
  `--radarr`/`--sonarr`/`--seerr` token pairs beside `--plex` (the file's documented
  precedent), giving seerr a dark ink like the other badges so contrast clears comfortably.

- [x] **V4 [medium]** `frontend/src/index.css:1746` · Four card surfaces use a literal `12px`
  radius (`.rules-card` 1746, `.intent-band` 1907, `.season-card` 1937, `.bulk-bar` 3292)
  while every other card uses `var(--radius)` (11px), so two near-identical radii coexist.
  `.season-card` is also a verbatim copy of the `.rules-card` block whose own comment says
  "Same card as .rules-card". **Fix:** replace the four literals with `var(--radius)` and
  merge `.season-card` into the `.rules-card` selector list so the settings-bearing card has
  one definition.

- [x] **V5 [low]** `frontend/src/index.css:3972` · `.set-row .set-control input/select`
  hardcodes `border-radius: 7px` and `.btn-link` (4097) hardcodes `8px`; both are the
  standard radius and will silently detach if `--radius-sm` ever changes. **Fix:** replace
  both literals with `var(--radius-sm)`.

- [x] **V6 [low]** `frontend/src/index.css:2041` · Dead CSS: `.gate-list`, `.signal-list`,
  `.gate-name`, `.signal-name`, `.gate-threshold`, `.signal-weight` (2011-2063, excluding
  the still-used `.toggle` rules) match nothing in any component, and `.gate-threshold`
  carries a third input chrome (padding `0.2rem 0.4rem`, 5px radius) that invites a future
  fork if reused. **Fix:** delete the unused rules, keeping the `.toggle` rules between them.

- [x] **V7 [low]** `frontend/src/index.css:4448` · The two overlay scrims use near-duplicate
  literals: `.modal-scrim` is `rgba(8, 9, 12, 0.55)`, `.sheet-scrim` (811) is
  `rgba(8, 9, 12, 0.5)`, for no stated reason. **Fix:** hoist one value to a `--scrim` token
  in `:root` and use it in both.

## 2. Component patterns

- [x] **C1 [high]** `frontend/src/components/PolicyEditor.tsx:1128` · Rule 41: three
  either-or choices are hand-rolled `<div className="segmented">` with raw `.seg` buttons
  (rule strength 1128-1143, Movies/TV 1729-1742, preset picker 1815-1826) while the same
  file imports and uses the real `Segmented` in three other places (260, 522, 2017). All
  three copies drop the primitive's `aria-pressed`, so the active option is invisible to
  assistive tech. **Fix:** replace all three with `<Segmented>`: `value={strength}` for rule
  strength, `onChange={switchMediaType}` for Movies/TV (the confirm interception already
  lives in `switchMediaType`), and `value={preset ?? "custom"}` for the preset row (an
  unmatched value renders no active pill, matching today's Custom state).

- [x] **C2 [high]** `frontend/src/components/ShowPanel.tsx:20` · `SeasonPill` (20-33) is a
  parallel implementation of ReviewQueue's `OverrideChip` + `chipWhy` (ReviewQueue.tsx
  475-505): identical operator strings ("Spared by hand · will be kept", "Reap requested ·
  kept for now:", "Reaped by hand · will be removed"), identical fallback chain to
  `CondemnedChip`/`StatusChip`, identical `replace(/^Kept · /, "")`, maintained in two files
  with different chip class families. A wording change in one will silently drift the other.
  **Fix:** extract one override-chip component (StatusChip.tsx is the natural home, next to
  `CondemnedChip`) taking override, effective, keptWhy, and a class-family variant prop;
  render it from both call sites and move `chipWhy` there as the single helper.

- [x] **C3 [medium]** `frontend/src/components/Settings.tsx:615` · The Plex PIN sign-in
  state machine exists twice and has already drifted: `PlexPanel.beginPoll`/`pick` (615-639,
  657-675) mirror `Login.PlexButton.poll`/`pick` (Login.tsx 66-95, 114-132) with the same
  5-minute deadline and ok/choose_server branching, but one polls every 2000ms and the other
  every 2500ms; the server-pick list UI is likewise duplicated (Login.tsx 160-181 vs
  Settings.tsx 857-878). The comment at 613 acknowledges the twin instead of sharing it.
  **Fix:** extract a shared `usePlexPinPoll` hook (start, poll with deadline, pick, cancel,
  callbacks for ok/choose_server/error) plus a small `ServerPickList` component, and drive
  both from them.

- [x] **C4 [medium]** `frontend/src/components/ShowPanel.tsx:36` · `ShowSynopsis` (36-48) is
  a line-for-line copy of WhyPanel's `Synopsis` (WhyPanel.tsx 161-173), including the
  150-character threshold. WhyPanel already exports `JumpPill` and `WhyHero` specifically
  for ShowPanel's reuse. **Fix:** export `Synopsis` from WhyPanel.tsx, import it in
  ShowPanel, delete `ShowSynopsis`.

- [x] **C5 [medium]** `frontend/src/components/WhyPanel.tsx:463` · The override mutation
  pair and its three-key invalidation (`["candidates"]`, `["group"]`, `["candidate"]`) are
  duplicated between WhyPanel (463-476) and ReviewQueue (1136-1149); WhyPanel's comment even
  says "the same mutation shape the cards use". Any new cache that overrides affect must be
  remembered in both lists. **Fix:** extract a `useOverrideMutations()` hook that owns the
  invalidation list once; use it in both.

- [x] **C6 [medium]** `frontend/src/components/ServiceModal.tsx:195` · The modal shell is
  hand-built twice (ServiceModal 193-203, ReapConfirm.tsx 79-86) with identical
  scrim-click-close, stopPropagation, header, and close button, and ServiceModal borrows
  `.reap-confirm-head`, a class whose CSS comment (index.css 4439) calls it "the one modal
  in the app that deletes". **Fix:** extract a `ModalShell` component (scrim, panel, header
  slot with close button) used by both; rename `.reap-confirm-head` to a neutral
  `.modal-head`, keeping reap-specific styles under their own classes. Coordinate with
  A2/A7: give the shell the dialog semantics once.

- [x] **C7 [medium]** `frontend/src/components/Settings.tsx:1532` · The scan-schedule preset
  picker (1528-1538) is a third choose-one control variant: bespoke `.preset` buttons
  (index.css 3747-3759), no `role="group"`, no `aria-pressed`, active state conveyed by
  color alone, while the policy page renders the same job as a segmented track. **Fix:**
  render the four presets through the shared `Segmented` (or per rule 41 a select beside the
  custom cron box) and delete the one-off `.preset` styles.

- [x] **C8 [medium]** `frontend/src/components/Settings.tsx:1324` · The Logs toolbar
  hand-rolls two one-off on/off controls: Live/Pause (1324-1327, state conveyed only by its
  changing label) and Wrap (1329-1339, which does declare `aria-pressed`), so the neighbors
  are internally inconsistent and neither is the product's one on/off control. **Fix:**
  either render both as labeled `Switch` rows, or keep one compact pressed-button treatment
  with `aria-pressed` on both and a comment in Switch.tsx sanctioning the exception.

- [x] **C9 [low]** `frontend/src/components/ReviewQueue.tsx:1441` · The active-filters row
  builds five copies of the identical removable-chip scaffolding (search 1441, media type
  1456, requested 1468, genre 1480, override 1492); only label, aria-label, and reset action
  differ. **Fix:** extract a local `FilterChip({ label, clearLabel, onClear })` and render
  all five through it.

- [x] **C10 [low]** `frontend/src/components/Settings.tsx:430` · `ServiceCard` hand-builds
  the test-badge look twice (fail badge at 430, ok badge with version suffix at 432-434)
  because the exported `TestBadge` (ServiceModal.tsx 39-48) only accepts an `InstanceTest`.
  **Fix:** widen `TestBadge` to accept a plain `{ ok, detail, version? }` shape and render
  it for all three states.

- [x] **C11 [low]** `frontend/src/components/ReviewQueue.tsx:867` · MovieCard and ShowCard
  duplicate the one-status-line ternary (867-874 vs 983-993) and already encode the
  condemned check differently (`item.verdict === "condemn"` vs derived `isReapTab`), which
  is where drift starts. **Fix:** extract
  `CardStatusLine({ condemned, dormantFor, reason, chip })` and render it from both cards.

- [x] **C12 [low]** `frontend/src/components/Settings.tsx:545` · Settings.tsx holds eight
  panels in 1888 lines; `PlexPanel` (545-1202, ~660 lines) and `LogsPanel` (1238-1412, with
  its module-scope store) are clean seams with no closure over Settings state, and
  SetupWizard already imports panels from here. The simulator column trio in PolicyEditor.tsx
  (1256-1458) is the same shape. **Fix:** move PlexPanel (with `connectionLabel`,
  `MANUAL_CONNECTION`) and LogsPanel (with `_logStore`, `LEVEL_RANK`, `levelClass`,
  `logTime`) into their own files; same mechanical split for the simulator trio.

## 3. Layout and spacing

Spacing rhythm across the app is disciplined (one control standard, consistent card padding,
a documented breakpoint set); only one finding here. Viewport-dependent layout issues are in
section 6.

- [x] **L1 [low]** `frontend/src/index.css:1689` · The file's only 700px breakpoint; every
  other width query sits on the 1100/900/640/560 grid, and the `.bar-table` it collapses
  lives in a column that is single-column below 900px anyway, so the one-off width buys
  nothing. **Fix:** fold the collapse into a `max-width: 640px` query, or add a comment
  justifying the width.

## 4. Interaction and behavior

- [x] **I1 [high]** `frontend/src/components/ReapConfirm.tsx:39` · On the one surface that
  deletes, an unknown safety state renders as a definite claim: `armed` is false while the
  safety query is pending or errored, and the sheet then says "Deletion is off. Turn it on
  in Policy → Deletion" (118-122), a state it could not read. Rule 17. The sibling ReapPlan
  (165-179) already distinguishes checking / couldn't-confirm / off. **Fix:** mirror
  ReapPlan's three states: pending shows "Checking whether deletion is on", error shows the
  amber couldn't-confirm tone, and only a successfully read `destructive_enabled === false`
  may claim off. Execute stays disabled in every non-armed state, as now.

- [x] **I2 [high]** `frontend/src/components/ReviewQueue.tsx:1141` · The per-card Spare and
  Reap buttons are mutations with no rendered error state: `setOverride`/`clearOverride`
  have `onSuccess: invalidate` and no `onError`, and neither `.isError` nor `.error` appears
  in the JSX. A failed override silently does nothing on the primary review surface. Rule
  36. **Fix:** render the same error WhyPanel shows for this exact action (WhyPanel.tsx
  637-639): `{(setOverride.isError || clearOverride.isError) && <p className="error
  bulk-error">Couldn't save that. Try again.</p>}` beside the existing bulkFailures block.

- [x] **I3 [high]** `frontend/src/components/SetupWizard.tsx:57` · The `!setup` branch
  conflates pending with error and renders "Setting things up…" with no Skip button and no
  error notice. App.tsx routes an errored setup-status query here on the explicit promise
  that the owner can still skip (App.tsx 360-367), but the only Skip lives in the
  full-render branch (83); with `retry: 1`, a failing `/api/setup/status` dead-ends the
  whole app. **Fix:** take `isError` from the query; in this branch always render the "Skip
  to the app" button, plus a `.notice.notice-error` ("Couldn't check the setup state. You
  can skip to the app and finish from Settings.") when errored, keeping "Setting things
  up…" only for pending.

- [x] **I4 [high]** `frontend/src/components/PolicyEditor.tsx:1667` · The whole Policy
  workspace shows "Loading…" forever when the `["policy", mediaType]` query errors: only
  `data` is destructured (1484), `draft` stays null, and no `isError` handling exists
  anywhere in the file; the pace section has the same defect via the `["profile"]` query
  (2180). With `retry: 1` the page never recovers without navigating away. Rule 36.
  **Fix:** destructure `isError` from both queries and render the GeneralPanel pattern
  (Settings.tsx 140-145): a `.notice.notice-error` "Couldn't load these settings. Reload to
  try again."

- [x] **I5 [high]** `frontend/src/components/Settings.tsx:547` · PlexPanel takes only `data`
  from the `["plex"]` query, so both pending and error render `linked = false`: the panel
  claims "No Plex server linked" and invites a needless re-link through the whole OAuth flow
  on a transient failure. Every sibling panel handles both states explicitly. **Fix:**
  destructure `isPending`/`isError` and early-return the GeneralPanel pattern before any
  branch that claims the server is unlinked.

- [x] **I6 [high]** `frontend/src/components/Settings.tsx:1820` · AdminPasswordForm routes
  failures into the same state as success (`onError` sets `msg`, 1777) and renders both as
  `<span className="muted">`, so "Password saved." and a wrong current password look
  identical on the security surface. Rule 42. **Fix:** split the channels: success stays
  muted; render errors as `.notice.notice-error` with a lead like "The password wasn't
  changed: {message}", matching every other Settings panel.

- [x] **I7 [high]** `frontend/src/components/Settings.tsx:1075` · The Plex link flow's
  failures render as grey muted text: the catch blocks in `beginPoll` (635), `startLink`
  (650), and `pick` (672) all write real errors into `message`, while the component's own
  comment (551-553) says failures get the separate `plexError` state "so they render as an
  error, not as grey status text", and `plexError` already renders as `.notice.notice-error`
  one line above (1074). **Fix:** route the three catch blocks into `setPlexError`, keeping
  only informational text (timeout notice, "Linked to X.") in `message`.

- [x] **I8 [high]** `frontend/src/components/Settings.tsx:409` · ServiceCard's `remove`
  mutation has no rendered error state: "Confirm remove" collapses the two-step confirm and
  a failed DELETE leaves the card unchanged, reading as an ignored click. `testSaved`
  (402-408) has the same gap for thrown transport errors. **Fix:** render
  `{(remove.error ?? testSaved.error) && <p className="notice notice-error">…</p>}` inside
  the card, the pattern GeneralPanel uses at 315-319.

- [x] **I9 [medium]** `frontend/src/components/Settings.tsx:1816` · The admin password form
  enables Save at 8 characters while its own placeholder says "at least 12 characters"
  (1810) and the server enforces 12 (`src/reaper/services/admin_password.py:28`, raising
  "Use at least 12 characters."). An 8-to-11 character password lights the button and is
  guaranteed to fail the round trip. **Fix:** change the gate to `pw.length < 12` so the
  button, the placeholder, and the server rule state one number.

- [x] **I10 [medium]** `frontend/src/components/Settings.tsx:1476` · MaintenanceJobs' "Run
  now" mutation renders no error: a job that fails to start re-enables the button with zero
  feedback, the exact "button appears to do nothing" failure ScanBar's comment exists to
  prevent. **Fix:** render `{run.error && <p className="notice notice-error">The job didn't
  start: {run.error.message}</p>}` under the job list.

- [x] **I11 [medium]** `frontend/src/components/Settings.tsx:1685` · NotificationsPanel
  takes only `data`, so while loading and on error it states "No Discord webhook set, so
  leaving-soon warnings won't be sent.", a definite claim about the one channel that warns
  the household before deletion, rendered when the state is unknown. **Fix:** destructure
  `isPending`/`isError`; show "Checking…" while pending and an error notice on failure,
  before either definite claim.

- [x] **I12 [medium]** `frontend/src/components/Settings.tsx:1523` · AutoScanSchedule
  swallows the `["schedule"]` query's pending/error states: `current` falls back to null,
  which exactly matches the "Off (scan by hand)" preset, so a failed read highlights "Off"
  as the active schedule; MaintenanceJobs shares the query and renders a bare empty list on
  the same failure. **Fix:** take `isPending`/`isError`; while unresolved mark no preset
  active and render "Checking the schedule…" or an error notice, and give MaintenanceJobs
  the same fallback.

- [x] **I13 [low]** `frontend/src/App.tsx:128` · UserMenu's sign out is a fire-and-forget
  async onClick: `api.logout()` failures are swallowed by `.catch(() => undefined)`, and if
  the session is still valid the `["me"]` refetch succeeds, so the user stays signed in with
  the dropdown open and no feedback. **Fix:** convert to a `useMutation` and render its
  error in the dropdown ("Couldn't sign you out. Try again."), keeping the invalidation in
  `onSettled`.

## 5. Accessibility

- [x] **A1 [critical]** `frontend/src/index.css:27` · `--faint` fails contrast everywhere it
  carries real text. Light `#9aa0ad`: 2.41:1 on `--bg`, 2.62:1 on `--surface` (below even
  the 3:1 large-text floor). Dark `#6b7180`: 3.92:1 on `--bg`, 3.52:1 on `--surface`,
  3.17:1 on `--surface-2`. Verified text uses include `.card-year` (review cards),
  `.auth-safety` (the login safety disclosure), `.savebar-hash`, `.log-t`, `.log-lv.debug`,
  `.title-ext`, `.setup-check em`. **Fix:** either darken/lighten the token to clear 4.5:1
  in both themes and both forced-theme blocks, or reserve `--faint` for decorative glyphs
  (chevrons, poster placeholder) and switch the listed text uses to `--muted` (which passes
  at 4.89-7.40:1).

- [x] **A2 [high]** `frontend/src/components/ReapConfirm.tsx:80` · The deletion confirmation
  modal has no dialog semantics or focus management: no `role="dialog"`, `aria-modal`,
  accessible name, Escape handling, initial focus, or containment; keyboard focus stays on
  the still-tabbable page behind the scrim. The in-repo pattern exists at Login.tsx 240-242.
  **Fix:** add `role="dialog" aria-modal="true" aria-labelledby` on the h2 (82), Escape
  through the same close guard that already blocks closing while `exec.isPending`, move
  focus in on open and back to the invoker on close, and trap Tab (or set `inert` on the app
  root). Do this once in the shared `ModalShell` from C6.

- [x] **A3 [high]** `frontend/src/components/Login.tsx:237` · LocalSheet stays mounted while
  closed: the scrim only gets `opacity: 0` / `pointer-events: none` and the sheet is
  translated off-screen, so Username, Password, and buttons remain in the Tab order
  invisibly with `aria-modal="true"` permanently in the tree. Additionally
  `autoFocus={open}` (263) only applies at mount, and the component mounts closed, so focus
  never moves into the sheet at all. **Fix:** set `inert` on the sheet container while
  closed (or unmount and animate on mount), and move focus to the username input with a ref
  plus an effect keyed on `open`.

- [x] **A4 [high]** `frontend/src/components/ReviewQueue.tsx:242` · All five queue toolbar
  dropdowns render through `Pill`, whose `<select>` has no accessible name: the wrapping
  label contains only an aria-hidden icon and the `title` sits on the label element. Screen
  readers announce five unnamed comboboxes on the primary surface (WCAG 4.1.2). **Fix:** in
  `Pill`, pass the existing `title` prop through as `aria-label` on the select.

- [x] **A5 [high]** `frontend/src/index.css:2001` · The keep-tags free-type input removes
  its focus outline with no replacement: `.tag-chips input { outline: none }` (specificity
  0-1-1) beats the global `:focus-visible` rule, and no `:focus-within` exists for the
  container, so keyboard users get no focus indicator at all (WCAG 2.4.7). **Fix:** add
  `.tag-chips:focus-within { outline: 2px solid var(--accent); outline-offset: 1px; }`, the
  same composite-control pattern as `.qty:focus-within` (4422).

- [x] **A6 [high]** `frontend/src/index.css:4312` · The setup checklist's done tick
  hardcodes `#fff` on a solid `var(--protect)` disc. In dark/forced-dark, `--protect` is
  `#5fce97` and white computes to 1.95:1, below even the 3:1 UI minimum, making the tick
  nearly invisible on the first-run surface. **Fix:** use `color: var(--protect-soft)`
  (8.32:1 dark, 4.66:1 light), the same ink convention `.chip-hand-spare` and
  `.strip-ov-spare` already use.

- [x] **A7 [medium]** `frontend/src/components/ServiceModal.tsx:194` · Same dialog gaps as
  A2 (no role, no aria-modal, no Escape, no focus management), verified zero matches for any
  of them in the file. **Fix:** same treatment as A2, via the shared shell.

- [x] **A8 [medium]** `frontend/src/components/PolicyEditor.tsx:922` · The custom-rule
  builders' controls have no accessible names: the condemn builder's field/comparison/value
  selects (922, 932, 968), the keeps builder's equivalents (1147, 1157, 1165, 1185, 1195),
  the ramp inputs (948-964), and the lean full-effect input (1202-1208) carry neither
  aria-label nor an associated label; screen readers hear a row of unnamed comboboxes and
  spinbuttons. (The weight inputs are correctly wrapped in labels.) **Fix:** give each an
  `aria-label` naming its role in the sentence: "Field", "Comparison", "Value", "Full effect
  at", "Most points it can add".

- [x] **A9 [medium]** `frontend/src/components/PolicyEditor.tsx:313` · GateRow's days-unit
  branch renders `QuantityInput` without `ariaLabel` (the people branch at 329 passes it, so
  this is an omission), and the ramp QuantityInputs (944-958) repeat it; the unit select
  falls back to generic "Unit". **Fix:** pass ``ariaLabel={`${meta.label} threshold`}``
  here and label the ramp inputs ("Starts counting at", "Full effect at"); consider making
  `ariaLabel` required on QuantityInput when no wrapping label provides a name.

- [x] **A10 [medium]** `frontend/src/components/PolicyEditor.tsx:764` · SuggestInput
  implements half the combobox pattern: `role="combobox"`, `aria-expanded`,
  `aria-autocomplete` exist, but there is no `aria-controls`, options have no ids, and no
  `aria-activedescendant` tracks the highlighted option, so arrowing gives no feedback; the
  input's only name is its placeholder. **Fix:** `useId` for the listbox, `aria-controls`
  on the input, per-option ids, `aria-activedescendant` following the highlight, plus an
  `aria-label` naming the field.

- [x] **A11 [medium]** `frontend/src/App.tsx:269` · The masthead nav marks the active view
  with the `.active` class only, no `aria-current`; the Settings tab bar (Settings.tsx 1869)
  and policy rail (PolicyEditor.tsx 1773) share the gap, while ReviewQueue's tabs expose
  `aria-selected`, so the app is internally inconsistent. **Fix:** add
  `aria-current={view === n.id ? "page" : undefined}` to the masthead buttons and the
  equivalent on the Settings tabs and policy rail.

- [x] **A12 [medium]** `frontend/src/App.tsx:136` · The user menu declares
  `aria-haspopup="menu"` but never `aria-expanded`; the popup uses `role="menu"` with none
  of the menu keyboard contract (no Escape, no arrows, no focus move; outside dismissal is
  mousedown-only, so tabbing away leaves it open) and contains a non-menuitem header div.
  **Fix:** add `aria-expanded={open}`, close on Escape and on focusout leaving the
  container, and either implement the menu keyboard pattern or drop the menu roles for a
  plain disclosure, which matches current behavior honestly.

- [x] **A13 [medium]** `frontend/src/components/Settings.tsx:1810` · The admin password
  inputs have placeholder-only names: the new-password field's accessible name becomes its
  hint ("at least 12 characters"), which does not identify the field and vanishes on typing;
  same pattern on the current-password input (1802) and DeletionToggle's password input
  (DeletionToggle.tsx:90). **Fix:** add `aria-label="New password"` / `"Current password"` /
  `"Admin password"` (or wrap in the login form's `.field-label` pattern).

- [x] **A14 [medium]** `frontend/src/components/SetupWizard.tsx:19` · Each setup checklist
  item conveys done vs not-done only through an aria-hidden glyph and color, so a screen
  reader hears the step text with no state on the first-run flow. **Fix:** expose state in
  text: a visually-hidden "Done: " / "Not done yet: " prefix, or `role="img"` with
  `aria-label={done ? "Done" : "Not done"}` on the glyph.

- [x] **A15 [low]** `frontend/src/index.css:3444` ·
  `.season-row.season-other { opacity: 0.6 }` dims whole interactive rows; their `--muted`
  content composites to roughly 2.5:1 on a light surface. `.card-spared` (0.7) and
  `.signal-unknown` (0.75) sit near 3.6:1. **Fix:** dim without breaking contrast: keep text
  at full opacity and reduce only posters/chips, or floor row opacity at 0.8 and verify the
  muted text still clears 4.5:1.

- [x] **A16 [low]** `frontend/src/components/ReviewQueue.tsx:1332` · The queue search input
  relies on its placeholder alone for a name; the log search (Settings.tsx:1311) sets an
  explicit `aria-label`, so this is also an internal inconsistency. **Fix:** add
  `aria-label="Search titles and shows"`.

- [x] **A17 [low]** `frontend/src/components/ReviewQueue.tsx:1307` · The lane switcher uses
  `role="tablist"`/`role="tab"` with `aria-selected` but none of the rest of the tabs
  pattern (no `aria-controls`, no tabpanel, no arrow keys, no roving tabindex), so the ARIA
  promises interactions that do not exist. **Fix:** complete the pattern or drop the tab
  roles and mark the active button with `aria-current`, matching the A11 fix.

- [x] **A18 [low]** `frontend/src/components/GracePanel.tsx:25` · Every grace row renders an
  identical "cancel" button with no per-item name: up to twenty indistinguishable "cancel"
  controls for an action that spares a specific file. **Fix:**
  ``aria-label={`Cancel the countdown and keep ${item.title}`}``.

- [x] **A19 [low]** `frontend/src/components/Fairness.tsx:48` · The per-requester
  disclosure buttons are all named "show"/"hide" with no row context and no `aria-expanded`.
  **Fix:** add `aria-expanded={open}` and
  ``aria-label={`${open ? "Hide" : "Show"} unwatched titles requested by ${row.name}`}``.

## 6. Responsiveness

- [x] **R1 [high]** `frontend/src/index.css:1329` · The phone full-screen why-panel never
  applies as designed. At 900px and below both media blocks match, and `main.split .why`
  from the 1100px block (specificity 0-2-1, line 928) beats the 900px block's bare `.why`
  (0-1-0) on every shared property: z-index 40 over 50, the rounded left radius over 0,
  `max-height: none` over `100dvh`, and its `width: min(440px, calc(100vw - 1.25rem))`
  combines with the 900px block's `inset: 0` so the over-constrained box drops `right: 0`.
  On a 375px phone the panel renders as a 355px-wide left-anchored sheet with a ~20px strip
  of queue visible on the right, not the full-screen sheet the comment (1320-1327) promises;
  the comment's "source order breaks the tie" reasoning only considered the base `.why`
  rule. The panel is always inside `main.split` when open (App.tsx:288). Hand-verified.
  **Fix:** change the 900px block's selector to `main.split .why` and add `width: auto`
  there; keep `inset: 0`, z-index 50, radius 0, `max-height: 100dvh`.

- [x] **R2 [high]** `frontend/src/index.css:949` · The masthead nav overflows and clips on
  phones: the five tabs total roughly 395-405px, the `.views` track gets ~335px at 375px,
  and `.views` is a non-wrapping, centered flex row with no overflow rule, so the leading
  tab clips past the viewport's left edge where no scroll can reach it. Affects viewports
  under ~450px. **Fix:** under ~480px give `.views` `overflow-x: auto` with
  `justify-content: flex-start` (or `safe center`), the app's documented sideways-scroll
  pattern (`.bulk-bar`, 3299), or shrink `.tab` padding/font at that width.

- [x] **R3 [high]** `frontend/src/components/ReapPlan.tsx:24` · The plan-steps table forces
  page-level horizontal scroll on phones: each Request cell holds inline-block `<code>` with
  an unbroken API path plus a `JSON.stringify` body (48), giving the table a ~550-600px min
  width, and no `overflow-x` container exists around any table in the app. **Fix:** wrap
  the table in a `div` with `overflow-x: auto`, and/or add `overflow-wrap: anywhere` to
  `.plan-steps code` (index.css 2334-2339).

- [x] **R4 [high]** `frontend/src/index.css:820` · The local-login bottom sheet has no
  max-height or inner scroll, unlike the app's one modal pattern (`.modal`:
  `max-height: 88dvh` + `overflow-y: auto`, with the comment at 4453-4456 explaining why
  dvh). `.sheet` is bottom-anchored inside the fixed scrim, so on short viewports (landscape
  phones, or when the error notice renders) the username field extends above the viewport
  and cannot be scrolled to. Rule 18. **Fix:** give `.sheet` `max-height: 85dvh` (or 88)
  and `overflow-y: auto`.

- [x] **R5 [medium]** `frontend/src/components/Fairness.tsx:115` · The fairness table's six
  columns have a min-content width of roughly 450-500px with no overflow wrapper, forcing
  page-level horizontal scroll below ~500px. **Fix:** same as R3: wrap in an
  `overflow-x: auto` container.

- [x] **R6 [medium]** `frontend/src/index.css:3400` · Expanded season rows have no
  narrow-viewport layout: the grid's fixed tracks (2.6rem score, ~55px size, ~130px nowrap
  override buttons, three 0.7rem gaps) plus the 2.4rem list indent leave the title column
  roughly 25px at 375px, so season names wrap letter-by-letter and the status chip
  ellipsizes to nothing; the 560px container query (2684) restacks only `.card`/
  `.card-head`. **Fix:** extend that container query to restack `.season-row` into two rows
  (grid-template-areas), or reduce the indent and drop the size cell at narrow widths.

- [x] **R7 [medium]** `frontend/src/index.css:1896` · Policy section jump targets hide under
  the stuck rail on phones: `scroll-margin-top` is a fixed 3.4rem sized for a one-row rail,
  but under ~560px the flex-wrapped rail becomes two rows (~5rem), so rail clicks and
  cross-page jumps (Policy → Deletion) land the heading under the sticky, opaque rail.
  **Fix:** raise `scroll-margin-top` under a narrow media query (about 6rem at max-width
  640px), or size it with a custom property set from the rail's actual height.

- [x] **R8 [medium]** `frontend/src/index.css:4000` · `.set-control` only gains `flex-wrap`
  inside the 640px query, but the Settings API-key row packs an input floored at 15rem (via
  `.set-row .set-control input`, 3968-3976, which outranks `.keyfield`) plus
  Show/Copy/Replace buttons: roughly 460-500px of unshrinkable content. Between 641px and
  ~730px the label column collapses to 80-150px, wrapping "API key" and its help a word per
  line; still squeezed at iPad portrait. **Fix:** move `flex-wrap: wrap` onto the base
  `.set-row .set-control` rule (inert while space suffices), or raise the query threshold
  to ~800px.

## 7. Copy and labeling

- [x] **Cp1 [high]** `src/reaper/services/instances.py:255` · A failed connection test
  surfaces the raw Python exception class and message to the operator ("ConnectError: All
  connection attempts failed", TLS stack traces), rendered verbatim by TestBadge
  (ServiceModal.tsx:44) and stored as `last_error` on the service card (Settings.tsx:430),
  reaching the Services panel and the first-run SetupWizard. Rule 21. Line 236's `str(exc)`
  for a malformed URL has the same problem. **Fix:** map the common failure families to
  outcome-led plain sentences (connection refused/DNS: "Couldn't reach the server at this
  address."; timeout: "The server didn't answer in time."; TLS verify: "The server's
  certificate couldn't be verified. If it is self-signed, turn off the certificate check."),
  keep the raw exception in the log only, and use a generic "Couldn't connect." fallback.

- [x] **Cp2 [high]** `src/reaper/engine/fields.py:107` · The vocabulary `help_text` strings
  render verbatim in the policy editor and violate rule 21: spaced `--` used as em dashes at
  lines 107, 135, 148, 164-165, 193, 237-238, 263, 276; literal markdown asterisks that
  render as asterisks ("watched by \*someone\*", 133); and line 193 exposes the internal
  field key and operator syntax (`season_rank <= 2`). **Fix:** reword each `--` with a
  period, comma, or colon; drop the asterisks; replace the `season_rank` aside with plain
  words such as "Keep the last 2 seasons means a rank of 2 or less."

- [x] **Cp3 [high]** `src/reaper/api/runs.py:270` · The 409 for a mismatched confirmation
  phrase, shown verbatim in the reap sheet (ReapConfirm.tsx:139), uses a spaced `--` as an
  em dash and formats the expected phrase with Python repr (`{expected!r}`), so the operator
  sees engineer-style quoting. Hand-verified. **Fix:** "That confirmation does not match
  this plan. Expected: REAP 3 ITEMS 120 GB. The plan may have changed since the page loaded.
  Reload, review, and confirm again." with plain interpolation.

- [x] **Cp4 [high]** `src/reaper/api/fairness.py:52` · The 400 detail when Seerr or Tautulli
  is missing uses a spaced `--` as an em dash, rendered verbatim by Fairness.tsx:88.
  **Fix:** "The fairness view needs a Seerr and a Tautulli instance: Seerr for who requested
  what, Tautulli for who watched it. Configure them in Settings."

- [x] **Cp5 [high]** `frontend/src/components/SetupWizard.tsx:47` · During the first-run
  scan the wizard shows the scan's raw internal phase id ("lists", "gathering") instead of
  the plain labels ScanBar already defines; `phaseLabel` is module-private so the wizard
  could not reuse it. The adjacent error branch (44-45) shows the backend error bare in
  muted text with no outcome lead. **Fix:** export `phaseLabel`/`PHASE_LABELS` from
  ScanBar.tsx and use it here; render the error branch as "The scan hit a problem: …" in an
  error notice, matching ScanBar.tsx:109.

- [x] **Cp6 [high]** `frontend/src/components/ReapConfirm.tsx:96` · "Dry-running every
  interlock…" and "smallest first, canary leads." (90) put engine vocabulary in front of
  the operator with no explanation; rule 21 bans this family ("guard", "coverage bp").
  **Fix:** "Checking every safety stop with a practice run…" and "smallest first, and the
  first item is a test: if it doesn't go exactly as planned, the run stops."

- [x] **Cp7 [high]** `frontend/src/components/ReapPlan.tsx:70` · Same vocabulary family:
  "Every interlock ran" (70), "smallest-first, canary leads" (149-150), and a bare "canary"
  tag in the steps table (41); note also the hyphenation drift ("smallest-first" here vs
  "smallest first" in ReapConfirm). **Fix:** "Every safety check ran"; align the summary
  with the Cp6 rewording; change the canary tag text to "test item" (keep the class name).

- [x] **Cp8 [high]** `frontend/src/components/PolicyEditor.tsx:87` · The "Honour protected
  lists" help promises "any other list you mark as protected", but no marking affordance
  exists anywhere: the only curated list synced as a protection is the IMDb Top 250 (the
  RadarrImportList provider was defined but never wired into any sync, and has since been
  removed). Rule 25: copy may
  only name wired mechanisms. **Fix:** describe only the wired source: "Right now this is
  the IMDb Top 250. Anything on it is kept."

- [x] **Cp9 [low]** `frontend/src/components/ScanBar.tsx:80` · Terminology drift for the
  fate of condemned items: "would be deleted" here and in PolicyEditor.tsx:1369, while the
  queue, chips, and DeletionToggle consistently say "removed". **Fix:** standardize on
  "removed" in both spots.

- [x] **Cp10 [low]** `frontend/src/components/Fairness.tsx:103` · "(Plex has not matched
  them)" uses the engine's matching vocabulary, the exact phrase WhyPanel deliberately
  translates away (WhyPanel.tsx:373 maps it to "This title couldn't be found in Plex.").
  **Fix:** reword to the why-panel's phrasing: "N could not be judged (they couldn't be
  found in Plex)".

- [x] **Cp11 [low]** `frontend/src/components/GracePanel.tsx:123` · "Cancel resets nothing
  else." is a riddle: "else" has no clear antecedent, and this blurb explains the one
  safety action on the panel. **Fix:** "Cancel does more than stop the clock: it spares the
  file, so it leaves the queue and the plan too."

- [x] **Cp12 [low]** `frontend/src/components/PolicyEditor.tsx:1384` · The histogram help
  says "Everyone's score, 0 to 100." but the bars are titles' scores, not people's, on the
  page where an operator learns what the score means. **Fix:** "Every title's score, 0 to
  100. The line is your threshold. Red bars are past it."

- [x] **Cp13 [low]** `frontend/src/components/WhyPanel.tsx:262` · The Limbo note ends
  "Either way, abstaining keeps the file." two paragraphs after the component's own comment
  that an operator should never be shown "abstain". **Fix:** "Either way, Reaper leaves it
  alone, and the file is kept."

- [x] **Cp14 [low]** `src/reaper/api/routes.py:1128` · The "Protections that fired" list
  mixes styles: the hand-spare row is a capitalized sentence with a period ("You spared
  this by hand.") beside lowercase fragments from the gates ("someone is watching it right
  now", "on your keep list, never reaped"), and gates.py:330 alone adds a trailing period.
  **Fix:** pick the dominant style (lowercase fragment, no trailing period) and align the
  hand-spare string in all three builders (routes.py:487, routes.py:1128, snapshot.py:792);
  drop the concatenated period in gates.py:330; ABSTAIN details stay full sentences per the
  existing convention.

## 8. Improvements

- [x] **M1 [medium]** `frontend/src/components/ReviewQueue.tsx:1164` · When a bulk override
  partially fails, the selection is cleared unconditionally: the operator is told "3 items
  could not be updated" with no way to know which three or retry them without re-finding
  each in a list of hundreds. The `mutationFn` already knows exactly which keys rejected.
  **Fix:** return the rejected keys from the `Promise.allSettled` results; in `onSuccess`,
  `setSelected(new Set(failedKeys))` so only the failures stay selected in the still-open
  Select mode, and word the error "3 items could not be updated; they are still selected so
  you can try again."

- [x] **M2 [medium]** `frontend/src/App.tsx:288` · The core review loop (open reasoning,
  decide, next item) has no keyboard path: nothing closes the why-panel but its close
  button (no Escape, even in the fixed-sheet mode with no scrim), and stepping to the next
  card means mousing back to the list. **Fix:** while `view === "review"` and
  `selected !== null`, a window keydown listener: Escape calls `setSelected(null)`;
  ArrowDown/ArrowUp (or j/k) move selection to the adjacent card in the queue's current
  order (ReviewQueue owns the ordered groups, so expose a next/previous callback the same
  way `onSelect` is passed down).

- [x] **M3 [medium]** `frontend/src/components/ReviewQueue.tsx:1607` · "Select all" only
  covers the rendered window of 40 cards; bulk-acting on everything matching a filter
  forces scrolling the entire list into existence 40 at a time, and the button's own
  tooltip admits the workaround ("scroll to show more"). **Fix:** when all shown are
  selected and more exist, offer "Select everything matching (N)" in the bulk bar that
  loops `fetchNextPage` until exhausted; if that proves slow on very large sets, note that
  a server endpoint returning matching keys is the cleaner path.

- [x] **M4 [medium]** `frontend/src/App.tsx:94` · First-run and safety pointers are dead
  prose while the cross-page jump machinery exists in the same component: ScanFreshness
  says "Run one from Settings → Jobs" (94) and SafetyBanner says "check Policy → Deletion"
  (58) / "Turn deletion on in Policy → Deletion" (70) as plain text, while GracePanel and
  ReapPlan render real jump buttons for the same destinations. **Fix:** pass
  `goToSettingsPanel("jobs")` into ScanFreshness and `goToPolicySection("deletion")` into
  SafetyBanner and render the destination phrases as the existing `.link` button pattern.

- [x] **M5 [medium]** `frontend/src/components/GracePanel.tsx:20` · The grace list is the
  last-chance rescue surface, but a row shows only title, size, and days left; the
  reasoning that condemned the item is unreachable from here, so checking "why is this one
  counting down?" means switching to Review and retyping the title. **Fix:** make the title
  open the item's why-panel: add the candidate id to `GraceItem` (small backend field on
  `/api/grace`; the snapshot row is already joined) and lift a jump callback through
  ReapPlan to Dashboard's `setSelected`; a frontend-only fallback is jumping to Review with
  the search prefilled.

- [x] **M6 [low]** `frontend/src/components/ReapPlan.tsx:206` · Recent plans render only
  "#12 planned · REAP 5 ITEMS 120 GB": no date, no marker of which plan is open above, and
  clicking one silently swaps the view. `Run` already carries `approved_at` and
  `snapshot_id`. **Fix:** show `date(r.approved_at)` per row, mark the open plan (compare
  with `run?.id`, reuse the tabs' active styling), and when the opened plan's `snapshot_id`
  is older than the latest snapshot say "built from an older scan".

- [x] **M7 [low]** `frontend/src/components/Fairness.tsx:63` · A requester's never-played
  titles are dead text; the report's natural next step ("show me those in the queue")
  requires retyping each title. **Fix:** make each title a `.link` button that jumps to
  Review with the search prefilled (an `onOpenInQueue` callback from Dashboard, same shape
  as ReapPlan's `onGoToDeletion`).

- [x] **M8 [low]** `frontend/src/components/ScanBar.tsx:49` · When a scan finishes,
  `invalidateQueries` silently swaps the queue and totals with no anchor to what changed.
  **Fix:** before invalidating on the running-to-stopped transition, read the cached
  `["snapshot"]` and render a one-line delta beside "Last scan" ("2 more condemned than the
  previous scan, 40 GB more reclaimable"), cleared on the next scan start; both numbers are
  already on the Snapshot type.

---

## Agent Rules

Direct constraints for the fixing agent and all future UI work. They extend CLAUDE.md rules
17-21/36/39-45 with the specific failure modes this review found. Treat each as a blocker,
not a suggestion.

1. **Every text, number, and select box uses the one control chrome**: padding
   `0.42rem 0.6rem`, `border: 1px solid var(--border-strong)`,
   `border-radius: var(--radius-sm)`, `background: var(--bg)`, and
   `:focus { outline: 2px solid var(--accent); outline-offset: 1px; }`. Never introduce a
   new padding/fill/radius/focus combination; width is the only per-context variable. If a
   class is named in the standard's comment at the top of index.css, it must actually match
   the standard.
2. **Never hand-roll a control that has a shared primitive.** A visible either-or or
   choose-one-of-few is `<Segmented>`; an on/off is `<Switch>`; a number with a unit is
   `QuantityInput`/`FixedQuantity` with the unit persistently in the box, never
   placeholder-only. A comment acknowledging a twin ("exactly like X's") is a blocker, not
   a mitigation: export and reuse instead.
3. **No hardcoded values that duplicate a token.** Radii, shadows, scrims, and brand colors
   come from `:root`; a new brand color mints a token pair beside `--plex` (the documented
   precedent). Any hex that equals an existing token byte-for-byte must be replaced by the
   token.
4. **Every `useQuery` on a gating, always-visible, or state-claiming surface destructures
   `isPending` and `isError` and renders explicit fallbacks.** Deriving a boolean from
   `data?.x` that defaults to a definite claim ("Deletion is off", "No Plex server linked",
   "No webhook set", "Off (scan by hand)") is forbidden: pending says it is checking, error
   says it could not look (amber, never reading as safe or as fine).
5. **Every `useMutation` renders its error.** An `onSuccess`-only mutation is incomplete;
   the failure renders as `.notice.notice-error` with an outcome-led plain-language
   sentence ("The scan didn't start: …"). Success and error never share one state variable
   or one visual channel.
6. **Client validation must state the same number as the server.** Any client-side gate
   mirroring a server constant carries a comment naming that constant (e.g.
   `MIN_PASSWORD_LENGTH`), and the gate, the placeholder, and the server rule agree.
7. **Modals and sheets get full dialog treatment once, in a shared shell**:
   `role="dialog"`, `aria-modal="true"`, `aria-labelledby` the heading, Escape close
   (through any existing pending-guard), initial focus in, focus restored on close, Tab
   containment or `inert` on the background, and `max-height` in `dvh` with
   `overflow-y: auto`. A closed-but-mounted overlay is `inert`.
8. **Every interactive control has an accessible name that survives accessible-name
   computation**: pass the visible `title`/label through to `aria-label` on the inner
   `select`/`input` (aria-hidden content contributes nothing); repeated per-row buttons
   include the row subject in their label; placeholders are never the only name.
9. **Never remove a focus outline without an equal-or-stronger replacement at
   equal-or-higher specificity.** Composite controls use the `:focus-within` ring pattern
   (`.qty:focus-within` is the reference).
10. **Expose active state to assistive tech wherever it is shown visually**: `aria-current`
    on nav/tabs-as-nav, `aria-pressed` on toggle buttons, and never claim an ARIA pattern
    (menu, tablist, combobox) without implementing its keyboard contract; otherwise use the
    honest simpler role.
11. **Every new text/background pairing is computed against WCAG AA before commit, in
    light, dark, and both forced-theme blocks** (4.5:1 normal text, 3:1 large text and UI
    glyphs). `--faint` is decorative-only until its ratios are fixed. Never hardcode `#fff`
    ink on a token fill; use the matching `-soft`/`-ink` token, and never convey state by
    color or a hidden glyph alone.
12. **Whole-row `opacity` on rows containing text is banned** if it drops the composited
    text below AA; dim posters and chips instead.
13. **Tables and unbroken strings always get an overflow strategy**: every `<table>` sits
    in an `overflow-x: auto` wrapper, and code/paths/JSON get `overflow-wrap: anywhere`.
    The page body never scrolls horizontally at 375px.
14. **New width queries use the 1100/900/640/560 grid.** When overriding a rule that has a
    higher-specificity sibling (`main.split .why`), the override block must match that
    selector's strength; verify the cascade result at 375px width and short landscape
    heights, not just the source order.
15. **Operator copy: no em dashes and no spaced `--` in any user-visible string, frontend
    or backend `detail`.** No engine vocabulary (interlock, canary, abstain, matched,
    guard, phase ids), no exception class names, no `!r`/repr quoting, no markdown markup
    in plain-text strings. Errors lead with the outcome. One name per concept: condemned
    items are "removed", not "deleted".
16. **Copy may only name mechanisms that are wired.** Before writing help/warning text that
    names a capability, confirm the route or UI path exists in the same commit.
17. **Bulk operations report and preserve their failures**: return per-item results from
    `allSettled`, keep failed items selected for retry, and never clear selection
    unconditionally on partial failure.
18. **Delete dead CSS in the change that orphans it**; an unused rule carrying a
    nonstandard control chrome is a standing invitation to fork the standard.

## Suggested fix order

1. **Batch 1, broken on real devices plus the contrast fail:** A1, R1, R2, R3, R4.
2. **Batch 2, the unknown-state and silent-failure sweep (one mechanical pattern):**
   I1-I13.
3. **Batch 3, the shared modal shell:** C6 + A2 + A7 together (build `ModalShell` once,
   with dialog semantics, then migrate both modals).
4. **Batch 4, copy:** Cp1-Cp14 (backend strings first, they need a test pass on
   `src/reaper`).
5. **Batch 5, component consolidation:** C1-C5, C7-C12, V2, then remaining V/L/R/A items
   by severity.
6. **Batch 6, improvements:** M1-M8, each is an independent change.

Run `uv run ruff format .` before staging any backend copy change, and the full CLAUDE.md
gate set before each commit. When a change is observable in the app, drive it end-to-end
per the `verify` skill; R1/R2/R6 specifically need a 375px viewport check and A1/A6 a
dark-mode check.
