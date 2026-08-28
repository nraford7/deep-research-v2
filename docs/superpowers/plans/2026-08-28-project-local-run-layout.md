# Project-Local Research Run Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every normal research run project-local under `research/<slug>/`, adopt the v2 reader-facing layout, support safe resume/extension, and provide explicit crash-recoverable legacy migration.

**Architecture:** A stdlib-only `RunLayout` adapter is the single physical-path authority for native v2, legacy, and deliberately unmanaged helper targets. State, locks, inventories, and journals are separate focused modules; `run_manager.py` composes them into lifecycle, extension, migration, and recovery commands. Existing helpers consume logical layout properties, so one pipeline can read legacy runs while all normal new writes are v2.

**Tech Stack:** Python 3.11+ standard library, pytest, existing vendored semantic-search engine, existing jimemo/built-in HTML exporter.

**Spec:** `docs/superpowers/specs/2026-08-28-project-local-run-layout-design.md`

## Global Constraints

- Default new-run library: `<captured-launch-project>/research/`; a launch directory literally named `research` is already the library.
- `--project-dir` means project; legacy `--output-root` and new `--library-dir` mean a direct library and must not gain another `research/` component.
- New managed runs are layout version 2; opening legacy never migrates it.
- V2 roots contain only topic-qualified Bible Markdown/HTML, optional `README.md`, `Sections/`, `Sources/`, and `Process/`.
- `Sources/` owns `bibliography.md`, `bibliography.bib`, and `claims.jsonl`; extracted bytes live only in `Sources/Extracted/`.
- Ordinary hard links are forbidden for inheritance; use verified copy-on-write cloning or copying.
- Every structural write carries an ownership token and respects sealed/frozen/transaction state.
- Persisted active artifact paths are safe POSIX run-root-relative paths; archival snapshots declare their own root.
- Migration is explicit, immediate by default, dry-runnable, journaled, recoverable, and never overwrites.
- Existing checked-in research runs are copied to temporary directories for tests/rehearsal and are never migrated in place.
- No new third-party runtime dependency is introduced.

## File and responsibility map

- `scripts/run_layout.py`: layout detection, canonical paths, project/library resolution, `slug-v1`, safe relative references, Bible discovery.
- `scripts/run_path_schema.py`: versioned persisted-path schemas, active/legacy/archive reference bases, validation and migration rewrites.
- `vendor/unicode/15.0.0/UnicodeData.txt`, `vendor/unicode/15.0.0/LICENSE.txt`: fixed Unicode decomposition data and Unicode license for host-independent `slug-v1`.
- `scripts/run_fs.py`: descriptor-relative rooted reads/writes/mkdir/enumeration with no-follow and state/token enforcement.
- `scripts/run_transactions.py`: renewable lease keeper, tokenized shared/exclusive locks, atomic publication, typed tree inventories, Merkle roots, journals, immutable-parent registry, recovery primitives.
- `scripts/run_state.py`: `run.json`, stage manifests, lifecycle transitions, native/legacy completion validation, seals.
- `scripts/run_extension.py`: parent validation/freezing, immutable ancestry snapshot, rebased inherited corpus, lineage and child cleanup.
- `scripts/run_migration.py`: discovery, ordered mapping, schema-aware rewrites, plans, apply/recover/rollback.
- `scripts/run_manager.py`: public CLI and stable result/exit codes; no low-level filesystem policy duplicated here.
- Existing research helpers: replace hard-coded `roundN`, scope, ledger, sections, export, and source paths with `RunLayout` properties.
- `scripts/batch_research.py`: project-local default, collision modes, delayed batch artifact creation, layout-aware completion.
- `scripts/export.py`, `scripts/research_bible_html.py`, `scripts/search.py`: v2 publication/source placement and one-representation indexing while retaining legacy/manual modes.
- `vendor/semantic_search/search.py`: optional logical document IDs/display paths and schema migration used by the wrapper; existing callers remain compatible.
- `SKILL.md`, `README.md`: lifecycle commands, four-way collision flow, v2 tree, extension and migration recipes.

---

### Task 1: Layout detection, project resolution, portable slugs, and safe references

**Files:**
- Create: `scripts/run_layout.py`
- Create: `scripts/run_path_schema.py`
- Create: `tests/test_run_layout.py`
- Create: `tests/test_run_path_schema.py`
- Add: `vendor/unicode/15.0.0/UnicodeData.txt`
- Add: `vendor/unicode/15.0.0/LICENSE.txt`

**Interfaces:**
- Produces: `LayoutKind`, `LayoutError`, `ResolvedRoot`, `RunLayout`, `PathBase`, `PathField`, `PathSchemaRegistry`, `PATH_SCHEMAS`, `resolve_project_root()`, `slugify_v1()`, `portable_collision_key()`, `safe_relpath()`.
- Consumes: only Python standard library.

- [ ] **Step 1: Write failing classification and path-contract tests**

```python
from pathlib import Path
import json
import pytest

from scripts.run_layout import (
    LayoutError, LayoutKind, RunLayout, resolve_project_root,
    safe_relpath, slugify_v1,
)

def test_v2_layout_exposes_reader_and_process_homes(tmp_path):
    run = tmp_path / "research" / "topic"
    (run / "Process").mkdir(parents=True)
    (run / "Process" / "run.json").write_text(
        json.dumps({"layout_version": 2, "schema_version": 1, "slug": "topic"})
    )
    layout = RunLayout.open(run)
    assert layout.kind is LayoutKind.V2
    assert layout.sections == run / "Sections"
    assert layout.extracted_sources == run / "Sources" / "Extracted"
    assert layout.round1 == run / "Process" / "round1"
    assert layout.claims == run / "Sources" / "claims.jsonl"

def test_present_malformed_v2_metadata_never_falls_back_to_legacy(tmp_path):
    run = tmp_path / "topic"
    (run / "Process").mkdir(parents=True)
    (run / "sections").mkdir()
    (run / "Process" / "run.json").write_text("{")
    with pytest.raises(LayoutError, match="corrupt"):
        RunLayout.open(run)

def test_project_and_direct_library_resolution_are_distinct(tmp_path):
    assert resolve_project_root(project_dir=tmp_path).library == tmp_path / "research"
    research = tmp_path / "research"
    assert resolve_project_root(project_dir=research).library == research
    custom = tmp_path / "custom"
    assert resolve_project_root(library_dir=custom).library == custom

@pytest.mark.parametrize("value", ["../x", "/x", "C:\\\\x", "a\\\\b", "a/../b"])
def test_safe_relpath_rejects_escape_and_non_posix_forms(value):
    with pytest.raises(ValueError):
        safe_relpath(value)

def test_slug_v1_is_portable_deterministic_and_bounded():
    assert slugify_v1("Crème / CON") == "creme-con"
    assert slugify_v1("CON") == "con-run"
    assert len(slugify_v1("é" * 400).encode()) <= 120
```

- [ ] **Step 2: Run the focused tests and observe the expected import failure**

Run: `python3 -m pytest tests/test_run_layout.py -q`

Expected: FAIL during collection because `scripts.run_layout` does not exist.

