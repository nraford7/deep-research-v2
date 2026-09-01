// ============================================================================
// deeper-research batch workflow  —  v3 (CLIs verified against scripts/*.py + SKILL.md)
// FOR REVIEW / one-question smoke first, THEN 6-way fan-out.
//
// Runs brief-questions through the LEGACY standalone pipeline in parallel, each
// producing a Source (markdown RESEARCH-REPORT_<slug>.md + html) under
// magic/research/<slug>/.  The workflow SCRIPT is the orchestrator (the role
// your hand-opened sessions played), so every leaf agent does ONE bounded job —
// nothing nests.
//
// v2 fixes vs the first sketch (all Codex findings + verified flags):
//   • real CLI flags for every script (scope --topic/--output, slice/chase --topic,
//     verify/lint POSITIONAL path, export standalone --sections/--bibliography/--output-dir)
//   • NEVER create a managed v2 run → export's standalone_mutation_guard stays in the
//     legacy path (per-run lease acquired+released in-process, no broker)
//   • real config path ~/.config/deeper-research/config.toml
//   • added dedup_bib bibliography stage + explicit markdown-assemble stage
//   • section writers VERIFY their own file (return word count) — no false "written"
//   • completion checked against real word count, not a truthy agent string
//   • summary uses original question index (no filtered-array bug)
//   • slug validation guard + args normalization (reject unsafe / malformed input)
//   • no "bible" token in code (house rule) — reportPath / sourceMd
//
// v3 adds two reader-facing-defect guards (both recurring in prior batches):
//   • PIPELINE-TOKEN LEAKS — section writers must not cite pipeline artifacts in
//     prose ([answer_NN], [round 2.5], [Round 2.5 deepening], gap_gap_*, *.txt,
//     Grokipedia). Prevented in the writer prompt AND swept+reattributed in a
//     fail-closed loop before export.
//   • LINT (fenced background w/ uncited quantities) — writers put every number
//     in an in-sentence citation and use NO editorial fences; the adversary stage
//     then loops verify+lint→remediate until lint_background exits 0.
//
// STILL TO CONFIRM ON THE LIVE SMOKE (marked // SMOKE):
//   • whether coverage_audit (squad) is run as a stage or folded in
//   • exact scope.json sibling-markdown side effects
// ============================================================================

export const meta = {
  name: 'deeper-research-batch',
  description: 'Run brief questions through the legacy deeper-research pipeline in parallel, each producing a Source (md+html)',
  phases: [
    { title: 'Retrieve' },     // scope→slice→gate→fetch→chase (+ position scaffold)
    { title: 'Synthesize' },   // synthesis.md (exact headers) → deepen
    { title: 'Sections' },     // parallel section writers  ← the un-nestable part
    { title: 'Assemble' },     // dedup_bib + concat markdown Source
    { title: 'Adversary' },    // verify + lint + cross-family grok refuter + leak sweep
    { title: 'Export' },       // export.py standalone → bibtex/claims/html
  ],
}

// ---- Input & config — ALL machine-specific paths come from args (nothing hardcoded) ----
// Launch with the Workflow tool, passing an args OBJECT:
//   args = {
//     project:   '/abs/path/to/your/project',       // REQUIRED — runs land in <project>/research/<slug>/
//     repo:      '/abs/path/to/deeper-research',     // REQUIRED — checkout holding scripts/ + config.py + llm.py
//     questions: [{ slug:'ch7-q1-topic', question:'full text' }, ...],   // REQUIRED
//     cfg:       '~/.config/deeper-research/config.toml',   // optional — provider config for the adversary
//     cap:       '1.50',                             // optional — per-run Exa retrieval cap (USD)
//   }
// (Every Bash stage also `source ~/.env` for EXA/OPENAI/XAI/etc keys — home-relative, not embedded here.)
const cfg = (args && typeof args === 'object' && !Array.isArray(args)) ? args : { questions: args }
const PROJECT = cfg.project
const REPO    = cfg.repo
if (!PROJECT || !REPO) {
  throw new Error('args.project and args.repo are required absolute paths — pass them in the Workflow args object')
}
const CFG = cfg.cfg || '~/.config/deeper-research/config.toml'   // expanded inside the agent's python
const CAP = String(cfg.cap || '1.50')                            // per-run Exa retrieval cap, USD
const ENV = `set -a; source ~/.env; set +a; export PYTHONPATH=${REPO}`

