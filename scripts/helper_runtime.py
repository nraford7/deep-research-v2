"""Shared compatibility and mutation guards for low-level research helpers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
from typing import Iterator

from scripts.run_layout import LayoutError, LayoutKind, RunLayout
from scripts.run_transactions import ImmutableRegistry, RunLease, guard_legacy_mutation


class ManagedHelperRequired(RuntimeError):
    pass


_BROKER_MANAGED: ContextVar[bool] = ContextVar("deeper_research_broker_managed", default=False)


@contextmanager
def broker_managed_context():
    token = _BROKER_MANAGED.set(True)
    try:
        yield
    finally:
        _BROKER_MANAGED.reset(token)


def is_broker_managed() -> bool:
    """Return whether the current in-process helper call owns the run lease."""

    return _BROKER_MANAGED.get()


def require_managed_mutation(layout: RunLayout, operation: str) -> None:
    if layout.kind is LayoutKind.V2 and not is_broker_managed():
        raise ManagedHelperRequired(
            f"{operation} targets managed v2 run {layout.run_root}; use run_manager invoke-helper"
        )


def resolve_helper_layout(
    run_dir: os.PathLike[str] | str,
    *,
    allow_unmanaged: bool = True,
) -> RunLayout:
    """Open a caller-supplied run without creating or migrating anything."""

    try:
        return RunLayout.open(run_dir, allow_unmanaged=allow_unmanaged)
    except LayoutError as exc:
        # Historical standalone helpers accepted any existing scratch directory,
        # including one already holding an input file.  Preserve that deliberate
        # compatibility without weakening RunLayout.open() or allowing creation.
        root = Path(run_dir).resolve(strict=False)
        if allow_unmanaged and root.is_dir() and exc.state == "invalid":
            return RunLayout(root, LayoutKind.UNMANAGED)
        raise


def enclosing_layout(path: os.PathLike[str] | str) -> RunLayout | None:
    target = Path(path).resolve(strict=False)
    current = target if target.is_dir() else target.parent
    for candidate in (current, *current.parents):
        if not candidate.exists() or not candidate.is_dir():
            continue
        try:
            return RunLayout.open(candidate)
        except LayoutError:
            continue
    return None


@contextmanager
def standalone_mutation_guard(
    output: os.PathLike[str] | str,
    *,
    operation: str,
) -> Iterator[RunLayout | None]:
    """Protect compatibility flags from becoming a managed-run write bypass."""

    layout = enclosing_layout(output)
    if layout is None:
        yield None
        return
    if layout.kind is LayoutKind.V2:
        raise ManagedHelperRequired(
            f"{operation} targets managed v2 run {layout.run_root}; use run_manager invoke-helper"
        )
    library = layout.run_root.parent
    guard_legacy_mutation(output, library)
    lease = RunLease.acquire(library, layout.run_root.name, operation=operation)
    try:
        yield layout
    finally:
        lease.release(lease.owner.token)


__all__ = [
    "ManagedHelperRequired",
    "broker_managed_context",
    "enclosing_layout",
    "is_broker_managed",
    "resolve_helper_layout",
    "require_managed_mutation",
    "standalone_mutation_guard",
]