- [ ] **Step 3: Implement the stdlib-only layout authority**

```python
class LayoutKind(Enum):
    V2 = "v2"
    LEGACY = "legacy"
    UNMANAGED = "unmanaged"

@dataclass(frozen=True)
class ResolvedRoot:
    project: Path | None
    library: Path

@dataclass(frozen=True)
class RunLayout:
    run_root: Path
    kind: LayoutKind
    metadata_data: dict | None = None

    @property
    def sections(self) -> Path:
        return self.run_root / ("Sections" if self.kind is LayoutKind.V2 else "sections")

    @property
    def round1(self) -> Path:
        return self.process / "round1" if self.kind is LayoutKind.V2 else self.run_root / "round1"
```

Implement all properties listed in the spec. `open()` must treat any malformed/unsupported `Process/run.json` as authoritative failure, treat simultaneous legacy/v2 homes as mixed, recognize legacy `sections|chapters`, `export`, `round1`, and root `scope.json`, and allow an empty path only through `allow_unmanaged=True`. Parse the checked-in official Unicode 15.0.0 `UnicodeData.txt` under its checked-in Unicode license; do not call the host's version-varying `unicodedata.normalize`. Implement device-name suffixing only when the complete normalized component is reserved, final 120-byte budgeting including extension/timestamp/retry suffixes, filesystem-safe timestamps, NFC/casefold collision keys, and safe `PurePosixPath` parsing.

Add `probe_filesystem(path) -> FilesystemCapabilities` using temporary exclusive names under the target library to determine case and Unicode normalization aliasing without leaving artifacts, but invoke it only after a mutating mode is selected. `capabilities_for_dry_run(path)` performs read-only conservative planning using portable collision keys, directory enumeration, and platform metadata, marks `write_probe_pending=True`, and calls no create/mkdir/rename/unlink primitive. Add simulated case-sensitive/case-insensitive probe tests, an audited zero-write dry-run test, and atomic collision-reservation tests; both the portable collision key and probed filesystem key must reject aliases before mutation.

- [ ] **Step 4: Add Bible selection fixtures for all checked-in naming families**

Add tests for `RESEARCH-BIBLE_topic.md`, `Persona-Construction-Research-Bible.md`, `western-philosophy-of-mind-BIBLE.md`, generic `RESEARCH-BIBLE.md` normalization, paired HTML, and ambiguous multiple candidates. Implement `RunLayout.discover_bible()` returning authoritative Markdown/HTML relative paths or a typed ambiguity error.

- [ ] **Step 5: Implement the versioned path-schema registry**

```python
class PathBase(Enum):
    RUN_ROOT = "run-root"
    LEGACY_ROUND1 = "legacy-round1"
    ARCHIVE_ROOT = "archive-root"
    NON_RESOLVING = "non-resolving"

@dataclass(frozen=True)
class PathField:
    field: str
    base: PathBase
    required: bool
```

Implement `PathSchemaRegistry.schema_for(relative_document)`, `resolve(layout, document, field, value)`, `validate_document`, `rewrite_document`, and `reject_unknown_path_fields`. Register slice/deepening JSONL `text_path/raw_path/run_dir`, claims `file`, evidence/fulltext/source manifests, coverage/verification/export inventories, stage inputs/outputs, lineage, and archival `snapshot.json`. Archival documents resolve only against their declared archive root; opaque provenance labels are non-resolving. Add fixture-driven tests that enumerate every path-bearing field found in complete read-only copies of `persona-construction` and `western-philosophy-of-mind`; every field must be registered, resolve safely, or be explicitly non-resolving.

- [ ] **Step 6: Run and commit**

Run: `python3 -m pytest tests/test_run_layout.py tests/test_run_path_schema.py -q`

Expected: PASS.

Commit: `git add scripts/run_layout.py scripts/run_path_schema.py tests/test_run_layout.py tests/test_run_path_schema.py vendor/unicode/15.0.0/UnicodeData.txt vendor/unicode/15.0.0/LICENSE.txt && git commit -m "Add versioned research run layout authority"`

---

### Task 2: Rooted I/O, renewable leases, immutable registry, journals, and crash-safe publication

**Files:**
- Create: `scripts/run_fs.py`
- Create: `scripts/run_transactions.py`
- Create: `tests/test_run_fs.py`
- Create: `tests/test_run_transactions.py`

**Interfaces:**
- Consumes: `ResolvedRoot`, `safe_relpath`, `portable_collision_key` from Task 1.
- Produces: `RootedFS`, `LeaseOwner`, `RunLease`, `ImmutableRegistry`, `TreeEntry`, `TreeInventory`, `Journal`, `publish_skeleton()`, `recover_creation()`.

- [ ] **Step 1: Write failing token/atomicity/inventory tests**

```python
def test_lock_token_is_required_and_stale_pid_reuse_is_not_accepted(tmp_path, monkeypatch):
    lock = RunLease.acquire(tmp_path, "topic", operation="create")
    with pytest.raises(LockError, match="ownership token"):
        lock.verify("wrong-token")
    lock.verify(lock.owner.token)

def test_publish_skeleton_never_replaces_a_racing_destination(tmp_path):
    library = tmp_path / "research"
    library.mkdir()
    tx = create_skeleton_transaction(library, "topic", question="Q")
    (library / "topic").mkdir()
    with pytest.raises(TransactionConflict):
        tx.publish()
    assert not (library / "topic" / "Process" / "run.json").exists()

def test_inventory_rejects_symlink_and_detects_unrecorded_file(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "a.txt").write_text("a")
    before = TreeInventory.capture(root)
    (root / "extra.txt").write_text("x")
    assert before.diff(TreeInventory.capture(root)).added == ["extra.txt"]

def test_rooted_write_rejects_component_swapped_to_symlink(rooted_fs, tmp_path):
    rooted_fs.mkdir("Sources/Extracted", parents=True)
    rooted_fs.test_hook = lambda: replace_directory_with_symlink(
        rooted_fs.root / "Sources", tmp_path / "escape"
    )
    with pytest.raises(UnsafePathError):
        rooted_fs.atomic_write_text("Sources/Extracted/a.txt", "secret")
    assert not (tmp_path / "escape" / "Extracted/a.txt").exists()

def test_legacy_immutable_registry_resolves_after_rename_and_alias(tmp_path):
    library, run = seed_legacy_run(tmp_path)
    record = ImmutableRegistry(library).record(run, kind="frozen", expected_root="abc")
    renamed = run.with_name("TOPIC")
    run.rename(renamed)
    assert ImmutableRegistry(library).resolve(renamed).run_id == record.run_id
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_run_fs.py tests/test_run_transactions.py -q`

Expected: FAIL because the transaction module is absent.

- [ ] **Step 3: Implement descriptor-relative rooted filesystem operations**

```python
@dataclass
class RootedFS:
    root: Path
    lease_token: str | None
    state_guard: Callable[[str, str], None]

    def read_bytes(self, relative: str) -> bytes:
        return self._read_regular(safe_relpath(relative))

    def atomic_write_text(self, relative: str, text: str) -> None:
        self.atomic_write_bytes(relative, text.encode("utf-8"))
```

