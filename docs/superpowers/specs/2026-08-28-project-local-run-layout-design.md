# Project-Local Research Run Layout, Migration, and Extension

**Date:** 2026-08-28
**Status:** Revised draft for independent and user review
**Risk:** High — persisted layout, migration, resume, and derivation semantics change.

## Goal and invariants

Every new deeper-research run is stored under the project from which it was launched,
normally at `<launch-project>/research/<run-slug>/`. Publications remain immediately
visible, evidence has one canonical home, and process machinery is contained below
`Process/`.

The implementation must preserve these invariants:

1. Opening a legacy run never moves it.
2. A completed or snapshotted parent is never mutated by extension.
3. Every artifact has one canonical home; mixed homes fail closed.
4. No structural operation overwrites an existing path or follows an unsafe link.
5. Migration is explicitly requested, durable across process or host failure, and
   reversible only while its complete post-migration tree is unchanged.
6. A derived run inherits an immutable, hash-addressed snapshot of the full available
   corpus and provenance. A prior Bible can guide gap analysis but is never evidence.
7. Completion is a validated, sealed state, not a filename-existence heuristic.

## Approved product decisions

1. The default library is `<launch-project>/research/`; create it when absent.
2. If launch occurs in a directory literally named `research`, use it as the library
   rather than creating `research/research/`.
3. New runs use a native, versioned layout through one central resolver.
4. Legacy runs remain readable and resumable until explicitly migrated or explicitly
   frozen/sealed as the immutable parent of a derived run.
5. Explicit migration applies immediately by default; `--dry-run` is optional.
6. Reader-facing folders are `Sections/`, `Sources/`, `Sources/Extracted/`, and
   `Process/`.
7. Topic-qualified Bible names are preserved and recorded authoritatively in metadata.
8. Technical round names remain unchanged inside `Process/`.
9. An existing slug triggers resume, extend, start-fresh, or cancel selection.
10. Extension inherits the full corpus and provenance, retrieves gaps, reruns the gate,
    and performs fresh synthesis and verification.

## Canonical layout (version 2)

```text
<launch-project>/
└── research/
    ├── .locks/                            # structural and index locks
    ├── .transactions/                     # durable migration journals
    ├── .semantic-index.db
    ├── .semantic-index.pending.json
    ├── _batch/
    └── <run-slug>/
        ├── RESEARCH-BIBLE_<topic>.md
        ├── RESEARCH-BIBLE_<topic>.html
        ├── README.md                      # optional
        ├── Sections/
        │   ├── 00-executive-summary.md
        │   ├── 01-<position>.md
        │   └── ...
        ├── Sources/
        │   ├── bibliography.md
        │   ├── bibliography.bib
        │   ├── claims.jsonl
        │   └── Extracted/
        │       ├── <source>.txt
        │       ├── <source>.pdf
        │       └── <source>.html
        └── Process/
            ├── run.json
            ├── scope.json
            ├── retrieval_ledger.json
            ├── lineage.json               # derived runs only
            ├── migration.json             # migrated runs only
            ├── seal.json                  # complete-run tree seal
            ├── snapshots/                 # frozen partial-parent manifests
            ├── stages/                     # versioned stage manifests
            ├── Inherited/                 # immutable ancestry snapshots
            ├── Legacy/                    # unclassified migration artifacts
            ├── round1/
            ├── round2/
            ├── round2_5/
            ├── round3/
            ├── round4/
            └── round5/
```

The run root contains only publications, an optional README, and the three named
folders. Project-wide locks, transactions, batch state, and the semantic index live at
the library root.

## Project and library resolution

The launcher captures its working directory before changing directories or invoking
helpers. Resolution is explicit and backward compatible:

- `--project-dir <dir>` means project root; its library is `<dir>/research`, except
  when `<dir>` itself is named `research`.
- Legacy `--output-root <dir>` retains its existing meaning: it is the library that
  directly receives run children. `--library-dir <dir>` is its clearer synonym.
- Without either flag, the captured launch directory is the project root.
- Conflicting project/library flags are rejected before creating anything.

The normal skill path uses `--project-dir`. The direct-library flags remain available
for existing scripts and deliberate nonstandard placement, but are documented as
compatibility controls. Helpers receive the resolved absolute run path and never infer
it again.

