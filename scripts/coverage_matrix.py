#!/usr/bin/env python3
"""coverage_matrix.py — the source-selection coverage matrix (pure, offline core).

ONE object, THREE reads. A research run's source selection is governed by a
single structure — a matrix whose cells are ``(subtopic, lens)`` pairs — and the
same object answers all three questions the pipeline keeps asking separately:

  * RANKING    — which candidate source to admit next (``score`` / ``rank``):
                 open-cell coverage first, then relevance, with authority only a
                 tiebreaker. A low-authority source that fills an OPEN cell beats
                 the Nth prestige source on a filled one.
  * SATURATION — when to stop (``status``): a two-condition stop over a GROWING
                 map — every required cell covered AND ``k_dry`` consecutive
                 rounds with no newly-nominated cell — gated first by required-
                 lane availability (a lane that never ran → ``PARTIAL``, never
                 ``SATURATED``). Convergence is not completeness; the growing map
                 and the lane gate are what keep "done" honest.
  * AUDIT      — what is missing (``empty_cells`` / ``single_primary_cells``):
                 required cells with no source, and cells resting on a single
                 primary. Saturation and these audits count DISTINCT PRIMARIES
                 (claim space), not documents — ten papers deriving from one
                 primary are one unit of evidence.

PURE: no network, no subprocess, no clock, no randomness. Age enters as
``age_years`` (the caller stamps it), so the module is a deterministic function
of its inputs and every list-returning read is stably ordered.

This module is the offline logic core; wiring it into scope.py / slice_search.py
/ coverage_audit.py is a separate, deferred step (see the design spec).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

_LN2 = math.log(2.0)


class Lens(str, Enum):
    """The axis a cell covers a subtopic along. String-valued for greppability."""

    METHOD = "method"          # methodological school
    PERSPECTIVE = "perspective"  # stakeholder / point of view
    PERIOD = "period"          # time period
    TRADITION = "tradition"    # cultural / geographic tradition
    SUBCLAIM = "subclaim"      # a specific sub-claim of the thesis


class InclusionReason(str, Enum):
    """Why a source is in the corpus. A label for auditing, not enforced logic."""

    ANCHOR = "anchor"                    # primary / canonical anchor
    EVIDENCE = "evidence"                # evidence for a specific claim
    UNIQUE_COVERAGE = "unique_coverage"  # the only source on a cell
    DISSENT = "dissent"                  # strongest counter-evidence
    UPDATE = "update"                    # contemporary update


@dataclass(frozen=True)
class Cell:
    """A unit of required coverage: one subtopic seen through one lens."""

    subtopic: str
    lens: Lens

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.subtopic, self.lens.value)


@dataclass
class Source:
    """A candidate or admitted source.

    ``primary_id`` is required and non-empty: a source that is itself primary
    sets ``primary_id == id``. ``inclusion_reason`` must be an
    ``InclusionReason`` — a source cannot be constructed (hence cannot be
    admitted) without a typed reason.
    """

    id: str
    primary_id: str
    relevance: float
    citation_count: int
    age_years: float
    authority: float
    lane: str
    inclusion_reason: InclusionReason
    cells: frozenset[Cell] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.primary_id:
            raise ValueError(f"source {self.id!r}: primary_id is required and non-empty")
        if not isinstance(self.inclusion_reason, InclusionReason):
            raise ValueError(
                f"source {self.id!r}: inclusion_reason must be an InclusionReason, "
                f"got {self.inclusion_reason!r}"
            )
        # Normalize whatever iterable of cells was passed into a frozenset.
        self.cells = frozenset(self.cells)


@dataclass(frozen=True)
class SliceWeighting:
    """Per-slice age policy for citation weight.

    ``age_adjusted_weight = citation_count * exp(sign * ln2 * age / half_life)``,
    ``sign = +1`` when ``older_is_better`` (canonical slice: weight DOUBLES every
    ``half_life_years`` of age — survival is signal) else ``-1`` (evidence slice:
    weight HALVES every ``half_life_years`` — recency is signal).
    """

    half_life_years: float
    older_is_better: bool

    def __post_init__(self) -> None:
        if self.half_life_years <= 0:
            raise ValueError("half_life_years must be > 0")

    def age_adjusted_weight(self, citation_count: int, age_years: float) -> float:
        sign = 1.0 if self.older_is_better else -1.0
        return citation_count * math.exp(sign * _LN2 * age_years / self.half_life_years)


class CoverageMatrix:
    """The single object read by ranking, saturation, and audit."""

    def __init__(
        self,
        required_cells: Iterable[Cell] = (),
        required_lanes: Iterable[str] = ("citation_graph",),
        k_dry: int = 2,
    ) -> None:
        self._required: set[Cell] = set(required_cells)
        self._nominated: set[Cell] = set(self._required)  # required cells are known
        self._required_lanes: set[str] = set(required_lanes)
        self._k_dry = k_dry
        self._sources: list[Source] = []
        self._lanes_run: set[str] = set()
        self._dry_rounds = 0
        self._round_had_new_cell = False

    # ---- map construction (growing map) ----------------------------------
    def require_cells(self, cells: Iterable[Cell]) -> None:
        for c in cells:
            self._required.add(c)
            self._nominated.add(c)

    def nominate_cell(self, cell: Cell) -> bool:
        """Add a cell to the map. Idempotent: re-nominating a known cell is a
        no-op and does NOT mark the round non-dry. Returns True iff the cell was
        genuinely new."""
        if cell in self._nominated:
            return False
        self._nominated.add(cell)
        self._round_had_new_cell = True
        return True

    # ---- admission -------------------------------------------------------
    def admit(self, source: Source) -> None:
        """Record a source (already validated by ``Source``), mark its lane run,
        and grow the map with any of its cells not yet known."""
        self._sources.append(source)
        self._lanes_run.add(source.lane)
        for cell in source.cells:
            self.nominate_cell(cell)

    def mark_lane_run(self, lane: str) -> None:
        """Record that a lane ran even if it admitted nothing."""
        self._lanes_run.add(lane)

    # ---- ranking ---------------------------------------------------------
    def open_cells_set(self) -> set[Cell]:
        """Required cells with zero admitted sources (the live open set)."""
        covered = {c for s in self._sources for c in s.cells}
        return {c for c in self._required if c not in covered}

    def score(self, candidate: Source) -> tuple[int, float, float]:
        """Lexicographic rank key: (open cells this source fills, relevance,
        authority). Authority is a tiebreaker/floor, never a primary driver."""
        open_cells = self.open_cells_set()
        open_filled = len(candidate.cells & open_cells)
        return (open_filled, candidate.relevance, candidate.authority)

    def rank(self, candidates: Iterable[Source]) -> list[Source]:
        """Best-first. Negated key (not ``reverse=True``) so genuine full ties
        preserve input order under the stable sort."""
        return sorted(candidates, key=lambda c: tuple(-x for x in self.score(c)))

    # ---- round / saturation ---------------------------------------------
    def end_round(self) -> None:
        self._dry_rounds = 0 if self._round_had_new_cell else self._dry_rounds + 1
        self._round_had_new_cell = False

    def status(self) -> str:
        """``PARTIAL`` | ``SATURATED`` | ``OPEN``. Lane gate is evaluated FIRST:
        a required lane that never ran forces ``PARTIAL`` regardless of coverage.
        Otherwise ``SATURATED`` iff k_dry dry rounds elapsed AND no empty cells;
        else ``OPEN``."""
        if not self._required_lanes.issubset(self._lanes_run):
            return "PARTIAL"
        if self._dry_rounds >= self._k_dry and not self.empty_cells():
            return "SATURATED"
        return "OPEN"

    # ---- audit reads (deterministically ordered) -------------------------
    def empty_cells(self) -> list[Cell]:
        return sorted(self.open_cells_set(), key=lambda c: c.sort_key)

    def single_primary_cells(self) -> list[Cell]:
        """Cells whose admitted sources resolve to exactly one distinct primary."""
        out: list[Cell] = []
        for cell in self._nominated:
            primaries = {s.primary_id for s in self._sources if cell in s.cells}
            if len(primaries) == 1:
                out.append(cell)
        return sorted(out, key=lambda c: c.sort_key)

    def primaries(self) -> list[str]:
        return sorted({s.primary_id for s in self._sources})