Implement `read_bytes/read_text`, `open_exclusive`, `atomic_write_bytes/text/json`, `mkdir`, `iter_regular`, `stat_regular`, `unlink_regular`, `rmdir_empty`, `rename_no_replace`, `copy_regular_from`, verified `reflink_or_copy_from`, and `cleanup_manifest_entries`. Walk every source and destination component from opened root descriptors with `dir_fd`, `O_DIRECTORY`, and `O_NOFOLLOW`; compare device/inode identities before use; create and rename temporary files descriptor-relatively; fsync files and both parent directories. `state_guard(operation, relative_path)` enforces lease token, active transaction, whole-run seal/freeze, and immutable `Process/Inherited/` subtree restrictions for every read or mutation; incomplete children may still write `Process/round*`. Cross-root move is copy-to-verified-temp + no-replace publish + source unlink only after journal commit. Cleanup removes only manifest-listed entries whose identities/digests still match. Reject managed mutation when reliable no-follow primitives are unavailable. Tests must component-swap every primitive and cover symlink, junction/reparse abstraction, mount/device change, FIFO/socket/device, wrong-token cases, and rejection of every mutation below `Process/Inherited/` while `Process/round2/` stays writable.

- [ ] **Step 4: Implement renewable shared/exclusive leases and no-replace publication**

```python
@dataclass(frozen=True)
class LeaseOwner:
    token: str
    lease_id: str
    host: str
    boot_id: str
    keeper_pid: int
    keeper_process_start: str
    operation: str
    created_at: str
    renewed_until: str

@dataclass
class RunLease:
    library: Path
    key: str
    owner: LeaseOwner
    shared: bool
```

Implement exact `RunLease.acquire(library, key, *, operation, shared=False)`, `verify(token)`, `renew(token)`, and `release(token)` methods. `prepare` launches a small stdlib lease-keeper/broker process that owns the local PID/process-start identity, maintains the lease record while `renewed_until` is valid, and exits/removes its holder on authenticated release or expiry. Its authenticated local IPC protocol has a closed request enum: `publish-artifact`, `invoke-helper`, `record-stage`, `export`, `finalize`, `mark-failed`, `renew`, and `release`. `publish-artifact` carries lease token, logical destination, expected scratch-source digest/size, and stage. `invoke-helper` carries one allowlisted helper ID plus a helper-specific typed argument object containing no executable or output path; the broker imports that helper's managed function, injects `RunLayout`/`RootedFS`, and returns captured status/output. The broker itself performs stage/export/finalize mutations. Unknown fields, helper IDs, paths, environment overrides, and commands are rejected. Every request renews the bearer token; long agent-only synthesis explicitly invokes `run_manager lease renew`. Shared holders coexist only with other readers; exclusive acquisition is atomic and waits/fails while any holder exists. Use a short guard lock solely while atomically updating the holders registry, canonical key ordering for batches, and explicit audited remote takeover after heartbeat/expiry rules. `publish_skeleton()` must fsync a valid hidden skeleton under `.transactions/`, hold the slug claim lease, and use a no-replace publish strategy.

- [ ] **Step 5: Implement the immutable-parent registry before helper conversion**

`ImmutableRegistry` lives under `.locks/immutable/` and atomically indexes canonical library-relative path, filesystem identity, and portable collision key to a stable `run_id`, expected root, kind, and derivation transaction. `resolve(path)` consults all three identities before current-tree hashing, detects rename/case/Unicode/hard-link aliases, and fails closed on conflicting records. Expose the guard through `RootedFS` and a lightweight `guard_legacy_mutation(path)` used by every legacy-aware writer. Commit/abort registry records only through journal intents so an immutable legacy parent and committed child are one recoverable outcome.

- [ ] **Step 6: Implement typed tree inventories and append-only journals**

Capture relative path, type, size, mode, mtime-ns, SHA-256, and directory membership without following links; reject special files and mount changes. Journal records contain monotonically increasing sequence, transaction/run IDs, intent/completion state, payload digest, and previous-record digest. Flush each record and affected directory. Add idempotent creation recovery that either publishes the verified skeleton or removes only manifest-listed unchanged temporary entries.

- [ ] **Step 7: Add cross-process lease and subprocess kill-boundary tests**

Exercise `prepare → broker invoke-helper(scope/retrieval fixture) → scratch writer → broker publish-artifact → broker export → record-stage → finalize`, expired/wrong-token requests, unknown helper/argument rejection, sealed/frozen/Inherited-subtree rejection, token renewal, explicit release, keeper expiry after launcher abandonment, stale local PID reuse, and remote takeover. The caller process receives read-only access to the run and a writable scratch only; an attempted direct run write must fail while the complete broker path succeeds. Run concurrent shared readers against an exclusive writer and prove the writer cannot enter early. Parameterize a tiny helper process with `DR_TEST_CRASH_AT=after-journal|after-skeleton|before-publish|after-publish`, kill with `os._exit(91)`, then call `recover_creation()`. Assert the slug is absent with no hidden leftovers or is a valid incomplete v2 run; never accept an invalid visible directory.

- [ ] **Step 8: Run and commit**

Run: `python3 -m pytest tests/test_run_fs.py tests/test_run_transactions.py -q`

Expected: PASS, including kill-point cases.

Commit: `git add scripts/run_fs.py scripts/run_transactions.py tests/test_run_fs.py tests/test_run_transactions.py && git commit -m "Add safe run I/O leases and transactions"`

---

### Task 3: Run metadata, stage manifests, resume validation, completion profiles, and seals

**Files:**
- Create: `scripts/run_state.py`
- Create: `tests/test_run_state.py`

**Interfaces:**
- Consumes: `RunLayout`, `PATH_SCHEMAS`, `RootedFS`, `TreeInventory`, `Journal`, `RunLease`.
- Produces: `RunMetadata`, `StageManifest`, `RunState`, `record_stage()`, `resume_plan()`, `validate_completion()`, `seal_run()`.

- [ ] **Step 1: Write lifecycle, DAG, and sealing RED tests**

```python
def test_resume_restarts_at_earliest_invalid_dependency(v2_run):
    record_stage(v2_run, "round0", inputs=[], outputs=["Process/scope.json"], tool="scope")
    record_stage(v2_run, "round1", inputs=["Process/scope.json"], outputs=["Process/round1/slice.jsonl"], tool="slice")
    (v2_run / "Process" / "scope.json").write_text("changed")
    assert resume_plan(v2_run).restart_stage == "round0"

def test_native_complete_requires_current_gate_verifier_adversary_and_exports(v2_run):
    result = validate_completion(v2_run)
    assert not result.ok
    assert {"evidence_gate", "integration", "adversary", "bible_html"} <= set(result.missing)

def test_migrated_legacy_profile_accepts_one_valid_h1_bible(legacy_run):
    (legacy_run / "export").mkdir()
    (legacy_run / "export" / "topic-BIBLE.md").write_text("# Topic\nBody")
    assert validate_legacy_completion(legacy_run).ok
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_run_state.py -q`

Expected: FAIL because `scripts.run_state` is absent.

- [ ] **Step 3: Implement metadata and versioned stage manifests**