## Portable slug contract

One versioned `slug-v1` generator and validator owns all slugs. It applies a vendored,
fixed Unicode 15.0 NFKD table, removes combining code points,
drops remaining non-ASCII code points, lowercases ASCII, converts non-alphanumerics to
single hyphens, strips leading/trailing dots, spaces, and hyphens, and uses
`research-run` when empty. The Unicode data version is recorded in metadata. It rejects
Windows device names, `.`/`..`, separators, control characters, and components whose
UTF-8 encoding exceeds 120 bytes. Long slugs are truncated on a character boundary and
retain a 10-character SHA-256 suffix derived from the untruncated normalized input.
Every variant reserves room before truncation for extension text, timestamp, and retry
suffix; the final component, not merely its base, must satisfy the 120-byte limit.

Collision keys are NFC + casefold, with trailing dots/spaces removed. Startup probes the
target filesystem for case sensitivity and rejects aliases under either the portable or
actual-filesystem key. A fresh or extended collision adds the filesystem-safe UTC form
`YYYYMMDDTHHMMSSffffffZ`; an atomic reservation retries with an incrementing suffix if
necessary.

## Central path and layout contract

`scripts/run_layout.py` defines `RunLayout`, the only module that translates logical
artifacts to physical paths. It exposes `run_root`, `sections`, `sources`,
`extracted_sources`, `process`, each round, scope, ledger, publication mappings,
bibliographies, claims, metadata, lineage, stage manifests, and lock identity.

`RunLayout.open(path)` classifies in this order:

1. If `Process/run.json` exists, parse and validate it first. Malformed metadata is
   `corrupt`; an unknown newer layout is `unsupported`. Neither may fall back to legacy.
2. A valid `layout_version: 2` plus any legacy artifact-home marker is `mixed`, even if
   files have identical checksums.
3. Recognized legacy markers without v2 metadata are `legacy`.
4. Contradictory or incomplete signatures are `invalid`.

Mixed, corrupt, unsupported, and invalid layouts are read-only failures outside an
explicit migration recovery transaction. New writes are v2. Legacy opens map logical
properties to legacy paths and permit read/resume without implicit migration.

### Safe paths and file types

All active v2 persisted references use POSIX paths relative to the run root. Parsing uses
`PurePosixPath`; reject empty components, `.`/`..`, backslashes, drive/UNC forms,
absolute paths, NUL/control characters, and platform-special names. Each component is
opened descriptor-relative without following links where supported, then verified to
remain below the expected root. On platforms lacking reliable no-follow primitives,
structural mutation refuses the operation.

Migration and inheritance accept regular files and ordinary directories only. They
reject symlinks, junctions/reparse points, devices, sockets, FIFOs, mount-point
boundaries, or a component whose filesystem identity changes between plan and use.
Reads may report such legacy entries but never dereference them. Temporary files use
exclusive creation in the destination directory followed by flush, fsync, and atomic
replace.

### Path-bearing schemas

The layout module owns a versioned registry for every persisted field that can name a
path. At minimum it covers slice/deepening JSONL (`text_path`, `raw_path`, historical
`run_dir` prefixes), claims JSONL (`file`), retrieval and source manifests, section and
verification inventories, export manifests, stage manifests, and lineage snapshots.
Active v2 values are run-root-relative; legacy adapters declare their historical base.
An inherited archival snapshot is the sole exception: its `snapshot.json` declares its
own snapshot-root reference base and original layout version, and copied metadata stays
byte-for-byte unchanged and resolves only inside that archival root. Only active child
rows are rebased to the child run root.

Migration rewrites registered references transactionally and validates every target
after movement. An unregistered JSON/JSONL path-like field or unresolvable reference is
a preflight error, never a guessed rewrite. Unknown opaque files may be preserved in
`Process/Legacy/` only when no active artifact points through them.

## Metadata, stage manifests, and lifecycle

`Process/run.json` is UTF-8 JSON with this minimum schema:

```json
{
  "layout_version": 2,
  "schema_version": 1,
  "run_id": "immutable UUID",
  "slug": "topic-slug",
  "question": "Original research question",
  "question_source": "user",
  "status": "incomplete",
  "completion_profile": "native-v2",
  "sealed": false,
  "frozen_for_derivation": false,
  "frozen_snapshot": null,
  "generation": 1,
  "created_at": "ISO-8601 UTC",
  "updated_at": "ISO-8601 UTC",
  "completed_at": null,
  "bible": null
}
```

