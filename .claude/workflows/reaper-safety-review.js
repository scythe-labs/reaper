export const meta = {
  name: 'reaper-safety-review',
  description: 'Adversarial multi-agent review of Reaper’s deletion path, capped so the fan-out cannot run away',
  whenToUse:
    'Before promoting dev to main, or when something feels systemically wrong on the deletion path. NOT for a normal change — use /reaper-review, which reviews the diff with 1-3 subagents.',
  phases: [
    { title: 'Review', detail: 'one reviewer per safety-path file group (7)' },
    { title: 'Verify', detail: 'independently refute the tier 1-2 candidates only' },
  ],
}

// ---------------------------------------------------------------------------
// Caps. The first run of this workflow spawned 43 agents: 10 reviewers raised
// ~33 candidates and every one got its own verifier, because nothing bounded
// the second stage. Concurrency was never the problem -- it caps how many run
// at once, not how many run at all. These three bound the COUNT.
// ---------------------------------------------------------------------------
const MAX_FINDINGS_PER_REVIEWER = 4 // schema-enforced; forces ranking over dumping
const VERIFY_TIERS_AT_OR_BELOW = 2 // only protection-fails and widens-deletion get a verifier
const VERIFY_BUDGET_FLOOR = 60_000 // below this many remaining tokens, report unverified instead

// Compact do-not-rediscover list, inlined so no reviewer spends a read on it.
const REFUTED = [
  'season_pruning/models: group_key unindexed causing full scans on every spare click',
  'policy/gates: a gate missing from PolicyBody.gates is silently not run; empty gates tuple validates',
  'facts_codec: facts_from_dict raises on stored facts_json predating an _OBS_FIELDS entry',
  'identity: rationale claims backtest/planner reach the production join; neither references identity',
  'middleware/settings: API-key lane may write /api/profile, turning off run caps',
  'instances: a stored API key can be shipped to any host by editing base_url and pressing Test',
  'db/session.py:33: PRAGMA synchronous=NORMAL makes the deletion journal non-durable',
  'seerr: paging advances by requested page size, not rows returned',
  'executor: measured dict typed int|None so live_sizes can hold None (handled at 1897/1904)',
  'executor: movie rescan not scoped to the movie folder (it is - 1777 passes movie["path"])',
  'executor: flat season layout makes _common_parent return the series root (correct for that layout)',
  'executor:2491 _path_within(p, "") returns True for any absolute path (reported 2026-07-26)',
  'executor:2069 rescan-widening guard skipped when series_path is empty (reported 2026-07-26)',
]

// 7 groups, balanced at roughly 1,100-1,400 lines each. Only the two files over
// 2,000 lines are split; splitting the rest costs a reviewer without buying
// coverage, and splitting snapshot.py would cut the gather-to-freeze pipeline
// in half, which is where its cross-region bugs live.
const GROUPS = [
  {
    key: 'executor-interlocks',
    files: 'src/reaper/services/executor.py (read lines 1-1300 with offset/limit)',
    focus: 'arming, the send loop, per-item interlocks, _fail/_mark_* state transitions, cap math',
  },
  {
    key: 'executor-send',
    files: 'src/reaper/services/executor.py (read lines 1300-2548 with offset/limit)',
    focus:
      'the movie and season send paths, size gates, Plex refresh scoping, _finalize_plex and the trash purge',
  },
  {
    key: 'snapshot',
    files: 'src/reaper/services/snapshot.py',
    focus:
      'gather -> freeze -> hash -> score. Prioritize: every except around a source read, degradation calls, whitelist staleness, and Absent-vs-Unknown routing. Do not read exhaustively; go where evidence can be lost.',
  },
  {
    key: 'policy-verdict',
    files: 'src/reaper/engine/policy.py and src/reaper/engine/verdict.py',
    focus:
      'stored-body loading and schema shims, rule evaluation, field specs per media type, and the single condemn/abstain/protect decision',
  },
  {
    key: 'gates-signals',
    files: 'src/reaper/engine/gates.py and src/reaper/engine/signals.py',
    focus:
      "every gate's fire/abstain path, and unsigned pressure over the fixed denominator with its coverage math",
  },
  {
    key: 'planner-pruning',
    files: 'src/reaper/services/planner.py and src/reaper/services/season_pruning.py',
    focus: 'plan construction, empty-selection handling, spare/override application',
  },
  {
    key: 'guard-route',
    files: 'src/reaper/clients/base.py and src/reaper/api/runs.py',
    focus:
      'GuardedTransport (armed checks, declared-intent journal coupling, GET-shaped mutation classification) and the one deleting route (confirmation phrase binding, armed check, policy_hash enforcement)',
  },
]

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    rules_loaded: {
      type: 'string',
      description:
        'Instrumentation on the harness, not on you. Did .claude/rules/backend.md get auto-injected into your context after you Read a src/reaper file? Answer exactly YES or NO, then a colon and the rule-number range it said it holds (or why not).',
    },
    findings: {
      type: 'array',
      maxItems: MAX_FINDINGS_PER_REVIEWER,
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          title: { type: 'string' },
          mechanism: { type: 'string', description: 'What the code does wrong, citing exact lines' },
          trigger: {
            type: 'string',
            description:
              'The concrete input or state that produces the wrong outcome, and what the operator loses',
          },
          tier: {
            type: 'integer',
            description:
              '1=protection that cannot fire or does not protect, 2=widens what gets deleted, 3=loses audit trail or ability to intervene, 4=everything else',
          },
          rule: { type: 'string', description: 'Numbered rule violated, e.g. "rule 93", or "none"' },
          fix: { type: 'string' },
        },
        required: ['file', 'line', 'title', 'mechanism', 'trigger', 'tier', 'rule', 'fix'],
      },
    },
  },
  required: ['rules_loaded', 'findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    real: { type: 'boolean', description: 'true only if you could NOT refute it' },
    corrected_line: { type: 'integer' },
    corrected_tier: { type: 'integer' },
    reasoning: {
      type: 'string',
      description: 'What you opened and what it showed. If refuted, why the trigger cannot occur.',
    },
  },
  required: ['real', 'reasoning'],
}