```python
@dataclass(frozen=True)
class StageManifest:
    schema_version: int
    stage: str
    generation: int
    tool: str
    config_fingerprint: str
    provider_identity: str | None
    dependencies: dict[str, int]
    inputs: tuple[ArtifactDigest, ...]
    outputs: tuple[ArtifactDigest, ...]
    result: str

@dataclass(frozen=True)
class ResumePlan:
    restart_stage: str | None
    reusable_outputs: tuple[str, ...]
    invalid_reasons: tuple[str, ...]
```

Implement the lifecycle exactly: new→incomplete; incomplete→complete/failed/frozen; failed→incomplete/frozen; complete and frozen terminal. Validate each manifest's dependency generations and normalized input/output digests. Legacy stages without manifests restart conservatively at the earliest unverifiable stage.

- [ ] **Step 4: Implement compound content roots and completion profiles**

Native validation must parse nonempty Bible Markdown/HTML, bibliography Markdown/BibTeX, and claims JSONL; resolve every claim/source reference; require current successful evidence-gate, integration, citation-verifier, and adversary manifests; and ensure Bible stems match `run.json.bible`. Implement `migrated-legacy-v1` as one unambiguous nonempty UTF-8 Markdown Bible containing H1, with all present references validated. Seal content using the canonical projections from the spec and refuse writes when sealed/frozen.

- [ ] **Step 5: Add freeze/seal crash recovery tests**

Kill at journal boundaries around final metadata and `Process/seal.json`; recovery must return either the exact pre-state or a fully validated sealed state. Test canonical projection stability for migrated runs containing `Process/migration.json`.

- [ ] **Step 6: Run and commit**

Run: `python3 -m pytest tests/test_run_state.py -q`

Expected: PASS.

Commit: `git add scripts/run_state.py tests/test_run_state.py && git commit -m "Add research run lifecycle and sealing"`

---

### Task 4: Public run manager for create, status, collision modes, stage recording, and finalize

**Files:**
- Create: `scripts/run_manager.py`
- Create: `tests/test_run_manager.py`

**Interfaces:**
- Consumes: Tasks 1–3 modules.
- Produces CLI commands `prepare`, `status`, `invoke-helper`, `publish-artifact`, `record-stage`, `export`, `finalize`, `lease renew`, `lease release`, `recover`; stable JSON result schema and exit codes.

- [ ] **Step 1: Write CLI behavior tests**

```python
def test_prepare_defaults_to_captured_project_research(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = run_manager.run(["prepare", "--slug", "topic", "--question", "Q", "--json"])
    assert result == 0
    assert (tmp_path / "research" / "topic" / "Process" / "run.json").exists()

def test_collision_without_mode_is_prewrite_error(tmp_path):
    create_v2(tmp_path / "research" / "topic")
    before = snapshot(tmp_path)
    assert run_manager.run(["prepare", "--project-dir", str(tmp_path), "--slug", "topic", "--question", "Q"]) == Exit.MODE_REQUIRED
    assert snapshot(tmp_path) == before

def test_frozen_resume_is_rejected_but_fresh_gets_sibling(tmp_path):
    parent = create_v2(tmp_path / "research" / "topic", status="frozen")
    assert run_manager.run(["prepare", "--library-dir", str(parent.parent), "--slug", "topic", "--question", "Q", "--mode", "resume"]) == Exit.FROZEN_PARENT
    assert run_manager.run(["prepare", "--library-dir", str(parent.parent), "--slug", "topic", "--question", "Q", "--mode", "fresh"]) == 0
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_run_manager.py -q`

Expected: FAIL because the CLI module is absent.

- [ ] **Step 3: Implement explicit subcommands and JSON results**

```python
class Exit(IntEnum):
    OK = 0
    INVALID_LAYOUT = 10
    MODE_REQUIRED = 11
    COLLISION = 12
    UNSAFE_INHERITANCE = 13
    LOCKED = 14
    FROZEN_PARENT = 15
    TRANSACTION_REQUIRED = 16

@dataclass(frozen=True)
class PrepareResult:
    action: str
    run_dir: Path | None
    choices: tuple[str, ...]
    resume_plan: ResumePlan | None
    lease_id: str | None
    lease_token: str | None
    lease_keeper_pid: int | None
    renewed_until: str | None
    broker_endpoint: str | None
    scratch_dir: Path | None
```

Implement exact `prepare_run(*, question: str, slug: str | None, project_dir: Path | None, library_dir: Path | None, mode: str | None) -> PrepareResult`. Interactive prompting remains owned by the skill; the CLI never prompts. A collision result lists valid choices. `resume` returns a `ResumePlan`; `fresh` publishes a timestamped sibling; `cancel` returns clean skip without run or `_batch` writes; complete resume is success/no-op. A mutating result returns the live lease identity/token/keeper/expiry, broker endpoint, and isolated scratch directory. In managed mode, `invoke-helper`, `publish-artifact`, `record-stage`, `export`, `finalize`, renew/release, and failure commands are thin authenticated IPC clients and never mutate the run from the caller process. `publish-artifact --scratch-file <path> --logical-path <run-relative> --sha256 <digest> --size <n> --lease-token <token>` sends the bounded request. `invoke-helper --helper <allowlisted-id> --args-json <typed-json>` contains no arbitrary command/output path. Broker-side `record-stage` consumes already-published logical paths; broker-side `export` invokes the managed export function; broker-side `finalize` validates, seals, and releases. Standalone library calls used by an owning coordinator still share the same broker handlers rather than a second mutation path.

- [ ] **Step 4: Add dry-run and corrupt-collision coverage**

Assert dry-run produces byte-identical JSON planning output but no `.locks`, `.transactions`, run, or `_batch` files. Monkeypatch/audit `os.open`, `mkdir`, `rename`, and `unlink` to prove no mutating call occurs; capability output must say `write_probe_pending`. Corrupt/mixed/non-run collisions expose only fresh/cancel, and fresh cannot touch the occupied path. A real write probe runs only after mutating mode and destination are confirmed, but before the first structural mutation.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest tests/test_run_manager.py tests/test_run_state.py -q`

Expected: PASS.

Commit: `git add scripts/run_manager.py tests/test_run_manager.py && git commit -m "Add project-local run lifecycle CLI"`

---

### Task 5: Make retrieval, gate, deepening, and ledger helpers layout-aware

**Files:**
- Modify: `scripts/slice_search.py`
- Modify: `scripts/fetch_fulltext.py`
- Modify: `scripts/citation_chase.py`
- Modify: `scripts/coverage_audit.py`
- Modify: `scripts/evidence_gate.py`
- Modify: `scripts/deepen_questions.py`
- Modify: `scripts/ledger.py`
- Modify: `scripts/background.py`
- Modify: `scripts/scope.py`
- Modify: `scripts/dedup_bib.py`
- Modify: `scripts/verify_citations.py`
- Modify: `scripts/classify_sources.py`
- Modify: `scripts/lit_search.py`
- Modify: `dispatch.py`
- Modify: corresponding existing tests; create `tests/test_helper_layouts.py`

**Interfaces:**
- Consumes: `RunLayout.open(run_dir, allow_unmanaged=True)`, `PATH_SCHEMAS`, `RootedFS`, lease verification, and `ImmutableRegistry` guard.
- Produces: identical helper behavior against legacy/unmanaged paths and v2 `Process/`/`Sources/` paths.

- [ ] **Step 1: Write one failing legacy/v2 parity test per helper family**

```python
@pytest.mark.parametrize("kind", ["legacy", "v2"])
def test_evidence_gate_reads_logical_round1(make_run, kind):
    run, layout = make_run(kind)
    seed_thick_corpus(layout.round1)
    assert evidence_gate.evaluate(run)[0] is True