Native runs receive UUIDv4 `run_id` values before publication. Migration derives a
UUIDv5 from a fixed application namespace plus the canonical pre-migration source path
and pre-migration tree Merkle root. The ID never changes after publication or relocation
and is carried by locks, lineage, snapshots, migration records, and semantic document
IDs.

When present, `bible` is authoritative:

```json
{
  "markdown": "RESEARCH-BIBLE_topic.md",
  "html": "RESEARCH-BIBLE_topic.html",
  "markdown_sha256": "...",
  "html_sha256": "..."
}
```

Allowed lifecycle transitions are:

```text
new -> incomplete
incomplete -> complete | failed | frozen
failed -> incomplete                 # explicit resume
failed -> frozen                     # validated derivation transaction only
complete -> complete                 # sealed terminal state
frozen -> frozen                     # partial ancestry is terminal/read-only
```

Activity is represented by a lock, not an `active` status. Every successful stage
increments `generation` and atomically writes `Process/stages/<stage>.json` with schema
version, stage/tool/config/provider identities, normalized input fingerprints,
dependency stages and generations, output paths/digests/sizes, start/end times, and
result. A missing legacy manifest makes that stage conservatively invalid on resume;
preserved files can still be inputs, but the earliest unverifiable stage and all
downstream dependants rerun.

### Completion and sealing

A native v2 run becomes `complete` and `sealed: true` only after one validation pass
confirms:

- metadata and every required stage manifest parse and form a current dependency DAG;
- Markdown and HTML publications are nonempty, share the authoritative topic-qualified
  stem, and match the recorded digests;
- `Sources/bibliography.md`, `.bib`, and `claims.jsonl` are nonempty and parse; every
  claim references a current section or publication and every evidence reference
  resolves safely;
- the evidence gate, integration verifier, and adversary stage report success for the
  current corpus/question/config fingerprints; and
- a seal manifest records a typed tree inventory and Merkle root for all evidence,
  provenance, and publication files.

`Process/seal.json` is non-self-referential: its content inventory includes finalized
`run.json` bytes but excludes integrity-manifest bytes (`Process/seal.json` and, for a
migrated run, `Process/migration.json`), library locks, and transaction state. Directory
membership still records the integrity-manifest names/types, and each manifest validates
its own canonical projection with its `self_commitment` omitted. Both manifests commit
the same ordinary-content Merkle root, eliminating cross-manifest digest cycles. The
completion transaction computes against proposed final metadata, journals and atomically
publishes metadata plus seal, then validates the compound tree before commit.

After sealing or freezing, normal helpers refuse writes anywhere in the run. Extension
reads the sealed/frozen generation. Index state is deliberately outside the seal.

A migrated legacy run that was complete under the historical contract is not silently
downgraded because it lacks artifacts that did not previously exist. The exact
`migrated-legacy-v1` profile requires one unambiguous selected Markdown Bible at the run
root or under `export/`, nonempty valid UTF-8, with at least one Markdown H1; this
matches the historical completion signal and requires neither HTML nor machine export
files. Every other present artifact and reference must still validate. The run preserves
`status: complete`, records which legacy signal qualified it, and is sealed after
migration. The migration record lists missing native-v2 requirements. It cannot be
resumed in place; a native-v2 publication upgrade is a fresh extension. A legacy run
without that validated Bible migrates as incomplete.

Legacy metadata derivation is deterministic and provenance-tagged: question comes from
`scope.json`, then the selected Bible title, then slug; timestamps come from embedded
compiled dates, then min/max artifact mtimes in UTC, then transaction time. Status comes
only from the applicable validated completion profile. Every fallback and source is
recorded in `Process/migration.json`.

## Structural locking and reservations

All create, resume, extend, migrate, recover, rollback, and indexing mutations use
library-root locks. A lock is created atomically and contains an unguessable ownership
token, canonical target identity, host and boot identity, PID and process-start identity,
operation, creation time, and heartbeat. Every mutating helper must receive and verify
the token; a path check alone is insufficient.

