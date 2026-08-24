#!/usr/bin/env python3
"""
background.py — marking convention + candidate finder for editorial background blocks.

An editorial "background" block is orienting synthesis the writer adds to frame a
section. It is NOT independently retrieved from the corpus, so it must be marked so
downstream steps (lint, verification) can hold it to a stricter rule: no invented
quantities. This module owns the markers, a canonical renderer, a deterministic
extractor, and a $0 token-overlap CANDIDATE FINDER.

corpus_support is a harmless candidate finder: it NOMINATES the best-overlapping
row for a claim but promotes nothing on its own. It cannot tell support from
contradiction ("vaccines cause autism" shares almost every token with "vaccines
do not cause autism"), so its output is a candidate to inspect, never a citation.

Public surface:
  BACKGROUND_OPEN / BACKGROUND_CLOSE  — HTML-comment delimiters
  BACKGROUND_LABEL                    — the visible blockquote label (note middot ·)
  render_background(text)             — wrap text into a canonical block
  find_background_blocks(text)        — extract inner text of every marked block
  has_unbalanced_fences(text)         — True when OPEN/CLOSE markers do not pair up
  corpus_support(claim, rows, ...)    — best-overlap candidate row above threshold, or None

Usage (as a library):
  import background as b
  block = b.render_background("A widget is a small mechanical device.")
  inner = b.find_background_blocks(md_text)
  cand = b.corpus_support("widgets are mechanical", rows)
"""

import re
from pathlib import Path

# Marking convention. Downstream tooling matches on these exact strings.
BACKGROUND_OPEN = "<!-- editorial:background -->"
BACKGROUND_CLOSE = "<!-- /editorial -->"
# Visible label. The separator is a middot ·, deliberately NOT an em-dash.
BACKGROUND_LABEL = "**Background · editorial synthesis (not independently retrieved):**"

# Deterministic extractor: capture everything between an OPEN and the next CLOSE,
# non-greedy, across newlines. re.escape keeps the markers literal.
_BLOCK_RE = re.compile(
    re.escape(BACKGROUND_OPEN) + r"(.*?)" + re.escape(BACKGROUND_CLOSE),
    re.DOTALL,
)

