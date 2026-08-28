"""Versioned physical layout authority for research runs.

This module is deliberately standard-library only.  Normal orchestration passes a
``RunLayout`` to helpers so those helpers never infer physical locations themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import uuid
from typing import Any, Mapping


LAYOUT_VERSION = 2
PATH_SCHEMA_VERSION = 1
SLUG_VERSION = "slug-v1"
UNICODE_VERSION = "15.0.0"
MAX_COMPONENT_BYTES = 120

_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_LEGACY_DIR_MARKERS = (
    "sections",
    "chapters",
    "export",
    "round1",
    "round2",
    "round2_5",
    "round3",
    "round4",
    "round5",
)
_LEGACY_FILE_MARKERS = ("scope.json", "retrieval_ledger.json")
_SAFE_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{12}Z$")


class LayoutKind(Enum):
    V2 = "v2"
    LEGACY = "legacy"
    UNMANAGED = "unmanaged"


class LayoutError(RuntimeError):
    """Raised when a run cannot be classified safely."""

    def __init__(self, state: str, detail: str):
        self.state = state
        self.detail = detail
        super().__init__(f"{state} layout: {detail}")


class BibleAmbiguityError(LayoutError):
    def __init__(self, detail: str):
        super().__init__("ambiguous Bible", detail)


@dataclass(frozen=True)
class ResolvedRoot:
    project: Path | None
    library: Path


@dataclass(frozen=True)
class FilesystemCapabilities:
    case_sensitive: bool
    normalization_sensitive: bool
    write_probe_pending: bool


@dataclass(frozen=True)
class BibleSelection:
    markdown: PurePosixPath
    html: PurePosixPath | None
    source_markdown: PurePosixPath | None = None
    source_html: PurePosixPath | None = None


def _absolute(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def resolve_project_root(
    *,
    project_dir: os.PathLike[str] | str | None = None,
    library_dir: os.PathLike[str] | str | None = None,
    output_root: os.PathLike[str] | str | None = None,
    launch_dir: os.PathLike[str] | str | None = None,
) -> ResolvedRoot:
    """Resolve a captured launch project separately from a direct run library."""

    direct_values = [value for value in (library_dir, output_root) if value is not None]
    if project_dir is not None and direct_values:
        raise ValueError("conflicting project and direct-library arguments")
    if len(direct_values) > 1 and _absolute(direct_values[0]) != _absolute(direct_values[1]):
        raise ValueError("conflicting library-dir and output-root arguments")
    if direct_values:
        return ResolvedRoot(project=None, library=_absolute(direct_values[0]))

    project = _absolute(project_dir if project_dir is not None else launch_dir or Path.cwd())
    library = project if project.name.casefold() == "research" else project / "research"
    return ResolvedRoot(project=project, library=library)


def _unicode_data_path() -> Path:
    return Path(__file__).resolve().parents[1] / "vendor" / "unicode" / UNICODE_VERSION / "UnicodeData.txt"


@lru_cache(maxsize=1)
def _unicode_tables() -> tuple[dict[int, tuple[int, ...]], dict[int, tuple[int, ...]], dict[int, str], dict[int, int]]:
    compatibility: dict[int, tuple[int, ...]] = {}
    canonical: dict[int, tuple[int, ...]] = {}
    categories: dict[int, str] = {}
    combining_classes: dict[int, int] = {}
    data_path = _unicode_data_path()
    try:
        lines = data_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:  # pragma: no cover - an installation integrity failure
        raise RuntimeError(f"required Unicode {UNICODE_VERSION} data is unavailable: {data_path}") from exc
    for line in lines:
        fields = line.split(";")
        if len(fields) < 6:
            continue
        codepoint = int(fields[0], 16)
        categories[codepoint] = fields[2]
        combining_classes[codepoint] = int(fields[3] or "0")
        decomposition = fields[5]
        if not decomposition:
            continue
        parts = decomposition.split()
        is_compat = parts[0].startswith("<")
        if is_compat:
            parts = parts[1:]
        mapping = tuple(int(part, 16) for part in parts)
        compatibility[codepoint] = mapping
        if not is_compat:
            canonical[codepoint] = mapping
    return compatibility, canonical, categories, combining_classes


def _recursive_decompose(codepoint: int, table: Mapping[int, tuple[int, ...]]) -> list[int]:
    mapping = table.get(codepoint)
    if mapping is None:
        return [codepoint]
    result: list[int] = []
    for child in mapping:
        result.extend(_recursive_decompose(child, table))
    return result


def _canonical_decomposed(value: str) -> str:
    _, canonical, _, combining_classes = _unicode_tables()
    output: list[int] = []
    cluster_start = 0
    for character in value:
        for codepoint in _recursive_decompose(ord(character), canonical):
            combining = combining_classes.get(codepoint, 0)
            if combining == 0:
                output.append(codepoint)
                cluster_start = len(output)
                continue
            insertion = len(output)
            while insertion > cluster_start and combining_classes.get(output[insertion - 1], 0) > combining:
                insertion -= 1
            output.insert(insertion, codepoint)
    return "".join(chr(codepoint) for codepoint in output)


def _nfkd_ascii(value: str) -> str:
    compatibility, _, categories, _ = _unicode_tables()
    output: list[str] = []
    for character in value:
        for codepoint in _recursive_decompose(ord(character), compatibility):
            if categories.get(codepoint, "").startswith("M"):
                continue
            if codepoint < 128:
                output.append(chr(codepoint).lower())
    return "".join(output)


def _is_device_name(component: str, *, include_extension_alias: bool = False) -> bool:
    normalized = component.rstrip(". ").casefold()
    if normalized in _DEVICE_NAMES:
        return True
    return include_extension_alias and normalized.partition(".")[0] in _DEVICE_NAMES


def _bounded_component(base: str, *, original: str, reserved_suffix: str = "") -> str:
    suffix_bytes = len(reserved_suffix.encode("utf-8"))
    budget = MAX_COMPONENT_BYTES - suffix_bytes
    if budget < 12:
        raise ValueError("reserved suffix leaves no safe filename budget")
    if len(base.encode("utf-8")) <= budget:
        return base
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    marker = f"-{digest}"
    prefix_budget = budget - len(marker)
    prefix = base.encode("utf-8")[:prefix_budget].decode("utf-8", errors="ignore").rstrip("- .")
    if not prefix:
        prefix = "run"
    return prefix + marker


def slugify_v1(value: str, *, reserved_suffix: str = "") -> str:
    """Create a deterministic portable slug using the vendored Unicode 15 table."""

    decomposed_ascii = _nfkd_ascii(str(value))
    normalized = re.sub(r"[^a-z0-9]+", "-", decomposed_ascii).strip("- .")
    if not normalized:
        normalized = "research-run"
    if _is_device_name(normalized):
        normalized += "-run"
    return _bounded_component(normalized, original=normalized, reserved_suffix=reserved_suffix)


def portable_collision_key(component: str) -> str:
    if not isinstance(component, str) or not component:
        raise ValueError("collision key requires a nonempty component")
    return _canonical_decomposed(component).casefold().rstrip(". ")


def _unsafe_component(component: str) -> bool:
    if component in {"", ".", ".."}:
        return True
    if component.endswith((".", " ")):
        return True
    if ":" in component or any(ord(character) < 32 or ord(character) == 127 for character in component):
        return True
    return _is_device_name(component, include_extension_alias=True)


def safe_relpath(value: os.PathLike[str] | str) -> PurePosixPath:
    """Parse a persisted run-root-relative path without platform reinterpretation."""

    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError(f"unsafe relative path: {raw!r}")
    if raw.startswith("/") or raw.startswith("//") or "//" in raw:
        raise ValueError(f"unsafe relative path: {raw!r}")
    components = raw.split("/")
    if any(_unsafe_component(component) for component in components):
        raise ValueError(f"unsafe relative path: {raw!r}")
    parsed = PurePosixPath(raw)
    if parsed.is_absolute() or parsed.as_posix() != raw:
        raise ValueError(f"unsafe relative path: {raw!r}")
    return parsed


def filesystem_safe_timestamp(value: datetime | None = None) -> str:
    moment = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


def capabilities_for_dry_run(path: os.PathLike[str] | str) -> FilesystemCapabilities:
    """Return conservative capabilities without invoking any mutating primitive."""

    target = Path(path)
    # Enumeration is intentionally read-only and helps surface an inaccessible target.
    if target.exists():
        tuple(entry.name for entry in target.iterdir())
    # Portable planning assumes aliases collide until a selected mutation mode probes.
    return FilesystemCapabilities(False, False, True)


def _exclusive_probe(path: Path, name: str) -> None:
    descriptor = os.open(path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def probe_filesystem(path: os.PathLike[str] | str) -> FilesystemCapabilities:
    """Probe case and normalization aliasing, removing all probe artifacts."""

    target = Path(path)
    if not target.is_dir():
        raise FileNotFoundError(f"filesystem probe target is not a directory: {target}")
    token = uuid.uuid4().hex
    case_name = f".research-case-{token}-a"
    unicode_name = f".research-unicode-{token}-é"
    created: list[str] = []
    try:
        _exclusive_probe(target, case_name)
        created.append(case_name)
        case_sensitive = not (target / case_name.upper()).exists()
        _exclusive_probe(target, unicode_name)
        created.append(unicode_name)
        decomposed_name = _canonical_decomposed(unicode_name)
        normalization_sensitive = decomposed_name == unicode_name or not (target / decomposed_name).exists()
        return FilesystemCapabilities(case_sensitive, normalization_sensitive, False)
    finally:
        for name in reversed(created):
            try:
                os.unlink(target / name)
            except FileNotFoundError:
                pass


def _filesystem_key(component: str, capabilities: FilesystemCapabilities) -> str:
    key = component.rstrip(". ")
    if not capabilities.normalization_sensitive:
        key = _canonical_decomposed(key)
    if not capabilities.case_sensitive:
        key = key.casefold()
    return key


def reserve_unique_directory(
    library: os.PathLike[str] | str,
    component: str,
    *,
    timestamp: str | None = None,
    capabilities: FilesystemCapabilities | None = None,
) -> Path:
    """Atomically reserve a portable directory name without replacing a race winner."""

    root = Path(library)
    if not root.is_dir():
        raise FileNotFoundError(f"library is not a directory: {root}")
    safe_component = slugify_v1(component)
    caps = capabilities or probe_filesystem(root)
    existing = tuple(entry.name for entry in root.iterdir())
    portable_keys = {portable_collision_key(name) for name in existing}
    filesystem_keys = {_filesystem_key(name, caps) for name in existing}
    if timestamp is None:
        if portable_collision_key(safe_component) in portable_keys or _filesystem_key(safe_component, caps) in filesystem_keys:
            raise FileExistsError(f"portable collision for {safe_component!r}")
        candidates = (safe_component,)
    else:
        if not _SAFE_TIMESTAMP_RE.fullmatch(timestamp):
            raise ValueError("timestamp must use YYYYMMDDTHHMMSSffffffZ")
        suffix = f"-{timestamp}"
        base = slugify_v1(component, reserved_suffix=suffix + "-999999")
        candidates = (f"{base}{suffix}", *(f"{base}{suffix}-{retry}" for retry in range(2, 1_000_000)))

    for candidate in candidates:
        if len(candidate.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise ValueError("reserved component exceeds 120 bytes")
        if portable_collision_key(candidate) in portable_keys or _filesystem_key(candidate, caps) in filesystem_keys:
            continue
        target = root / candidate
        try:
            os.mkdir(target)
        except FileExistsError:
            portable_keys.add(portable_collision_key(candidate))
            filesystem_keys.add(_filesystem_key(candidate, caps))
            continue
        return target
    raise FileExistsError(f"could not reserve a collision-free name for {component!r}")


@dataclass(frozen=True)
class RunLayout:
    run_root: Path
    kind: LayoutKind
    metadata_data: dict[str, Any] | None = None
    _legacy_sections_name: str | None = None

    @classmethod
    def open(cls, path: os.PathLike[str] | str, *, allow_unmanaged: bool = False) -> "RunLayout":
        root = _absolute(path)
        if not root.is_dir():
            raise LayoutError("invalid", f"run root is not a directory: {root}")
        metadata = root / "Process" / "run.json"
        # Compare directory-entry spellings, not Path.exists(): on a case-insensitive
        # filesystem ``root / 'sections'`` aliases the native v2 ``Sections`` home.
        actual_names = {entry.name for entry in root.iterdir()}
        legacy_markers = [name for name in _LEGACY_DIR_MARKERS if name in actual_names]
        legacy_markers.extend(name for name in _LEGACY_FILE_MARKERS if name in actual_names)
        if metadata.exists():
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise LayoutError("corrupt", f"cannot parse Process/run.json: {exc}") from exc
            if not isinstance(payload, dict):
                raise LayoutError("corrupt", "Process/run.json must contain an object")
            version = payload.get("layout_version")
            if version != LAYOUT_VERSION:
                state = "unsupported" if isinstance(version, int) and version > LAYOUT_VERSION else "corrupt"
                raise LayoutError(state, f"layout_version {version!r} is not supported")
            if not isinstance(payload.get("schema_version"), int) or not isinstance(payload.get("slug"), str):
                raise LayoutError("corrupt", "Process/run.json is missing its schema_version or slug")
            if legacy_markers:
                raise LayoutError("mixed", f"v2 metadata coexists with legacy homes: {', '.join(legacy_markers)}")
            return cls(root, LayoutKind.V2, payload)

        if legacy_markers:
            sections_name = "sections" if (root / "sections").is_dir() else "chapters" if (root / "chapters").is_dir() else None
            return cls(root, LayoutKind.LEGACY, None, sections_name)
        if allow_unmanaged and not any(root.iterdir()):
            return cls(root, LayoutKind.UNMANAGED)
        raise LayoutError("invalid", "no recognized v2 or legacy run signature")

    @property
    def sections(self) -> Path:
        if self.kind is LayoutKind.V2:
            return self.run_root / "Sections"
        if self.kind is LayoutKind.LEGACY:
            return self.run_root / (self._legacy_sections_name or "sections")
        return self.run_root

    @property
    def sources(self) -> Path:
        return self.run_root / "Sources" if self.kind is LayoutKind.V2 else self.run_root / "round1" / "sources"

    @property
    def extracted_sources(self) -> Path:
        return self.sources / "Extracted" if self.kind is LayoutKind.V2 else self.sources

    @property
    def process(self) -> Path:
        return self.run_root / "Process" if self.kind is LayoutKind.V2 else self.run_root

    def round(self, number: str | int) -> Path:
        name = f"round{number}"
        return self.process / name

    @property
    def round1(self) -> Path:
        return self.round(1)

    @property
    def round2(self) -> Path:
        return self.round(2)

    @property
    def round2_5(self) -> Path:
        return self.round("2_5")

    @property
    def round3(self) -> Path:
        return self.round(3)

    @property
    def round4(self) -> Path:
        return self.round(4)

    @property
    def round5(self) -> Path:
        return self.round(5)

    @property
    def scope(self) -> Path:
        return self.process / "scope.json"

    @property
    def ledger(self) -> Path:
        return self.process / "retrieval_ledger.json"

    @property
    def bibliography_md(self) -> Path:
        if self.kind is LayoutKind.V2:
            return self.sources / "bibliography.md"
        export = self.run_root / "export" / "bibliography.md"
        return export if export.exists() else self.sections / "bibliography.md"

    @property
    def bibliography_bib(self) -> Path:
        return self.sources / "bibliography.bib" if self.kind is LayoutKind.V2 else self.run_root / "export" / "bibliography.bib"

    @property
    def claims(self) -> Path:
        return self.sources / "claims.jsonl" if self.kind is LayoutKind.V2 else self.run_root / "export" / "claims.jsonl"

    @property
    def metadata(self) -> Path:
        return self.run_root / "Process" / "run.json"

    @property
    def lineage(self) -> Path:
        return self.process / "lineage.json"

    @property
    def stage_manifests(self) -> Path:
        return self.process / "stages"

    @property
    def lock_identity(self) -> str:
        if self.metadata_data and isinstance(self.metadata_data.get("run_id"), str):
            return self.metadata_data["run_id"]
        return hashlib.sha256(os.fsencode(self.run_root)).hexdigest()

    def _relative_if_file(self, path: Path) -> PurePosixPath | None:
        if not path.is_file():
            return None
        return PurePosixPath(path.relative_to(self.run_root).as_posix())

    def discover_bible(self) -> BibleSelection:
        declared = self.metadata_data.get("bible") if self.metadata_data else None
        if declared is not None:
            if not isinstance(declared, dict) or not isinstance(declared.get("markdown"), str):
                raise LayoutError("corrupt", "run.json bible must declare a Markdown path")
            markdown = safe_relpath(declared["markdown"])
            html_value = declared.get("html")
            html = safe_relpath(html_value) if isinstance(html_value, str) else None
            if not (self.run_root / markdown).is_file():
                raise LayoutError("corrupt", f"declared Bible is missing: {markdown}")
            return BibleSelection(markdown, html, markdown, html)

        homes = [self.run_root] if self.kind is LayoutKind.V2 else [self.run_root, self.run_root / "export"]
        markdown_files: list[Path] = []
        for home in homes:
            if home.is_dir():
                markdown_files.extend(path for path in home.iterdir() if path.is_file() and path.suffix.casefold() == ".md")

        exact_generic = [path for path in markdown_files if path.name.casefold() == "research-bible.md"]
        categories = [
            [path for path in markdown_files if path.name.casefold().startswith("research-bible") and path not in exact_generic],
            [path for path in markdown_files if path.name.casefold().endswith("-research-bible.md")],
            [path for path in markdown_files if path.name.casefold().endswith("-bible.md")],
            exact_generic,
        ]
        selected: Path | None = None
        for candidates in categories:
            unique = sorted(set(candidates), key=lambda path: path.as_posix().casefold())
            if not unique:
                continue
            if len(unique) > 1:
                names = ", ".join(path.relative_to(self.run_root).as_posix() for path in unique)
                raise BibleAmbiguityError(f"multiple equally authoritative candidates: {names}")
            selected = unique[0]
            break
        if selected is None:
            raise LayoutError("invalid", "no canonical Bible Markdown candidate")

        source_markdown = PurePosixPath(selected.relative_to(self.run_root).as_posix())
        target_markdown = source_markdown
        if selected.name.casefold() == "research-bible.md":
            target_markdown = source_markdown.with_name(f"RESEARCH-BIBLE_{slugify_v1(self.run_root.name)}.md")
        source_html_path = selected.with_suffix(".html")
        source_html = self._relative_if_file(source_html_path)
        target_html = target_markdown.with_suffix(".html") if source_html is not None else None
        return BibleSelection(target_markdown, target_html, source_markdown, source_html)


__all__ = [
    "BibleAmbiguityError",
    "BibleSelection",
    "FilesystemCapabilities",
    "LAYOUT_VERSION",
    "LayoutError",
    "LayoutKind",
    "MAX_COMPONENT_BYTES",
    "ResolvedRoot",
    "RunLayout",
    "SLUG_VERSION",
    "UNICODE_VERSION",
    "capabilities_for_dry_run",
    "filesystem_safe_timestamp",
    "portable_collision_key",
    "probe_filesystem",
    "reserve_unique_directory",
    "resolve_project_root",
    "safe_relpath",
    "slugify_v1",
]
