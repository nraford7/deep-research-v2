"""Deterministic planning and crash-safe application of legacy run migrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Iterable
import uuid

from scripts.run_layout import LayoutError, LayoutKind, RunLayout, portable_collision_key, safe_relpath, slugify_v1
from scripts.run_transactions import LeaseConflict, RunLease, TreeInventory


class MigrationError(RuntimeError):
    pass


class RollbackConflict(MigrationError):
    pass


@dataclass(frozen=True)
class MigrationTarget:
    source: Path
    destination_library: Path
    destination: Path


@dataclass(frozen=True)
class MoveOp:
    source: str
    destination: str


@dataclass(frozen=True)
class RewriteOp:
    source: str
    destination: str
    content: bytes


@dataclass(frozen=True)
class MigrationPlan:
    target: MigrationTarget
    moves: tuple[MoveOp, ...]
    rewrites: tuple[RewriteOp, ...]
    source_root_digest: str

    def dest(self, source: str) -> str:
        source = safe_relpath(source).as_posix()
        for operation in self.moves:
            if operation.source == source:
                return operation.destination
        for operation in self.rewrites:
            if operation.source == source:
                return operation.destination
        raise KeyError(source)

    def preview_rewrite(self, field: str, value: str) -> str:
        return _rewrite_reference(field, value, {op.source: op.destination for op in self.moves})

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.target.source),
            "destination": str(self.target.destination),
            "source_root_digest": self.source_root_digest,
            "moves": [op.__dict__ for op in self.moves],
            "rewrites": [{"source": op.source, "destination": op.destination, "size": len(op.content)} for op in self.rewrites],
        }


_ROUND_RE = re.compile(r"^(round(?:1|2|2_5|3|4|5))(?:/(.*))?$")
_GENERIC_BIBLE = {"RESEARCH-BIBLE.md", "research-bible.md"}


def _destination_for(relative: str, slug: str) -> str:
    path = safe_relpath(relative).as_posix()
    name = PurePosixPath(path).name
    lower = path.casefold()
    if path in _GENERIC_BIBLE:
        return f"RESEARCH-BIBLE_{slug}.md"
    if name.casefold().endswith(("-bible.md", "-bible.html")) or "research-bible_" in name.casefold():
        return name
    if lower == "readme.md":
        return "README.md"
    if lower in {"bibliography.md", "export/bibliography.md", "sections/bibliography.md", "chapters/bibliography.md"}:
        return "Sources/bibliography.md"
    if lower in {"bibliography.bib", "export/bibliography.bib"}:
        return "Sources/bibliography.bib"
    if lower in {"claims.jsonl", "export/claims.jsonl"}:
        return "Sources/claims.jsonl"
    if lower.startswith("round1/sources/"):
        return "Sources/Extracted/" + path.split("/", 2)[2]
    if lower.startswith("sections/") or lower.startswith("chapters/"):
        return "Sections/" + path.split("/", 1)[1]
    match = _ROUND_RE.match(path)
    if match:
        suffix = f"/{match.group(2)}" if match.group(2) else ""
        return f"Process/{match.group(1)}{suffix}"
    if lower in {"scope.json", "retrieval_ledger.json"}:
        return "Process/" + name
    return "Process/Legacy/" + path


def _rewrite_reference(field: str, value: str, mapping: dict[str, str]) -> str:
    value = value.replace("\\", "/")
    if field in {"text_path", "raw_path"} and value.startswith("sources/"):
        return "Sources/Extracted/" + value.split("/", 1)[1]
    if field == "file" and value.startswith(("sections/", "chapters/")):
        return "Sections/" + value.split("/", 1)[1]
    stripped = value.removeprefix("./")
    if stripped in mapping:
        return mapping[stripped]
    return value


_PATH_FIELDS = {"text_path", "raw_path", "file", "run_dir", "path", "input", "output", "snapshot"}


def _rewrite_value(value: Any, mapping: dict[str, str], *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _rewrite_value(v, mapping, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(item, mapping, key=key) for item in value]
    if isinstance(value, str) and key in _PATH_FIELDS:
        return _rewrite_reference(key, value, mapping)
    return value


def _rewritten_bytes(path: Path, relative: str, mapping: dict[str, str]) -> bytes | None:
    suffix = path.suffix.casefold()
    if suffix not in {".json", ".jsonl"}:
        return None
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        payload = json.loads(text)
        rewritten = _rewrite_value(payload, mapping)
        return (json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n").encode()
    rows = []
    for line in text.splitlines():
        if line.strip():
            rows.append(_rewrite_value(json.loads(line), mapping))
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else "")).encode()


def plan_migration(source, destination_library=None) -> MigrationPlan:
    source = Path(source).resolve(strict=True)
    layout = RunLayout.open(source)
    if layout.kind is not LayoutKind.LEGACY:
        raise MigrationError("only recognized legacy runs can be migrated")
    library = Path(destination_library or source.parent).resolve(strict=False)
    destination = source if library == source.parent else library / slugify_v1(source.name)
    inventory = TreeInventory.capture(source)
    files = [entry.relative for entry in inventory.entries.values() if entry.kind == "file"]
    mapping = {relative: _destination_for(relative, slugify_v1(source.name)) for relative in files}
    if "export/bibliography.md" in mapping:
        for fallback in ("bibliography.md", "sections/bibliography.md", "chapters/bibliography.md"):
            if fallback in mapping:
                mapping[fallback] = "Process/Legacy/" + fallback
    reverse: dict[str, str] = {}
    for origin, target in mapping.items():
        key = portable_collision_key(target)
        if key in reverse:
            raise MigrationError(f"multiple legacy artifacts map to {target}: {reverse[key]}, {origin}")
        reverse[key] = origin
    moves: list[MoveOp] = []
    rewrites: list[RewriteOp] = []
    for relative in sorted(files):
        target = mapping[relative]
        try:
            content = _rewritten_bytes(source / relative, relative, mapping)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"cannot parse path-bearing document {relative}: {exc}") from exc
        if content is None:
            moves.append(MoveOp(relative, target))
        else:
            rewrites.append(RewriteOp(relative, target, content))
    return MigrationPlan(
        MigrationTarget(source, library, destination),
        tuple(moves),
        tuple(rewrites),
        inventory.root_digest,
    )


def discover_targets(path, *, destination_library=None) -> tuple[MigrationTarget, ...]:
    root = Path(path).resolve(strict=True)
    candidates: list[Path] = []
    try:
        if RunLayout.open(root).kind is LayoutKind.LEGACY:
            candidates = [root]
    except LayoutError:
        pass
    if not candidates:
        libraries = [root] if root.name == "research" else [root / "research", root]
        for library in libraries:
            if not library.is_dir():
                continue
            for child in library.iterdir():
                if not child.is_dir() or child.name.startswith("."):
                    continue
                try:
                    if RunLayout.open(child).kind is LayoutKind.LEGACY:
                        candidates.append(child)
                except LayoutError:
                    continue
    identities = set()
    targets = []
    library_override = Path(destination_library).resolve(strict=False) if destination_library else None
    for candidate in candidates:
        info = os.stat(candidate, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            continue
        identities.add(identity)
        library = library_override or candidate.parent
        destination = candidate if library == candidate.parent else library / slugify_v1(candidate.name)
        targets.append(MigrationTarget(candidate, library, destination))
    ordered = sorted(targets, key=lambda target: str(target.source).casefold())
    for index, target in enumerate(ordered):
        for other in ordered[index + 1:]:
            if target.source in other.source.parents or other.source in target.source.parents:
                raise MigrationError("overlapping migration targets are unsafe")
    return tuple(ordered)


def _copy_inventory(source: Path, destination: Path) -> None:
    inventory = TreeInventory.capture(source)
    destination.mkdir(parents=True, exist_ok=False)
    for entry in inventory.entries.values():
        target = destination / entry.relative
        origin = source / entry.relative
        if entry.kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target, follow_symlinks=False)


def _write_plan_tree(plan: MigrationPlan, stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=False)
    for operation in plan.moves:
        target = stage / operation.destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.target.source / operation.source, target, follow_symlinks=False)
    for operation in plan.rewrites:
        target = stage / operation.destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(operation.content)
    backup = stage / "Process" / "MigrationBackup" / "tree"
    _copy_inventory(plan.target.source, backup)


def _file_hashes(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    exclude = exclude or set()
    hashes = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in exclude:
                hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _acquire_migration_leases(source: Path, destination: Path, operation: str) -> list[RunLease]:
    requests = {(str(source.parent.resolve()), source.name), (str(destination.parent.resolve()), destination.name)}
    leases: list[RunLease] = []
    try:
        for library_value, key in sorted(requests):
            leases.append(RunLease.acquire(Path(library_value), key, operation=operation))
        return leases
    except LeaseConflict:
        for lease in reversed(leases):
            lease.release(lease.owner.token)
        raise


def _release_leases(leases: Iterable[RunLease]) -> None:
    for lease in reversed(list(leases)):
        lease.release(lease.owner.token)


def apply_migration(plan: MigrationPlan, *, crash_at: str | None = None) -> Path:
    """Apply one migration through a staged tree and recoverable rename boundary."""
    source = plan.target.source
    destination = plan.target.destination
    if TreeInventory.capture(source).root_digest != plan.source_root_digest:
        raise MigrationError("legacy source changed after planning")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination != source and destination.exists():
        raise MigrationError(f"migration destination already exists: {destination}")
    if os.stat(source).st_dev != os.stat(destination.parent).st_dev:
        raise MigrationError("cross-device migration is not supported safely")
    leases = _acquire_migration_leases(source, destination, "migrate")
    try:
        # A cooperating writer may have changed the source while the leases were
        # being acquired; the digest is authoritative only after both are held.
        if TreeInventory.capture(source).root_digest != plan.source_root_digest:
            raise MigrationError("legacy source changed while migration was acquiring its leases")
        transaction_id = str(uuid.uuid4())
        tx = destination.parent / ".transactions" / f"migration-{transaction_id}"
        stage = tx / "staged" / destination.name
        backup = tx / "legacy-backup" / source.name
        state_path = tx / "state.json"
        tx.mkdir(parents=True)
        source_parent = os.stat(source.parent, follow_symlinks=False)
        state = {
            "schema_version": 1, "transaction_id": transaction_id, "status": "intent",
            "source": str(source), "destination": str(destination), "stage": str(stage),
            "backup": str(backup), "source_root_digest": plan.source_root_digest,
            "source_parent_device": source_parent.st_dev,
            "source_parent_inode": source_parent.st_ino,
        }
        _atomic_json(state_path, state)
        _write_plan_tree(plan, stage)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        bible = next((op.destination for op in (*plan.moves, *plan.rewrites) if op.destination.casefold().endswith(".md") and "bible" in PurePosixPath(op.destination).name.casefold()), None)
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"deeper-research:{plan.source_root_digest}"))
        metadata = {
            "layout_version": 2, "schema_version": 1, "run_id": run_id,
            "slug": slugify_v1(destination.name), "slug_version": "slug-v1",
            "unicode_version": "15.0.0", "question": destination.name,
            "question_source": "legacy-migration", "status": "incomplete",
            "completion_profile": "migrated-legacy", "sealed": False,
            "frozen_for_derivation": False, "frozen_snapshot": None,
            "generation": 1, "created_at": now, "updated_at": now,
            "completed_at": None,
            "bible": ({"markdown": bible, "html": None} if bible else None),
        }
        _atomic_json(stage / "Process" / "run.json", metadata)
        expected = _file_hashes(stage)
        migration_payload = {
            "schema_version": 1, "transaction_id": transaction_id,
            "original_source": str(source), "destination": str(destination),
            "source_root_digest": plan.source_root_digest,
            "backup_tree": "Process/MigrationBackup/tree",
            "expected_files": expected,
            "moves": [op.__dict__ for op in plan.moves],
            "rewrites": [{"source": op.source, "destination": op.destination} for op in plan.rewrites],
        }
        migration_path = stage / "Process" / "migration.json"
        _atomic_json(migration_path, migration_payload)
        migration_digest = hashlib.sha256(migration_path.read_bytes()).hexdigest()
        _atomic_text(stage / "Process" / "migration.sha256", migration_digest + "\n")
        state["migration_sha256"] = migration_digest
        _atomic_json(state_path, state)
        if crash_at == "after-stage":
            raise RuntimeError("migration crash injection: after-stage")
        # Revalidate immediately before the first destructive rename.
        if TreeInventory.capture(source).root_digest != plan.source_root_digest:
            raise MigrationError("legacy source changed during migration staging")
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, backup)
        state["status"] = "source-backed-up"
        _atomic_json(state_path, state)
        if crash_at == "after-source-backup":
            raise RuntimeError("migration crash injection: after-source-backup")
        os.rename(stage, destination)
        state["status"] = "committed"
        _atomic_json(state_path, state)
        return destination
    finally:
        _release_leases(leases)


def _load_migration(migrated: Path) -> dict[str, Any]:
    try:
        migration_path = migrated / "Process" / "migration.json"
        raw = migration_path.read_bytes()
        payload = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        sidecar = (migrated / "Process" / "migration.sha256").read_text(encoding="ascii").strip()
        if sidecar != digest:
            raise RollbackConflict("migration metadata digest does not match its sidecar")
        transaction_id = payload.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise RollbackConflict("migration transaction ID is missing")
        state_path = migrated.parent / ".transactions" / f"migration-{transaction_id}" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (state.get("transaction_id") != transaction_id
                or Path(state.get("destination", "")) != migrated
                or payload.get("destination") != state.get("destination")
                or payload.get("original_source") != state.get("source")
                or payload.get("source_root_digest") != state.get("source_root_digest")
                or state.get("migration_sha256") != digest):
            raise RollbackConflict("migration metadata does not match its transaction anchor")
        original_parent = Path(state["source"]).parent
        parent_info = os.stat(original_parent, follow_symlinks=False)
        if (parent_info.st_dev != state.get("source_parent_device")
                or parent_info.st_ino != state.get("source_parent_inode")):
            raise RollbackConflict("original source parent identity changed")
        if payload.get("backup_tree") != "Process/MigrationBackup/tree":
            raise RollbackConflict("migration inverse path is invalid")
        return payload
    except (OSError, json.JSONDecodeError) as exc:
        raise RollbackConflict("migration metadata is missing or corrupt") from exc


def rollback_migration(migrated) -> Path:
    migrated = Path(migrated).resolve(strict=True)
    payload = _load_migration(migrated)
    expected = payload.get("expected_files") or {}
    actual = _file_hashes(migrated, exclude={"Process/migration.json", "Process/migration.sha256"})
    actual.pop("Process/migration.json", None)
    unknown = sorted(set(actual) - set(expected))
    missing = sorted(set(expected) - set(actual))
    changed = sorted(path for path in set(actual) & set(expected) if actual[path] != expected[path])
    if unknown:
        raise RollbackConflict(f"unrecorded migrated entries: {', '.join(unknown)}")
    if missing or changed:
        raise RollbackConflict(f"migrated entries changed or missing: {', '.join(missing + changed)}")
    original = Path(payload["original_source"])
    if not original.is_absolute() or original.name in {"", ".", ".."}:
        raise RollbackConflict("original source path is unsafe")
    try:
        common = Path(os.path.commonpath((migrated, original)))
    except ValueError as exc:
        raise RollbackConflict("original source and migrated run are on unrelated roots") from exc
    if common == Path(common.anchor):
        raise RollbackConflict("original source path is outside the migration project scope")
    if original != migrated and original.exists():
        raise RollbackConflict(f"original source path is occupied: {original}")
    leases = _acquire_migration_leases(migrated, original, "rollback-migration")
    try:
        transaction = migrated.parent / ".transactions" / f"rollback-{uuid.uuid4()}"
        restored = transaction / "restored" / original.name
        quarantine = transaction / "migrated" / migrated.name
        backup_tree = migrated / "Process" / "MigrationBackup" / "tree"
        _copy_inventory(backup_tree, restored)
        if TreeInventory.capture(restored).root_digest != payload["source_root_digest"]:
            raise RollbackConflict("embedded inverse does not reproduce the original tree")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.rename(migrated, quarantine)
        os.rename(restored, original)
        shutil.rmtree(transaction)
        return original
    finally:
        _release_leases(leases)


def _validated_recovery_state(root: Path, state_path: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        transaction_id = state["transaction_id"]
        source = Path(state["source"])
        destination = Path(state["destination"])
        stage = Path(state["stage"])
        backup = Path(state["backup"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid migration recovery state: {state_path}") from exc
    tx = state_path.parent
    if transaction_id != tx.name.removeprefix("migration-"):
        raise MigrationError("migration recovery transaction ID does not match its directory")
    if not all(path.is_absolute() for path in (source, destination, stage, backup)):
        raise MigrationError("migration recovery paths must be absolute")
    if destination.parent != root:
        raise MigrationError("migration recovery destination escapes the selected library")
    if stage != tx / "staged" / destination.name or backup != tx / "legacy-backup" / source.name:
        raise MigrationError("migration recovery staging paths are not transaction-scoped")
    common = Path(os.path.commonpath((root, source)))
    if common == Path(common.anchor):
        raise MigrationError("migration recovery source is outside the project scope")
    try:
        parent_info = os.stat(source.parent, follow_symlinks=False)
    except OSError as exc:
        raise MigrationError("migration recovery source parent is unavailable") from exc
    if (parent_info.st_dev != state.get("source_parent_device")
            or parent_info.st_ino != state.get("source_parent_inode")):
        raise MigrationError("migration recovery source parent identity changed")
    return state


def _verify_staged_anchor(stage: Path, state: dict[str, Any]) -> None:
    expected = state.get("migration_sha256")
    migration_path = stage / "Process" / "migration.json"
    if not isinstance(expected, str) or len(expected) != 64 or not migration_path.is_file():
        raise MigrationError("staged migration metadata has no transaction anchor")
    if hashlib.sha256(migration_path.read_bytes()).hexdigest() != expected:
        raise MigrationError("staged migration metadata changed after intent")


def recover_migration(library, *, mode: str = "continue") -> list[dict[str, str]]:
    if mode not in {"continue", "abort"}:
        raise ValueError("recovery mode must be continue or abort")
    root = Path(library).resolve(strict=True)
    outcomes = []
    for state_path in sorted(root.glob(".transactions/migration-*/state.json")):
        state = _validated_recovery_state(root, state_path)
        source, destination = Path(state["source"]), Path(state["destination"])
        stage, backup = Path(state["stage"]), Path(state["backup"])
        status = state["status"]
        if status == "committed":
            if mode == "abort":
                restored = rollback_migration(destination)
                state["status"] = "aborted"
                _atomic_json(state_path, state)
                outcomes.append({"transaction_id": state["transaction_id"], "status": "aborted", "path": str(restored)})
            else:
                outcomes.append({"transaction_id": state["transaction_id"], "status": "committed", "path": str(destination)})
            continue
        leases = _acquire_migration_leases(source, destination, "recover-migration")
        try:
            if mode == "continue" and status == "intent":
                if TreeInventory.capture(source).root_digest != state["source_root_digest"]:
                    raise MigrationError("recovery source changed after migration intent")
                _verify_staged_anchor(stage, state)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, backup)
                status = state["status"] = "source-backed-up"
                _atomic_json(state_path, state)
            if mode == "continue" and status == "source-backed-up":
                if destination.exists():
                    raise MigrationError("recovery destination is unexpectedly occupied")
                _verify_staged_anchor(stage, state)
                os.rename(stage, destination)
                state["status"] = "committed"
                _atomic_json(state_path, state)
                outcomes.append({"transaction_id": state["transaction_id"], "status": "committed", "path": str(destination)})
            elif mode == "abort":
                if status == "source-backed-up":
                    if source.exists():
                        raise MigrationError("abort source is unexpectedly occupied")
                    os.rename(backup, source)
                if stage.exists():
                    shutil.rmtree(stage)
                state["status"] = "aborted"
                _atomic_json(state_path, state)
                outcomes.append({"transaction_id": state["transaction_id"], "status": "aborted", "path": str(source)})
        finally:
            _release_leases(leases)
    return outcomes


__all__ = [
    "MigrationError", "MigrationPlan", "MigrationTarget", "MoveOp", "RewriteOp",
    "apply_migration", "discover_targets", "plan_migration", "recover_migration",
    "rollback_migration", "RollbackConflict",
]