- A library reservation lock atomically claims a slug name without creating its run path.
- A per-run exclusive lock covers resume, migration, rollback, and creation.
- Extension takes a stable parent snapshot under a parent shared lock and holds the new
  child's exclusive lock until its creation manifest commits.
- Migration batches acquire locks by canonical portable collision key to avoid deadlock.
- The semantic index has its own library-wide exclusive lock.

Local locks are stale only when host/boot identity matches and both PID and process-start
identity prove the owner is gone. Remote locks require heartbeat expiry plus explicit
takeover/recovery; PID age alone is never enough. Takeover writes an audit record.
Creating or inspecting a collision does not create the run directory, `_batch` state,
or other user artifacts until a mode is selected and the atomic reservation succeeds.

New/fresh initialization builds and fsyncs a valid minimal v2 skeleton in a hidden
transaction directory under the library, including `run.json`, then atomically renames
that directory with no-replace semantics onto the still-nonexistent reserved slug. A
destination that appears after the claim aborts without overwrite. A crash before rename
leaves only a journaled hidden
temporary that `recover` can finish or remove by exact creation manifest; a crash after
rename leaves a valid incomplete run. Creation and fresh-mode tests kill the worker at
every publish boundary. Extension uses the same atomic skeleton publication before its
longer, separately manifested inheritance phase.

## Existing-slug decision flow

### Interactive

If the portable collision key belongs to a valid legacy or v2 run, offer:

1. **Resume** — continue an incomplete/failed run.
2. **Extend** — create a derived sibling from a stable parent snapshot.
3. **Start fresh** — create a timestamped sibling with no inheritance.
4. **Cancel** — make no changes.

Resume on a completed run reports success/no-op and returns to the choice; it never
unseals the parent.

A frozen run offers Extend, Start fresh, and Cancel. Resume is disabled with
`frozen-parent`; headless `--mode resume` returns that stable nonzero result. Re-extension
from the frozen generation is allowed and leaves the parent unchanged.

If the colliding path is mixed, corrupt, unsupported, invalid, or not a run, Resume and
Extend are disabled with the classification reason. Fresh and Cancel remain available;
Fresh reserves a different sibling and never touches the occupied path.

### Headless and batch

Collision requires global `--mode resume|extend|fresh|cancel` or a per-job `mode` column;
per-job mode wins. Missing mode exits with `mode-required` before any write. `extend`
requires a per-job or global new question; per-job question wins. `cancel` is a clean
skipped result. Resume-complete is success/no-op. Dry-run and batch preparation perform
all collision checks before creating run or batch artifacts. Stable exit/result codes
distinguish complete, skipped, mode-required, unsafe-inheritance, locked, and failed.

Fresh siblings append a microsecond UTC timestamp. Extended siblings use
`<parent-slug>-extended-<readable-question>-<question-hash>`, then the same timestamp and
atomic retry rule on collision.

## Resume semantics

Resume is valid only for incomplete or failed runs. Under its exclusive lock it:

1. opens the existing layout without moving it;
2. validates safe references, stage manifests, fingerprints, outputs, and dependencies;
3. retains manifest-verified outputs;
4. reruns the earliest absent, malformed, unverifiable, or input-invalidated stage and
   all downstream dependants; and
5. updates metadata through the defined lifecycle.

Legacy stages without manifests rerun conservatively from the earliest unverifiable
point. A complete/sealed run is never mutated.

## Extension semantics

Extension accepts a sealed completed parent or an inactive partial parent whose evidence
gate passed and whose entire inheritable corpus validates. A live/locked partial parent
is refused. A partial-parent derivation holds the parent exclusive lock through copying
and uses one journal for the parent freeze and child-lineage commit. It prepares
`Process/snapshots/<generation>.json`, a typed inventory and Merkle root of every regular
artifact except locks and transaction markers. The inventory includes directory
membership and all older snapshot entries. To break self-reference, it represents the
current snapshot manifest by a canonical projection without `self_commitment`, and
represents `run.json` by a canonical projection without `frozen_snapshot`.

