# Why the locked decisions are what they are

Each heading here is a row of the **Decisions locked** table in `docs/STATUS.md`. That table
holds the *choice*, in a phrase, because it is state and has to stay scannable. This file holds
the *reasoning*, because reasoning has a lifespan of years and does not change when a milestone
does.

Read the row first, then the section here if you are about to change the behavior it describes.
A decision reversed is edited here in place, with the reversal stated: several of these were
reversed once already, and the reversal is the part a future reader needs most.

This file is **appended to, by topic** — it is knowledge, not a changelog. It is *not* frozen:
everything here is doctrine still in force. The story of how a fix was chosen is
`docs/history/`; measured findings are `docs/LEARNINGS.md`.


## What a hand reap may overrule

**Choice: Everything except a structural stop.**

A hand reap condemns past every cautious protection, whether it FIRED or merely could not be
CHECKED. The only two refusals left are a *fired* `verdict.STRUCTURAL_GATES` member: something
streaming right now, and `unmanaged` (retired, so only a stored explanation can carry one).
Neither is a judgment about whether the file is wanted, which is the whole test: deleting
mid-stream breaks a session someone is watching, and a file no *arr manages has no path to delete
through, so overruling either cannot give the operator what they asked for. **The why panel's
strongest keep-claim now branches on that same test** (`WhyPanel.structuralStop`, one derivation
with the held-reap note beside it): a fired structural stop reads "it's kept, and a Reap won't
remove it", and every cautious protection reads "kept unless you Reap it yourself". It had said
"kept no matter what, and nothing can change that" about both, a few inches above the Reap button
that removes the file. **This reverses the earlier rule that a blocked gate holds a reap
fail-closed** (#84's fix, and `DEFERRABLE_BLOCK_GATES` with it, both now deleted). The reversal is
deliberate: a block means Reaper could not answer a question, and the operator standing at the
panel that names the failed check can answer it, so refusing them was Reaper overruling the
better-informed party. It also made Reaper unusable exactly where it was least reliable, since a
watch mirror shallower than the library blocks the popularity gate on nearly everything and
blocked every reap with it, which pushes an operator to delete outside Reaper with no journal and
no interlocks at all. What actually spares a file is unchanged and is a live read rather than a
frozen guess: `executor._being_watched_now` re-polls Plex per item at send time and spares on ANY
read failure, `executor._watched_since_approval` refuses an item played since approval, and the
transport guard refuses any mutation unless the host is armed and the intent was journalled first.
Two things still hold a reap on the stored-row path and neither is a protection: a bad Plex match
and an explanation that could not be parsed, both of which mean "we do not know what this row IS"
rather than "we could not check whether it is wanted". **"Could not be parsed" is the why panel's
own test, run on the reap path rather than approximated there** (`engine.explanation.read_explanation`,
one derivation for both readers, rule 104). It was two tests, and the narrower one sat on the
destructive path: the panel refused any bad field anywhere in the document while the reap read
checked only whether the two protections lists were the right shape, so a row with a string where
a signal's contribution belongs rendered blank and reaped anyway (**#142 closed**). An explicit
empty protections list still reaps — the scan looked and found nothing — but a `null`, a missing
key, or `{}` no longer does: those record no answer at all, and rule 1's omitted-is-not-empty
resolves toward keeping on a deletion path. **What counts as a bad match is phrased
against the one CLEAN value, never against a list of the bad ones** (`condemned.bad_match` and
`MATCH_CLEAN`, rule 1): it was a written-out set of three, so the fourth status shipped below
would have been reapable the moment it landed — Reaper offering to delete a file while refusing to
say what it is. Deriving that set from `identity.MatchStatus` (rule 103) fixed the drift but not
the shape, because a denylist has to enumerate every way a row can be unidentifiable and the one
it misses fails open: any stored status this build's enum does not define — a row frozen by a
later build and then rolled back, or a corrupted explanation — read as a confident bind and let
the reap through. An allow-list of one holds them all instead. `BAD_MATCH_STATES` survives as the
drift test's subject and as documentation, not as the gate. **That fourth status is `CONFLICTED`,
split off `AMBIGUOUS` (#202 closed).** One value carried four structurally different abstains and
every surface rendered it as one sentence, "more than one thing in your Plex" — true of the two
multiplicity shapes (an id or a file name naming several rows), false of the two disagreement
shapes (two id kinds naming different rows; the id, file name and title tiers naming different
rows), where each kind of evidence found ONE row and they differed. That is Plex and the *arr
describing one file differently over a library that may hold exactly one copy, so the operator was
sent hunting a duplicate that is not there. The disagreement pair now says so and names the app to
go fix (Radarr for a movie, Sonarr for a show), across all six surfaces that branch on the status.
**And an abstain finally offers a way out**: every jump link is built from the item's own rating
key, which is null on exactly these rows, so the panel named a problem in Plex and gave nothing to
open. `Resolution.candidate_rating_keys` carries the rows it was choosing between through to
`LinksOut.match_candidates`, and the notice offers a Plex and a Tautulli pill per row.
`GateResult.defers_to_owner` survives the change but no longer decides anything about a reap: it
tells the operator whether Reaper actually made the comparison, and **both surfaces that speak for
a conflict now read it** — the card's chip (`api.routes._chip`) and the why panel's verdict note,
which reaches it through `api.schemas.GateOutcomeOut` → `api.ts` (**#86 closed**). The panel had
been running the retired wording test, which passes for all three conflict shapes, so it opened
"This was watched more than a season your keep rule protects" about a comparison nobody made,
while its own reason block printed the producer's "Reaper cannot tell whether Season N …" denial
of it three sections below, far enough apart that nobody had both on screen at once. Three-state,
and the third state is the point: a row that can distinguish neither shape now asserts neither
("Reaper couldn't settle this one on its own"), where a `bool` default would have picked one for
it. Two rows reach that state, a row frozen before the flag and one carrying a value nothing can
read, and `engine.gates.thaw_defers_to_owner` is the one derivation all three readers go through.
**Its twin on the same model went the same way (rule 72)**: `threshold` had three readers and two
coercion rules too, so a stored `70.5` or `"abc"` failed the enclosing `Explanation` and blanked
every signal, protection and match block on the panel, beside a chip that read the row fine.
`api.schemas.thaw_threshold` now serves `_chip`, `_primary_reason` and the wire schema alike, and
a match block of the wrong shape reads as absent rather than taking the panel with it. **Both
readers reach that third state by one derivation (`engine.gates.thaw_defers_to_owner`, #112
closed)**, which they did not at first: the chip tested the raw byte with `is True`/`is False`
while the schema read it through Pydantic's lax coercion, so `1` and `"true"` gave the panel a
comparison the chip beside it declined to claim, and `2`, `"banana"`, `[]` or `{}` were refused
outright — failing the enclosing `Explanation`, dropping `_explanation_out` to its degraded body,
and leaving the operator a panel with no signals, no protections and no threshold beside a chip
that read the same row fine and a hand Reap that still condemned. Anything that is not exactly a
bool now reads as the third state, on both sides: a value nobody can read tells the two shapes
apart exactly as well as no value does, and it costs the operator none of the evidence still
legible around it (rule 96). Only a *hand-edited or corrupted* `reaper.db` reaches it —
`GateResult.defers_to_owner` is typed `bool` and `snapshot._explain` writes it directly — so this
is hardening, not a live defect. The reap half of that issue had already closed from the engine
side and stays open in every arm: no blocked gate holds a hand reap, so "Spare it to keep it, or
Reap it to remove it" is a promise the backend keeps whichever shape the conflict is

## Watch-history reach

**Choice: Every reader that goes through `Facts`**

answers only for a span its history covers (`Facts.history_reach_days`, `fields.reach_shortfall`)
— the popularity gate, the operator's own protect and removal rules, the graded keeps, and the
`FEW_WATCHERS` signal. **#94 is closed**, and it was the last reader that did not: the
keep-conflict detector compared two truncated counts and silently stopped flagging, reading the
mirror through a local variable rather than a fact, which is how the first sweep missed it. It now
takes each season's shortfall beside its count (`plan_series_prune(shortfall_by_season=...)`, from
the shared `gates.lifetime_shortfall`) and raises the conflict wherever more history could
overturn the outcome — the pruned count losing to a bound it may yet clear, or winning against one
that may yet rise. An outcome the bound already earns still stands, so a season nobody watched
over a mirror covering its whole life still clears. **The reach of that is wide, and it is the
intended cost rather than a side effect**: a season the mirror does *not* cover conflicts against
every kept season whatever either count says, because more history can always lift a lower bound
above anything. So wherever the watch history is shallower than the library is old, every prunable
season of an affected show is held, and *automatic* TV season pruning is inert until the mirror
catches up — the alternative being decisions taken on two numbers Reaper knows are wrong. It no
longer costs the operator the hand reap as well, which is what makes that bearable rather than a
dead feature: a blocked gate stopped holding a reap (see **What a hand reap may overrule**), so a
held season is one
they can still remove themselves. `_detect_conflicts` shipped claiming in writing that it did
*not* degenerate this way; it does, the claim is gone, and
`test_a_short_mirror_holds_every_prunable_season_of_an_old_show` pins it, because the mutation
that makes the degeneration total passed all 2626 tests. **#95 is closed**: the mid-binge hold now
consults the reach (`gates.progress_is_establishable`, taken by `plan_series_prune` as
`progress_established`). `in_progress_hold_days` is the span that guard *claims to cover*, not a
bound on the mirror, so where the reach does not span it — 0 included, an unbounded claim no
finite mirror supports — the viewer set is **un-establishable rather than empty** and every season
on disk is held ("your watch history is too short to tell who is part-way through") instead of an
unseeable viewer reading as an absent one. That hold is a **blocked** PROTECT, not a plain one
(`ProtectedSeason.unestablishable` → `season_scan.guard_result`), because it is a check that could
not be *answered* rather than a protection that fired, and the operator is entitled to see which
(rule 93). The reason it was ORIGINALLY encoded that way — `blocked` held a hand reap where a
plain PROTECT on this gate did not, so emptying `prunable` would have retired the keep-rule
conflict and turned a season a hand reap was refused on into one it deletes — has lapsed with the
row above: both are overrulable now, so the encoding decides what the panel says, not what a reap
may do (rule 143). Only the blanket hold carries the flag; a season an actually-visible viewer
holds stays a definite keep. `season_scan.gather`'s own `in_progress_hold_days` default moved 0 →
180 to match the policy's, since 0 is a value no shipped policy has and every test omitting it was
exercising that unbounded claim (rule 141). **There are three causes now, not one**, and they
share the encoding above because they are one question with three answers missing: the reach
(`progress_established`), a season whose plays stopped being readable (`progress_unreadable`,
`watch_evidence`), and a season with no Plex rating key at all this scan
(`progress_seasons_unmatched`, **#472 closed**). Each names its own remedy, since one sentence
for three would send the operator to the wrong place — more history, repair at the source, fix
the match. The third is **scoped to a show that did bind to Plex**, and that boundary is the part
to preserve: the harm needs a *mix*, where the seasons that resolved carry fully readable facts
and condemn at full confidence on a viewer the missing ones hid, so one duplicate "Season 3" in
Plex put the season a viewer had just finished season 3 for onto the reap list. Where the show
itself never bound, every season already takes Unknown from its own branch and abstains, so
widening the hold there moves a whole population of unmatched shows out of the review queue and
protects nothing further. Below that the count is a *lower bound*, so an outcome
a deeper mirror could overturn reports "could not check" instead: the gate and a protect rule
block, the signal withholds its pressure and lets coverage fall, a keep takes its full discount.
An outcome the bound already earns still fires — a count that clears "at least N" stays clear
however much history arrives. The two counts need different spans: recent watchers the policy's
window, all-time the item's whole life here (`Facts.days_since_added`). The shipped 1095-day
dormancy floor masks the *window* half, since dormancy is clamped to the reach, so condemning
under that floor means the reach already spans any ≤1095-day window; it bites operators who
lowered it. It masks nothing on the all-time half, where the span needed is the item's age rather
than a window, so a long-lived title behind a shorter mirror reads "could not check" on shipped
defaults. **A popularity window the mirror cannot cover is now named in the editor, before a scan
runs into it (#85 closed).** `policy.inspect` takes the live reach
(`api.routes._history_reach_days`, derived through `dormancy.history_reach_days` off
`history_sync.horizon`, the same derivation `ScanContext` uses) and warns when the window outruns
it. **The span it measures is `PolicyBody.popularity_window_days()`, not the enabled gate row
(#133 closed).** That call falls back to 365 with the gate off or absent and `build_gates` hands
the fallback to `CustomProtectGate` regardless of the switch, so an operator's own keep-outright
rule on `recent_watchers` fails closed library-wide against a year they never set — and the editor
invites exactly that, since `KeepRulesEditor` only hides a field whose gate is *on*. Reading the
row scoped the warning to one of the two PROTECT-lane readers and left the other silent. Where the
gate is on it anchors on `gates.server_popularity.window_days`; where it is off that picker is not
rendered at all, so it anchors on `protect_conditions`, beside the rule that is doing the blocking
and can be deleted right there. **That arm names the rule and counts them (#157 closed).** "Remove
that rule" identified it by what it does, and two rules on one field are constructible — `addHard`
appends unconditionally and `PolicyBody` validates the pair — so the singular remedy was factually
wrong there: removing one of a pair leaves the message byte-identical while a live protection is
gone. The description did not discriminate either, since a `watchers_all_time` rule beside it also
counts who watched a title and the only separator, the window, is unrendered by this branch's own
premise. The label comes from the registry the editor renders its cards from
(`fields.RECENT_WATCHERS.label` through `GET /api/vocabulary`), so it is the string already on
their screen rather than a second spelling of it (rule 144), and the count ranges over the rules
actually blocking rather than over `protect_conditions`. It is claimed only where it is true of
EVERY item, which is the field *and* the operator: `fields._survives_more_history` blocks only the
outcomes more history could overturn, so under `gte` every item is a fired PROTECT or a block and
nothing is condemned, while under `lte` an item already over the bar is settled, comes back a
plain checked ABSTAIN and stays condemnable — claiming an empty list there would be false in the
reassuring direction, and the remedy riding with it ("remove that rule") would strip the
protection off the items that *are* blocked. The condemn lane cannot empty the list through
*pressure*: it withdraws that pressure and keeps its weight in the denominator. It can through
*coverage*, **and that is warned about now (#164 closed)** — a blocked signal leaves the numerator
while staying in the denominator, so enough weight on reach-bounded fields drops coverage under
`coverage_floor_bp` and `decide_verdict` abstains library-wide. Measured with the gate off, a
60-day floor and a 90-day reach: weights of 40 unwatched / 60 few-watchers give coverage 0.40
against a floor of 0.50, and 35/20/10 beside a graded rule of 35 on the same count gives 0.45.
Weights need only total 100, so both splits are legal. The warning **asks `decide_verdict` rather
than comparing against the floor itself** (rule 3/22), which costs nothing and covers the second
way the same withheld weight empties the list: the score ceiling falls with coverage, so at 45
withheld a policy clears the 0.50 floor and still cannot reach a threshold of 70. It is summed
over the readers whose block is **library-wide**, which is not every reader of the field: the
built-in `FEW_WATCHERS` withholds on every observation it can take, and a graded custom rule does
too since `distinct_watchers` is never `Absent` in any fact builder, but a **boolean** rule goes
through `fields.evaluate` and keeps `_survives_more_history`'s earned outcomes. Measured at a
90-day reach: the same 35-point rule leaves coverage 0.45 for a title with 0 recent watchers and
0.80 for one with 50, where the graded arm holds 0.45 for both. A boolean rule therefore moves
**one** of the two bounds and is summed separately: it is all-or-nothing, so under `lte` the
blocked outcome is the MATCH and no item can earn the weight at all — one under the bar is
blocked, one over it did not fire — while coverage keeps it on the second. Driven on that same
35/20/10 split beside a 35-point `lte` rule, every watcher count from 0 to 50 abstains on a
ceiling of 45 against a threshold of 70, and all of them condemn once the mirror is deep: the list
is empty and the page used to say nothing, which the test asserting that silence described as
intended. Under `gte` the reverse holds, a settled item earns the full weight, and counting it
would claim an empty list that is not empty (rule 144), so
`fields.can_add_pressure_under_a_shortfall` reads the op — coverage alone cannot tell the two
apart. What is still not claimed is the partial case, an `lte` rule abstaining exactly the titles
nobody watched where the rest of the weight can still reach the threshold; that set cannot be
sized from one reach and stays filed as #215. The message names the weight to move rather than a
control label, since the signal sliders are labeled in `policyMeta.ts` and a second spelling here
would drift from them, and it anchors on `signals` or `custom_condemn` following whichever card
holds **more** of the points, ties to the signals card (rule 42). It followed mere presence of
built-in weight at first, so 5 points on the slider beside 50 on a custom rule sent the operator
to the smaller number and left the card that has to change unmarked; both tests covering the
anchor put the whole withheld weight on one side, where presence and magnitude agree. **The other
two lanes that can empty the list are warned about now (#144 closed), and the detector reads the
registry's span rather than one lane.** `_protect_blocks_on_reach` answers with the `ReachSpan`
instead of a yes for the window, because the operator test that decides whether a block is total
(`fields._survives_more_history`) reads the op alone and is span-agnostic — so scoping it to
`POPULARITY_WINDOW` was never the registry speaking. `watchers_all_time` carries the other span,
is PROTECT-only, and blocks through `gates.lifetime_shortfall` for every item the mirror does not
reach back to the arrival of. That warning **names the affected set instead of claiming an empty
list**: the span it needs is the item's age rather than a policy setting, and `inspect` is handed
one reach and never a list of arrival dates, so "nothing will be flagged" would be false in the
reassuring direction for a young library the mirror covers outright. The lean lane is the second:
a graded keep takes its *full* `max_discount` on a shortfall for every item it reaches
(`signals.evaluate_keep`, no earned-outcome test at all), and `score()` is `max(0, base -
keep_discount)` over a base bounded by `MAX_SCORE`, so keeps worth more than `MAX_SCORE -
condemn_at` hold them all under the threshold as provably as a blocked protect does. **That total
is summed per span, never per rule**: `evaluate_keep` grants each blocked keep its full
`max_discount` and `score()` subtracts the sum, so two keeps of 20 against a headroom of 30 empty
the list exactly as one keep of 40 does, and a per-rule test left that silent. Window keeps and
lifetime keeps are counted apart because they bound different things — a window shortfall is a
property of the operator's data so it reaches every item, while a lifetime shortfall is a property
of each item's age — so only window keeps crossing the headroom on their own may claim an empty
list, and the combined case names the affected set instead. The pre-existing `graded_keeps`
warning is a different warning and does not cover either, since it fires on `total_keep >=
condemn_at` — 70 against a headroom of 30 on shipped values, so a keep at 40 sat in a dead zone
warning about nothing. Both new warnings anchor on the rule doing it, the lean one naming the rule
because a `GradedKeepSpec` carries a name the operator typed where a `ConditionSpec` does not. The
dormancy guard covers all three, for the reason it always did: under the floor every item is kept
on age alone, so the remedy would move no verdict. `PolicyRuleEditors`' `leanFields` is not
gate-filtered, which is why the lean check does not read the gate either. **The two ends of that
control no longer stack (#134 closed)**: a window under 30 days also draws a "very short" warning
telling the operator to raise it, which sat in the adjacent sentence to a shortfall warning
telling them to lower it, with no way to tell which applied. Where both hold the shortfall speaks
for the control alone and carries the other's fault in its remedy clause. Shortening to the reach
does clear the shortfall; it just buys the other fault to do it, so waiting is the move that
clears one without deepening the other and it leads. Most blocks clear on the next scan, which is
why no surface was ever obliged to name a remedy for one; the ones that do not are all this same
family, a mirror shallower than the question, and the others sit on the season path
(`season_scan`'s lifetime-shortfall conflict, `gates.progress_is_establishable`). **The mid-binge
hold is warned about now too (#154 closed)**, and the sentence that used to sit here — "this is
the member with a control the operator can turn" — was false of it from the day that guard
shipped: `in_progress_hold_days` is a control on this same editor, one card down, and a hold the
mirror cannot span makes the viewer set un-establishable so `plan_series_prune` holds every season
on disk. The journey is what made it bite: an operator on a short mirror gets the window warning,
follows it, lowers the window to match their history, clears it, and is left with a page carrying
no warnings and a list still empty, because the warning they just cleared was the only surface
that ever named their reach. It is guarded on `progress_is_establishable` rather than on a
shortfall, because the two disagree at `in_progress_hold_days = 0` — "hold a place forever", which
no finite mirror supports and the predicate refuses at any reach, while `history_shortfall(reach,
0.0)` finds a zero-day span covered and returns `None` — so the predicate decides whether to
speak, the shortfall supplies the cause clause, and the zero arm carries its own. That predicate
moved from `services.season_pruning` to `engine.gates` beside its two siblings so `inspect` could
ask it without an engine module importing a service, one derivation rather than two (rule 104).
**The lifetime-shortfall conflict is the fifth member, and it is warned about now (#224 closed).**
It had been deferred as "the one member with no control behind it", turning on each item's age
against the reach with no setting the operator can move. That premise was wrong:
`flag_keep_conflicts` is a switch on this same editor, one row up ("Ask me first when a removal
looks unusual"), and off means the keep rule is simply followed. What is true is narrower, and is
why the warning carries no remedy naming it: turning it off is the *delete-more* direction, on two
numbers Reaper knows are wrong, so this family will not recommend it, and rendering beside the
switch is as close to offering it as the prime directive allows. The silence was real rather than
merely imprecise wording. Driven on the shipped TV policy at a 1200-day reach, deep enough that
all four siblings above and the dormancy-floor root are correctly silent, against a show 2000 days
old: `inspect` returned **no warning at all** while every prunable season came back held and
`auto_approvable` False. Like the `watchers_all_time` branch it **names the affected set rather
than claiming an empty list**, for the identical reason: the span is each item's age, and
`inspect` is handed one reach and never a list of arrival dates, so "nothing will be flagged"
would be false in the reassuring direction for a library the mirror covers outright. It also says
where those shows go, because they are not lost: every conflict carries `shortfall`, so
`season_scan.guard_result` marks each as a comparison Reaper did not make and the show waits in
"Needs a look", where a hand reap still condemns it. Beyond the dormancy guard the four share, it
is silenced by the **mid-binge hold** as well, which is rule 143's shape rather than tidiness:
where that guard cannot be established `plan_series_prune` holds every season *on disk*, so
`prunable` is empty, `_detect_conflicts` iterates nothing, and this lane is never reached, while
the branch above already claims the strictly stronger "no TV season will be flagged". The two
conditions read one hoisted derivation (`mid_binge_holds_everything`), not two copies of the
predicate call. It also asks whether the keep rule can produce a comparison partner at all:
`_detect_conflicts` iterates `prunable` against the kept seasons and that list drops specials, so
a policy keeping none on age alone (`keep_last_seasons` 0 with `keep_first_season` off) raises no
conflict however short the mirror is, and claiming the hold there would be false in the reassuring
direction — the operator would read that old shows are waiting for them while every season of
those shows is condemnable on score. Because the warning asserts a behavior of a *different
module* across a boundary, the rule 144 risk is that it quietly becomes a lie if
`_detect_conflicts` ever stops degenerating;
`test_the_policy_page_now_speaks_for_the_hold_this_test_pins` sits beside that behavior in
`test_season_pruning` so both go red together. **It fires only where that window is what is
actually holding the list back** (`inspect`'s `reach_clears_dormancy`): `MinDormancyGate` protects
anything younger than its threshold and PROTECT beats blocked, while dormancy is clamped to the
reach, so below the floor every item is kept on age alone and the popularity window decides
nothing. On both shipped policies those two ranges are disjoint, a 1095-day floor against a
365-day window, so the warning is scoped to the operators the masking sentence above already
names, the ones who lowered the floor. Without that test it fired for every install holding under
a year of history and told them to shorten a keep protection that changes no verdict. **The floor
itself is warned about now, and it is the ROOT of this family rather than another member of it
(#217 closed).** Dormancy is clamped to the mirror, so the most dormant any item can read IS the
reach, and `MinDormancyGate` PROTECTs anything under its threshold: a floor above the reach holds
the whole library on age alone until the mirror catches up. On the shipped 1095-day floor that is
every operator with under three years of history, which is most new installs. All five warnings
above are guarded on `reach_clears_dormancy` and go silent below the floor, each correctly, since
their remedies would move no verdict there — so the aggregate was a page that went quietest
exactly where the list was emptiest, with nothing speaking for the condition that silenced
everything. This branch is that voice, anchored on `gates.min_dormancy.threshold` where the
control is, and it cannot stack with the five because it fires on precisely the negation they are
guarded on. Driven on the shipped movie policy at a 90-day reach, the editor now returns this one
warning where it used to return none. `warn`, not `danger` — the outcome is that Reaper deletes
nothing. A caller that cannot read the mirror passes `None` and the detector stays quiet, the
`requests_app_configured` posture, since guessing short would condemn a window that is fine. The
cause clause is `gates.history_shortfall`'s own sentence rather than a restatement, because the
why panel prints that string to the same operator (rule 144), and the window is named ahead of it
so the in-margin arm's "that far" has something to point at. Note what this did **not** need to
fix: the hand reap. The issue was filed saying every Reap was refused, which the blocked-gate
reversal under **What a hand reap may overrule** had already closed by the time it was worked;
`reap_override_verdict_decoded` returns `condemn` on such a row today. **Scales is the same bound
down the reporting lane**, which reaches `watch_event` directly and never through `Facts`: it
cannot fail closed on a report, so it names the span instead (`PersonDetailOut.horizon_at` →
`components/watchReach.ts`). A zero reads "none since {date}" rather than "not watched", and an
empty mirror asserts no figure at all rather than a confident 0%

## Watch history that vanished

**Choice: A high-water mark that cannot fall**, never a remapped key

A Plex rating key is not stable. A file that leaves a library and comes back gets a new one,
while Tautulli keeps every earlier play filed under the old one, so Reaper reads the mirror by
the key the item carries *now*, finds nothing, and reports `Known(0)` watchers with maximum
dormancy. That is an affirmative "nobody ever watched this" about a title somebody watched:
maximum condemn pressure on the item that deserves it least, and the read path cannot tell it
from a genuinely unwatched item, because both are "no rows for this key". Measured, not feared:
0% of recently-played movies, rising to 1.5%, 1.5% and 4.5% across the 40th, 50th and 60th
deciles of a six-figure history, on a server that had never run a deletion through Reaper
(`docs/LEARNINGS.md`).

**The two rejected fixes are the reason this is written down.** *Remapping the key* needs a
remembered key to stay trustworthy after Plex may have reissued it to something else, and
`metadata_items.id` is a SQLite integer id, not a never-reused handle. *Keying the mirror on the
guid* was measured unsafe: about one guid in twenty-five in that library sits on more than one
live rating key, the same title held twice in HD and 4K, so a guid does not identify one item
and a guid-keyed read pools two separate candidates' plays. What is left is the one invariant needing
no key at all — **all-time watch evidence cannot fall** — so a count dropping to zero, or a last
play moving earlier in time, is a transition no library performs and those facts read `Unknown`.
A never-watched item reads zero on every scan, and 0 → 0 is not a fall, which is what makes the
check safe to run library-wide.

The mark therefore lives outside the snapshot lifecycle (`watch_high_water`, keyed on the
durable `media_key`) and is only ever raised in SQL. Compared against the previous snapshot
alone, the first blind scan would write zero as the new baseline and nothing could notice again.
A reading carrying no evidence is not recorded at all: it could not raise a mark, and a stored
zero would assert that Reaper measured the title and found nothing watched.

**Two limits are accepted, not fixed.** A title whose plays were *already* unreachable the first
time Reaper measured it has no mark to fall from, so the check never fires for it — the
population the deciles above describe. Closing that needs the play's guid, which the watch
mirror does not carry and cannot cheaply be made to: `history_sync` rebuilds the whole mirror on
any change to its column tuple, `plex.py` parses raw guids to `ExternalIds` and keeps no
`plex://` string to match on, and the guid would still not identify one item. Deliberately
deferred as not worth the lift (#269). Separately, `snapshot._fold_merged_watch_stats` unions a
merged group's counts onto the canonical item, so removing a duplicate listing is a real fall
with no churn behind it; it reads as unreadable and holds that title. Both are keep-direction,
and both are stated in `services/watch_evidence.py` rather than papered over.

**The escape hatch is required, not a convenience, and it comes in two widths.** Rebuild a
library without repairing its history and every watched title reads zero at once, so every one
is held and nothing is reapable — correct, and unusable. Settings → Plex discards the whole
record, two-step and behind the admin password, with a standing warning saying what it costs.
Deliberately not paired with a cache rebuild, which was the first design and was wrong: the
mirror is a faithful copy, so re-syncing fetches the same stale rows back. The repair is at the
source, in Tautulli's Fix Metadata screen.

**The password is the same one that arms deletion**, on the same lockout, and it was added after
the two-step shipped alone. Discarding the whole record is the only control in Settings that
withdraws a protection from every title at once: the mark is what separates "plays we can no
longer read" from "nobody ever watched this", so the scan after a discard scores every churned
title as never watched and `MIN_DORMANCY`, `SERVER_POPULARITY` and `DATA_HORIZON` all stop
holding it. No file goes when it is pressed, which is why a typed confirmation phrase would be
theater — but a stray click or a stale tab reaching it is the same failure arming has, and it
gets the same answer. With no admin password set the control is not offered and the route
refuses, pointing at the password step rather than at a password to guess. **The narrow twin
below takes no password**, and the asymmetry is the whole point: the gate is priced on losing
every mark at once, so charging it for one title would push an operator with one stale record
toward the control that clears the library.

The **per-title** escape is the why panel's, and it exists because the global one was the only
exit (#275). Two ordinary events leave a mark describing something its item no longer is —
removing a duplicate listing, and rebuilding an *arr database so a different title inherits the
record — and clearing either one cost every real mark in the library, which is a protection loss
reached by doing the one thing the UI offered. `watch_evidence.forget_one` is the narrow twin.

**Why it is a control and not an inference.** Both events present exactly as a real churn does:
a fall with no key change to explain it. Recording an identity beside the mark was tried on
paper and rejected — an operator correcting a wrong match in Radarr changes the identity under a
live mark, so discarding it there would withdraw the protection at the moment it was working,
and the scan cannot tell that from an *arr rebuild. A person knows which happened. The scan does
not, so the escape is a button rather than a rule, and "delete marks not seen this scan" stays
refused: it erases the protection during an *arr outage, the exact failure the guard exists for.

## Delete mode

**Choice: Grace is a notice window, not a gate.**

It starts a DB-only countdown and drives Leaving Soon + Discord. Nothing on the deletion path
reads it, so what actually spares a file at send time is the live played-since-approval and
streaming vetoes.

## Setup readiness

**Choice: Scanning and reaping are two readinesses, reported apart.**

`GET /api/setup/status` returns both. `scan_ready` is Tautulli plus one *arr, mirroring
`scan_runner.build_sources`. `reap_ready` is that plus a linked Plex and the admin password, and
every conjunct is a refusal that already exists elsewhere: `api.runs._preflight_refusal` returns a
409 without Plex or Tautulli, `executor.execute` raises the same two sentences as its backstop,
and `PUT /api/settings/safety` refuses to arm without a password.

The alternative — folding Plex into `complete` — was rejected twice, and the second time is the
reason this section exists. Plex is genuinely optional for a *scan*: an unlinked Plex adds no
degradation, unlike a linked one that fails, and an install that only ever wants to see what
Reaper would remove is a real install. So `complete` still means "the wizard has nothing left to
push", and it stays blind to Plex.

What that left was a `complete` reading like both: an operator with Tautulli and one *arr finished
the wizard, was told "You're all set", filled a review queue, and had their first real reap refused
at the button (#383). One boolean cannot answer two questions, so there are two. The refusal is
correct and unchanged; what moved is when it is said.

The sentences live in `frontend/src/reapReadiness.ts` rather than at each site, because the fact is
now stated on the last setup step and above Execute on the Reap page, and rule 144 is exactly about
one claim written twice. Two tests hold it, and it takes both: `reapReadiness.test.ts` walks all 16
combinations and asserts the list is empty exactly when the payload says reap-ready, which is
self-consistency, and `test_repo_hygiene.py` parses `reap_ready` out of `api/setup.py` and asserts
the frontend reads exactly its conjuncts, which is the tie across the wire. The first was described
as doing both for a while, and was not: the expected side is a hand transcription of the Python in
the same file as the assertion, so a conjunct added on the server left it green.

## Kill switch

**Choice: Asymmetric, not one-way**

arming is password-gated, disarming is one ungated click. The UI is the live control; the env var
supplies the default only until the toggle is first written, after which the stored value wins for
good. Re-read before every item, so disarming halts a run in flight

## Section nav

**Choice: Its own grammar, not the pill track.**

A rail whose active cut is a segment of the masthead's own bottom border on a wide screen; under
900px a fixed bottom bar of 24px icons, labels kept in the accessibility tree. The bar is 3.5rem
tall because that height *is* the tap target, and it has to clear 44px (WCAG AAA, Apple) and 48px
(Material). The pill track (`.tabs`, `.segmented`) now means only "pick a view of the same set",
so navigating and filtering stop looking alike. Reap carries the armed state as a dot, amber when
the safety read fails. **Settings' own nine-section rail takes the same 900px boundary**: below it
the wrapped tabs (two lines from 860px, three from 470px) become one `.settings-picker` select,
swapped in JS off `NARROW_SCREEN_QUERY` so only one of the two is ever in the tree. The Policy
rail keeps its tabs — four labels, and it reports what you have scrolled to

## Settings saves

**Choice: One save bar on General, the policy editor's `.savebar` reused**

(rule 43). Its six per-row Save buttons were rendered inside the right-aligned control box, so the
first keystroke shoved the field being typed in 71px sideways. The bar names every unsaved field,
sends them in one request, offers Discard, holds the whole save while the accent hex is
half-typed, and renders a refusal inside itself, since the route writes all six fields or none.
Controls that take effect the moment they change are not drafts and stay out of it: the
reverse-proxy Switch and the expand-seasons select save on the spot, and the theme select is local
to the browser. The spare-length Segmented was a third until it started staging its mode in the
bar instead: Forever is 0 in the same field the day box edits, so a press that wrote it dropped
the field out of the bar and took the day box's Discard with it. **Switching section asks first**:
switching panel unmounts that bar and every draft in it, so both switch paths (the rail button and
the narrow-screen picker) route through one `switchPanel` that raises the policy editor's two-step
confirm while the panel being left is dirty, and the notice clears itself when the draft does.
**Five panels report a draft**, which is the whole population, not just the one with the bar:
General, Plex's web address, manual connection row and pre-link certificate choice (it only writes
once a server is linked), the Discord webhook URL, Security's three admin-password boxes, and
Backup's staged restore from the moment the upload starts rather than from the summary landing,
each through its own `onDirtyChange`. **The last two took a hop the first three did not (#135)**:
their drafts live in a child component (`AdminPasswordForm`, `RestoreCard`), so the signal is
declared there and reported up through the panel. Backup keeps its card through a failed refetch
too, and took the shared stale line with it: the never-loaded sentence it used to show says to
reload, and a reload does not run the card's unmount cleanup, so the one exit that orphaned the
staged archive was an operator doing what the page told them. It carries its own sentence in the
confirm, because the shared one would be false twice over — what is waiting is an uploaded file
rather than a setting, and leaving does not merely forget it: the card sends `restoreCancel` on
unmount, so the archive already staged on the server goes with it instead of sitting there
unreachable, an un-armed stage having no surface anywhere in the app. **It names the archive it is
reclaiming**, since two of these cards are live now — Settings, and the wizard's restore door —
and an upload replaces the staging directory rather than adding to it, so an unscoped discard let
the first card to leave take the second card's archive and leave that operator holding a reviewed
summary with nothing behind it (#387). The token minted at stage time answers who staged what, the
way it already answers what the password may arm (rule 73), and the server discards nothing once it
no longer matches. The operator's own Cancel on an ARMED restore stays unscoped, because that card
may never have seen the summary behind it: an armed restore survives the browser that made it, and
a scoped Cancel there would refuse the one press that clears it. That cleanup asks the SERVER
whether a restore is armed in the moment before it sends, because the same route discards an armed
restore too and the state holding both at once is reachable — staged here, armed from a second
tab, which nothing refreshes this tab's cached answer for, so reading the cache would disarm
exactly the restore the guard exists to protect. It waits on an upload or a confirm still in
flight for the same reason: a prepare stages its archive after the card is gone, and a confirm has
already been paid for with the admin password. It is the two intra-Settings paths only: leaving
Settings by the masthead, by the safety banner's Policy link, or by browser Back still drops the
drafts silently. What a panel reports is what it would LOSE, not what a bar names: a proxy list
parked behind its own switch is dropped from the bar on purpose and still counts, and a webhook
too malformed for Save to accept is still a secret to re-copy. Both halves of that report are read
against every early return (rule 146), which is why a failed refetch now keeps the Plex and
Security forms on their last good values instead of trading them for an error paragraph. The two
fail in opposite directions and both are the same mistake: Plex went on reporting a draft whose
boxes, Save and Discard the error branch had taken off screen, while Security's form lives in a
child, so the same branch UNMOUNTED it and three typed password boxes went with it in silence.
Security is the one whose clock always runs — `useSafety` polls every 15 seconds and on window
focus, so one failed poll reached that state with the operator doing nothing but typing. Jobs has
one too, and it is the conditional one: 1.5s while anything is running. General, Plex, Security
and Backup all then SAY the read failed, from one shared `StaleReadNotice`: keeping the form is
what keeps the draft reachable, and keeping it silently presents link state, servers, libraries
and whether a password is even set as current when the panel knows they are stale. **About and
Jobs take that same line for a different reason**: they hold no draft at all, and kept their
content through a failed refetch anyway while printing "couldn't load this page" on top of it, so
the sentence was simply false above a page that was right there. Jobs reads worst stale, since its
rows carry next-run times and a running flag and the query polls itself every 1.5s while anything
runs, so it reaches the failed read with the operator doing nothing, and
the notice goes ABOVE the rows, since it says what is below may be out of date and a panel is
plain block flow. **Several reads failing at once now say it once (#198)**, naming the panel:
Jobs read that one failure twice over, its own notice plus the Leaving Soon row's inside it, and
Plex read it up to four times, since `invalidateAllPlex` refetches every group on the page and one
server switch against an unreachable Plex drew a line over each. The scan row's schedule line was
already exempt, on `failed && !job`, which is the same judgment made once by hand. `collapseStaleReads`
is the rule and it counts the lines that WOULD draw rather than grouping by invalidation: React
Query does not expose what caused a refetch, and Jobs' two reads are independent polls that fail
apart, so an invalidation-group rule would collapse nothing on the panel this was filed about. **A
lone failure keeps its own noun in its own slot**, because "the library list" tells the operator
more than "the Plex settings" and the panel noun is only worth reaching for once it speaks for
several; and only a failed REFETCH counts, never a read that never landed, which is a different
claim ("there is nothing here") that keeps its own never-loaded notice per group. **Notifications is the seventh**, and it is a third reason: it never had an
early return to lose its form to, so the failed read printed "couldn't check whether Discord is
connected" directly above three controls derived from that very answer — the
keep-the-current-webhook placeholder, an enabled Remove, and a Send test that fires at the stored
webhook — and told the operator to reload, which throws away a pasted webhook that is a secret and
is never shown again. The notice takes a noun for what it is about ("these settings", "these
details", "these jobs", "the shelf status", "whether Discord is connected", "the library list",
"the Leaving Soon settings"), so a caller may vary what the line is about but never what it says,
and each noun is pinned by a test rather than left to the loose form that matched the default one.
**It is no longer a settings line (#190)**: the same divided test now covers the review queue, the
expanded season list under a show card, the Scales board, the not-in-the-last-scan panel, the
services list and the service editor's two mapping grids, with nouns of their own ("the queue",
"the seasons", "Scales", "the last scan", "your connections", "this instance's folders", "your
Plex libraries", "this portal's services", "your Sonarr and Radarr connections"). The season list
writes the sentence as a `.season-list-note` rather than a notice, from the same one declaration,
because the list's own loading and failed lines are already that grammar and a notice block does
not fit inside the card. Not rule 42, which permits bare `.error` in the review surfaces rather
than banning `.notice` there: the queue itself renders one. **No count of the callers is written
down anywhere**: one was, and it stood at six while Notifications already was the seventh, which
is how a gap gets recorded as future work and then reads as an assurance that the sweep is
finished. What IS counted is the population the sweep walks (#197):
`test_every_query_failure_branch_is_counted` resolves every `error`/`isError` in shipped
`frontend/src` to the hook that produced it and pins 46 read handles across 15 files, failing on a
`use*` initializer it has never seen rather than guessing whether it is a read or a mutation. It
deliberately cannot tell a divided branch from an undivided one — roughly half of the 46 are
undivided on purpose, every safety indicator among them — so what it proves is that a new one
cannot arrive without somebody deciding which of the two it is. The three previous sweeps each
found sites the last had missed, and the old `<Notice>` count could not have caught any of them:
it was a different population, so it agreed with itself while disagreeing with the tree.
**Surfaces, not files**: Plex carries the line at three places (#166) and the service editor at
four, Plex's status read having taken it in #140 while its library grid and its Leaving Soon group
went on trading the whole grid, and both shelf switches, for an error paragraph over a list still
held — so a bare `isError` found in one branch is a reason to read the rest of the file. **The one
site that deliberately keeps refusing is the reap ledger** (`ReapBreakdown`): what it holds is a
set of delete counts and its key is override-aware, so a Spare click refetches it and a kept
ledger would state how many titles a reap removes at the moment the operator changed that number —
the same answer the simulator's column already gives, written down there now rather than left
looking unswept. **The shared line no longer says "Reload to try again." (#153)**: it carried that
advice on all seven, which is the same harm dropping the never-loaded sentence from Backup fixed,
reintroduced by the line that replaced it and on more panels than the original reached — three
typed password boxes on Security, a staged restore on Backup, a pasted webhook on Notifications, a
typed address on Plex and General, and no `beforeunload` handler anywhere to ask first. Nothing
honest replaces it: `refetchOnWindowFocus` is off app-wide, and the only intervals behind any of
these reads are Security's, which always runs, and Jobs', which runs only while a job does — so a
line promising Reaper keeps trying would be false on most callers and, on Jobs, false exactly half
the time. That paragraph used to say "only Security has an interval" two sentences after saying
Jobs polls itself every 1.5s, which is the rule 144 failure this whole line exists to avoid, in
the doc rather than the code (#196). A retry the operator can press without losing the draft is
not built into the shared line; three exist elsewhere in the tree, two inside a failure notice in
the log panel and one beside the Plex server list. **Eight hand-written siblings kept the clause
and have now lost it too (#195)**, each of them rendering while something destructible is on
screen: the Discord panel, whose box takes a secret Reaper encrypts and never shows again and
which has no early return, so that box is up in every branch; the queue's freshness line, above a
bulk selection "Select everything matching" may have walked thousands of rows to build; both
add-a-rule forms, which destroyed unsaved rules in one sentence while the next called them
unaffected; Plex's library grid and Leaving Soon group; the Pace notice inside an editor whose
savebar may be dirty; and the reap ledger's unknown-allowance branch. What remains is pinned per
file by `test_the_reload_advice_population_is_pinned_per_file`, because the enumeration behind
#195 was not the whole population: four more said it, and #225 settled what they cost from the
component tree. `<main>` is a plain ternary on one `view`, so the plan loader, the ledger refusal
and the not-in-scan panel all sit in an arm that has already unmounted the queue, and their advice
costs nothing. The reap sheet was the exception, rendered outside `<main>` and gated on
`reapSheetRun`, so it opens over a mounted queue and a reload drops a bulk selection nothing asks
about; it points at its own close now. **The same split now covers the two gates above everything
(#181)**: a failed `["setup"]` refetch used to render the setup wizard over a configured install's
whole Dashboard, and a failed `["me"]` refetch the login screen at a signed-in operator — both
from ordinary in-app actions that invalidate those keys on their success path, and both unmounting
every panel below, so Settings' unsaved-edits guard never ran because the unmount came from above
the panel holding the draft (rule 146). A status or a user that never landed still routes to the
wizard and to Login, which is what those arms were written for; a signed-out answer arrives as
data (`["me"] = null`), not as an error, so the gate catches it either way. **That only holds
because a writer meaning to convey SIGNED OUT puts that state in rather than refetching to
discover it**: the unauthorized handler exempts the whole `/api/auth/` prefix, seven routes
including this key's own read, so a 401 there comes back as an error with the last good user still
held beside it, and a sign-out that merely invalidated the key left the operator on their own
dashboard over a dead session until an unrelated poll happened to 401 (`useSafety`, 15s, on a path
that is not exempt). Sign-out writes `["me"] = null` on success and keeps the refetch for the
failure case, where the question is open rather than answered and the session may well still be
live. A draft is measured against the stored value in one canonical form both sides compose (rule
39), so opening the manual address row reports nothing, and a Save re-seeds the box from its own
response, so an address cleared back to the hosted default stops reporting one instead of asking
to be discarded forever. Neither panel reports one before its boxes hold what the server sent
(#139): the form renders on the first pass that has the stored row and an effect copies it in
after that, so General gates its comparisons on a seeded flag and Plex on which stored value its
boxes were seeded from — a sentinel no stored value can equal, because `["plex"]` stays cached
across a section switch and hands the panel the stored address on the very first render, where a
guard seeded from that value agrees with it and passes. Plex's own inline Save reads that same
guard rather than a second copy of its comparison, so the bar's claim and the button on screen
cannot drift apart. Plex keeps two inline Saves, deferred in writing

## Settings row layout

**Choice: One fixed control track for every box, released for everything else.**

`.set-row` reserves a 22rem control column so every text, number and select box lines up on one
edge (rule 40). A Switch, a button and a link buy nothing from it, so seven rows carry a
`.set-row-plain` opt-out (`minmax(0, 1fr) auto`) and give the width back to the label and its help
paragraph: measured, the reverse-proxy row falls from 291.9px to 153.1px at 641px, and the General
panel saves 178.5px there and 59.5px at 768px. The control's right edge does not move at any
width, and nothing changes at 900px and above. `.set-row-cluster` remains the other opt-out, for
the API key's field-plus-buttons and the manual Plex address

## Peer trust

**Choice: `reaper.auth.proxy` is the only place a forwarded header is believed, and every
launch passes uvicorn `--no-proxy-headers` so that stays true**

(**#125 closed**). uvicorn ships `proxy_headers=True` with `forwarded_allow_ips="127.0.0.1"`, so
its `ProxyHeadersMiddleware` rewrote `scope["client"]` and `scope["scheme"]` from
`X-Forwarded-For`/`-Proto` before `AuthGuard` existed — making the trust decision one layer above
the operator's `trusted_proxies` setting, from a default they never set. Wherever Reaper's peer
really is loopback (host networking, a same-host proxy published to `127.0.0.1:8420`, a container
sharing the netns, the dev servers) a caller could rotate a fake address past the per-IP lockout
on sign-in, the API key and the admin password, and hand itself a `Secure`/`__Host-` cookie its
own browser drops on a plain-HTTP install. Both with reverse-proxy trust **off** and nothing
listed, which is what `.env.example` already promised was fail-closed. The shipped bridge-network
compose sees the gateway rather than loopback and so was never remotely exposed; that bounded it,
it did not make the promise true.
`tests/test_repo_hygiene.py::test_every_uvicorn_launch_disables_proxy_headers` matches the
invocation rather than a list of files, so a launch added later is covered when it is written
(rule 72). **`is_secure_request` also stopped short-circuiting on the ASGI scheme**, which was
wrong twice: it answered about the proxy's leg instead of the browser's, so a proxy speaking HTTPS
to Reaper while serving plain HTTP got a cookie the browser drops, and the scheme is the very
field such a middleware derives from the header, so believing it laundered an unauthenticated
claim into a trusted answer. A trusted proxy's claim now outranks the scheme in both directions;
an untrusted `https` claim is refused outright; a bare scheme is still evidence only when nothing
claimed one, which is what keeps direct TLS working

## ORM

**Choice: Plain SQLAlchemy, not SQLModel**

the model layer carries safety-bearing nullability and constraints, and we keep them declared in
one place we control

## Migrations

**Choice: Baseline `22777b2b5015` is frozen going forward**

(testers have real data). It was edited before the freeze held, which is why
`heal_candidate_size_nullable` carries a reflection guard (rule 81). Every schema change is its
own revision chained onto head: an add, a new table, a backfill, or a guarded rebuild. Nearly
always *widening* — the one exception is that same heal migration also dropping a stray server
default, safe only because the ORM carries the Python-side default. `cache.db` stays disposable.

## Gate retirement

**Choice: a stored body self-heals on load, and the save boundary only accepts a gate the
engine can build.**

`UnmanagedGate` was retired under rule 38/117: it shipped enabled by default and could not
fire. `PolicyBody.RETIRED_GATES` drops it from a stored body on load, which is what keeps
existing installs scanning, because every policy row on the test server named it and
`build_gates` refuses a gate it cannot build. The set covers `OTHERS_WATCHING` too, retired
earlier and missed by the first version: it never shipped in a default policy, but the save
boundary accepts any `GateId`, and a body carrying one had no self-heal. `tests/test_policy.py`
pins the set against every id `build_gates` cannot construct, so the next retirement cannot
forget it.

The save boundary was the wider half of the same hole: `GateSettingIn.gate` took any `GateId`,
including the two the engine emits with no policy row behind them, so a hand-crafted save could
store a gate no scan could build. `POLICY_AUTHORABLE_GATES` (in `engine/gates.py`, pinned
against `GATE_TYPES`) is what the boundary now checks, and a wire-schema refusal reaches the
operator without pydantic's `Value error,` prefix.

Retiring a gate moves `scoring_hash` and `evidence_hash` as well as `policy_hash`, so the first
Policy page after an upgrade shows the simulator's "needs a fresh scan" state with no numbers;
that notice states the condition instead of telling the operator they changed something.

`GateId.UNMANAGED` and the four surfaces that decode a stored explanation stay
(`STRUCTURAL_GATES`, the chip phrase, the why-panel line, and `WhyPanel.tsx`'s `CHECK_COPY`
entry for the gate's blocked branch). `Facts.is_managed` stays too: it is a true observation and
the evidence any re-wiring would need, which is a Plex-first scan path, not a change to the
gate.

## Plex index retirement

**Choice: a spine row the sweep did not return is dropped, but only once the sweep has
actually spoken.**

The Plex index retires spine rows Plex no longer has (`services/library_index`). The Tautulli
media-info listing is a cache and lags in both directions; only the fresh-additions half was
handled, so a rating key retired in Plex survived as a phantom carrying a stale title, year and
added-at and no ids or file name. Carrying neither, it could only act through the resolver's
title+year tier, and there it both vetoed good binds (the file name naming the real row, the
phantom's title naming itself, abstain) and originated its own: with the *arr's file renamed,
title+year is the last tier standing, the item binds a rating key Plex 404s, reads as *matched*,
and the fact layer takes its affirmative branch — `Known(0)` watchers, dormancy anchored on the
phantom's added-at — so a live file collects maximum condemn pressure at full coverage from an
item that is gone. A dropped spine row makes the item resolve unmatched, which keeps it.

It is gated on the sweep having actually spoken, since a failed sweep and an unconfigured Plex
both return an empty map and reading either as "Plex has none of these" would retire the library
on a read that never happened (rule 2). Past 20 rows overall **and** 10% of any ONE library's own
spine rows it degrades instead, because that is a section the sweep never walked rather than a
stale cache. The share is per section on purpose: measured across every library of the type, a
whole-library total could not deliver the case it exists for, since a small library vanishing
whole beside a much larger one clears the row floor but sits under 10% of the total, so the scan
would have reported nothing while every item in that library silently resolved unmatched. Both
media types go through one builder.

## Why-panel scope

**Choice: it renders for keeps as well as deletes.**

An item can score high enough to be condemned on score alone and still be protected by a gate,
and the panel says so in as many words, with the numbers that produced the verdict:

```
Example Movie  (5.9 GB)
VERDICT: CONDEMN   score 91/100  (threshold 70)

  +70.0/70   not watched in 5 years, 7 months
  +20.0/20   nobody watched it in the last year
  + 1.0/10   IMDb 5.4

  ✓ Untouched for 5 years, 7 months, past the 3 years it has to sit unwatched first.
  ✓ 5.4 on IMDb from 6,000 votes, below the 7.5 you keep.
  ✓ Nobody here watched it in the last year.
```

A tool that only explains its deletions cannot be trusted about its keeps. So every protection
that was checked and did *not* fire is shown too, which is what makes the record auditable
rather than a verdict handed down.


## Size acquisition

**Choice: Sonarr or Radarr's own total, never a stand-in.**

`Candidate.size_bytes` is the accounting column: it feeds the byte caps, the reclaim estimate
and the number printed beside the confirmation phrase. It therefore has to measure what a
delete would free as closely as anything available can, and only the service that will perform
the delete is allowed to say. Every other source Reaper already holds was considered and
rejected, and each rejection is cheap to re-litigate by accident, so they are written down here.

**For a movie it is a lower bound, and that is accepted rather than fixed.** This paragraph used
to say "a bound is not a measurement," which read as a principle and was really a claim about
Radarr that turned out to be false in the other direction. A season prune deletes the same
`EpisodeFiles` rows Sonarr's statistic sums, so there the number and the delete are one quantity.
A movie delete takes the whole folder while `sizeOnDisk` sums tracked rows, so untracked extras
are freed uncounted. Measured (#317, learning 14b): the folder held more in 221 of 221 sampled
folders and never less, by 0.02% at the median, 1.2% at the 90th percentile and 44% at the worst,
0.2% aggregated. Three quarters of those bytes are untracked video rather than artwork, so the
distribution is a small floor with a heavy tail.

The cost is that a byte cap behaves like a slightly larger one. It cannot delete anything
unapproved: the operator approves items, and the caps only bound how many approved items proceed.

Three fixes were considered and declined. **A headroom margin** on the byte caps would have to be
sized to the median, where it buys nothing, or to the tail, where it eats most of the operator's
cap; either way "your 500 GB cap really means 480 GB" is a hidden fudge. **Walking the folder** is
genuinely available — Radarr's `/api/v3/filesystem` returns a size per file and recurses, which is
how the measurement above was taken — but it puts a new endpoint dependency and an N-request walk
per item on the deletion path to correct 0.2%. **Rewording the operator copy** was declined for
the twenty-odd strings that say "disk freed" or "would be freed": at the resolution an operator
reads a round-number cap, the figure is right, and hedging every one of them would cost more
confidence than the gap it describes. What was *not* declined is the absolute claim: three copies
said the rolling byte cap made a multi-terabyte incident arithmetically unreachable and that no
sequence of runs could exceed it. Those are bounds now, in `ProfileSettings`,
`Executor._check_rolling_caps` and `tests/test_policy.py::TestCaps`.

**No Plex season or episode fetch.** A show listing carries no `Media` element, `PlexClient`
has no children method for seasons or episodes (`collection_children` is the only one), and
season rating keys come from Tautulli rather than Plex. Reaching them through plexapi objects
would reload per item, which is the blowup `clients/plex.py` was rewritten to avoid. Sonarr's
episode files are cheaper and authoritative.

**No Tautulli `file_size`.** The sweep carries it, and it reports `0` for every show-level row
while lagging for movies. Adopting it would reintroduce exactly the zero-means-unknown trap the
nullable column exists to remove.

**No new movie-file route on `RadarrClient`.** Unverifiable from this repo — no OpenAPI spec is
vendored — and unnecessary, because `movieFile` already rides on the list payload that
`snapshot`'s helpers parse. The same argument retired `includeEpisodeFile=true` on the episodes
call: `episode_files(series_id)` is verified by production use in `executor._send_season`.

**Never sum `PlexItem.files`, and never sum across `merged_rating_keys`.** Both over-state, and
on the deletion lane an over-stated size is the direction that spends a byte cap on bytes that
were never there. `_parse_sweep_element` flattens every Part of every Media into one tuple, so
an optimized copy inflates the sum; merged listings are byte-identical twins of one file, so N
listings yields N times the bytes.

**Never read the size from `facts_json` per consumer.** It is nullable by design, so it cannot
be a sole source, and reading it there splits one fact across two stores that can disagree. The
column is the accounting surface and holds the accounting truth.

**An unmeasurable size is stored NULL, never a sentinel.** A `-1`, or a `0` beside a
`size_known` flag, is the same defect wearing a different number: every arithmetic site accepts
it silently and produces a wrong total, and it holds only for as long as every call site
remembers to ask. A NULL makes the sites that forget raise. The repo had already made this call
once for `watch_event.watched_status`. Existing stored zeros were not backfilled or
reinterpreted — they are irrecoverably ambiguous, `Candidate` rows are snapshot-scoped, and the
next scan regenerates them.

The refusal that follows from all of this lands at the **planner**, not the verdict: rule 22
keeps condemn/abstain/protect in one function, the scoring lane is already honest about an
unknown size, and feeding an accounting column back into the decision would make a verdict
depend on which acquisition rung fired. Whether an item can be safely *acted on* is a different
question from what the evidence says.

## Versioning

**Choice: CalVer `vYYYY.M.N`, tagged by CI on every push to `main`**

A date answers what operators actually ask ("am I on this month's build?"), and nothing about
Reaper's surface fits semver's promise: there is no API anyone pins against, and every release
must be safe to take. `N` counts cuts within the month; the month is unpadded because PEP 440
normalizes `2026.08` to `2026.8`, and two spellings of one release is how version comparisons
quietly break.

The tag comes from `release.yml`, not a hand: `main` is release-only, so a push to it *is* the
release decision, and the workflow derives the next free number from the tags that already
exist (idempotent, so a failed cut is re-run with `workflow_dispatch`). The cut first waits for
ci.yml's gate to report green on the same sha: a promotion squash is a sha CI never tested, and
the two workflows otherwise share nothing (#429). Binaries, the snap, the
versioned image, and the GitHub release all bake the same string via `buildinfo.json`, and the
in-app update check compares against it, so an operator is only ever told about a version whose
artifacts exist. Dev builds stay sha-named (`dev (abc1234)`): the rolling `dev-build`
prerelease is replaced nightly and never enters winget or the snap stable channel.

The notes are GitHub's own generation, sectioned by label through `.github/release.yml`, over
the pull requests between the previous tag and this one. That range only exists on `dev`: the
promotion squash is one commit for a whole release, so the tag points at the promotion PR's
head commit rather than the squash it became — a commit the promotion recipe in `CLAUDE.md`
keeps connected to `dev`'s line. The two carry the same tree, which the workflow verifies
through the API rather than assumes (the promotion branch deletes itself at merge, so the
commit is not in the release checkout; the first cut fell back over exactly that and was
retargeted by hand). The artifacts still build from the pushed sha. The first cut ships
`.github/first-release-notes.md`: a generated list spanning the whole history is not release
notes.
