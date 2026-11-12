---
paths:
  - "tests/**/*.py"
  - "frontend/src/**/*.test.ts"
  - "frontend/src/**/*.test.tsx"
  - "frontend/src/test/**"
---

# Test blockers

Every rule here is a blocker. Rule numbers are permanent, because existing tests and the
archived review passes in `docs/history/` cite them. Where two rules overlap, the more specific
one governs. A test docstring or code comment states its constraint in plain language and
does not cite a rule number. Holds 37, 118, 119, 132, 133, 135–137, 141, 145, 147.

**37. Tests that boot the app are hermetic.** Use the shared autouse `_hermetic` fixture in
`tests/conftest.py`. It stubs env seeding and the network calls made at startup. Never let a
test read the developer's `.env` or reach the network.

**118. Every deletion-path interlock has a test that fails when the interlock is deleted.**
When a guard upstream makes the interlock unreachable through the public path, write the test
against the interlock function directly. A refactor can delete an interlock that is unreachable
and untested, and no test fails. This rule covers the route-to-planner conversion of an empty
selection as fully as it covers the byte cap. Sometimes an interlock's two arms cannot be told
apart at that function's interface. Then say so in the test's own docstring, and name the test
for what it does pin. A test that cannot tell the two arms apart must never read as a proof.

**119. A test never re-implements production logic, never asserts on a bare `Exception`, and
never rests on an environmental accident.**
- Agreement tests call the real function (rule 3/22), never a transcribed copy.
- Write expectations as explicit tables from the spec. Never transcribe the branch structure
  the test checks.
- Where a test mirrors a production pipeline, extract the shared part and point both at it.
  Never patch the copy. When the copy also generates the fixture's own baseline, nothing can show
  that the copy drifted from production.
- Assert the domain error and its message, never `pytest.raises(Exception)`.
- A test whose evidence is a closed port, a non-root uid, or any other property of the machine
  proves nothing. Stub the boundary and assert what was actually sent. Add a case no
  environment can skip.

**132. Test infrastructure may not imply coverage that does not exist.** A fixture, helper, or
`conftest.py` comment that names a function's poll loop reads as "this is covered". If that
function has no test, write one or stop naming it.

**133. A test leaves no process-global state behind, and never rests on a clock production
also samples.** Anything that mutates process-global configuration (logging, env, registries)
restores it. Otherwise the next test in the same xdist worker inherits it. A test that re-reads
the wall clock production also reads rests on an environmental accident, as in rule 119. Freeze
the clock or assert a range. Never sample the clock twice and compare.