# Token stopwords for the overlap scorer. Kept small and generic on purpose so the
# scorer stays a cheap heuristic, not a language model.
STOPWORDS = {
    "the", "a", "an", "of", "in", "and", "on", "for", "to", "with", "by",
    "is", "are", "was", "were", "be", "been", "being", "as", "at", "or",
    "from", "that", "this", "these", "those", "it", "its", "into", "than",
    "then", "but", "not", "no", "so", "such", "which", "who", "whom", "whose",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def render_background(text: str) -> str:
    """Wrap ``text`` in the canonical background block: OPEN marker, the label,
    the text as a blockquote body, then the CLOSE marker. Emitters and tests use
    this so every marked block looks identical."""
    body = "\n".join(f"> {line}" if line else ">"
                     for line in text.strip().splitlines())
    return "\n".join([
        BACKGROUND_OPEN,
        BACKGROUND_LABEL,
        "",
        body,
        BACKGROUND_CLOSE,
    ])


def find_background_blocks(text: str) -> list:
    """Return the inner text of every OPEN..CLOSE block, in document order.

    Deterministic and robust to multiple blocks and multiline content. The label
    line and blockquote ``>`` markers are stripped so callers see the prose only.
    Returns [] when there are no marked blocks."""
    blocks = []
    for m in _BLOCK_RE.finditer(text):
        inner = m.group(1).strip("\n")
        lines = []
        for line in inner.splitlines():
            stripped = line.strip()
            if stripped == BACKGROUND_LABEL:
                continue
            # Strip a leading blockquote marker ("> " or a bare ">").
            if stripped.startswith(">"):
                line = re.sub(r"^\s*>\s?", "", line)
            lines.append(line)
        blocks.append("\n".join(lines).strip())
    return blocks


def has_unbalanced_fences(text: str) -> bool:
    """Return True when the background markers in ``text`` do not pair up cleanly.

    find_background_blocks only sees BALANCED OPEN..CLOSE pairs, so a malformed
    fence (an OPEN with no CLOSE, a CLOSE with no OPEN, or a CLOSE appearing before
    its OPEN) yields zero blocks and would slip past a block-only lint. This walks
    the markers in document order and reports any imbalance:

      - a CLOSE seen while no OPEN is pending (close before open), or
      - an OPEN still pending after the whole text is scanned (open never closed).

    Nested opens (a second OPEN before the first CLOSE) also count as unbalanced.
    """
    depth = 0
    for m in re.finditer(
            re.escape(BACKGROUND_OPEN) + "|" + re.escape(BACKGROUND_CLOSE), text):
        if m.group(0) == BACKGROUND_OPEN:
            if depth != 0:
                # A second OPEN before the first CLOSE: markers do not nest.
                return True
            depth += 1
        else:  # a CLOSE marker
            if depth == 0:
                # CLOSE with no pending OPEN (close before open).
                return True
            depth -= 1
    return depth != 0


def _tokenize(text: str) -> set:
    """Lowercase, split on non-alphanumerics, drop stopwords and short tokens."""
    return {t for t in _TOKEN_RE.findall(text.lower())
            if len(t) > 2 and t not in STOPWORDS}


def _row_text(row: dict, round1_dir=None) -> str:
    """Gather whatever text a row carries: inline fields plus, if present, the
    file the ``text_path`` points at.

    ``text_path`` resolution order for a relative path: the row's own run
    directory when it records one ("run_dir"/"_run_dir"), else ``round1_dir`` when
    the caller passes one, else CWD. Production rows do NOT carry a run_dir key, so
    the ``round1_dir`` fallback is what actually anchors those reads instead of
    silently resolving against CWD."""
    parts = []
    for key in ("text", "brief", "title"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    # "highlights" is a list of extracted snippet strings on retrieval rows.
    hl = row.get("highlights")
    if isinstance(hl, list):
        for h in hl:
            if isinstance(h, str) and h.strip():
                parts.append(h)
    tp = row.get("text_path")
    if isinstance(tp, str) and tp.strip():
        p = Path(tp)
        if not p.is_absolute():
            base = row.get("run_dir") or row.get("_run_dir") or round1_dir
            if base:
                p = Path(base) / tp
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
    return "\n".join(parts)


def corpus_support(claim: str, rows: list, threshold: float = 0.3, round1_dir=None):
    """$0 token-overlap CANDIDATE FINDER (not a support verdict).

    Score each row by how much of the claim's tokens it covers (claim-coverage
    ratio: shared tokens / claim tokens). Return the best row scoring at or above
    ``threshold`` (default 0.3), annotated with its score, else None.

    This only NOMINATES a best-overlapping candidate; it does NOT decide support
    (overlap cannot tell "X causes Y" from "X does not cause Y"), so its result is
    advisory only. Nothing auto-promotes a memory claim: fenced background stays
    fenced unless a human cites it by hand.

    A row is a dict that may carry text under "text", "brief", "title",
    "highlights" (a list of snippet strings), or a "text_path" pointing at a
    sources/*.txt file — whatever is available is read. ``round1_dir`` anchors a
    relative "text_path" for production rows that lack a run_dir key.
    """
    claim_tokens = _tokenize(claim)
    if not claim_tokens:
        return None
    best = None
    best_score = 0.0
    for row in rows:
        row_tokens = _tokenize(_row_text(row, round1_dir=round1_dir))
        if not row_tokens:
            continue
        shared = claim_tokens & row_tokens
        score = len(shared) / len(claim_tokens)
        if score > best_score:
            best_score = score
            best = row
    if best is not None and best_score >= threshold:
        result = dict(best)
        result["_support_score"] = round(best_score, 4)
        return result
    return None
