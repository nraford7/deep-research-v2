"""Versioned registry for path-bearing JSON and JSONL fields.

Persisted paths are data with security consequences.  This registry prevents migration,
extension, and resume code from guessing how an unfamiliar field should resolve.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from scripts.run_layout import LayoutKind, RunLayout, safe_relpath


PATH_SCHEMA_VERSION = 1


class PathSchemaError(ValueError):
    pass


class PathBase(Enum):
    RUN_ROOT = "run-root"
    LEGACY_ROUND1 = "legacy-round1"
    ARCHIVE_ROOT = "archive-root"
    NON_RESOLVING = "non-resolving"


@dataclass(frozen=True)
class PathField:
    field: str
    base: PathBase
    required: bool = False


@dataclass(frozen=True)
class PathSchema:
    pattern: str
    fields: tuple[PathField, ...]
    version: int = PATH_SCHEMA_VERSION


_PATH_LIKE_FIELD = re.compile(
    r"(?:^|_)(?:path|paths|file|files|dir|directory|root|input|inputs|output|outputs|snapshot)$",
    re.IGNORECASE,
)


class PathSchemaRegistry:
    def __init__(self, schemas: Iterable[PathSchema]):
        self._schemas = tuple(schemas)

    @property
    def schemas(self) -> tuple[PathSchema, ...]:
        return self._schemas

    def schema_for(self, relative_document: os.PathLike[str] | str) -> PathSchema:
        document = safe_relpath(relative_document).as_posix()
        for schema in self._schemas:
            if fnmatch.fnmatchcase(document.casefold(), schema.pattern.casefold()):
                return schema
        raise PathSchemaError(f"no registered path schema for {document}")

    def field_for(self, relative_document: os.PathLike[str] | str, field: str) -> PathField:
        schema = self.schema_for(relative_document)
        for registered in schema.fields:
            if registered.field == field:
                return registered
        raise PathSchemaError(f"unregistered path field {field!r} in {safe_relpath(relative_document)}")

    @staticmethod
    def _base_for(
        layout: RunLayout,
        path_field: PathField,
        *,
        archive_root: os.PathLike[str] | str | None,
    ) -> Path | None:
        if path_field.base is PathBase.NON_RESOLVING:
            return None
        if path_field.base is PathBase.ARCHIVE_ROOT:
            if archive_root is None:
                raise PathSchemaError("archive root is required for an archival path")
            root = Path(archive_root).resolve(strict=False)
            try:
                root.relative_to(layout.run_root)
            except ValueError as exc:
                raise PathSchemaError("archive root must be inside the active run") from exc
            return root
        if path_field.base is PathBase.LEGACY_ROUND1 and layout.kind is LayoutKind.LEGACY:
            return layout.round1
        return layout.run_root

    def resolve(
        self,
        layout: RunLayout,
        relative_document: os.PathLike[str] | str,
        field: str,
        value: os.PathLike[str] | str,
        *,
        archive_root: os.PathLike[str] | str | None = None,
    ) -> Path | None:
        registered = self.field_for(relative_document, field)
        base = self._base_for(layout, registered, archive_root=archive_root)
        if base is None:
            return None
        relative = safe_relpath(value)
        target = base.joinpath(*relative.parts)
        # Lexical containment is sufficient here because descriptor-relative I/O performs
        # the later no-follow identity checks before use.
        try:
            target.relative_to(base)
        except ValueError as exc:  # pragma: no cover - safe_relpath already prevents this
            raise PathSchemaError(f"path escapes its declared base: {value!r}") from exc
        return target

    @staticmethod
    def _iter_values(value: Any) -> Iterable[str]:
        if value is None:
            return
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, list):
            for item in value:
                yield from PathSchemaRegistry._iter_values(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from PathSchemaRegistry._iter_values(item)
            return
        raise PathSchemaError(f"path field contains unsupported {type(value).__name__} value")

    def reject_unknown_path_fields(
        self,
        relative_document: os.PathLike[str] | str,
        document: Mapping[str, Any],
    ) -> None:
        schema = self.schema_for(relative_document)
        registered = {field.field for field in schema.fields}

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if _PATH_LIKE_FIELD.search(str(key)) and key not in registered:
                        raise PathSchemaError(
                            f"unregistered path-like field {key!r} in {safe_relpath(relative_document)}"
                        )
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document)

    def validate_document(
        self,
        layout: RunLayout,
        relative_document: os.PathLike[str] | str,
        document: Mapping[str, Any],
        *,
        archive_root: os.PathLike[str] | str | None = None,
    ) -> None:
        if not isinstance(document, Mapping):
            raise PathSchemaError("persisted path document must be an object")
        schema = self.schema_for(relative_document)
        self.reject_unknown_path_fields(relative_document, document)
        for field in schema.fields:
            if field.required and field.field not in document:
                raise PathSchemaError(f"required path field {field.field!r} is missing")
            if field.field not in document or document[field.field] is None:
                continue
            for value in self._iter_values(document[field.field]):
                self.resolve(
                    layout,
                    relative_document,
                    field.field,
                    value,
                    archive_root=archive_root,
                )

    def rewrite_document(
        self,
        layout: RunLayout,
        relative_document: os.PathLike[str] | str,
        document: Mapping[str, Any],
        rewrites: Mapping[Path, os.PathLike[str] | str],
        *,
        archive_root: os.PathLike[str] | str | None = None,
    ) -> dict[str, Any]:
        """Return a rewritten copy; the caller commits it only after full validation."""

        self.validate_document(layout, relative_document, document, archive_root=archive_root)
        result: dict[str, Any] = deepcopy(dict(document))
        schema = self.schema_for(relative_document)

        def rewrite_value(field: PathField, value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, str):
                resolved = self.resolve(
                    layout,
                    relative_document,
                    field.field,
                    value,
                    archive_root=archive_root,
                )
                if resolved is None or resolved not in rewrites:
                    return value
                replacement = os.fspath(rewrites[resolved])
                return safe_relpath(replacement).as_posix()
            if isinstance(value, list):
                return [rewrite_value(field, item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite_value(field, item) for key, item in value.items()}
            raise PathSchemaError(f"path field contains unsupported {type(value).__name__} value")

        for field in schema.fields:
            if field.field in result:
                result[field.field] = rewrite_value(field, result[field.field])
        return result


def _schema(pattern: str, *fields: tuple[str, PathBase, bool] | tuple[str, PathBase]) -> PathSchema:
    normalized: list[PathField] = []
    for field in fields:
        normalized.append(PathField(field[0], field[1], field[2] if len(field) == 3 else False))
    return PathSchema(pattern, tuple(normalized))


# Ordered most-specific first.  Legacy and v2 document names share schemas; the
# resolver selects the historical base from the RunLayout kind.
PATH_SCHEMAS = PathSchemaRegistry(
    (
        _schema(
            "Process/Inherited/*/snapshot.json",
            ("path", PathBase.ARCHIVE_ROOT),
        ),
        _schema(
            "*/slice*.jsonl",
            ("text_path", PathBase.LEGACY_ROUND1),
            ("raw_path", PathBase.LEGACY_ROUND1),
            ("run_dir", PathBase.LEGACY_ROUND1),
        ),
        _schema(
            "*/deepening*.jsonl",
            ("text_path", PathBase.LEGACY_ROUND1),
            ("raw_path", PathBase.LEGACY_ROUND1),
            ("run_dir", PathBase.LEGACY_ROUND1),
        ),
        _schema("Sources/claims.jsonl", ("file", PathBase.RUN_ROOT, True)),
        _schema("export/claims.jsonl", ("file", PathBase.RUN_ROOT, True)),
        _schema(
            "*/fulltext_manifest.json",
            ("text_path", PathBase.LEGACY_ROUND1),
            ("raw_path", PathBase.LEGACY_ROUND1),
        ),
        _schema(
            "*/evidence_manifest.json",
            ("text_path", PathBase.LEGACY_ROUND1),
            ("raw_path", PathBase.LEGACY_ROUND1),
        ),
        _schema(
            "*/source_manifest*.json",
            ("text_path", PathBase.LEGACY_ROUND1),
            ("raw_path", PathBase.LEGACY_ROUND1),
        ),
        _schema("*/coverage*.json", ("file", PathBase.RUN_ROOT)),
        _schema("*/verification*.json", ("file", PathBase.RUN_ROOT)),
        _schema(
            "Process/export_manifest.json",
            ("inputs", PathBase.RUN_ROOT),
            ("outputs", PathBase.RUN_ROOT),
        ),
        _schema(
            "Process/stages/*.json",
            ("inputs", PathBase.RUN_ROOT),
            ("outputs", PathBase.RUN_ROOT),
        ),
        _schema(
            "Process/lineage.json",
            ("source_label", PathBase.NON_RESOLVING),
            ("snapshot", PathBase.RUN_ROOT),
        ),
    )
)


__all__ = [
    "PATH_SCHEMAS",
    "PATH_SCHEMA_VERSION",
    "PathBase",
    "PathField",
    "PathSchema",
    "PathSchemaError",
    "PathSchemaRegistry",
]
