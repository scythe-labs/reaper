// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Stage 4's per-surface fan-out (docs/I18N_PLAN.md §10), committed so every Stage 4
// session runs the same harness.
//
// args: an array of surfaces, each { name, namespace, files } with files spelled
// repo-relative ("frontend/src/components/JobsPanel.tsx"). Keep a run inside the
// session's agent guideline: each surface costs two agents.
//
// The split write is the design: extraction agents convert their surface's files in
// place and RETURN the key map; only the orchestrator touches the shared files
// (locales/en/ui.json, and CONVERTED in i18n-extraction.test.ts), because parallel
// writers would clobber them. The missing-key gate is what makes that split safe: a key
// an agent used but did not return fails CI instead of rendering as a raw id on an
// operator's screen. After a run the orchestrator merges each surface's keys into
// en.json, adds its files to CONVERTED, reconciles the verifier's leftovers, runs the
// full gates (each read by exit code, rule 134), and commits the group.

export const meta = {
  name: 'i18n-stage4-extract',
  description: 'Extract one group of UI surfaces into the i18n catalog',
  whenToUse: 'Stage 4 of docs/I18N_PLAN.md, one invocation per surface group',
  phases: [
    { title: 'Extract', detail: 'one agent per surface converts its files in place', model: 'sonnet' },
    { title: 'Verify', detail: 'a fresh agent per surface hunts leftover literals', model: 'sonnet' },
  ],
}

const KEYMAP = {
  type: 'object',
  required: ['keys'],
  properties: {
    keys: {
      type: 'object',
      description: 'full dotted catalog key -> exact English ICU message',
      additionalProperties: { type: 'string' },
    },
    notes: { type: 'string', description: 'anything the merge step must know; empty if nothing' },
  },
}

const VERDICT = {
  type: 'object',
  required: ['leftovers', 'keysUsed'],
  properties: {
    leftovers: {
      type: 'array',
      items: { type: 'string' },
      description: 'path:line: text, one per user-visible literal still hardcoded',
    },
    keysUsed: {
      type: 'array',
      items: { type: 'string' },
      description: 'every catalog key the files reference',
    },
  },
}

const extractPrompt = (s) => `Convert one Reaper frontend surface to i18next: Stage 4 of docs/I18N_PLAN.md.
Surface "${s.name}", catalog namespace "${s.namespace}". Convert every file below, each completely (rule 144: a surface extracts whole, never "the easy strings first"):
${s.files.join('\n')}

Before editing, read docs/I18N_PLAN.md §3-§4, .claude/rules/frontend.md, and the finished exemplar frontend/src/components/SafetyBanner.tsx with its test.

How to convert:
- Every user-visible string moves to the catalog: JSX text, aria-label / title / placeholder / alt / label attributes, announce() messages, toast and confirm copy, empty states, error text, and copy composed in plain code that reaches the operator.
- The English text moves VERBATIM. You are extracting, not rewording: the suite's copy-bound queries must keep passing on byte-identical output. Leave every test file unedited, and do not run the suite to judge your work: until the orchestrator merges the catalog, your keys render as raw ids and the tests fail by design.
- Keys are explicit and namespaced under "${s.namespace}.", camelCase segments named for the message's role, never the English sentence itself.
- Hand-built plurals and selects become ICU MessageFormat ({n, plural, one {...} other {...}}). Markup inside a sentence becomes a <Trans> placeholder named for the control, never after an HTML void element (<link> swallows children; use <btn>).
- Components use const { t } = useTranslation() from react-i18next; non-component modules import i18next from the i18n module (frontend/src/i18n.ts). Pass t() a literal key, never a computed one.
- Not copy, left alone: class names, query keys, API paths, test ids, log-only strings, comparison values.
- NEVER edit: frontend/src/locales/en/ui.json, frontend/src/i18n-extraction.test.ts, frontend/src/i18n-keys.test.ts, any *.test.* file, or any file outside the list above. The orchestrator merges the catalog after this run; until then the missing-key gate fails by design.

Return keys: a flat map of every full dotted key you used to its exact English ICU message.`

const verifyPrompt = (s) => `Adversarial check of an i18n extraction that just converted these files (Stage 4 of docs/I18N_PLAN.md). Read ONLY them:
${s.files.join('\n')}

You have no stake in the extraction being done. Report:
- leftovers: every user-visible English literal still hardcoded, as "path:line: text". Look past the mechanical cases (JSX text, visible attributes, announce()) to what a static gate misses: copy built with string concatenation or template literals in plain code, copy passed through props, default parameter copy. Class names, catalog keys, API paths, test ids and log-only strings are not copy. Empty if clean.
- keysUsed: every catalog key the files reference (t("..."), i18nKey="...").`

const results = await pipeline(
  args,
  (s) =>
    agent(extractPrompt(s), {
      model: 'sonnet',
      schema: KEYMAP,
      phase: 'Extract',
      label: `extract:${s.name}`,
    }),
  (extracted, s) =>
    extracted &&
    agent(verifyPrompt(s), {
      model: 'sonnet',
      schema: VERDICT,
      phase: 'Verify',
      label: `verify:${s.name}`,
    }).then((v) => ({
      surface: s.name,
      namespace: s.namespace,
      files: s.files,
      keys: extracted.keys,
      notes: extracted.notes ?? '',
      leftovers: v ? v.leftovers : ['verifier returned nothing: re-verify this surface'],
      keysUsed: v ? v.keysUsed : [],
    })),
)

return results.filter(Boolean)
