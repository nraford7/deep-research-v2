"""Create a new research run by inheriting a frozen, provenance-rich corpus."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from scripts.run_layout import LayoutKind, RunLayout, safe_relpath
from scripts.run_manager import (
    PrepareResult,
    _collision_paths,
    _fresh_component,
    _managed_result,
    prepare_run,
)
from scripts.run_state import RunMetadata, transition_status, validate_legacy_completion, validate_seal
from scripts.run_transactions import ImmutableRegistry, RunLease, TreeInventory, create_skeleton_transaction


class ExtensionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtensionPlan:
    parent: Path
    child: Path
    parent_run_id: str
    child_run_id: str
    snapshot_root: str
    inherited_rows: int
    prepared: PrepareResult


def _copy_tree(source: Path, destination: Path) -> None:
    inventory = TreeInventory.capture(source)  # rejects symlinks/special files
    destination.mkdir(parents=True, exist_ok=False)
    for entry in inventory.entries.values():
        target = destination / entry.relative
        origin = source / entry.relative
        if entry.kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
        elif entry.kind == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target, follow_symlinks=False)


def _eligible(layout: RunLayout) -> tuple[str, bool]:
    if layout.kind is LayoutKind.V2:
        metadata = RunMetadata.load(layout)
        if metadata.sealed or metadata.status == "complete":
            check = validate_seal(layout)
            if not check.ok:
                raise ExtensionError("completed parent seal is invalid")
            return metadata.run_id, False
        gate = layout.stage_manifests / "evidence_gate.json"
        if metadata.status not in {"incomplete", "failed", "frozen"} or not gate.is_file():
            raise ExtensionError("partial v2 parent must have a recorded evidence-gate stage")
        return metadata.run_id, not metadata.frozen_for_derivation
    if validate_legacy_completion(layout).ok:
        return f"legacy:{layout.run_root.name}", False
    if not (layout.round1 / "evidence_gate.json").is_file():
        raise ExtensionError("partial legacy parent must have gate-passed Round-1 evidence")
    return f"legacy:{layout.run_root.name}", False


def _inherit_active_corpus(parent: RunLayout, child: RunLayout, parent_run_id: str) -> int:
    destination = child.round1 / "inherited_corpus.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    extracted = child.extracted_sources / "inherited"
    extracted.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for document in sorted(parent.round1.glob("slice_*.jsonl")):
        for raw in document.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            value = row.get("text_path")
            if isinstance(value, str) and value:
                try:
                    relative = safe_relpath(value)
                except ValueError as exc:
                    raise ExtensionError(f"unsafe inherited text_path: {value!r}") from exc
                base = parent.run_root if parent.kind is LayoutKind.V2 else parent.round1
                origin = base / relative
                if origin.is_file():
                    digest = hashlib.sha256(origin.read_bytes()).hexdigest()[:16]
                    name = f"{digest}-{origin.name}"
                    target = extracted / name
                    if not target.exists():
                        shutil.copy2(origin, target, follow_symlinks=False)
                    row["text_path"] = target.relative_to(child.run_root).as_posix()
            row["inherited_from_run_id"] = parent_run_id
            rows.append(row)
    destination.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return len(rows)


def prepare_extension(parent_run, question: str, *, dry_run: bool = False) -> ExtensionPlan | PrepareResult:
    parent = RunLayout.open(parent_run)
    parent_run_id, should_freeze = _eligible(parent)
    if dry_run:
        return prepare_run(
            question=question,
            slug=parent.run_root.name,
            library_dir=parent.run_root.parent,
            mode="fresh",
            dry_run=True,
        )

    before = TreeInventory.capture(parent.run_root)
    parent_lease = RunLease.acquire(parent.run_root.parent, parent.run_root.name, operation="extend")
    prepared = None
    transaction = None
    published = False
    try:
        library = parent.run_root.parent
        component = _fresh_component(library, parent.run_root.name)
        transaction = create_skeleton_transaction(library, component, question=question.strip())
        aliases = _collision_paths(library, component)
        if aliases:
            raise ExtensionError(f"extension child alias appeared before construction: {aliases[0]}")
        child = RunLayout.open(transaction.skeleton)
        child_metadata = RunMetadata.load(child)
        snapshot = child.process / "Inherited" / parent.run_root.name / "snapshot"
        _copy_tree(parent.run_root, snapshot / "tree")
        snapshot_payload = {
            "schema_version": 1,
            "parent_run_id": parent_run_id,
            "source_root_digest": before.root_digest,
            "tree": "tree",
            "prior_bible": {"role": "orientation_only"},
        }
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "snapshot.json").write_text(
            json.dumps(snapshot_payload, indent=2) + "\n", encoding="utf-8")
        inherited_rows = _inherit_active_corpus(parent, child, parent_run_id)
        lineage = {
            "schema_version": 1,
            "parent_run_id": parent_run_id,
            "parent_path": parent.run_root.name,
            "snapshot": snapshot.relative_to(child.run_root).as_posix(),
            "snapshot_root": before.root_digest,
            "prior_bible_role": "orientation_only",
            "inherited_rows": inherited_rows,
        }
        child.lineage.write_text(json.dumps(lineage, indent=2) + "\n", encoding="utf-8")

        if should_freeze:
            transition_status(parent, "frozen", frozen_snapshot=before.root_digest)
        final_parent = TreeInventory.capture(parent.run_root)
        ImmutableRegistry(parent.run_root.parent).record(
            parent.run_root,
            kind="frozen-partial" if should_freeze else "extension-parent",
            expected_root=final_parent.root_digest,
            run_id=parent_run_id,
        )
        # Complete parents must remain byte-for-byte unchanged.
        if not should_freeze and TreeInventory.capture(parent.run_root) != before:
            raise ExtensionError("extension changed an immutable completed parent")
        child_path = transaction.publish()
        published = True
        child = RunLayout.open(child_path)
        # Inheritance is a coordinator-owned construction phase. Launch the
        # child broker only after it ends so no out-of-process lease exists
        # while the snapshot and provenance files are being populated.
        prepared = _managed_result(
            action="extended",
            run_dir=child.run_root,
            project=None,
            library=library,
            transaction_id=transaction.transaction_id,
        )
        return ExtensionPlan(
            parent.run_root,
            child.run_root,
            parent_run_id,
            child_metadata.run_id,
            before.root_digest,
            inherited_rows,
            prepared,
        )
    except Exception:
        # Creation recovery owns hidden-skeleton cleanup; never remove an
        # unverified tree here. Release only the construction lease we still own.
        if transaction is not None and not published:
            try:
                transaction.lease.release(transaction.lease.owner.token)
            except Exception:
                pass
        raise
    finally:
        parent_lease.release(parent_lease.owner.token)


def recover_extension(library) -> list[dict[str, str]]:
    """Report committed lineage records; incomplete creations use normal recovery."""
    outcomes = []
    root = Path(library)
    if not root.is_dir():
        return outcomes
    for lineage in root.glob("*/Process/lineage.json"):
        try:
            payload = json.loads(lineage.read_text(encoding="utf-8"))
            outcomes.append({"child": lineage.parents[1].name, "parent_run_id": payload["parent_run_id"]})
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    return outcomes


__all__ = ["ExtensionError", "ExtensionPlan", "prepare_extension", "recover_extension"]
