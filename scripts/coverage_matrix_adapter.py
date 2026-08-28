#!/usr/bin/env python3
"""coverage_matrix_adapter.py — build a CoverageMatrix from the run's artifacts.

The coverage-matrix core (``scripts.coverage_matrix``) is pure logic; it knows
nothing about the deeper-research pipeline's file formats. This adapter is the
thin, OFFLINE bridge: it turns a ``scope.py`` output payload and the retrieved
``round1/slice_*.jsonl`` rows into a populated ``CoverageMatrix`` and a
JSON-serializable report.

STDLIB + CORE ONLY. It deliberately does NOT import ``slice_search`` (which pulls
``requests``/``config``/``ledger`` at module load); the one function it needs,
``_norm_key``, is duplicated verbatim below so this module stays importable
without network or heavy deps. No clock: ``current_year`` is passed in.

v1 CELL CONTRACT (see the integration design spec): required cells are seeded
from the scope payload's ``domains`` HOSTNAMES (e.g. ``rand.org``) and a source
covers a cell iff its URL host equals that hostname. ``ranked_domains`` (topic
names like "technology") are NOT seeded — no per-row topic signal exists offline
to populate them, so seeding them would guarantee permanently-empty cells and
mislead a reader. OPEN cells here therefore mean "an unmatched scope hostname,"
not necessarily thin evidence. ``matrix_report`` states this in ``coverage_note``.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scripts.coverage_matrix import (
    Cell,
    CoverageMatrix,
    InclusionReason,
    Lens,
    Source,
)

# Tier vocabulary emitted by slice_search.tier_of → a fixed authority weight in
# [0, 1]. Missing/unknown tiers fall back to the lowest weight.
TIER_AUTHORITY = {
    "peer_reviewed": 1.0,
    "book": 0.8,
    "institutional": 0.7,
    "preprint": 0.6,
    "news": 0.4,
    "wiki": 0.3,
    "blog": 0.2,
    "unknown": 0.1,
}
_AUTHORITY_DEFAULT = 0.1


def _norm_key(url):
    """Normalization key for cross-result dedupe. Mirrors
    ``slice_search._norm_key`` verbatim (duplicated to keep this module
    stdlib-only and offline): prefer a DOI when the URL is a doi.org link; else
    lowercase host + path with the trailing slash and utm_* query params
    stripped."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower()
    path = parts.path or ""
    if host.endswith("doi.org"):
        doi = path.strip("/").lower()
        if doi:
            return f"doi:{doi}"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")]
    query = urlencode(kept)
    return urlunsplit(("", host, path, query, "")).lstrip("/") or host


def _url_host(url: str) -> str:
    """Lowercase host of a URL, or "" if unparseable."""
    if not url:
        return ""
    return (urlsplit(url.strip()).netloc or "").lower()


def _year(published_date) -> int | None:
    """First 4 chars of published_date as an int year, else None. Mirrors the
    intent of ``slice_search._year_or_nd`` but returns int|None (never the
    string "n.d.") so callers never risk ``int("n.d.")``."""
    s = str(published_date) if published_date is not None else ""
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _reason_for_slice(slice_name: str) -> InclusionReason:
    """Typed inclusion reason by EXACT slice-name match (contract #1)."""
    if slice_name == "anchor":
        return InclusionReason.ANCHOR
    if slice_name.startswith("gap_"):
        return InclusionReason.UNIQUE_COVERAGE
    return InclusionReason.EVIDENCE


def cells_from_scope(payload: dict) -> list[Cell]:
    """Required cells seeded from the scope payload's ``domains`` hostnames
    (lens TRADITION). Deduped, order-stable. Empty/missing → []."""
    seen: set[str] = set()
    cells: list[Cell] = []
    for host in (payload or {}).get("domains") or []:
        h = str(host).strip().lower()
        if h and h not in seen:
            seen.add(h)
            cells.append(Cell(subtopic=h, lens=Lens.TRADITION))
    return cells


def source_from_row(row: dict, current_year: int | None,
                    scope_cells=()) -> Source | None:
    """Map one slice JSONL row to a Source, or None if it has no URL (mirroring
    ``slice_search._result_to_item``). Cells are the scope cells whose subtopic
    equals this row's URL host — a real match that may be empty (the source is
    still admitted so its lane counts as run)."""
    url = (row.get("url") or "").strip()
    if not url:
        return None
    year = _year(row.get("published_date"))
    if current_year is not None and year is not None:
        age_years = max(0, current_year - year)
    else:
        age_years = 0
    host = _url_host(url)
    cells = frozenset(c for c in scope_cells if c.subtopic == host)
    has_body = bool(row.get("text_path") or row.get("text") or row.get("highlights"))
    return Source(
        id=url,
        primary_id=_norm_key(url) or url,
        relevance=0.7 if has_body else 0.4,
        citation_count=0,  # rows carry no citation count (documented degradation)
        age_years=age_years,
        authority=TIER_AUTHORITY.get(row.get("tier"), _AUTHORITY_DEFAULT),
        lane=row.get("slice", "unknown"),
        inclusion_reason=_reason_for_slice(row.get("slice", "")),
        cells=cells,
    )


def build_matrix(payload: dict, rows, current_year: int | None = None,
                 required_lanes=("anchor",), k_dry: int = 2) -> CoverageMatrix:
    """Assemble a CoverageMatrix from a scope payload + an iterable of slice
    rows. Rows with no URL are skipped (not admitted). Anchor rows set
    ``lane="anchor"`` so admitting them clears the required-lane gate."""
    scope_cells = cells_from_scope(payload)
    m = CoverageMatrix(required_cells=scope_cells, required_lanes=required_lanes,
                       k_dry=k_dry)
    for row in rows:
        src = source_from_row(row, current_year, scope_cells)
        if src is not None:
            m.admit(src)
    return m


_COVERAGE_NOTE = (
    "v1: cells seeded from scope hostname `domains` and matched by row url-host; "
    "topic-domain coverage is future work. OPEN cells mean an unmatched scope "
    "hostname, not necessarily thin evidence."
)


def matrix_report(matrix: CoverageMatrix) -> dict:
    """JSON-serializable, deterministically-ordered snapshot of the matrix."""
    empty = [[c.subtopic, c.lens.value] for c in matrix.empty_cells()]
    single = [[c.subtopic, c.lens.value] for c in matrix.single_primary_cells()]
    return {
        "status": matrix.status(),
        "empty_cells": empty,
        "single_primary_cells": single,
        "primaries": len(matrix.primaries()),
        "n_required": len(matrix._required),
        "n_sources": len(matrix._sources),
        "coverage_note": _COVERAGE_NOTE,
    }