Only after the child snapshot, rebased corpus, and lineage are fully verified does that
transaction commit both sides: child ancestry becomes sealed and the parent atomically
changes to `status: frozen`, `frozen_for_derivation: true`, with `frozen_snapshot`
recording generation, Merkle root, manifest path, and manifest-projection SHA-256. A
killed derivation recovers to either an incomplete parent with no committed child or a
frozen parent with a committed child; it cannot strand a frozen parent without lineage.
The parent then becomes permanently read-only, and continuing that research requires
another extension. This makes the actual partial parent immutable, not merely the
child's copy.

For an unmigrated legacy parent, the same inventory, synthetic metadata, and immutable
`run_id` are stored under `.locks/immutable/` in the library registry rather than adding
v2 markers to the run.
The registry has a stable index keyed by canonical library-relative path, filesystem
identity, and portable collision key; its record stores the original `run_id` and
expected Merkle root, so helpers resolve immutability before hashing possibly changed
contents. Synthetic metadata uses the same deterministic question/timestamp fallbacks as
migration and UUIDv5 of canonical source path plus pre-freeze tree root. A partial legacy
parent must meet the gate-passed minimum and receives a `frozen` record. A completed
legacy parent must meet the exact `migrated-legacy-v1` validation profile and receives a
`legacy-complete-seal` record. Either record commits in the same two-sided derivation
transaction as the child and leaves the parent tree unchanged. Every updated legacy-aware
mutating helper checks the registry and refuses the immutable tree; the snapshot receives
the synthetic `run.json` equivalent. Direct edits remain detectable because the expected
Merkle root no longer matches.

A completed/frozen parent uses a shared lock; a newly frozen partial parent remains under
the exclusive derivation lock. The operation captures the generation/root before copying
and recomputes the same non-self-referential inventory afterward. Any change aborts.

The child stores a recursive, immutable ancestry snapshot at
`Process/Inherited/<parent-slug>/snapshot/`. The snapshot includes:

- parent `run.json`, scope, retrieval ledger, lineage, stage, snapshot, and seal manifests;
- all slice/deepening JSONL, source manifests, gate reports, configuration/provider
  provenance, plus verification/adversary records, bibliography, claims, and prior Bible
  when those later-stage artifacts are present;
- every registered target referenced by those artifacts, including parent `Sections/`
  and publication files required by claims or manifests;
- the parent's complete existing `Process/Inherited/` chain; and
- a typed manifest of source path, ancestry, size, mode, SHA-256, and snapshot location.

The minimum partial-parent snapshot is therefore scope, retrieval ledger, gate-passed
Round-1 slice/deepening rows, source manifests, gate report, configuration/provider
provenance, extracted source bytes, run metadata, and the frozen snapshot manifest.
Later-stage artifacts are inherited when present but are not eligibility requirements.
`snapshot.json` declares its archival reference base and original layout, so the copied
tree and hashes stay unchanged while active child rows use rebased references.
The path layer seals `Process/Inherited/` immediately after inheritance and rejects every
later write, rename, or deletion below it, including from legacy low-level helpers.

`Process/lineage.json` has a versioned minimum schema containing child `run_id`, direct
parent `run_id`/slug/layout, parent generation and Merkle root, the
parent's original question, the child's new question, UTC derivation time, snapshot
manifest digest, inherited/new source counts, and the ordered ancestry `run_id` chain.
It stores no active parent filesystem locator. `parent_run_id` is the only resolvable
identity; an opaque `parent_origin_label` may be retained for human provenance but is
never parsed or resolved as a filesystem path.

Extracted source bytes are reflinked/copy-on-write only when the platform supports and
verifies independent copy-on-write semantics; otherwise they are copied. Ordinary hard
links are forbidden. Partial parents are always copied. The child verifies all hashes
and writes rebased rows to `Process/round1/inherited_corpus.jsonl`, preserving original
slice/source identity and full ancestry while pointing at independent files under
`Sources/Extracted/`.

Child creation records every created path and expected digest while holding its exclusive
reservation. On failure, cleanup removes only unchanged manifest-created entries in
reverse order. Any unmanifested or changed entry blocks cleanup and is reported; no
recursive deletion of an unverified directory is allowed. Aside from the explicit,
journaled partial-parent freeze transition, the parent is never changed.

The new question and inherited scope feed Round 0. The prior Bible is available only to
identify gaps and terminology and is excluded from evidence counts and claims. Round 1
retrieves gaps; the gate evaluates inherited plus new corpus; Rounds 2–4 synthesize,
integrate, verify, and adversarially review from scratch. Bibliography and claims are
regenerated for the child.

