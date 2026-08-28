"""Deterministic planning and crash-safe application of legacy run migrations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable

from scripts.run_layout import LayoutError, LayoutKind, RunLayout, portable_collision_key, safe_relpath, slugify_v1
from scripts.run_transactions import TreeInventory


class MigrationError(RuntimeError):
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


__all__ = [
    "MigrationError", "MigrationPlan", "MigrationTarget", "MoveOp", "RewriteOp",
    "discover_targets", "plan_migration",
]