def test_v2_fulltext_writes_active_refs_relative_to_run_root(v2_run, monkeypatch):
    process_run(v2_run, min_chars=10, max_bytes=1000, max_chars=1000, timeout=1)
    row = read_first_jsonl(v2_run / "Process/round1/slice_web.jsonl")
    assert row["text_path"].startswith("Sources/Extracted/")
    assert (v2_run / row["text_path"]).is_file()
```

- [ ] **Step 2: Run the focused RED set**

Run: `python3 -m pytest tests/test_helper_layouts.py -q`

Expected: FAIL because helpers still hard-code legacy paths.

- [ ] **Step 3: Replace physical path construction with layout properties**

Refactor each helper into a managed function accepting `(layout: RunLayout, fs: RootedFS, typed_args)`, plus its existing CLI adapter. A managed CLI invocation with broker endpoint/token serializes only its allowlisted helper ID and typed semantic arguments to `run_manager invoke-helper`; it never constructs a writable local `RootedFS`. The broker imports and invokes the managed function outside the contained worker sandbox. An owning coordinator and tests may call the same function through the broker handler, never a parallel unchecked write path. Use `layout.round1`, `.round2_5`, `.scope`, `.retrieval_ledger`, and `.extracted_sources`. Extend `RetrievalLedger` to accept a rooted logical ledger or legacy explicit path without changing existing callers. For v2 rows persist `Sources/Extracted/<name>`; legacy/unmanaged rows retain `sources/<name>` relative to `round1`. Resolve both through the path registry, never `run_dir / untrusted_string`.

Every JSON/JSONL read and write calls `PATH_SCHEMAS.validate_document`; active rows are normalized through its declared base, and unknown path-like fields fail before publication. Completion, extension, and migration later consume the same registry rather than re-declaring field names.

Add broker-aware managed modes to `scope.py`, `dedup_bib.py`, `verify_citations.py`, `classify_sources.py`, and `lit_search.py`. Their legacy explicit input/output flags first call the central enclosure guard: managed v2 destinations reject local mutation and require a broker request; legacy destinations acquire/renew a lease and consult `ImmutableRegistry`; genuinely unmanaged paths retain standalone behavior. `dedup_bib.py` places the master bibliography in `Sources/bibliography.md` and decisions in `Process/round3/dedup-decisions.md`; verification/classification/literature outputs go under `Process/round4/`.

- [ ] **Step 4: Preserve old tests and add mutation guards**

Existing blank-`tmp_path` fixtures intentionally exercise unmanaged compatibility and must remain green. Parameterize every mutating entrypoint—scope, slice, fetch, chase, audit, gate manifest, deepening, ledger, dedup, verification, classification, literature, and dispatch—to prove canonical v2 placement and rejection of a sealed/frozen run, active transaction, wrong/missing token, immutable legacy registry, and wrong-home explicit output. Add adversarial component-swap tests for current writers such as full-text and ledger replacement.

Update `dispatch.py` to resolve an existing managed `--run-dir` through `RunLayout`, write scope and rounds to logical homes, and print `Sections/` for v2. Add `--project-dir/--slug/--question` as the normal creation path through `prepare_run()` while retaining explicit `--run-dir` for legacy/manual callers. It must never create an unmanaged run implicitly.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest tests/test_scope.py tests/test_slice_search.py tests/test_fetch_fulltext.py tests/test_citation_chase.py tests/test_coverage_audit.py tests/test_evidence_gate.py tests/test_deepen_questions.py tests/test_ledger.py tests/test_parsers.py tests/test_lit_search_graph.py tests/test_corpus_support.py tests/test_corpus_support_paths.py tests/test_dispatch_mode.py tests/test_helper_layouts.py -q`

Expected: PASS.

Commit: `git add dispatch.py scripts/scope.py scripts/slice_search.py scripts/fetch_fulltext.py scripts/citation_chase.py scripts/coverage_audit.py scripts/evidence_gate.py scripts/deepen_questions.py scripts/ledger.py scripts/background.py scripts/dedup_bib.py scripts/verify_citations.py scripts/classify_sources.py scripts/lit_search.py tests/test_helper_layouts.py tests/test_dispatch_mode.py tests/test_scope.py tests/test_slice_search.py tests/test_fetch_fulltext.py tests/test_citation_chase.py tests/test_coverage_audit.py tests/test_evidence_gate.py tests/test_deepen_questions.py tests/test_ledger.py tests/test_parsers.py tests/test_lit_search_graph.py tests/test_corpus_support.py tests/test_corpus_support_paths.py && git commit -m "Route research helpers through RunLayout"`

---

### Task 6: V2 export, HTML, claims, and semantic indexing

**Files:**
- Modify: `scripts/export.py`
- Modify: `scripts/research_bible_html.py`
- Modify: `scripts/search.py`
- Modify: `vendor/semantic_search/search.py`
- Create: `tests/test_semantic_engine_logical_ids.py`
- Modify: `tests/test_export_html.py`
- Modify: `tests/test_research_bible_html.py`
- Modify: `tests/test_search_wrapper.py`
- Create: `tests/test_search_layouts.py`

**Interfaces:**
- Consumes: `RunLayout`, `RunState`, index lock from Tasks 1–3.
- Produces: `export.py --run-dir`, canonical run-root Bible pair, `Sources/` machine exports, stable one-representation index selection.

- [ ] **Step 1: Write v2 export and index-selection RED tests**

```python
def test_run_dir_export_places_every_reader_and_source_artifact(v2_run):
    assert export.main(["--run-dir", str(v2_run), "--bible", str(v2_run / "RESEARCH-BIBLE_topic.md")]) == 0
    assert (v2_run / "RESEARCH-BIBLE_topic.html").exists()
    assert (v2_run / "Sources/bibliography.bib").exists()
    rows = read_jsonl(v2_run / "Sources/claims.jsonl")
    assert all(row["file"].startswith("Sections/") for row in rows)

def test_index_selects_complete_bible_or_incomplete_sections_once(tmp_path):
    complete, partial = seed_complete_and_partial_runs(tmp_path / "research")
    docs = search.select_documents(tmp_path / "research")
    assert docs[complete.run_id] == [complete.bible_markdown]
    assert docs[partial.run_id] == sorted(partial.sections.glob("*.md"))

def test_relocation_reconciles_same_logical_id_without_duplicate(tmp_path):
    run = seed_complete_run(tmp_path / "research/topic")
    search.reconcile(tmp_path / "research")
    relocate_without_changing_run_id(run, tmp_path / "research/renamed")
    search.reconcile(tmp_path / "research")
    assert indexed_rows(tmp_path / "research", run.run_id) == 1
    assert query_display_path(tmp_path / "research", run.run_id).startswith("renamed/")
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_export_html.py tests/test_search_layouts.py -q`