## Migration command and discovery

```text
python3 scripts/run_manager.py migrate <path> [<path> ...] [--project-dir <dir>]
python3 scripts/run_manager.py migrate <path> --library-dir <dir>   # in-place library
python3 scripts/run_manager.py migrate <path> --output-root <dir>   # legacy synonym
python3 scripts/run_manager.py migrate <path> --dry-run
python3 scripts/run_manager.py recover <transaction> --continue|--abort
python3 scripts/run_manager.py rollback <migrated-run>
```

An explicit run path migrates that run. A path literally named `research` is a library
and discovers immediate run children. A project discovers both `<project>/research/*`
and legacy direct children that have a strong run signature: `scope.json` plus a round
directory, or a canonical Bible/export plus a round/sections directory. Ambiguous
single-marker children are reported and fail preflight. Known repository/tooling
directories are not excluded merely by name; discovery is signature-driven.

Direct legacy children of a project relocate to `<project>/research/<slug>/`.
`--library-dir` or legacy `--output-root` declares a direct library and migrates its
children in place without adding `research/`. An explicit run outside a `research`
parent is ambiguous unless one of `--project-dir`, `--library-dir`, or `--output-root`
declares its destination context; ambiguity fails before writes. Destination collisions
fail; migration never invents a sibling name. The plan states every source and destination.

Arguments are canonicalized by real path, filesystem identity, Unicode/case collision
key, and ancestry. Exact duplicate identities are deduplicated deterministically;
overlapping project/library/run targets, aliases, and escaping symlink arguments are a
preflight error. The full batch is planned and validated before any target starts.

### Canonical migrated publication

`run.json.bible` is the authority. Selection order is: a metadata-declared pair; one
matching topic-qualified `RESEARCH-BIBLE*.md`; one recognized topic-qualified
`*-Research-Bible.md`; one case-normalized topic-qualified `*-BIBLE.md`; otherwise
preflight fails as ambiguous. A valid topic-qualified
basename is preserved even when it differs from the run slug. A generic
`RESEARCH-BIBLE.md` becomes `RESEARCH-BIBLE_<run-slug>.md`. HTML is regenerated or
renamed to the selected Markdown stem. Alternate drafts remain under `Process/Legacy/`.

### Ordered legacy mapping

Specific mappings run before general directory mappings; a claimed source path cannot
match twice:

```text
selected export/*BIBLE*.md|html       -> run root
selected root *-BIBLE.md|html         -> run root
sections/bibliography.md              -> Sources/bibliography.md
export/bibliography.md                -> Sources/bibliography.md
export/bibliography.bib               -> Sources/bibliography.bib
export/claims.jsonl                   -> Sources/claims.jsonl
sections/dedup-decisions.md           -> Process/round3/dedup-decisions.md
round1/sources/**                     -> Sources/Extracted/**
sections/<remaining position>.md      -> Sections/
scope.json                            -> Process/scope.json
retrieval_ledger.json                 -> Process/retrieval_ledger.json
round2_5/**                           -> Process/round2_5/**
round1/**, round2/** ... round5/**    -> Process/<same round>/**
remaining recognized intermediates   -> Process/Legacy/<original relative path>
```

`persona-construction/export/bibliography.md` and equivalent legacy variants therefore
map to the canonical source bibliography. README and the selected Bible remain
reader-facing. No file is deleted as a duplicate.

### Durable transaction and crash recovery

Before mutating any run, migration acquires all required locks, revalidates the plan,
and creates a durable write-ahead journal at
`<resolved-library>/.transactions/<transaction-id>/journal.jsonl`, a location never
moved by the transaction. The journal records transaction/owner tokens, complete typed
pre/post trees, source/destination identities and digests, reference rewrites, and each
operation's intent and completion. Journal appends, state-file replacements, file data,
and affected parent directories are flushed and fsynced at defined boundaries before
the next mutation.

Normal open is blocked while a run has an active transaction marker. On exception the
same process invokes idempotent abort recovery. After SIGKILL, restart, or power loss,
`recover --continue` replays incomplete intents and verifies the planned v2 result;
`recover --abort` reverses completed operations and verifies the original typed tree.
Recovery itself journals every step and is safely repeatable. Locks and journals remain
until a committed or fully restored terminal record is durable.