// Every agent writes its own result to disk BEFORE returning it. A workflow's
// return value only becomes durable once the whole run finishes and the caller
// processes it, so a run that dies partway -- a lost session, an exhausted
// quota -- used to leave its verified work recoverable only by hand-parsing
// the session's raw JSONL. Writing per agent makes each result durable the
// moment it exists, which is the only point at which nothing can be lost.
const RUN = (args && args.runId) || 'latest'
const OUT = '.claude/review-findings/' + RUN

// Verifiers legitimately write a throwaway test to check whether a claimed
// trigger fires -- a test outside tests/ would not see conftest's fixtures, so
// "never write to the repo" is the wrong rule. Bound it instead: one namespaced
// path, and never an edit to something that already exists.
const WRITE_RULES = `
You are REVIEWING, not changing the code. Never edit or delete an existing repo file -- not
source, not tests, not docs. If you need a throwaway test to check whether a trigger really
fires, create it at tests/test_zz_scratch_<something>.py, run it, and delete it before you
return. That path is the only file you may create besides the one named below.
`

const persistFirst = (path, what) => `
BEFORE you return anything, use the Write tool to save ${what} to:

    ${path}

Write it as JSON, exactly the object you are about to return. Do this FIRST -- if this run is
interrupted after you finish but before it completes, that file is the only place your work
survives. Create parent directories as needed. Then return the same object as your answer.
`

const reviewPrompt = (g) => `You are reviewing Reaper's DELETION PATH. Reaper permanently removes media files from a
self-hosted server. The prime directive: every ambiguity resolves toward KEEPING the file.

Scope: ${g.files}
Focus: ${g.focus}

Read the file(s) with the Read tool. The repo's numbered engineering rules load AUTOMATICALLY
into your context when you read a governed file -- do not go looking for them, and do not read
.claude/rules/ manually. Cite them by number when a finding violates one.

RANK BY BLAST RADIUS, not generic severity:
  tier 1 - a protection that cannot fire, or fires and does not protect (the silent class)
  tier 2 - a path that widens what gets deleted (fail-open, empty-set expansion, cap over the wrong set)
  tier 3 - loss of the audit trail or the operator's ability to intervene
  tier 4 - everything else (correctness, security, performance, production readiness)

You may report AT MOST ${MAX_FINDINGS_PER_REVIEWER} findings. That is a hard cap, so spend it on
the lowest tiers you can actually substantiate -- one tier 1 beats four tier 4s. Only tier 1 and
tier 2 findings are independently verified downstream, so a tier 3-4 finding must stand on its
own or not be reported.

Report a finding ONLY if you can state the concrete trigger: the input or state that produces
the wrong outcome. "This could be fragile" is not a finding. Do not report code that is fine.
Zero findings is a valid and useful answer -- return an empty array rather than padding.

ALREADY REFUTED or ALREADY REPORTED -- do NOT raise these again:
${REFUTED.map((r) => '  - ' + r).join('\n')}

Also answer the rules_loaded instrumentation field honestly. Report only what you can see in
this context -- a previous run's reviewer confidently reported a rules file as absent that the
InstructionsLoaded hook recorded loading four times for the exact file it named.
${WRITE_RULES}${persistFirst(OUT + '/review-' + g.key + '.json', 'your findings')}`