let QUESTIONS = cfg.questions
if (typeof QUESTIONS === 'string') { QUESTIONS = JSON.parse(QUESTIONS) }
if (!Array.isArray(QUESTIONS) || QUESTIONS.length === 0) {
  throw new Error('args.questions must be a non-empty array of {slug, question}')
}
// Slug guard: only [a-z0-9-], 3..80 chars — slugs become writable paths + shell args.
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
for (const q of QUESTIONS) {
  if (!q || typeof q.slug !== 'string' || typeof q.question !== 'string')
    throw new Error(`each item needs string slug + question: ${JSON.stringify(q)}`)
  if (!SLUG_RE.test(q.slug) || q.slug.length < 3 || q.slug.length > 80)
    throw new Error(`unsafe slug (need kebab a-z0-9, 3-80): "${q.slug}"`)
}
const slugs = QUESTIONS.map(q => q.slug)
if (new Set(slugs).size !== slugs.length) throw new Error('duplicate slugs in brief')

const runDirOf = (q) => `${PROJECT}/research/${q.slug}`

// Short-circuit ONLY on real failure sentinels — never on an agent's chatty note.
// (v3.1: a synthesis agent wrote a success narrative into `note`, which the old
//  `if (r.note)` guards misread as failure and skipped stages 3-6.)
const FAIL_NOTES = new Set(['exa-credits', 'gate-failed', 'no-retrieval', 'sections-failed', 'no-sections'])
const failed = (r) => !r || FAIL_NOTES.has(r.note)

// ---- Schemas: force structured hand-offs ------------------------------------
const RETRIEVAL = { type:'object', required:['corpusCount','positions','note'], properties:{
  corpusCount:{ type:'integer' },
  positions:{ type:'array', items:{ type:'object', required:['slug','title'],
    properties:{ slug:{type:'string', pattern:'^[a-z0-9-]+$'}, title:{type:'string'} } } },
  note:{ type:'string' } }}                     // '' = ok | 'exa-credits' | 'gate-failed'
const SECTIONS  = { type:'object', required:['written','note'], properties:{
  written:{ type:'array', items:{ type:'object', required:['path','words'],
    properties:{ path:{type:'string'}, words:{type:'integer'} } } },
  note:{ type:'string' } }}
const SECTION1  = { type:'object', required:['path','words'], properties:{
  path:{type:'string'}, words:{type:'integer'} }}   // words=0 ⇒ the write failed
const REPORT    = { type:'object', required:['reportPath','words','note'], properties:{
  reportPath:{type:'string'}, htmlPath:{type:'string'}, words:{type:'integer'}, note:{type:'string'} }}

log(`Batch: ${QUESTIONS.length} questions → parallel pipelines`)
// NOTE on load: pipeline() caps concurrent agents at ~min(16, cores-2). With 6
// questions × ~9 section writers, retrieval calls to the SHARED Exa account can
// still stack. If credits are tight, run the brief in 2-3 question chunks.