`Process/migration.json` stores the transaction UUID as an opaque identifier, not a
run-relative path. The resolver locates it under the already known library root. This is
the only cross-run lookup and never enters the artifact safe-path API.

A migration batch has one mutable coordinator journal and one per-run journal segment.
Each run segment becomes immutable at its `ready-to-commit` boundary and its digest enters
that run's `migration.json`; the coordinator may continue appending later run outcomes
and immutable segment digests. Earlier runs may commit before a later run fails. Tests
kill a worker at every journal boundary and prove that continue or abort reaches one
valid layout without data loss.

### Rollback after commit

Committed `Process/migration.json` embeds the finalized inverse plan and compound
canonical pre/post state: ordinary-content paths, types, sizes, modes, relevant
timestamps and SHA-256 values; complete directory membership; plus canonical projections
for the migration and optional seal manifests. It records the immutable per-run journal
segment's SHA-256. Rollback verifies both embedded state and external segment when present; the
embedded copy remains sufficient if library transaction retention later removes the
external journal. To avoid a self-referential digest, the post inventory represents
`Process/migration.json` and `Process/seal.json` with typed canonical-content commitments
computed while each `self_commitment` is omitted; both commit the same ordinary-content
root and never one another's bytes. The per-run segment becomes immutable at
`ready-to-commit`, its digest is placed in `migration.json`, and subsequent transaction
completion is recorded in a separate atomic state file. Rollback
first requires the current entire run tree to equal the
expected post-migration tree. Any changed, missing, or unrecorded entry blocks rollback.
The inverse operation is a new journaled transaction with the same crash-recovery
guarantees; it never deletes an unexpected path or overwrites a reused legacy path.

## Semantic indexing

Indexing selects exactly one content representation per run: the canonical Bible when
the applicable completion profile validates, otherwise verified `Sections/*.md` (or
legacy `sections/*.md`). Stable document IDs are based on run identity plus logical
section/publication identity, not physical path, so migration replaces rather than
duplicates entries.

The library index lock serializes updates. Each refresh uses one database transaction
and atomic commit. Batch workers enqueue run IDs; the coordinator performs one
reconciliation after workers finish. Index failure is nonfatal to research completion
but atomically records a pending/stale entry with the last error and is retryable on the
next search, export, or explicit index command.

## Compatibility and orchestration

- No legacy run moves merely because it is opened, resumed, indexed, or exported.
- Normal new runs use `--project-dir` and v2 paths.
- Legacy `--output-root` remains a direct library root; no automatic `/research` suffix.
- Low-level legacy helper flags remain supported while normal orchestration converges on
  `--run-dir` and `RunLayout`.
- `export.py --sections --bibliography --output-dir` remains valid. The v2 path writes
  the authoritative Bible pair at run root and bibliography/claims in `Sources/`.
- Migration is explicit and immediate by default; installation never bulk-migrates.
- Documentation presents v2 first and labels legacy placement as compatibility only.

Before any low-level helper writes, it checks whether the destination is enclosed by a
managed run. A v2 destination requires `--run-dir` plus the current ownership token and
rejects sealed/frozen runs. A detected legacy run preserves the old CLI but internally
checks the library immutable-parent registry, then acquires and releases its inferred
per-run lock; a live lock or immutable-parent record fails safely. A genuinely
unmanaged output directory with no run markers retains historical standalone behavior.
Thus compatibility flags cannot bypass locking, transaction markers, or immutability.

Every structural command reports resolved project/library/run identity, layout and
metadata schema, mode, lock/transaction identity, lifecycle transition, inherited/new
source counts, move counts, manifest/journal path, and recovery result. Expected errors
use stable nonzero codes.

## Acceptance matrix