Where the suite already makes a delay instant, elapsed time cannot fail at all, so a range on
it proves nothing. `conftest.py` makes every `asyncio.sleep` instant, so a clipped and an
unclipped delay return equally fast (#346). For a delay, take `conftest.py`'s `slept` fixture.
It owns `loop.time()` and advances it by each delay. Assert the delay that was asked for. The
requested delay is the behavior, and elapsed time is a measurement of the machine. This is how
rule 118 applies to anything bounded by a deadline.

**135. A module mock answers everything the tree under test reads, and a gap fails the run,
because a warning cannot.** `vi.mock("../api", …)` hands every consumer `undefined` for what it
omits, including reads no test in the file names. React Query renders that as a failed read, so
the test passes against the fallback. `src/test/setup.ts` fails the run on such a gap.
`src/test/apiFixtures.ts` holds the payloads as the shipped defaults, so adding one does not
change what a passing test renders.

The gate in `setup.ts` misses a mock gap when the read goes through an arrow function. The gate
sees `queryFn: api.profile` handed `undefined`. It cannot see
`queryFn: () => api.vocabularyValues(f)`, where the `queryFn` exists and throws inside. React
Query files that as an ordinary rejection, and the tree renders its failed-read branch with no
error and no warning. Read the tree's `queryFn`s, not just its hooks.

**136. A state update outside `act(...)` fails the run, with no exemption for the framework.**
`src/test/setup.ts` enforces this. Such an update means the test asserted on a state the
component had already left.

- **Test clients come from `testQueryClient()` (`src/test/queryClient.ts`), never `new
  QueryClient`.** A `new QueryClient` retries and refetches on window focus by default.
  User-event's click fires real focus events. So a click on anything focusable refetches every
  stale query. The refetch lands after the assertions, and the failure names a component the
  test never touched.
- **Import `userEvent` at the top of the file.** A mid-test `await import(…)` is a bare await
  outside `act`, and a fetch that `render()` started can land during it.
- **Seed with `seedSettings()`** the settings a test does not vary. A mock alone answers a
  microtask late, so a synchronous test body asserts on the pending panel. Never seed a key the
  test does vary. Seeded data is fresh, so the seed answers a read the test means to reject or
  stall, and nothing reports it.
- **Fix the async, not the scheduler.**

`setup.ts` also fails the run when two siblings share a key. React 19 paints both, so no
assertion catches the duplicate, and rule 19's reconciliation guarantee is lost with no error.
Without this gate, a test of a key fix passes whether or not the fix works, and reads as a proof
it is not (rule 118).

**137. A test acts on a control only once the control can be acted on, because user-event
reports "disabled" as success.** `click`, `selectOptions` and `upload` return having dispatched
nothing when the target is disabled, with no error and no warning. A test that acts one turn
early does nothing at all. It then fails downstream on a state its no-op never produced, and the
failure reads as a component bug. Wait for the control, not for the page. Wherever a query's
loading state disables the control, run `await waitFor(() => expect(control).toBeEnabled())`
before acting. An `await findBy*` that a faster read's markup satisfies proves nothing about the
read that enables the control. Finding a control by a label that changes with loading
("Refresh"/"Refreshing…") already waits for that read. A control whose accessible name stays the
same gives no such wait, and a `<select>` is the usual case. The same race follows an awaited
action. `await user.click(…)` resolves on the click, not on the read it starts. A
synchronous query after it asserts on markup that has not arrived.

**141. A fixture that pins the same value as the production default cannot prove the value was
passed.** An omission and a correct pass produce identical output, so the assertion holds either
way. Choose fixture values that differ from the default, and sweep more than one. One
non-default value proves the argument reaches the callee. A sweep proves the right value reaches
it, and catches a caller that passes a constant. Exclude the default from a sweep, and say why
in the test. A default left in the sweep adds a case that cannot fail, and nothing reports it. A
sweep that exercises a code path other than the live one has the same gap. Read the captured
value with `.get` and assert on the value, never `kwargs["name"]`. An omission there raises
`KeyError`, so the test fails for the wrong reason. It then pins that an argument was passed,
never which value. This is how rule 118 applies to defaulted arguments.

**145. A guard that scans the tree is proven against the population it claims to cover, not
only against a mutation of one member it already found.** A mutation test can only break a
member the matcher already collected. A member the matcher never saw is missing from both the
guard and its proof. Count what the scan collects. Reconcile that number by hand against the
members you believe exist. Then pin the count. A yes-or-no assertion over the set, such as "no offenders found", cannot
tell a member that complies from one that dropped out of the walk. The count catches a member
that left the walk. It does not catch a field dropped from a member that stayed, and it says
nothing about the state each member was driven in (#167). So where a member's behavior forks on
a branch, drive every branch. Treat the branch the default state reaches with no setup as the
one where a missing declaration hides.

Scope the walk too. `rglob` honors no ignore file, so it descends into gitignored agent
worktrees, whole repo copies inside the repo root, and the session scratch beside them.
`_repo_text_files` asks git for the checkout's files instead. Copy that form. A skip list kept
by hand beside `.gitignore` goes stale every time the ignore file grows. A skip list must also
match on the repo-relative path. `path.parts` of the absolute path matches the worktree the
suite is running in, and the walk silently comes back empty.

**147. A guard that scans source text is proven against every form the tree spells the thing
in, not only against every member the walk found.** The guard is bounded by the syntax it can
parse. Pin the count of the population the ban itself scans. Before shipping a matcher, write
down the spellings it accepts, and run it against the spellings it rejects. Read the whole
attribute or call and inspect inside it. Prefer that to anchoring on a delimiter that only one
spelling puts there.
