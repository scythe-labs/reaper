export const meta = {
  name: 'collections',
  description: 'Build one lane of the collections feature (#816), then review it two ways',
  whenToUse:
    'Executing docs/COLLECTIONS_PLAN.md. Run once per lane: {lane:"backend"}, then {lane:"frontend"}, then {lane:"docs"}. Read the findings between runs and decide what gets fixed; the workflow never decides that for you.',
  phases: [
    { title: 'Build', detail: 'one Sonnet worker per phase, serial (they collide on files)' },
    { title: 'Review', detail: 'reaper-review and ponytail-review in parallel, read-only' },
  ],
}

// ---------------------------------------------------------------------------
// The orchestrator is the Opus session that calls this, not the script. Each run
// builds one lane and hands back findings; adjudication happens outside, because
// reaper-review and ponytail-review disagree by design and the tiebreak is the
// plan's fence (collections are navigation, never protection). A script cannot
// make that call, so it does not pretend to.
// ---------------------------------------------------------------------------

const PLAN = 'docs/COLLECTIONS_PLAN.md'
const WORKER = 'sonnet'

// Serial, and deliberately so. Phases 1-3 all touch src/reaper/; 4-5 both touch
// ReviewQueue.tsx. Parallel saves minutes and costs a conflict in the one file the
// whole feature routes through.
const LANES = {
  backend: [
    { key: 'plex-read', title: 'Phase 1, the Plex read' },
    { key: 'snapshot', title: 'Phase 2, into the snapshot' },
    { key: 'api', title: 'Phase 3, the API' },
    { key: 'search', title: 'Phase 3b, search reaches collection names' },
  ],
  frontend: [
    { key: 'chip', title: 'Phase 4, the chip' },
    { key: 'screen', title: 'Phase 5, the collection screen' },
  ],
  docs: [{ key: 'docs', title: 'Phase 6, docs' }],
}

const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      maxItems: 6, // forces ranking over dumping
      items: {
        type: 'object',
        required: ['file', 'summary', 'why_it_matters'],
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          summary: { type: 'string' },
          why_it_matters: { type: 'string' },
          // reaper-review ranks by distance from the deletion path; ponytail-review
          // has no tiers and leaves this out.
          tier: { type: 'integer' },
        },
      },
    },
  },
}

const lane = (args && args.lane) || 'backend'
const steps = LANES[lane]
if (!steps) throw new Error(`unknown lane ${lane}; expected backend, frontend or docs`)

// --- build -----------------------------------------------------------------

phase('Build')
log(`${lane}: ${steps.length} phase${steps.length === 1 ? '' : 's'}, serial`)

const built = []
for (const step of steps) {
  const result = await agent(
    `Read ${PLAN} in full, then read the rules files its phase touches (.claude/rules/) before
     editing anything.

     Implement ONLY "${step.title}". Do not start the next phase, and do not refactor anything
     the phase does not name.

     Hard constraints, from the plan's fence:
       - Collections are navigation, never protection. No gate, no signal, no policy FieldSpec.
       - A failed collection read must NOT degrade the snapshot, and the comment at the read
         site says why in those words.
       - Land the phase's "Done when" test in the same commit as the code.

     Run the gates for what you touched and read the EXIT CODE, never a pipe's tail (rule 134).
     Commit the phase with a Conventional Commit subject.

     Return, as plain text: what you changed, the gate commands you ran with their exit codes,
     and anything the plan told you to do that you did not do, with the reason.`,
    { label: `build:${step.key}`, phase: 'Build', model: WORKER },
  )
  built.push({ step: step.key, result })
}

// --- review ----------------------------------------------------------------
//
// Two lenses, in parallel, read-only. They answer different questions and are
// expected to conflict: the first will ask for a guard the second calls
// speculative. Neither gets to decide.

phase('Review')

const diff = `the changes on this branch against origin/dev for the ${lane} lane`

const [safety, bloat] = await parallel([
  () =>
    agent(
      `Load the reaper-review skill and follow it. Review ${diff}.

       Rank by distance from the deletion path. This feature claims to sit entirely off it, so
       your first job is to check that claim rather than assume it: find any path where a
       collection read can change a verdict, withdraw a protection, degrade a scan, or reach the
       executor. If there is none, say so plainly and spend the rest of the pass on correctness.

       Report findings only. Fix nothing, file nothing.`,
      { label: 'review:reaper', phase: 'Review', model: WORKER, schema: FINDINGS },
    ),
  () =>
    agent(
      `Load the ponytail-review skill and follow it. Review ${diff}.

       Hunt over-engineering only: reinvented stdlib, a second copy of something already in this
       repo, an abstraction with one caller, dead flexibility. ${PLAN} names two siblings that
       must not be duplicated, the genre json_each predicate and find_collection's paging loop,
       so check those first.

       Report findings only. Fix nothing, file nothing.`,
      { label: 'review:ponytail', phase: 'Review', model: WORKER, schema: FINDINGS },
    ),
])

const safetyFindings = safety?.findings ?? []
const bloatFindings = bloat?.findings ?? []
log(`${lane}: ${safetyFindings.length} safety, ${bloatFindings.length} complexity`)

return {
  lane,
  built,
  reaperReview: safetyFindings,
  ponytailReview: bloatFindings,
  // Said out loud so a caller reading only the return value knows the job is not finished.
  next: 'Adjudicate these against the plan fence, dispatch fixes, then run the next lane.',
}