| Scenario | Required result |
|---|---|
| Launch from project without `research/` | Create `project/research/<slug>/` in v2 |
| Launch from project with `research/` | Add one child without touching siblings |
| Launch from a directory named `research` | Use it; never create `research/research` |
| Explicit legacy `--output-root` | Continue direct-child semantics unchanged |
| Crash during create/fresh publication | Recover hidden transaction or leave valid incomplete run |
| Open legacy run | Resolve old paths; make no moves |
| Open malformed/newer v2 metadata | Fail corrupt/unsupported; never fall back |
| Resume partial run | Preserve verified stages; rerun earliest unverifiable dependency |
| Existing slug interactively | Offer resume/extend/fresh/cancel before writes |
| Invalid/non-run collision | Disable resume/extend; fresh uses sibling; cancel makes no writes |
| Interactive or headless cancel | Clean skip with no run or batch artifacts created |
| Completed resume | Success/no-op; parent remains sealed |
| Frozen-parent resume | Disable/return `frozen-parent`; extend remains available |
| Headless collision without mode | Fail before run or batch artifacts are created |
| Fresh | Reserve a unique sibling; inherit nothing |
| Extend completed parent | Independent full-corpus snapshot and fresh synthesis |
| Extend completed unmigrated legacy parent | Validate/seal externally; parent tree unchanged |
| Extend partial parent | Freeze parent; inherit stable gate-passed minimum; otherwise refuse |
| Crash during partial-parent derivation | Recover to parent/no-child or immutable parent/committed child |
| Failed child inheritance | Remove only unchanged manifest-created entries |
| Native completion | Strict current gate/verifier/adversary/artifact validation and seal |
| Migrated historical completion | Preserve validated legacy-complete profile explicitly |
| Legacy `*-BIBLE.md` fixture | Select and preserve it as canonical publication |
| Migration default | Preflight, lock, journal, then execute immediately |
| Migration dry run | Identical deterministic plan; zero writes |
| Process death during migration/recovery | Continue or abort to one valid layout |
| Rollback unchanged tree | Restore exact legacy typed tree and checksums |
| Rollback after any tree change | Refuse with complete conflict report |
| Path/symlink/case alias | Fail before mutation |
| Semantic index | One representation per run; serialized, retryable updates |

## Verification and rehearsal

Implementation is test-first and includes:

1. Unit tests for project/library resolution, legacy `--output-root`, portable slugging,
   actual-filesystem case behavior, layout classification, safe no-follow paths,
   metadata/state transitions, stage DAGs, completion profiles, and tokenized locks.
2. Legacy and v2 fixtures for every affected helper and all registered path schemas.
3. Interactive contract tests plus global/per-job headless collision modes and exit codes.
4. Create/fresh subprocess-death tests at every skeleton publication boundary.
5. Extension tests for complete and partial parents, parent freezing, live-parent
   refusal, generation consistency, reflink/copy paths, full recursive ancestry,
   archival reference bases, lineage schema, parent immutability,
   combined gating, fresh outputs, and conflict-safe cleanup. Subprocess-death tests hit
   every parent-freeze, child-lineage, and two-sided commit journal boundary for v2 and
   legacy parents.
6. Migration plan/apply/recover/rollback tests covering overlapping targets, direct
   legacy project children, canonical Bible selection, mapping precedence, all reference
   rewrites, unknown fields, collision aliases, journal loss, and complete tree inventories.
7. Subprocess-death tests at every write-ahead-journal boundary and during abort/continue.
8. Index concurrency, stable-ID replacement, pending retry, and no-duplicate tests.
9. Export, search, batch, and resume tests for legacy and v2, followed by the full suite
   and skill validation.
10. A temporary-copy rehearsal of at least two representative checked-in legacy runs:
   migrate, recover from forced interruption, resume/export one, extend one, index both,
   roll one back, and prove all source/publication hashes and parent immutability.
11. Independent code review against this specification and the complete diff.

Tests never migrate checked-in example data in place.

## Abort criteria

Stop shipment if any operation mutates a sealed or snapshotted parent; loses or changes
an unexplained artifact; permits a path escape or unsafe file type; overwrites or aliases
a collision; cannot recover a killed migration to its exact pre- or post-state; creates
a default run outside the launch project; duplicates indexed content; or breaks legacy
resume, direct-library, export, batch, or search behavior.

## Non-goals

- Automatic installation-time or first-open migration.
- Treating a prior Bible as evidence.
- Ordinary hard-link sharing between runs.
- Renaming round internals beyond nesting under `Process/`.
- Publishing, uploading, or deleting user research.
- Changing semantic-index storage beyond path discovery, stable IDs, and serialization.
- Migrating checked-in example runs as part of implementation.