Expected: FAIL because `--run-dir` and document selection do not exist.

- [ ] **Step 3: Add managed export mode without breaking standalone mode**

Refactor export into `export_managed(layout, fs, args)` and the existing standalone function. `--run-dir --broker-endpoint --lease-token` sends the broker `export` request; the contained caller never writes the run. Broker-side managed export derives sections, master bibliography, output locations, and Bible mapping from `RunLayout`, writes through `RootedFS`, and records the export stage. Reject conflicting physical flags. Existing `--sections --bibliography --output-dir` remains unmanaged/legacy compatible after enclosure/immutability guards. Store claims `file` fields as safe run-root-relative paths. HTML discovery follows `run.json.bible` first and existing legacy discovery second.

- [ ] **Step 4: Add a backward-compatible logical-ID engine schema and adapter**

Extend the vendored engine's `files` schema with nullable unique `logical_id` and `display_path`, preserving physical-path behavior when callers omit them. Add an idempotent schema migration using `PRAGMA user_version`, backfill legacy rows with `logical_id = path`, and add `cmd_index_documents(root, db_path, documents, *, rebuild)` where each document is `{logical_id, content_path, display_path}`. Reconcile by logical ID: update moved content/display paths, delete orphan logical IDs, and keep unchanged embeddings. Query output and JSON use `display_path`, never the internal content path. Keep existing `cmd_index(... in_patterns=...)` byte-compatible.

- [ ] **Step 5: Add serialized one-representation indexing and pending recovery**

Implement `select_documents(library) -> dict[run_id, list[DocumentSpec]]`. Use canonical Bible for validated complete profiles and verified sections otherwise; legacy uses its selected Bible when complete, sections otherwise. Generate `logical_id = sha256(run_id + "\0" + logical_document_id)`. Hold the index lease for schema migration and one SQLite transaction; atomically update `.semantic-index.pending.json` on failure and retry pending IDs on search/export/index. Add concurrent-refresh and process-death tests proving atomic old-or-new results, orphan removal, migration/relocation replacement, and crash retry without duplicates.

- [ ] **Step 6: Run and commit**

Run: `python3 -m pytest tests/test_export_html.py tests/test_research_bible_html.py tests/test_search_wrapper.py tests/test_search_layouts.py -q`

Expected: PASS, including jimemo and built-in HTML tests.

Commit: `git add scripts/export.py scripts/research_bible_html.py scripts/search.py vendor/semantic_search/search.py tests/test_export_html.py tests/test_research_bible_html.py tests/test_search_wrapper.py tests/test_search_layouts.py tests/test_semantic_engine_logical_ids.py && git commit -m "Export and index versioned research runs"`

---

### Task 7: Project-local batch preparation and explicit collision modes

**Files:**
- Modify: `scripts/batch_research.py`
- Modify: `tests/test_batch_research.py`

**Interfaces:**
- Consumes: `prepare_run()`, `slugify_v1()`, `validate_completion()`.
- Produces: project/default and direct-library CLI flags, per-row/global modes, deferred logs, layout-aware worker completion.

- [ ] **Step 1: Rewrite preparation tests around pre-write plans**

```python
def test_batch_defaults_to_project_research_and_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = batch_research.prepare_jobs(["Question"], project_dir=tmp_path, dry_run=True)
    assert plan[0].run_dir.parent == tmp_path / "research"
    assert list(tmp_path.iterdir()) == []

def test_per_row_mode_overrides_global_mode(tmp_path):
    seed_incomplete(tmp_path / "research/topic")
    jobs = batch_research.prepare_jobs(
        ["topic\textend\tNew question"], project_dir=tmp_path, mode="fresh"
    )
    assert jobs[0].mode == "extend"
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_batch_research.py -q`

Expected: FAIL against old eager-directory behavior.

- [ ] **Step 3: Implement planning-first batch semantics**

Extend input grammar to `slug<TAB>mode<TAB>question`, while retaining plain question and historical `slug<TAB>question`. Add `--project-dir` defaulting to captured CWD, `--library-dir`, legacy `--output-root`, `--mode`, and `--new-question`. Resolve all jobs and collision outcomes before creating `_batch`; only execution creates logs and invokes workers. Each prepared job carries lease/broker/scratch data. Codex workers run with workspace-write rooted at the isolated scratch directory, so the managed run is readable but not writable; Claude containment mounts skill and run read-only and only scratch writable. Prompts require `publish-artifact` for every generated Round 2/3/4/section/Bible artifact, followed by `record-stage/finalize`. Add invocation tests proving CWD/writable roots are scratch rather than the run.

Add an end-to-end contained-worker fixture whose direct `run_dir/write.txt` attempt is denied, then whose broker requests run scope, a deterministic offline retrieval-helper fixture, scratch artifact publication, export, stage recording, and finalization successfully. Assert all managed bytes were written by the broker PID, the worker had no writable run mount, and the resulting seal validates.

- [ ] **Step 4: Replace filename heuristics with state validation**

Worker success is return code zero plus `validate_completion(run).ok`; a completed resume is success/no-op. Batch workers enqueue semantic-index reconciliation, and the coordinator performs one locked refresh after all workers exit. Preserve capacity backoff and credential scrubbing unchanged.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest tests/test_batch_research.py tests/test_dispatch_mode.py -q`

Expected: PASS.

Commit: `git add scripts/batch_research.py tests/test_batch_research.py && git commit -m "Make batch runs project-local and collision-aware"`

---

### Task 8: Immutable parent snapshots and first-class extension

**Files:**
- Create: `scripts/run_extension.py`
- Create: `tests/test_run_extension.py`
- Modify: `scripts/run_manager.py`

**Interfaces:**
- Consumes: layout, `PATH_SCHEMAS`, `RootedFS`, leases, `ImmutableRegistry`, inventories, state, journals.
- Produces: `ExtensionPlan`, `prepare_extension()`, `recover_extension()`, CLI `extend` through `prepare --mode extend`.

- [ ] **Step 1: Write complete, partial, legacy, and unsafe-parent RED tests**

```python
def test_complete_parent_extension_copies_full_recursive_provenance(sealed_parent):
    child = prepare_extension(sealed_parent, "What changes under condition X?")
    assert (child / "Process/Inherited" / sealed_parent.name / "snapshot/snapshot.json").exists()
    assert (child / "Process/round1/inherited_corpus.jsonl").exists()
    assert read_json(child / "Process/lineage.json")["parent_run_id"] == sealed_parent.run_id
    assert TreeInventory.capture(sealed_parent.path) == sealed_parent.before

def test_partial_parent_freeze_and_child_commit_are_one_recoverable_outcome(partial_parent):
    crash_extension(partial_parent, at="after-parent-freeze-intent")
    recover_extension(partial_parent.library)
    assert (parent_is_incomplete_without_child(partial_parent) or
            parent_is_frozen_with_committed_child(partial_parent))

