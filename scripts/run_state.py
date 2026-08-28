"""Run metadata, stage DAGs, completion profiles, freezing, and compound seals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import uuid

from scripts.run_fs import RootedFS, UnsafePathError
from scripts.run_layout import LayoutError, LayoutKind, RunLayout, safe_relpath
from scripts.run_path_schema import PATH_SCHEMAS, PathSchemaError
from scripts.run_transactions import ImmutableRecord, ImmutableRegistry, Journal, RunLease, TreeInventory


METADATA_SCHEMA_VERSION = 1
STAGE_SCHEMA_VERSION = 1
SEAL_SCHEMA_VERSION = 1
LEGACY_RUN_NAMESPACE = uuid.UUID("07d71d10-f985-52a6-81a5-2758da7da70b")
STAGE_ORDER = (
    "round0",
    "round1",
    "evidence_gate",
    "round2",
    "round2_5",
    "round3",
    "integration",
    "citation_verifier",
    "adversary",
    "export",
)
REQUIRED_NATIVE_STAGES = (
    "evidence_gate",
    "integration",
    "citation_verifier",
    "adversary",
    "export",
)


class LifecycleError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _layout(value: os.PathLike[str] | str | RunLayout) -> RunLayout:
    return value if isinstance(value, RunLayout) else RunLayout.open(value)


@dataclass(frozen=True)
class RunMetadata:
    layout_version: int
    schema_version: int
    run_id: str
    slug: str
    question: str
    question_source: str
    status: str
    completion_profile: str
    sealed: bool
    frozen_for_derivation: bool
    frozen_snapshot: str | None
    generation: int
    created_at: str
    updated_at: str
    completed_at: str | None
    bible: Mapping[str, Any] | None
    extras: Mapping[str, Any]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunMetadata":
        required = {
            "layout_version",
            "schema_version",
            "slug",
            "status",
            "sealed",
            "frozen_for_derivation",
            "generation",
        }
        missing = required - set(payload)
        if missing:
            raise LifecycleError(f"run metadata is missing {sorted(missing)}")
        known = {
            "layout_version",
            "schema_version",
            "run_id",
            "slug",
            "question",
            "question_source",
            "status",
            "completion_profile",
            "sealed",
            "frozen_for_derivation",
            "frozen_snapshot",
            "generation",
            "created_at",
            "updated_at",
            "completed_at",
            "bible",
        }
        return cls(
            int(payload["layout_version"]),
            int(payload["schema_version"]),
            str(payload.get("run_id") or ""),
            str(payload["slug"]),
            str(payload.get("question") or ""),
            str(payload.get("question_source") or "unknown"),
            str(payload["status"]),
            str(payload.get("completion_profile") or "native-v2"),
            bool(payload["sealed"]),
            bool(payload["frozen_for_derivation"]),
            str(payload["frozen_snapshot"]) if payload.get("frozen_snapshot") is not None else None,
            int(payload["generation"]),
            str(payload.get("created_at") or ""),
            str(payload.get("updated_at") or ""),
            str(payload["completed_at"]) if payload.get("completed_at") is not None else None,
            payload.get("bible") if isinstance(payload.get("bible"), Mapping) else None,
            {key: value for key, value in payload.items() if key not in known},
        )

    @classmethod
    def load(cls, run: os.PathLike[str] | str | RunLayout) -> "RunMetadata":
        layout = _layout(run)
        if layout.kind is not LayoutKind.V2:
            raise LifecycleError("native metadata is available only for v2 runs")
        try:
            payload = json.loads(layout.metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleError("run metadata is corrupt") from exc
        if not isinstance(payload, dict):
            raise LifecycleError("run metadata must be an object")
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "layout_version": self.layout_version,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "slug": self.slug,
            "question": self.question,
            "question_source": self.question_source,
            "status": self.status,
            "completion_profile": self.completion_profile,
            "sealed": self.sealed,
            "frozen_for_derivation": self.frozen_for_derivation,
            "frozen_snapshot": self.frozen_snapshot,
            "generation": self.generation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "bible": dict(self.bible) if self.bible is not None else None,
        }
        payload.update(self.extras)
        return payload


@dataclass(frozen=True)
class ArtifactDigest:
    path: str
    kind: str
    sha256: str
    size: int


@dataclass(frozen=True)
class StageManifest:
    schema_version: int
    stage: str
    generation: int
    tool: str
    config_fingerprint: str
    provider_identity: str | None
    dependencies: Mapping[str, int]
    inputs: tuple[ArtifactDigest, ...]
    outputs: tuple[ArtifactDigest, ...]
    started_at: str
    completed_at: str
    result: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageManifest":
        try:
            return cls(
                int(payload["schema_version"]),
                str(payload["stage"]),
                int(payload["generation"]),
                str(payload["tool"]),
                str(payload.get("config_fingerprint") or ""),
                str(payload["provider_identity"]) if payload.get("provider_identity") is not None else None,
                {str(key): int(value) for key, value in dict(payload.get("dependencies", {})).items()},
                tuple(ArtifactDigest(**item) for item in payload.get("inputs", [])),
                tuple(ArtifactDigest(**item) for item in payload.get("outputs", [])),
                str(payload["started_at"]),
                str(payload["completed_at"]),
                str(payload["result"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LifecycleError("stage manifest is corrupt") from exc

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["inputs"] = [asdict(item) for item in self.inputs]
        payload["outputs"] = [asdict(item) for item in self.outputs]
        payload["dependencies"] = dict(self.dependencies)
        return payload


@dataclass(frozen=True)
class ResumePlan:
    restart_stage: str | None
    reusable_outputs: tuple[str, ...]
    invalid_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunState:
    layout: RunLayout
    metadata: RunMetadata

    @classmethod
    def open(cls, run: os.PathLike[str] | str | RunLayout) -> "RunState":
        layout = _layout(run)
        return cls(layout, RunMetadata.load(layout))


def _artifact(layout: RunLayout, relative: str) -> ArtifactDigest:
    parsed = safe_relpath(relative)
    target = layout.run_root.joinpath(*parsed.parts)
    if target.is_file():
        data = target.read_bytes()
        return ArtifactDigest(parsed.as_posix(), "file", hashlib.sha256(data).hexdigest(), len(data))
    if target.is_dir():
        inventory = TreeInventory.capture(target)
        return ArtifactDigest(parsed.as_posix(), "directory", inventory.root_digest, len(inventory.entries))
    raise LifecycleError(f"stage artifact is missing or unsafe: {parsed}")


def _manifest_path(layout: RunLayout, stage: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", stage):
        raise LifecycleError(f"unsafe stage identifier: {stage!r}")
    return layout.stage_manifests / f"{stage}.json"


def _load_manifest(layout: RunLayout, stage: str) -> StageManifest:
    path = _manifest_path(layout, stage)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"stage manifest {stage!r} is absent or corrupt") from exc
    if not isinstance(payload, dict):
        raise LifecycleError(f"stage manifest {stage!r} must be an object")
    manifest = StageManifest.from_dict(payload)
    if manifest.schema_version != STAGE_SCHEMA_VERSION or manifest.stage != stage:
        raise LifecycleError(f"stage manifest {stage!r} has the wrong identity or schema")
    return manifest


def record_stage(
    run: os.PathLike[str] | str | RunLayout,
    stage: str,
    *,
    inputs: Sequence[str],
    outputs: Sequence[str],
    tool: str,
    dependencies: Sequence[str] = (),
    config_fingerprint: str = "",
    provider_identity: str | None = None,
    started_at: str | None = None,
    result: str = "success",
) -> StageManifest:
    layout = _layout(run)
    if layout.kind is not LayoutKind.V2:
        raise LifecycleError("stage manifests can be written only to native v2 runs")
    metadata = RunMetadata.load(layout)
    if metadata.sealed or metadata.frozen_for_derivation or metadata.status in {"complete", "frozen"}:
        raise LifecycleError("sealed or frozen runs cannot record stages")
    if result != "success":
        raise LifecycleError("record_stage records only successful stages")
    dependency_generations: dict[str, int] = {}
    for dependency in dependencies:
        dependency_generations[dependency] = _load_manifest(layout, dependency).generation
    generation = metadata.generation + 1
    manifest = StageManifest(
        STAGE_SCHEMA_VERSION,
        stage,
        generation,
        tool,
        config_fingerprint,
        provider_identity,
        dependency_generations,
        tuple(_artifact(layout, path) for path in inputs),
        tuple(_artifact(layout, path) for path in outputs),
        started_at or _now(),
        _now(),
        result,
    )
    fs = RootedFS(layout.run_root)
    transaction_id = str(uuid.uuid4())
    journal = Journal(
        layout.run_root.parent / ".transactions" / transaction_id / "journal.jsonl",
        transaction_id,
        metadata.run_id,
    )
    journal.append("intent", {"operation": "record-stage", "stage": stage, "generation": generation})
    fs.atomic_write_json(
        f"Process/stages/{stage}.json",
        manifest.to_dict(),
        create_parents=True,
    )
    updated = replace(metadata, generation=generation, updated_at=_now())
    fs.atomic_write_json("Process/run.json", updated.to_dict())
    journal.append("complete", {"operation": "record-stage", "stage": stage, "generation": generation})
    return manifest


def _artifact_is_current(layout: RunLayout, artifact: ArtifactDigest) -> bool:
    try:
        return _artifact(layout, artifact.path) == artifact
    except (LifecycleError, OSError, UnsafePathError):
        return False


def _manifest_errors(
    layout: RunLayout,
    manifest: StageManifest,
    *,
    config_fingerprint: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if manifest.result != "success":
        errors.append(f"{manifest.stage}: result is not success")
    if config_fingerprint is not None and manifest.config_fingerprint != config_fingerprint:
        errors.append(f"{manifest.stage}: config fingerprint changed")
    for artifact in manifest.inputs:
        if not _artifact_is_current(layout, artifact):
            errors.append(f"{manifest.stage}: input {artifact.path} changed")
    for artifact in manifest.outputs:
        if not _artifact_is_current(layout, artifact):
            errors.append(f"{manifest.stage}: output {artifact.path} changed")
    for dependency, generation in manifest.dependencies.items():
        try:
            current = _load_manifest(layout, dependency)
        except LifecycleError:
            errors.append(f"{manifest.stage}: dependency {dependency} is missing")
            continue
        if current.generation != generation:
            errors.append(f"{manifest.stage}: dependency {dependency} generation changed")
    return errors


def resume_plan(
    run: os.PathLike[str] | str | RunLayout,
    *,
    config_fingerprint: str | None = None,
) -> ResumePlan:
    layout = _layout(run)
    if layout.kind is LayoutKind.LEGACY:
        return ResumePlan("round0", (), ("legacy stages have no verifiable manifests",))
    metadata = RunMetadata.load(layout)
    if metadata.sealed or metadata.frozen_for_derivation:
        return ResumePlan(None, (), ("run is terminal and cannot be resumed",))
    reusable: list[str] = []
    reasons: list[str] = []
    restart: str | None = None
    for stage in STAGE_ORDER:
        path = _manifest_path(layout, stage)
        if not path.is_file():
            restart = stage
            reasons.append(f"{stage}: manifest is missing")
            break
        try:
            manifest = _load_manifest(layout, stage)
            stage_errors = _manifest_errors(layout, manifest, config_fingerprint=config_fingerprint)
        except LifecycleError as exc:
            stage_errors = [str(exc)]
            manifest = None
        if stage_errors:
            restart = stage
            reasons.extend(stage_errors)
            break
        assert manifest is not None
        reusable.extend(artifact.path for artifact in manifest.outputs)
    return ResumePlan(restart, tuple(reusable), tuple(reasons))


def _valid_nonempty_text(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


def _validate_claims(layout: RunLayout) -> tuple[bool, str | None]:
    if not layout.claims.is_file() or layout.claims.stat().st_size == 0:
        return False, "claims file is missing or empty"
    count = 0
    try:
        for line_number, line in enumerate(layout.claims.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("claim row must be an object")
            PATH_SCHEMAS.validate_document(layout, "Sources/claims.jsonl", row)
            target = PATH_SCHEMAS.resolve(layout, "Sources/claims.jsonl", "file", row["file"])
            assert target is not None
            relative = target.relative_to(layout.run_root)
            if relative.parts[0] != "Sections" and len(relative.parts) != 1:
                raise ValueError("claim target must be a section or root publication")
            if not target.is_file():
                raise ValueError("claim target is missing")
            count += 1
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError, PathSchemaError) as exc:
        return False, f"invalid claims: {exc}"
    return (count > 0, None if count > 0 else "claims file has no rows")


def _validate_stage_dag(layout: RunLayout, metadata: RunMetadata) -> list[str]:
    errors: list[str] = []
    manifests: dict[str, StageManifest] = {}
    if not layout.stage_manifests.is_dir():
        return ["stage manifest directory is missing"]
    for path in sorted(layout.stage_manifests.glob("*.json")):
        try:
            manifest = _load_manifest(layout, path.stem)
        except LifecycleError as exc:
            errors.append(str(exc))
            continue
        if manifest.generation > metadata.generation:
            errors.append(f"{manifest.stage}: generation exceeds run metadata")
        manifests[manifest.stage] = manifest
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage: str) -> None:
        if stage in visited:
            return
        if stage in visiting:
            errors.append(f"stage dependency cycle contains {stage}")
            return
        visiting.add(stage)
        manifest = manifests.get(stage)
        if manifest is not None:
            for dependency, generation in manifest.dependencies.items():
                dependency_manifest = manifests.get(dependency)
                if dependency_manifest is None:
                    errors.append(f"{stage}: dependency {dependency} is absent")
                else:
                    if dependency_manifest.generation != generation:
                        errors.append(f"{stage}: dependency {dependency} generation is stale")
                    if dependency_manifest.generation >= manifest.generation:
                        errors.append(f"{stage}: dependency {dependency} is not upstream")
                    visit(dependency)
        visiting.remove(stage)
        visited.add(stage)

    for stage in manifests:
        visit(stage)
    required_ancestor = {
        "evidence_gate": "round1",
        "integration": "evidence_gate",
        "citation_verifier": "integration",
        "adversary": "citation_verifier",
        "export": "adversary",
    }

    def has_ancestor(stage: str, ancestor: str, seen: set[str] | None = None) -> bool:
        if stage == ancestor:
            return True
        seen = set() if seen is None else seen
        if stage in seen or stage not in manifests:
            return False
        seen.add(stage)
        return any(has_ancestor(dependency, ancestor, seen) for dependency in manifests[stage].dependencies)

    for stage, ancestor in required_ancestor.items():
        if stage in manifests and not has_ancestor(stage, ancestor):
            errors.append(f"{stage}: required upstream stage {ancestor} is not in its dependency DAG")
    return errors


def _validate_migrated_v2_completion(layout: RunLayout, metadata: RunMetadata) -> ValidationResult:
    missing: list[str] = []
    errors: list[str] = []
    try:
        selected = layout.discover_bible()
        source = selected.source_markdown or selected.markdown
        bible_path = layout.run_root / source
        text = bible_path.read_text(encoding="utf-8")
        if not text.strip() or re.search(r"(?m)^#\s+\S", text) is None:
            missing.append("bible_h1")
        if metadata.bible and metadata.bible.get("markdown_sha256"):
            if metadata.bible["markdown_sha256"] != hashlib.sha256(bible_path.read_bytes()).hexdigest():
                errors.append("migrated Bible digest does not match metadata")
    except (LayoutError, OSError, UnicodeError, ValueError) as exc:
        missing.append("bible_markdown")
        errors.append(str(exc))
    if layout.claims.exists():
        claims_ok, claims_error = _validate_claims(layout)
        if not claims_ok:
            missing.append("claims")
            if claims_error:
                errors.append(claims_error)
    return ValidationResult(not missing and not errors, tuple(dict.fromkeys(missing)), tuple(errors))


def validate_completion(run: os.PathLike[str] | str | RunLayout) -> ValidationResult:
    layout = _layout(run)
    if layout.kind is not LayoutKind.V2:
        return ValidationResult(False, ("native-v2",), ("native completion requires a v2 run",))
    missing: list[str] = []
    errors: list[str] = []
    metadata = RunMetadata.load(layout)
    if metadata.completion_profile == "migrated-legacy-v1":
        return _validate_migrated_v2_completion(layout, metadata)
    if metadata.completion_profile != "native-v2":
        return ValidationResult(False, ("completion_profile",), ("unsupported completion profile",))
    bible = metadata.bible
    markdown_path: Path | None = None
    html_path: Path | None = None
    if not bible or not isinstance(bible.get("markdown"), str):
        missing.append("bible_markdown")
    else:
        try:
            markdown_rel = safe_relpath(bible["markdown"])
            markdown_path = layout.run_root / markdown_rel
            if not _valid_nonempty_text(markdown_path):
                missing.append("bible_markdown")
            elif bible.get("markdown_sha256") != hashlib.sha256(markdown_path.read_bytes()).hexdigest():
                errors.append("Bible Markdown digest does not match metadata")
        except (ValueError, OSError) as exc:
            errors.append(f"invalid Bible Markdown: {exc}")
    if not bible or not isinstance(bible.get("html"), str):
        missing.append("bible_html")
    else:
        try:
            html_rel = safe_relpath(bible["html"])
            html_path = layout.run_root / html_rel
            if not _valid_nonempty_text(html_path) or "<html" not in html_path.read_text(encoding="utf-8").casefold():
                missing.append("bible_html")
            elif bible.get("html_sha256") != hashlib.sha256(html_path.read_bytes()).hexdigest():
                errors.append("Bible HTML digest does not match metadata")
        except (ValueError, OSError, UnicodeError) as exc:
            errors.append(f"invalid Bible HTML: {exc}")
    if markdown_path is not None and html_path is not None and markdown_path.stem != html_path.stem:
        errors.append("Bible Markdown and HTML stems differ")

    if not _valid_nonempty_text(layout.bibliography_md):
        missing.append("bibliography_md")
    if not _valid_nonempty_text(layout.bibliography_bib):
        missing.append("bibliography_bib")
    else:
        try:
            if "@" not in layout.bibliography_bib.read_text(encoding="utf-8"):
                errors.append("BibTeX bibliography has no entries")
        except (OSError, UnicodeError):
            errors.append("BibTeX bibliography is not valid UTF-8")
    claims_ok, claims_error = _validate_claims(layout)
    if not claims_ok:
        missing.append("claims")
        if claims_error:
            errors.append(claims_error)

    for stage in REQUIRED_NATIVE_STAGES:
        try:
            manifest = _load_manifest(layout, stage)
            stage_errors = _manifest_errors(layout, manifest)
        except LifecycleError as exc:
            missing.append(stage)
            errors.append(str(exc))
            continue
        if stage_errors:
            missing.append(stage)
            errors.extend(stage_errors)
    errors.extend(_validate_stage_dag(layout, metadata))
    return ValidationResult(not missing and not errors, tuple(dict.fromkeys(missing)), tuple(errors))


def validate_legacy_completion(run: os.PathLike[str] | str | RunLayout) -> ValidationResult:
    layout = _layout(run)
    if layout.kind is not LayoutKind.LEGACY:
        return ValidationResult(False, ("legacy",), ("historical completion requires a legacy run",))
    try:
        selected = layout.discover_bible()
        source = selected.source_markdown or selected.markdown
        text = (layout.run_root / source).read_text(encoding="utf-8")
    except (LayoutError, OSError, UnicodeError) as exc:
        return ValidationResult(False, ("bible_markdown",), (str(exc),))
    if not text.strip() or re.search(r"(?m)^#\s+\S", text) is None:
        return ValidationResult(False, ("bible_h1",), ("legacy Bible is empty or has no H1",))
    if layout.claims.exists():
        try:
            for line in layout.claims.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                PATH_SCHEMAS.validate_document(layout, "export/claims.jsonl", row)
                target = PATH_SCHEMAS.resolve(layout, "export/claims.jsonl", "file", row["file"])
                if target is None or not target.is_file():
                    raise ValueError("legacy claim target is missing")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError, PathSchemaError) as exc:
            return ValidationResult(False, ("claims",), (str(exc),))
    return ValidationResult(True)


def derive_legacy_metadata(
    run: os.PathLike[str] | str | RunLayout,
    *,
    transaction_time: str | None = None,
) -> dict[str, Any]:
    layout = _layout(run)
    if layout.kind is not LayoutKind.LEGACY:
        raise LifecycleError("synthetic legacy metadata requires a legacy run")
    inventory = TreeInventory.capture(layout.run_root)
    canonical_identity = f"{layout.run_root.resolve()}:{inventory.root_digest}"
    run_id = str(uuid.uuid5(LEGACY_RUN_NAMESPACE, canonical_identity))
    question = ""
    question_source = "slug"
    if layout.scope.is_file():
        try:
            scope = json.loads(layout.scope.read_text(encoding="utf-8"))
            for key in ("question", "research_question", "query", "topic"):
                if isinstance(scope, dict) and isinstance(scope.get(key), str) and scope[key].strip():
                    question = scope[key].strip()
                    question_source = f"scope.json:{key}"
                    break
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    bible_source: str | None = None
    if not question:
        try:
            selected = layout.discover_bible()
            bible_source = (selected.source_markdown or selected.markdown).as_posix()
            text = (layout.run_root / bible_source).read_text(encoding="utf-8")
            heading = re.search(r"(?m)^#\s+(.+?)\s*$", text)
            if heading:
                question = heading.group(1).strip()
                question_source = f"bible:{bible_source}"
        except (LayoutError, OSError, UnicodeError):
            pass
    if not question:
        question = layout.run_root.name
    mtimes = [entry.mtime_ns for entry in inventory.entries.values()]
    fallback = transaction_time or _now()
    created_at = (
        datetime.fromtimestamp(min(mtimes) / 1_000_000_000, timezone.utc).isoformat().replace("+00:00", "Z")
        if mtimes
        else fallback
    )
    updated_at = (
        datetime.fromtimestamp(max(mtimes) / 1_000_000_000, timezone.utc).isoformat().replace("+00:00", "Z")
        if mtimes
        else fallback
    )
    completed = validate_legacy_completion(layout).ok
    return {
        "layout_version": 1,
        "schema_version": METADATA_SCHEMA_VERSION,
        "run_id": run_id,
        "slug": layout.run_root.name,
        "question": question,
        "question_source": question_source,
        "status": "complete" if completed else "incomplete",
        "completion_profile": "migrated-legacy-v1" if completed else "native-v2",
        "sealed": completed,
        "frozen_for_derivation": False,
        "frozen_snapshot": None,
        "generation": 1,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": updated_at if completed else None,
        "bible_source": bible_source,
        "tree_root": inventory.root_digest,
    }


def seal_legacy_complete(
    run: os.PathLike[str] | str | RunLayout,
    *,
    library: os.PathLike[str] | str,
    journal: Journal,
) -> ImmutableRecord:
    layout = _layout(run)
    validation = validate_legacy_completion(layout)
    if not validation.ok:
        raise LifecycleError(f"legacy run does not meet its historical completion profile: {validation.errors}")
    synthetic = derive_legacy_metadata(layout)
    return ImmutableRegistry(library).record(
        layout.run_root,
        kind="legacy-complete-seal",
        expected_root=synthetic["tree_root"],
        run_id=synthetic["run_id"],
        derivation_transaction=journal.transaction_id,
        journal=journal,
    )


_ALLOWED_TRANSITIONS = {
    "new": {"incomplete"},
    "incomplete": {"complete", "failed", "frozen"},
    "failed": {"incomplete", "frozen"},
    "complete": {"complete"},
    "frozen": {"frozen"},
}


def transition_status(
    run: os.PathLike[str] | str | RunLayout,
    target: str,
    *,
    frozen_snapshot: str | None = None,
) -> RunMetadata:
    layout = _layout(run)
    metadata = RunMetadata.load(layout)
    if target not in _ALLOWED_TRANSITIONS.get(metadata.status, set()):
        terminal = metadata.status in {"complete", "frozen"}
        raise LifecycleError(
            f"{metadata.status!r} is terminal" if terminal else f"illegal lifecycle transition {metadata.status!r} -> {target!r}"
        )
    if target == "complete":
        raise LifecycleError("complete status may be entered only through seal_run")
    if target == "frozen" and not frozen_snapshot:
        raise LifecycleError("freezing requires a verified snapshot root")
    updated = replace(
        metadata,
        status=target,
        sealed=metadata.sealed or target == "complete",
        frozen_for_derivation=metadata.frozen_for_derivation or target == "frozen",
        frozen_snapshot=frozen_snapshot if target == "frozen" else metadata.frozen_snapshot,
        completed_at=_now() if target == "complete" else metadata.completed_at,
        updated_at=_now(),
        generation=metadata.generation + 1,
    )
    RootedFS(layout.run_root).atomic_write_json("Process/run.json", updated.to_dict())
    return updated


def make_state_guard(
    run: os.PathLike[str] | str | RunLayout,
    *,
    lease: RunLease | None = None,
    transaction_id: str | None = None,
):
    layout = _layout(run)

    def guard(operation: str, relative: str) -> None:
        if lease is not None:
            lease.verify(lease.owner.token)
        metadata = RunMetadata.load(layout)
        mutation = operation in {"write", "mkdir", "unlink", "rmdir", "rename", "copy"}
        if mutation and (metadata.sealed or metadata.frozen_for_derivation or metadata.status in {"complete", "frozen"}):
            raise UnsafePathError("run is sealed or frozen")
        active = metadata.extras.get("active_transaction")
        if mutation and active is not None and active != transaction_id:
            raise UnsafePathError("another transaction owns this run")

    return guard


def _ordinary_projection(run_root: Path, *, proposed_metadata: bytes | None = None) -> tuple[str, list[dict[str, Any]]]:
    inventory = TreeInventory.capture(run_root)
    projected: list[dict[str, Any]] = []
    integrity_paths = {"Process/seal.json", "Process/migration.json"}
    all_paths = set(inventory.entries)
    all_paths.add("Process/seal.json")
    for relative in sorted(all_paths):
        if relative.startswith((".locks/", ".transactions/", ".index/")):
            continue
        if relative in integrity_paths:
            if relative == "Process/migration.json" and relative not in inventory.entries:
                continue
            projected.append({"path": relative, "kind": "integrity-manifest"})
            continue
        entry = inventory.entries.get(relative)
        if entry is None:
            continue
        if entry.kind == "directory":
            members = set(entry.members)
            if relative == "Process":
                members.add("seal.json")
            projected.append({"path": relative, "kind": "directory", "mode": entry.mode, "members": sorted(members)})
            continue
        digest = entry.sha256
        size = entry.size
        if relative == "Process/run.json" and proposed_metadata is not None:
            digest = hashlib.sha256(proposed_metadata).hexdigest()
            size = len(proposed_metadata)
        projected.append({"path": relative, "kind": "file", "mode": entry.mode, "size": size, "sha256": digest})
    return _canonical_digest({"entries": projected}), projected


def _seal_payload(run_root: Path, proposed_metadata: bytes, run_id: str, generation: int) -> dict[str, Any]:
    root_digest, projection = _ordinary_projection(run_root, proposed_metadata=proposed_metadata)
    payload: dict[str, Any] = {
        "schema_version": SEAL_SCHEMA_VERSION,
        "run_id": run_id,
        "generation": generation,
        "ordinary_content_root": root_digest,
        "inventory": projection,
        "sealed_at": _now(),
    }
    payload["self_commitment"] = _canonical_digest(payload)
    return payload


def _raise_crash(transaction_id: str) -> None:
    error = SystemExit(91)
    error.transaction_id = transaction_id
    raise error


def seal_run(
    run: os.PathLike[str] | str | RunLayout,
    *,
    crash_at: str | None = None,
    lease: RunLease | None = None,
) -> dict[str, Any]:
    layout = _layout(run)
    if lease is not None:
        lease.verify(lease.owner.token)
    validation = validate_completion(layout)
    if not validation.ok:
        raise LifecycleError(f"run cannot be sealed: missing={validation.missing}, errors={validation.errors}")
    metadata = RunMetadata.load(layout)
    if metadata.frozen_for_derivation:
        raise LifecycleError("a frozen partial run cannot be completed in place")
    if metadata.sealed:
        seal = json.loads((layout.process / "seal.json").read_text(encoding="utf-8"))
        if not validate_seal(layout).ok:
            raise LifecycleError("existing seal is invalid")
        return seal
    transaction_id = str(uuid.uuid4())
    transaction_root = layout.run_root.parent / ".transactions" / transaction_id
    proposed = replace(
        metadata,
        status="complete",
        sealed=True,
        generation=metadata.generation + 1,
        completed_at=_now(),
        updated_at=_now(),
    )
    proposed_bytes = (json.dumps(proposed.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    seal = _seal_payload(layout.run_root, proposed_bytes, proposed.run_id, proposed.generation)
    transaction_root.mkdir(parents=True)
    plan = {
        "transaction_id": transaction_id,
        "run_id": metadata.run_id,
        "run_root": str(layout.run_root),
        "pre_metadata": metadata.to_dict(),
        "proposed_metadata": proposed.to_dict(),
        "seal": seal,
    }
    plan_path = transaction_root / "seal-plan.json"
    descriptor = os.open(plan_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        plan_bytes = (json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        os.write(descriptor, plan_bytes)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_descriptor = os.open(transaction_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    journal = Journal(transaction_root / "journal.jsonl", transaction_id, metadata.run_id)
    journal.append("intent", {"operation": "seal", "ordinary_content_root": seal["ordinary_content_root"]})
    if crash_at == "after-journal":
        _raise_crash(transaction_id)
    fs = RootedFS(layout.run_root)
    fs.atomic_write_json("Process/run.json", proposed.to_dict())
    if crash_at == "after-metadata":
        _raise_crash(transaction_id)
    fs.atomic_write_json("Process/seal.json", seal)
    if crash_at == "after-seal":
        _raise_crash(transaction_id)
    checked = validate_seal(layout)
    if not checked.ok:
        raise LifecycleError(f"compound seal validation failed: {checked.errors}")
    journal.append("complete", {"operation": "seal", "ordinary_content_root": seal["ordinary_content_root"]})
    return seal


def recover_seal(
    run: os.PathLike[str] | str | RunLayout,
    transaction_id: str,
    *,
    continue_seal: bool,
) -> None:
    layout = _layout(run)
    transaction_root = layout.run_root.parent / ".transactions" / transaction_id
    try:
        plan = json.loads((transaction_root / "seal-plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("seal recovery plan is unavailable") from exc
    if plan.get("run_root") != str(layout.run_root):
        raise LifecycleError("seal recovery target does not match its plan")
    journal = Journal.load(transaction_root / "journal.jsonl")
    fs = RootedFS(layout.run_root)
    if continue_seal:
        fs.atomic_write_json("Process/run.json", plan["proposed_metadata"])
        fs.atomic_write_json("Process/seal.json", plan["seal"])
        checked = validate_seal(layout)
        if not checked.ok:
            raise LifecycleError(f"recovered seal is invalid: {checked.errors}")
        journal.append("recovered", {"operation": "seal", "status": "complete"})
        return
    seal_path = layout.process / "seal.json"
    if seal_path.exists():
        current = json.loads(seal_path.read_text(encoding="utf-8"))
        if current != plan["seal"]:
            raise LifecycleError("seal changed; abort recovery refuses to remove it")
        seal_path.unlink()
    fs.atomic_write_json("Process/run.json", plan["pre_metadata"])
    journal.append("recovered", {"operation": "seal", "status": "aborted"})


def validate_seal(run: os.PathLike[str] | str | RunLayout) -> ValidationResult:
    layout = _layout(run)
    try:
        metadata = RunMetadata.load(layout)
        payload = json.loads((layout.process / "seal.json").read_text(encoding="utf-8"))
    except (LifecycleError, OSError, json.JSONDecodeError) as exc:
        return ValidationResult(False, ("seal",), (str(exc),))
    if not metadata.sealed or metadata.status != "complete":
        return ValidationResult(False, ("sealed_metadata",), ("metadata is not complete and sealed",))
    if not isinstance(payload, dict) or payload.get("schema_version") != SEAL_SCHEMA_VERSION:
        return ValidationResult(False, ("seal",), ("seal schema is invalid",))
    commitment = payload.get("self_commitment")
    unsigned = dict(payload)
    unsigned.pop("self_commitment", None)
    if commitment != _canonical_digest(unsigned):
        return ValidationResult(False, ("seal",), ("seal self-commitment is invalid",))
    actual_root, _ = _ordinary_projection(layout.run_root)
    if actual_root != payload.get("ordinary_content_root"):
        return ValidationResult(False, ("seal",), ("ordinary content root changed",))
    return ValidationResult(True)


__all__ = [
    "ArtifactDigest",
    "LifecycleError",
    "REQUIRED_NATIVE_STAGES",
    "ResumePlan",
    "RunMetadata",
    "RunState",
    "STAGE_ORDER",
    "StageManifest",
    "ValidationResult",
    "make_state_guard",
    "derive_legacy_metadata",
    "record_stage",
    "recover_seal",
    "resume_plan",
    "seal_run",
    "seal_legacy_complete",
    "transition_status",
    "validate_completion",
    "validate_legacy_completion",
    "validate_seal",
]