const verifyPrompt = (f) => `Adversarially verify this code-review finding. YOUR JOB IS TO REFUTE IT.

  File: ${f.file}:${f.line}
  Claim: ${f.title}
  Mechanism: ${f.mechanism}
  Trigger: ${f.trigger}
  Claimed tier: ${f.tier}

Open the cited lines. Grep the real call sites. Check whether the claimed trigger can ACTUALLY
occur, or whether a guard upstream already prevents it. Check whether the claimed wrong outcome
really follows.

Default to real=false when uncertain. A plausible-but-wrong finding costs more than a missed
one here, because it sends someone editing the deletion path for no reason.

If it survives, correct the line number and the tier if they are wrong.
${WRITE_RULES}${persistFirst(
  OUT + '/verify-' + f.group + '-' + f.line + '.json',
  'your verdict, plus the finding it judges (copy the file/line/title/tier through so the file stands alone)'
)}`

phase('Review')

const results = await pipeline(
  GROUPS,
  (g) =>
    agent(reviewPrompt(g), { label: 'review:' + g.key, phase: 'Review', schema: FINDINGS_SCHEMA }),
  (r, g) => {
    if (!r) return { group: g.key, rules_loaded: 'AGENT FAILED', verified: [], unverified: [] }

    const eligible = r.findings.filter((f) => f.tier <= VERIFY_TIERS_AT_OR_BELOW)
    const skipped = r.findings.filter((f) => f.tier > VERIFY_TIERS_AT_OR_BELOW)

    // Degrade rather than overrun: past the floor, everything is reported unverified.
    const broke = budget.total && budget.remaining() < VERIFY_BUDGET_FLOOR
    if (broke) {
      log(
        g.key + ': budget floor reached, reporting ' + r.findings.length + ' finding(s) unverified'
      )
      return { group: g.key, rules_loaded: r.rules_loaded, verified: [], unverified: r.findings }
    }

    log(
      g.key +
        ': ' +
        r.findings.length +
        ' candidate(s), ' +
        eligible.length +
        ' to verify, ' +
        skipped.length +
        ' reported unverified (tier > ' +
        VERIFY_TIERS_AT_OR_BELOW +
        '), rules_loaded=' +
        r.rules_loaded
    )

    return parallel(
      eligible.map(
        (f) => () =>
          agent(verifyPrompt(f), {
            label: 'verify:' + g.key + ':' + f.line,
            phase: 'Verify',
            schema: VERDICT_SCHEMA,
          }).then((v) => ({ ...f, group: g.key, verdict: v }))
      )
    ).then((verified) => ({
      group: g.key,
      rules_loaded: r.rules_loaded,
      verified: verified.filter(Boolean),
      unverified: skipped,
    }))
  }
)

const ok = results.filter(Boolean)
const all = ok.flatMap((r) => r.verified)
const confirmed = all.filter((f) => f.verdict && f.verdict.real)
const refuted = all.filter((f) => f.verdict && !f.verdict.real)
const unverified = ok.flatMap((r) => r.unverified)

const tierOf = (f) => (f.verdict && f.verdict.corrected_tier) || f.tier
confirmed.sort((a, b) => tierOf(a) - tierOf(b))
unverified.sort((a, b) => a.tier - b.tier)

// A cap that silently drops work reads as "we covered everything". Say what it cost.
log(
  'agents: ' +
    ok.length +
    ' reviewers + ' +
    all.length +
    ' verifiers. ' +
    unverified.length +
    ' finding(s) were reported without independent verification.'
)

return {
  instrumentation: ok.map((r) => r.group + ': ' + r.rules_loaded),
  tally: {
    groups: ok.length,
    raised: all.length + unverified.length,
    confirmed: confirmed.length,
    refuted: refuted.length,
    unverified: unverified.length,
    agents: ok.length + all.length,
  },
  confirmed: confirmed.map((f) => ({
    file: f.file,
    line: (f.verdict && f.verdict.corrected_line) || f.line,
    tier: tierOf(f),
    title: f.title,
    mechanism: f.mechanism,
    trigger: f.trigger,
    rule: f.rule,
    fix: f.fix,
    verifier: f.verdict.reasoning,
  })),
  unverified: unverified.map((f) => ({
    file: f.file,
    line: f.line,
    tier: f.tier,
    title: f.title,
    trigger: f.trigger,
    rule: f.rule,
    fix: f.fix,
    note: 'tier > ' + VERIFY_TIERS_AT_OR_BELOW + ', not independently verified',
  })),
  refuted: refuted.map((f) => ({
    file: f.file,
    line: f.line,
    title: f.title,
    why: f.verdict.reasoning,
  })),
}