const results = await pipeline(
  QUESTIONS,

  // ── STAGE 1 · RETRIEVE ────────────────────────────────────────────────────
  (q) => agent(
    `RETRIEVAL stage, ONE question, LEGACY standalone flow. Do NOT run run_manager (no managed v2 run).
     Question: "${q.question}"
     Run dir: ${runDirOf(q)}
     Prefix EVERY Bash call with: ${ENV}
     From ${REPO}, run in order (these flags are verified):
       1. python3 scripts/scope.py --topic "${q.question}" --output ${runDirOf(q)}/scope.json   (creates the run dir + scope.json)
       2. python3 scripts/slice_search.py --run-dir ${runDirOf(q)} --topic "${q.question}" --max-retrieval-usd ${CAP}
          → if it prints 402 NO_MORE_CREDITS, return note='exa-credits' and STOP (do NOT retry).
       3. python3 scripts/fetch_fulltext.py --run-dir ${runDirOf(q)}
       4. python3 scripts/evidence_gate.py --run-dir ${runDirOf(q)}
          → exit 22 = gate failed: return note='gate-failed' and STOP.
       5. python3 scripts/citation_chase.py --run-dir ${runDirOf(q)} --topic "${q.question}"
          → OpenAlex 429/exit 40 is NON-fatal; read round1/citation_chase_status.json and continue.
     Then read round1/brief_*.md + a sample of round1/sources/*.txt full-texts and propose 6-10 positions.
     Return {corpusCount, positions:[{slug,title}], note:''}`,
    { label: `retrieve:${q.slug}`, phase: 'Retrieve', schema: RETRIEVAL }
  ),

  // ── STAGE 2 · SYNTHESIZE ──────────────────────────────────────────────────
  (r, q) => {
    if (failed(r)) return r
    return agent(
      `SYNTHESIS stage for "${q.question}". Run dir ${runDirOf(q)}. Prefix Bash: ${ENV}
       1. Read the WHOLE Round-1 corpus (round1/slice_*.jsonl + brief_*.md + every round1/sources/*.txt with a text_path).
       2. Write research synthesis to ${runDirOf(q)}/round2/synthesis.md using these EXACT headers (deepen_questions parses them verbatim):
            ## Comparison
            ## Surprises
            ## Openings
            ## New Questions
            ## Root Cause Questions
            ## Consequence Questions
          Include ONE near-top "field map" section (mainstream vs heterodox; settled vs contested).
       3. python3 scripts/deepen_questions.py --run-dir ${runDirOf(q)} --round2-file ${runDirOf(q)}/round2/synthesis.md --max-retrieval-usd ${CAP}
          (Exa deep-reasoning; partial answers are fine — do NOT require N/N. 402 = skip, non-fatal.)
       Confirm or adjust the ${r.positions.length} positions from the synthesis.
       Return {corpusCount, positions, note:''}`,
      { label: `synth:${q.slug}`, phase: 'Synthesize', schema: RETRIEVAL }
    )
  },

  // ── STAGE 3 · SECTIONS ────────────────────────────────────────────────────
  // The part a leaf claude -p cannot do. The SCRIPT fans out; each leaf writes
  // one file and VERIFIES it (returns word count; 0 ⇒ failed).
  async (r, q) => {
    if (failed(r)) return { written: [], note: r ? r.note : 'no-retrieval' }
    const secs = await parallel(r.positions.map((p, n) => () => {
      const file = `${runDirOf(q)}/sections/${String(n+1).padStart(2,'0')}-${p.slug}.md`
      return agent(
        `Write ONE deep section for the Source on "${q.question}".
         Position: ${p.title}  →  write to ${file}
         Read the assigned full-texts under ${runDirOf(q)}/round1/sources/ (and round2_5/ deepening answers).
         House style: "# Title", then "## The claim / ## The argument / ## The strongest objection, and the reply / ## How it differs from its rivals". 2.5-4k words.
         Cite [domain.tld, YYYY] or [Author, YYYY].
         TWO HARD RULES (reader-facing prose must be clean):
           (a) NO PIPELINE TOKENS in prose. A deepening answer (round2_5/answer_NN_*.md) is an INDEX into the
               primary sources it used — open it, find the primary source, and cite THAT. Never write
               [answer_NN], [answer_0N, round 2.5], [round2_5 answer], [Round 2.5 deepening], gap_gap_*, a
               *.txt filename, or "Grokipedia". Those are internal artifacts, not citations.
           (b) EVERY QUANTITY IS CITED IN ITS OWN SENTENCE. Write framing as ORDINARY PROSE — NO
               editorial-fence / editorial:background markers. If a sentence states a number, effect size, %,
               or count, it must carry an inline [source, YYYY] in the SAME sentence. Bare quantities inside a
               fenced block are the lint_background failure — avoid fences entirely.
         After writing, self-check and fix before returning:
           ${ENV}
           grep -nE '\\[[^]]*(answer_[0-9]|round ?2[._]5|gap_gap_|\\.txt|Grokipedia)[^]]*\\]' ${file} || echo NO_LEAKS
           python3 ${REPO}/scripts/lint_background.py ${file} || echo LINT_FLAGGED
         Resolve any NO_LEAKS-absent hits (reattribute to the primary) and any LINT_FLAGGED lines (unfence + cite in-sentence), then:
           wc -w ${file}
         Return {path:"${file}", words:<the wc -w number, 0 if the file was not written>}`,
        { label: `sec:${q.slug}:${p.slug}`, phase: 'Sections', schema: SECTION1 }
      )
    }))
    const written = secs.filter(Boolean).filter(s => s.words > 0)
    if (written.length === 0) return { written: [], note: 'sections-failed' }
    return { written, note: '' }
  },

  // ── STAGE 4 · ASSEMBLE (bibliography + markdown Source) ────────────────────
  (s, q) => {
    if (failed(s)) return s
    return agent(
      `ASSEMBLE stage for "${q.question}". Run dir ${runDirOf(q)}. Prefix Bash: ${ENV}
       1. Build the master bibliography:
          python3 scripts/dedup_bib.py ${runDirOf(q)}/round1/brief_*.md --output ${runDirOf(q)}/sections/bibliography.md
       2. Assemble the markdown Source (export.py does NOT do this — it needs the file to exist):
          concatenate, in section-number order, the field map + all ${runDirOf(q)}/sections/NN-*.md,
          then append the bibliography, into ${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md
          (single H1 at top; NEVER use the word "bible" anywhere — house term is "the literature"/"source").
       3. Report the assembled size:  ${ENV}; wc -w ${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md
       Return {written:${JSON.stringify(s.written)}, note:''}`,
      { label: `assemble:${q.slug}`, phase: 'Assemble', schema: SECTIONS }
    ).then(() => ({ written: s.written, note: '' }))   // force-preserve written; ignore chatty return
  },

  // ── STAGE 5 · ADVERSARY (cross-family; grok via llm.py) ────────────────────
  (s, q) => {
    if (failed(s) || !s.written.length) return s
    return agent(
      `ADVERSARY stage for "${q.question}". Run dir ${runDirOf(q)}. Prefix Bash: ${ENV}
       1. Cross-family refuter (MUST be non-Anthropic — sections were written by Claude). Write a python snippet:
            import os, config as C, llm
            providers,_ = C.load_config([os.path.expanduser('${CFG}')], os.environ)
            assert providers.get('grok'), 'grok/xai not configured'   # fail loud if absent
            llm.call_model(providers['grok'], SYS, USER)
          Shard ${runDirOf(q)}/sections/ into ~10k-word chunks; ask grok to REFUTE each claim.
          Apply ONLY grok's overclaim/superlative softenings + genuine catches. IGNORE its
          "fabricated / future-date" flags AFTER grep-verifying the phrase is verbatim in round1/sources/
          (known false-alarm pattern every batch so far).

       2. PIPELINE-TOKEN LEAK SWEEP (fail-closed). Reader-facing prose must contain ZERO pipeline artifacts.
          Find every leak across sections:
            grep -rnE '\\[[^]]*(answer_[0-9]|round ?2[._]5|Round 2\\.5 deepening|gap_gap_|\\.txt|Grokipedia)[^]]*\\]' ${runDirOf(q)}/sections/
          For EACH hit, REATTRIBUTE not delete: open the referenced round2_5/answer_NN_*.md (or round1/sources file),
          find the PRIMARY source it rests on, and replace the token with [primary-source, YYYY]. Only if no primary
          exists, remove the bracketed token and its claim. Re-run the grep until it returns NOTHING.

       3. LINT REMEDIATION LOOP (fail-closed).
            python3 scripts/lint_background.py ${runDirOf(q)}/sections/
          For each flagged line: either UNFENCE the editorial/background block (it becomes normal cited prose) or
          add an in-sentence [source, YYYY] for the bare quantity. Re-run lint_background until it exits 0.
          (Do not silence by deleting real evidence — cite it.)

       4. python3 scripts/verify_citations.py ${runDirOf(q)}/sections/ --output ${runDirOf(q)}/round4/citation-verification.md
          (OpenAlex may hang → watchdog; orphaned/weak-title cites are EXPECTED for canonical author-year works, non-fatal.)

       5. If ANY section changed in steps 1-3, RE-ASSEMBLE ${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md
          (re-concat field map + sections + bibliography) so the markdown Source reflects the cleaned sections.
       Return {written:${JSON.stringify(s.written)}, note:''}`,
      { label: `adv:${q.slug}`, phase: 'Adversary', schema: SECTIONS }
    ).then(() => ({ written: s.written, note: '' }))   // force-preserve written; ignore chatty return
  },

  // ── STAGE 6 · EXPORT (standalone — legacy guard, no broker) ────────────────
  (s, q) => {
    if (failed(s) || !s.written.length) return { reportPath:'', words:0, note: s ? s.note : 'no-sections' }
    return agent(
      `EXPORT stage for "${q.question}". Run dir ${runDirOf(q)}. Prefix Bash: ${ENV}
       Standalone export (do NOT pass --run-dir — that is the managed/broker path):
         python3 scripts/export.py \\
           --sections ${runDirOf(q)}/sections \\
           --bibliography ${runDirOf(q)}/sections/bibliography.md \\
           --output-dir ${runDirOf(q)} \\
           --bible ${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md
       (This emits bibliography.bib + claims.jsonl + the HTML companion; the markdown Source already exists from Assemble.)
       Verify deliverables exist and report actuals:
         ${ENV}
         test -s ${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md && wc -w ${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md
         ls ${runDirOf(q)}/*.html
       Return {reportPath:"${runDirOf(q)}/RESEARCH-REPORT_${q.slug}.md", htmlPath:"<the .html path>", words:<wc -w number>, note:''}`,
      { label: `export:${q.slug}`, phase: 'Export', schema: REPORT }
    )
  }
)

// ── Summary (index-safe: results[i] aligns with QUESTIONS[i]) ─────────────────
const done = [], skipped = []
QUESTIONS.forEach((q, i) => {
  const r = results[i]
  if (r && r.reportPath && r.words > 0) done.push({ slug: q.slug, words: r.words, html: r.htmlPath })
  else skipped.push({ slug: q.slug, note: r ? r.note : 'died' })
})
log(`Done ${done.length}/${QUESTIONS.length}.  Skipped/failed: ${skipped.length}`)
return { done, skipped }