def test_inheritance_never_uses_ordinary_hardlinks(sealed_parent):
    child = prepare_extension(sealed_parent, "Q2")
    assert os.stat(first_source(sealed_parent)).st_ino != os.stat(first_source(child)).st_ino
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_run_extension.py -q`

Expected: FAIL because the extension module is absent.

- [ ] **Step 3: Implement eligibility and archival snapshots**

Validate sealed v2, frozen v2, gate-passed inactive partial v2, completed legacy, and gate-passed partial legacy. Build synthetic legacy metadata without changing the legacy tree. Copy the full available process/provenance tree plus every registered referenced target into an archival root with its own declared base. Copy extracted bytes; optionally use a verified reflink implementation, never `os.link`. Rebase only active child rows to `Sources/Extracted/` and record full ancestry.

- [ ] **Step 4: Implement two-sided freeze/lineage journaling and cleanup**

Hold the parent lease throughout partial inheritance. Commit the frozen parent record and sealed child ancestry together through Task 2's `ImmutableRegistry`. For failure remove only unchanged manifest-created paths; untracked/changed child entries block cleanup. Resolve legacy identities by canonical relative path, filesystem identity, and portable collision key before hashing. Add kill points at every registry/parent/child journal boundary, including original path, rename, case/Unicode alias, and filesystem-identity alias access through every legacy-aware writer.

- [ ] **Step 5: Assert the new Bible is never treated as inherited evidence**

Test that inherited corpus rows enumerate only source rows/files, lineage marks the prior Bible `orientation_only`, evidence counts exclude it, Round-1 output starts with inherited rows plus new retrieval, and completion requires fresh integration/adversary/export generations.

- [ ] **Step 6: Run and commit**

Run: `python3 -m pytest tests/test_run_extension.py tests/test_run_manager.py -q`

Expected: PASS.

Commit: `git add scripts/run_extension.py scripts/run_manager.py tests/test_run_extension.py tests/test_run_manager.py && git commit -m "Add immutable research run extension"`

---

### Task 9: Deterministic migration planning and schema-aware path rewriting

**Files:**
- Create: `scripts/run_migration.py`
- Create: `tests/test_run_migration_plan.py`
- Modify: `scripts/run_manager.py`

**Interfaces:**
- Consumes: layout, `PATH_SCHEMAS`, `RootedFS`, state, leases, transactions.
- Produces: `MigrationTarget`, `MoveOp`, `RewriteOp`, `MigrationPlan`, `discover_targets()`, `plan_migration()`, CLI `migrate --dry-run`.

- [ ] **Step 1: Copy real legacy shapes into fixtures and write RED planning tests**

Use minimal copies modeled on `research/persona-construction` and `research/western-philosophy-of-mind`, including `export/bibliography.md`, `*-BIBLE.md`, `round1/sources`, claims `file`, slice `text_path/raw_path`, and root-prefixed references.

```python
def test_plan_uses_ordered_nonoverlapping_mapping(persona_fixture):
    plan = plan_migration(persona_fixture, persona_fixture.parent)
    assert plan.dest("export/bibliography.md") == "Sources/bibliography.md"
    assert plan.dest("round1/sources/a.txt") == "Sources/Extracted/a.txt"
    assert plan.dest("round1/slice_publication.jsonl") == "Process/round1/slice_publication.jsonl"

def test_dry_run_rewrites_all_registered_paths_without_writes(western_fixture):
    before = TreeInventory.capture(western_fixture)
    plan = plan_migration(western_fixture, western_fixture.parent)
    assert plan.preview_rewrite("text_path", "sources/a.txt") == "Sources/Extracted/a.txt"
    assert TreeInventory.capture(western_fixture) == before
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_run_migration_plan.py -q`

Expected: FAIL because migration planning is absent.

- [ ] **Step 3: Implement signature-based target discovery and deduplication**

Classify explicit run, literal `research` library, declared direct library, and project. Project discovery covers `project/research/*` plus strong-signature direct children. Canonicalize filesystem identity, real path, ancestry, and collision keys; deduplicate exact identity but reject overlapping/aliased targets. Ambiguous explicit external runs require destination context.

- [ ] **Step 4: Implement the exact ordered mapping and Bible selection**

Apply specific bibliography/claims/dedup/source mappings before general round/section rules. Preserve recognized topic-qualified Bible basenames; normalize only generic `RESEARCH-BIBLE.md`; pair/regenerate HTML. Put unknown safe opaque artifacts under `Process/Legacy/<original-relative-path>`; reject unregistered path-like JSON fields.

- [ ] **Step 5: Implement schema-aware reference rewrites**

Cover claims `file`, slice/deepening `text_path` and `raw_path`, historical `run_dir`, evidence/fulltext manifests, coverage/verification/export inventories, and lineage/stage paths. Preview rewritten documents in memory, parse them, and verify every referenced destination before the plan is valid.

- [ ] **Step 6: Run and commit**

Run: `python3 -m pytest tests/test_run_migration_plan.py -q`

Expected: PASS; `--dry-run` leaves the fixture byte-identical.

Commit: `git add scripts/run_migration.py scripts/run_manager.py tests/test_run_migration_plan.py && git commit -m "Plan legacy research migrations safely"`

---

### Task 10: Migration apply, process-death recovery, and exact rollback

**Files:**
- Modify: `scripts/run_migration.py`
- Create: `tests/test_run_migration_recovery.py`
- Modify: `scripts/run_manager.py`

**Interfaces:**
- Consumes: `MigrationPlan`, `RootedFS`, `Journal`, leases, compound inventories, v2 metadata/sealing.
- Produces: `apply_migration()`, `recover_migration()`, `rollback_migration()`, CLI `migrate`, `recover --continue|--abort`, `rollback`.

- [ ] **Step 1: Write apply/rollback and no-overwrite RED tests**

```python
def test_apply_and_rollback_preserve_complete_legacy_tree(legacy_fixture):
    before = TreeInventory.capture(legacy_fixture)
    migrated = apply_migration(plan_migration(legacy_fixture, legacy_fixture.parent))
    assert RunLayout.open(migrated).kind is LayoutKind.V2
    rollback_migration(migrated)
    assert TreeInventory.capture(legacy_fixture) == before

def test_rollback_refuses_any_unrecorded_or_changed_entry(migrated_fixture):
    (migrated_fixture / "Process/round1/unrecorded.txt").write_text("x")
    with pytest.raises(RollbackConflict, match="unrecorded"):
        rollback_migration(migrated_fixture)

def test_direct_project_child_relocates_and_rolls_back_across_both_parents(project_fixture):
    source = project_fixture / "legacy-topic"
    before = TreeInventory.capture(source)
    migrated = apply_migration(plan_migration(source, project_fixture))
    assert migrated == project_fixture / "research/legacy-topic"
    rollback_migration(migrated)
    assert TreeInventory.capture(source) == before
    assert not (project_fixture / "research/legacy-topic").exists()

def test_rollback_uses_embedded_inverse_when_external_segment_was_retained_then_removed(migrated_fixture):
    external_segment(migrated_fixture).unlink()
    rollback_migration(migrated_fixture)
    assert exact_legacy_tree_restored(migrated_fixture)

def test_multi_target_late_failure_preserves_first_committed_segment(batch_fixture):
    crash_batch(batch_fixture, after="first-committed-second-intent")
    recover_batch(batch_fixture, mode="continue")
    assert all(run_is_valid_terminal(run) for run in batch_fixture.runs)
    assert first_run_migration_segment_digest_still_matches(batch_fixture)
```

- [ ] **Step 2: Run RED tests**

Run: `python3 -m pytest tests/test_run_migration_recovery.py -q`

Expected: FAIL because apply/recovery is absent.

- [ ] **Step 3: Implement per-run WAL segments and compound post-state commit**

Acquire all run locks in canonical order, revalidate the entire batch, then create coordinator and per-run journals below `.transactions/`. For each op write intent, fsync, perform no-overwrite move/write, fsync parents, verify, then record completion. Store deterministic migrated metadata, completion profile, embedded inverse plan, full pre/post compound state, and immutable per-run journal digest in `Process/migration.json`.

- [ ] **Step 4: Implement idempotent continue/abort and journaled rollback**

`recover --continue` replays incomplete intents; `--abort` inverses completed intents. Both verify final original or v2 compound state and can be repeated after another crash. Normal opens fail while the active marker exists. Rollback validates every current entry and canonical manifest projection before starting its own recoverable inverse transaction.

- [ ] **Step 5: Add exhaustive subprocess-death matrix**

Generate the journal boundary names from production code and parameterize worker death at every boundary during apply, continue, abort, and rollback. Run the entire matrix for in-place library migration and direct-project-child relocation, covering both source and destination parent-directory fsync boundaries. Add multi-target coordinator cases in which run 1 is ready/committed and run 2 fails or dies; repeated continue/abort must preserve the immutable first segment and land every run in a valid terminal state. Delete a retained external per-run segment after commit and prove rollback succeeds from the embedded inverse/compound state. Assert no mixed layout, overwrite, lost bytes, duplicate operation, or dangling active marker.

- [ ] **Step 6: Run and commit**

Run: `python3 -m pytest tests/test_run_migration_plan.py tests/test_run_migration_recovery.py -q`

Expected: PASS.

Commit: `git add scripts/run_migration.py scripts/run_manager.py tests/test_run_migration_recovery.py && git commit -m "Add recoverable research run migration"`

---

### Task 11: Skill orchestration, documentation, compatibility sweep, and rehearsal

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `scripts/watched.py`
- Create: `tests/test_skill_run_layout_contract.py`
- Create: `tests/rehearse_run_migration.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: user-facing default workflow, explicit migration recipe, deterministic temporary rehearsal.

- [ ] **Step 1: Write documentation contract tests before editing prose**

```python
def test_skill_documents_project_local_v2_and_four_collision_choices():
    text = Path("SKILL.md").read_text()
    for token in ["<project>/research/<run-slug>", "Sections/", "Sources/Extracted/", "Process/", "Resume", "Extend", "Start fresh", "Cancel"]:
        assert token in text

def test_skill_export_invokes_managed_mode_and_finalize():
    text = Path("SKILL.md").read_text()
    assert "scripts/export.py --run-dir" in text
    assert "scripts/run_manager.py finalize" in text
```

- [ ] **Step 2: Run RED documentation tests**

Run: `python3 -m pytest tests/test_skill_run_layout_contract.py -q`

Expected: FAIL against legacy output documentation.

- [ ] **Step 3: Rewrite lifecycle and output documentation**

At invocation capture the launch project, call `run_manager prepare`, present the four choices only when required, and retain the returned run path, lease, broker, and scratch directory through every stage. Agent-authored synthesis, integration, sections, adversary fixes, and Bible Markdown are created only in scratch; the run is treated read-only. Publish each artifact through `run_manager publish-artifact`, then record its stage. Renew before long reasoning intervals. Export with managed `--run-dir`, finalize/seal/release, then reconcile the index. Document extension as immutable ancestry plus fresh gate/synthesis; document `migrate` as immediate default with `--dry-run`, `recover`, and `rollback`. Preserve a clearly labeled legacy/manual helper section and the direct-library meaning of `--output-root`.

Update `scripts/watched.py` command text to use `Sections/` and `Process/round4/` for v2 while retaining legacy examples in its compatibility branch.

- [ ] **Step 4: Implement the temporary two-fixture rehearsal**

`tests/rehearse_run_migration.py` copies the complete checked-in `persona-construction` and `western-philosophy-of-mind` legacy run trees into `tempfile.TemporaryDirectory()`, placing one as a direct project child and one under `research/`. It also derives a third temporary incomplete fixture by copying persona-construction and removing only its selected Bible before the baseline inventory. Record full source/publication hashes; dry-run all; migrate all; inject process death during relocation and recover. On the two migrated-complete fixtures, assert resume is success/no-op and export is refused/no-op with byte-identical sealed state. On the migrated-incomplete fixture, prepare resume, publish a Bible Markdown candidate from scratch through the broker, run managed export, and verify the resulting artifacts without claiming completion until fresh required stage manifests exist. Upgrade a completed publication only through extension while proving its parent unchanged; reconcile and query the index without duplicate logical IDs; roll one run back; and compare exact applicable original tree inventories and hashes. It refuses any mutation target within the repository's checked-in `research/` tree.

- [ ] **Step 5: Run compatibility and full verification**

Run:

```bash
python3 -m pytest tests/test_skill_run_layout_contract.py -q
python3 tests/rehearse_run_migration.py
python3 -m pytest tests/ -q
python3 -m compileall -q scripts
git diff --check
```

Expected: documentation contract PASS; rehearsal prints `REHEARSAL PASS`; entire suite PASS; compilation and diff checks exit zero.

- [ ] **Step 6: Commit the final integration**

Commit: `git add SKILL.md README.md scripts/watched.py tests/test_skill_run_layout_contract.py tests/rehearse_run_migration.py && git commit -m "Document project-local research run workflow"`

---

## Self-review checklist

- **Spec coverage:** Tasks 1–4 cover resolution, slugs, layout, state, locks, collision, resume, and completion; Tasks 5–7 cover all current helper/export/index/batch consumers; Task 8 covers complete/partial/legacy extension and immutability; Tasks 9–10 cover discovery, rewriting, journaling, recovery, and rollback; Task 11 covers user orchestration and rehearsal.
- **Placeholder scan:** The plan contains no `TBD`, `TODO`, “similar to,” deferred implementation, or unspecified error-handling step. Variadic tuple annotations are type syntax, not omitted work.
- **Type consistency:** `RunLayout`, `ResolvedRoot`, `RunLock`, `TreeInventory`, `Journal`, `RunState`, `ResumePlan`, and `MigrationPlan` names are introduced once and consumed consistently by later tasks.
- **Safety:** No task mutates checked-in research fixtures; every destructive cleanup is manifest-limited; migration and extension kill-point tests precede release.
- **Ship gate:** After implementation, obtain an Agency-composed fresh-eyes code review against the approved spec, this plan, the complete diff, and fresh test/rehearsal evidence before merge/push.
