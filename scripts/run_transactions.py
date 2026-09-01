"""Leases, immutable ancestry, inventories, journals, and run publication."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import ctypes
import errno
import fcntl
import hashlib
import importlib
import json
import math
import multiprocessing
import threading
from multiprocessing import process as multiprocessing_process
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Iterator, Mapping

from scripts.run_fs import UnsafePathError
from scripts.run_layout import (
    LAYOUT_VERSION,
    SLUG_VERSION,
    UNICODE_VERSION,
    RunLayout,
    portable_collision_key,
    safe_relpath,
    slugify_v1,
)


class LockError(RuntimeError):
    pass


class LeaseConflict(LockError):
    pass


class TransactionConflict(RuntimeError):
    pass


class JournalError(RuntimeError):
    pass


class BrokerProtocolError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TreeEntry:
    relative: str
    kind: str
    size: int
    mode: int
    mtime_ns: int
    sha256: str | None
    device: int
    inode: int
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class TreeDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]


@dataclass(frozen=True)
class TreeInventory:
    root: Path
    root_device: int
    entries: Mapping[str, TreeEntry]
    root_digest: str

    @classmethod
    def capture(cls, root: os.PathLike[str] | str) -> "TreeInventory":
        root_path = Path(root).resolve(strict=True)
        root_info = os.lstat(root_path)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise UnsafePathError(f"inventory root is not an ordinary directory: {root_path}")
        entries: dict[str, TreeEntry] = {}

        def visit(directory: Path, prefix: str) -> str:
            directory_info = os.lstat(directory)
            if directory_info.st_dev != root_info.st_dev:
                raise UnsafePathError(f"mount/device boundary encountered: {prefix or '.'}")
            names = tuple(sorted(child.name for child in os.scandir(directory)))
            child_digests: list[tuple[str, str]] = []
            for name in names:
                child_path = directory / name
                relative = f"{prefix}/{name}" if prefix else name
                info = os.lstat(child_path)
                if stat.S_ISLNK(info.st_mode):
                    raise UnsafePathError(f"symlink encountered in inventory: {relative}")
                if info.st_dev != root_info.st_dev:
                    raise UnsafePathError(f"mount/device boundary encountered: {relative}")
                if stat.S_ISDIR(info.st_mode):
                    digest = visit(child_path, relative)
                    entry = TreeEntry(
                        relative,
                        "directory",
                        0,
                        stat.S_IMODE(info.st_mode),
                        info.st_mtime_ns,
                        digest,
                        info.st_dev,
                        info.st_ino,
                        tuple(sorted(grandchild.name for grandchild in os.scandir(child_path))),
                    )
                elif stat.S_ISREG(info.st_mode):
                    hasher = hashlib.sha256()
                    with child_path.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
                    entry = TreeEntry(
                        relative,
                        "file",
                        info.st_size,
                        stat.S_IMODE(info.st_mode),
                        info.st_mtime_ns,
                        digest,
                        info.st_dev,
                        info.st_ino,
                    )
                else:
                    raise UnsafePathError(f"special file encountered in inventory: {relative}")
                entries[relative] = entry
                child_digests.append((name, digest))
            return _canonical_digest({"kind": "directory", "members": child_digests})

        root_digest = visit(root_path, "")
        return cls(root_path, root_info.st_dev, entries, root_digest)

    def diff(self, other: "TreeInventory") -> TreeDiff:
        before = set(self.entries)
        after = set(other.entries)
        shared = before & after
        changed = tuple(sorted(path for path in shared if self.entries[path] != other.entries[path]))
        return TreeDiff(tuple(sorted(after - before)), tuple(sorted(before - after)), changed)

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "root_device": self.root_device,
            "root_digest": self.root_digest,
            "entries": {path: asdict(entry) for path, entry in sorted(self.entries.items())},
        }


@dataclass(frozen=True)
class JournalRecord:
    sequence: int
    transaction_id: str
    run_id: str
    state: str
    payload: Mapping[str, Any]
    payload_digest: str
    previous_digest: str | None
    record_digest: str
    recorded_at: str


@dataclass
class Journal:
    path: Path
    transaction_id: str
    run_id: str
    records: tuple[JournalRecord, ...] = ()

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.exists() and not self.records:
            loaded = self.load(self.path)
            if loaded.transaction_id != self.transaction_id or loaded.run_id != self.run_id:
                raise JournalError("journal identity does not match")
            self.records = loaded.records

    def append(self, state: str, payload: Mapping[str, Any]) -> JournalRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sequence = len(self.records) + 1
        previous = self.records[-1].record_digest if self.records else None
        payload_data = dict(payload)
        base = {
            "sequence": sequence,
            "transaction_id": self.transaction_id,
            "run_id": self.run_id,
            "state": state,
            "payload": payload_data,
            "payload_digest": _canonical_digest(payload_data),
            "previous_digest": previous,
            "recorded_at": _iso(),
        }
        digest = _canonical_digest(base)
        row = dict(base, record_digest=digest)
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.path.parent)
        record = JournalRecord(**row)
        self.records = (*self.records, record)
        return record

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> "Journal":
        journal_path = Path(path)
        records: list[JournalRecord] = []
        previous: str | None = None
        identity: tuple[str, str] | None = None
        for line_number, line in enumerate(journal_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalError(f"invalid journal JSON on line {line_number}") from exc
            received_digest = row.pop("record_digest", None)
            calculated = _canonical_digest(row)
            if received_digest != calculated:
                raise JournalError(f"record digest mismatch on line {line_number}")
            if row.get("payload_digest") != _canonical_digest(row.get("payload", {})):
                raise JournalError(f"payload digest mismatch on line {line_number}")
            if row.get("sequence") != line_number or row.get("previous_digest") != previous:
                raise JournalError(f"journal chain mismatch on line {line_number}")
            current_identity = (row.get("transaction_id"), row.get("run_id"))
            if identity is None:
                identity = current_identity
            elif identity != current_identity:
                raise JournalError("journal transaction identity changed")
            record = JournalRecord(**row, record_digest=received_digest)
            records.append(record)
            previous = received_digest
        if not records or identity is None:
            raise JournalError("journal is empty")
        return cls(journal_path, identity[0], identity[1], tuple(records))


@dataclass(frozen=True)
class LeaseOwner:
    token: str
    lease_id: str
    host: str
    boot_id: str
    keeper_pid: int
    keeper_process_start: str
    operation: str
    created_at: str
    renewed_until: str


def _boot_id() -> str:
    proc = Path("/proc/sys/kernel/random/boot_id")
    if proc.is_file():
        return proc.read_text(encoding="ascii").strip()
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{socket.gethostname()}:{sys.platform}"))


@contextmanager
def _registry_guard(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@dataclass
class RunLease:
    library: Path
    key: str
    owner: LeaseOwner
    shared: bool

    @property
    def _key_id(self) -> str:
        return hashlib.sha256(portable_collision_key(self.key).encode("utf-8")).hexdigest()

    @property
    def _lease_dir(self) -> Path:
        return self.library / ".locks" / "leases" / self._key_id

    @property
    def _holders_path(self) -> Path:
        return self._lease_dir / "holders.json"

    @property
    def _guard_path(self) -> Path:
        return self.library / ".locks" / "leases" / ".guard"

    @classmethod
    def acquire(
        cls,
        library: os.PathLike[str] | str,
        key: str,
        *,
        operation: str,
        shared: bool = False,
        ttl_seconds: float = 30.0,
        audited_takeover: bool = False,
    ) -> "RunLease":
        library_path = Path(library).resolve(strict=True)
        if not library_path.is_dir():
            raise LockError("lease library is not a directory")
        now = _utc_now()
        owner = LeaseOwner(
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            lease_id=str(uuid.uuid4()),
            host=socket.gethostname(),
            boot_id=_boot_id(),
            keeper_pid=os.getpid(),
            keeper_process_start=f"{os.getpid()}:{time.monotonic_ns()}",
            operation=operation,
            created_at=_iso(now),
            renewed_until=_iso(now + timedelta(seconds=ttl_seconds)),
        )
        candidate = cls(library_path, key, owner, shared)
        with _registry_guard(candidate._guard_path):
            candidate._lease_dir.mkdir(parents=True, exist_ok=True)
            holders = candidate._read_holders()
            active: list[dict[str, Any]] = []
            stale: list[dict[str, Any]] = []
            for holder in holders:
                if _parse_iso(holder["owner"]["renewed_until"]) > now:
                    active.append(holder)
                else:
                    stale.append(holder)
            if stale and not audited_takeover:
                raise LeaseConflict("expired holders require an explicit audited takeover")
            if active and (not shared or any(not holder["shared"] for holder in active)):
                raise LeaseConflict(f"lease {key!r} conflicts with an active holder")
            active.append({"owner": asdict(owner), "shared": shared})
            candidate._write_holders(active)
        return candidate

    def _read_holders(self) -> list[dict[str, Any]]:
        if not self._holders_path.exists():
            return []
        try:
            payload = json.loads(self._holders_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LockError("lease holder registry is corrupt") from exc
        if not isinstance(payload, list):
            raise LockError("lease holder registry is corrupt")
        return payload

    def _write_holders(self, holders: list[dict[str, Any]]) -> None:
        _atomic_json(self._holders_path, holders)

    def _matching_holder(self, holders: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
        for holder in holders:
            if holder.get("owner", {}).get("token") == token:
                return holder
        return None

    def verify(self, token: str, *, expected_owner: LeaseOwner | None = None) -> None:
        if token != self.owner.token:
            raise LockError("ownership token does not match")
        with _registry_guard(self._guard_path):
            holder = self._matching_holder(self._read_holders(), token)
            if holder is None:
                raise LockError("ownership token is no longer active")
            recorded = LeaseOwner(**holder["owner"])
            expected = expected_owner or self.owner
            identity = (
                recorded.lease_id,
                recorded.host,
                recorded.boot_id,
                recorded.keeper_pid,
                recorded.keeper_process_start,
            )
            expected_identity = (
                expected.lease_id,
                expected.host,
                expected.boot_id,
                expected.keeper_pid,
                expected.keeper_process_start,
            )
            if identity != expected_identity:
                raise LockError("lease owner identity does not match")
            if _parse_iso(recorded.renewed_until) <= _utc_now():
                raise LockError("lease has expired")

    def renew(self, token: str, *, ttl_seconds: float = 30.0) -> None:
        self.verify(token)
        with _registry_guard(self._guard_path):
            holders = self._read_holders()
            holder = self._matching_holder(holders, token)
            if holder is None:
                raise LockError("ownership token is no longer active")
            renewed = replace(self.owner, renewed_until=_iso(_utc_now() + timedelta(seconds=ttl_seconds)))
            holder["owner"] = asdict(renewed)
            self._write_holders(holders)
            self.owner = renewed

    def release(self, token: str) -> None:
        if token != self.owner.token:
            raise LockError("ownership token does not match")
        with _registry_guard(self._guard_path):
            holders = self._read_holders()
            if self._matching_holder(holders, token) is None:
                raise LockError("ownership token is no longer active")
            self._write_holders([holder for holder in holders if holder["owner"]["token"] != token])

    @classmethod
    def release_recorded(cls, library: Path, key: str, owner_data: Mapping[str, Any]) -> None:
        owner = LeaseOwner(**dict(owner_data))
        lease = cls(library, key, owner, False)
        try:
            lease.release(owner.token)
        except LockError:
            pass


_BROKER_FIELDS: dict[str, frozenset[str]] = {
    "publish-artifact": frozenset(
        {"action", "token", "logical_destination", "scratch_name", "sha256", "size", "stage"}
    ),
    "invoke-helper": frozenset({"action", "token", "helper_id", "args"}),
    "record-stage": frozenset({"action", "token", "stage", "manifest"}),
    "export": frozenset({"action", "token", "options"}),
    "finalize": frozenset({"action", "token"}),
    "mark-failed": frozenset({"action", "token", "reason"}),
    "renew": frozenset({"action", "token"}),
    "release": frozenset({"action", "token"}),
}
_FORBIDDEN_HELPER_ARG_KEY = re.compile(
    r"(?:^|_)(?:command|cmd|executable|environment|env|path|dir|root|output|destination)$",
    re.IGNORECASE,
)


def validate_broker_request(
    request: Mapping[str, Any],
    *,
    allowed_helpers: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise BrokerProtocolError("broker request must be an object")
    action = request.get("action")
    if action not in _BROKER_FIELDS:
        raise BrokerProtocolError("unknown broker action")
    expected = _BROKER_FIELDS[action]
    unknown = set(request) - expected
    missing = {"action", "token"} - set(request)
    if unknown or missing:
        raise BrokerProtocolError(f"invalid fields for {action}: unknown={sorted(unknown)}, missing={sorted(missing)}")
    if not isinstance(request.get("token"), str):
        raise BrokerProtocolError("broker token must be a string")
    if action == "publish-artifact":
        if not isinstance(request.get("logical_destination"), str):
            raise BrokerProtocolError("logical destination must be a safe relative path")
        safe_relpath(request["logical_destination"])
        scratch_name = request.get("scratch_name")
        if not isinstance(scratch_name, str):
            raise BrokerProtocolError("scratch_name must be a safe relative path")
        safe_relpath(scratch_name)
        digest = request.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise BrokerProtocolError("publish digest must be lowercase SHA-256")
        if not isinstance(request.get("size"), int) or request["size"] < 0:
            raise BrokerProtocolError("publish size must be a nonnegative integer")
        if not isinstance(request.get("stage"), str):
            raise BrokerProtocolError("publish stage must be a string")
    elif action == "invoke-helper":
        helper_id = request.get("helper_id")
        if not isinstance(helper_id, str) or allowed_helpers is None or helper_id not in allowed_helpers:
            raise BrokerProtocolError("helper ID is not allowlisted")
        arguments = request.get("args")
        if not isinstance(arguments, dict):
            raise BrokerProtocolError("helper args must be an object")
        unknown_args = set(arguments) - set(allowed_helpers[helper_id])
        if unknown_args:
            raise BrokerProtocolError(f"unknown typed helper arguments: {sorted(unknown_args)}")

        def reject_escape_keys(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if _FORBIDDEN_HELPER_ARG_KEY.search(str(key)):
                        raise BrokerProtocolError(f"helper argument may not carry {key!r}")
                    reject_escape_keys(child)
            elif isinstance(value, list):
                for child in value:
                    reject_escape_keys(child)

        reject_escape_keys(arguments)
    elif action == "record-stage":
        if not isinstance(request.get("stage"), str) or not isinstance(request.get("manifest"), dict):
            raise BrokerProtocolError("record-stage requires a stage and typed manifest object")
    elif action == "export" and not isinstance(request.get("options"), dict):
        raise BrokerProtocolError("export options must be an object")
    elif action == "mark-failed" and not isinstance(request.get("reason"), str):
        raise BrokerProtocolError("mark-failed reason must be a string")
    return dict(request)


def _default_broker_dispatch(request: Mapping[str, Any], context: Mapping[str, Any], lease: RunLease) -> Any:
    return {"action": request["action"], "context": dict(context)}


def _process_start_identity(pid: int) -> str | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            return proc_stat.read_text(encoding="ascii").split()[21]
        except (OSError, IndexError):
            return None
    try:
        return subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class _AlreadyAnswered(Exception):
    """Control flow: the request was rejected before dispatch; only the reply is owed."""


class BrokerStop(BrokerProtocolError):
    """The current request is answered with this error and then the broker EXITS:
    it can no longer vouch for the run (lease lost) or for its own worker (helper
    past its deadline). With hard_exit=True a worker thread is still alive, so the
    process ends immediately WITHOUT releasing the lease — the holder ages out via
    the TTL instead, so no new owner can acquire while the abandoned worker could
    still write; the daemon thread dies with the process."""

    def __init__(self, message: str, *, hard_exit: bool = False) -> None:
        super().__init__(message)
        self.hard_exit = hard_exit


# A helper that never returns must not make the broker (and its lease) immortal.
# Per-run override: context["helper_deadline_seconds"] (finite, > 0; else default).
BROKER_HELPER_DEADLINE: float = 7200.0
# Total time a client gets to deliver one framed request. The per-recv timeout is
# an inactivity timeout, so without this a one-byte-every-few-seconds client could
# hold the (single-threaded, not-yet-authenticated) request loop past the TTL.
# Per-run override: context["request_deadline_seconds"].
BROKER_REQUEST_DEADLINE: float = 10.0


def _positive_finite(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number > 0 else default


def _helper_deadline(context: Mapping[str, Any]) -> float:
    return _positive_finite(context.get("helper_deadline_seconds"), BROKER_HELPER_DEADLINE)


def _request_deadline(context: Mapping[str, Any]) -> float:
    return _positive_finite(context.get("request_deadline_seconds"), BROKER_REQUEST_DEADLINE)


def _safe_str(exc: BaseException) -> str:
    try:
        text = str(exc)
    except BaseException:  # a __str__ that raises ANYTHING must not escape a failure path
        text = ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _dispatch_keeping_lease_alive(dispatcher, validated, context, lease, ttl_seconds: float):
    """Run the (synchronous, minutes-long) helper in a worker thread while this
    thread renews the lease every ttl/3. Requests stay serialized — one helper at a
    time — but the holder no longer expires under a helper that outlives the TTL
    (production TTL is 300 s; slice-search and full-text fetches run longer).

    Two ways this stops early, both raising BrokerStop(hard_exit=True) — answer if
    the client is still there, then end the process immediately WITHOUT releasing
    the lease (the holder ages out via the TTL), taking the daemon worker down:
    - renewal fails (holder gone: suspend past the TTL, an audited takeover) — a new
      owner may already hold the run, so the worker must not keep writing;
    - the helper passes its deadline — a hung helper must not make the lease
      immortal."""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = dispatcher(validated, context, lease)
        except BaseException as exc:  # re-raised on the broker thread below
            box["error"] = exc

    deadline = _helper_deadline(context)
    worker = threading.Thread(target=_run, name="broker-helper", daemon=True)
    started = time.monotonic()
    worker.start()
    renew_every = max(0.01, ttl_seconds / 3)   # stays below any accepted TTL
    while worker.is_alive():
        # poll no later than the deadline itself, so a small deadline is not
        # stretched to the next ttl/3 tick
        remaining = deadline - (time.monotonic() - started)
        worker.join(timeout=max(0.01, min(renew_every, remaining)))
        if not worker.is_alive():
            break
        if time.monotonic() - started > deadline:
            raise BrokerStop(
                f"helper {validated.get('helper_id', validated.get('action'))!s} passed its {deadline:g}s deadline"
                " — broker stopping without releasing", hard_exit=True)
        try:
            lease.renew(lease.owner.token, ttl_seconds=ttl_seconds)
        except Exception as exc:
            # The holder is gone (suspend past the TTL, an audited takeover): a new
            # owner may already be writing. Exclusivity beats a clean finish — stop
            # NOW and take the worker down with the process; never drain it.
            raise BrokerStop(f"lease lost while the helper ran ({_safe_str(exc)}) — broker stopping at once", hard_exit=True)
    if "error" in box:
        # Whatever the helper raised (any BaseException) is THIS request's failure.
        raise BrokerProtocolError(_safe_str(box["error"]))
    return box.get("result")


def _broker_main(
    connection: Any,
    library: str,
    key: str,
    operation: str,
    shared: bool,
    ttl_seconds: float,
    dispatcher_module: str | None,
    dispatcher_name: str | None,
    context: Mapping[str, Any],
    allowed_helpers: Mapping[str, frozenset[str]],
    parent_pid: int,
    parent_start: str | None,
) -> None:
    lease: RunLease | None = None
    server: socket.socket | None = None
    socket_path: Path | None = None
    try:
        lease = RunLease.acquire(
            library,
            key,
            operation=operation,
            shared=shared,
            ttl_seconds=ttl_seconds,
            audited_takeover=False,
        )
        socket_path = Path("/tmp") / f"drb-{hashlib.sha256(lease.owner.lease_id.encode()).hexdigest()[:24]}.sock"
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        previous_umask = os.umask(0o177)
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
        finally:
            os.umask(previous_umask)
        server.listen(8)
        server.settimeout(max(0.05, min(ttl_seconds / 3, 1.0)))
        connection.send({"ok": True, "owner": asdict(lease.owner), "socket_path": str(socket_path)})
        if dispatcher_module and dispatcher_name:
            dispatcher = getattr(importlib.import_module(dispatcher_module), dispatcher_name)
        else:
            dispatcher = _default_broker_dispatch
        running = True
        while running:
            parent_present = os.getppid() == parent_pid and _process_start_identity(parent_pid) == parent_start
            try:
                client, _ = server.accept()
            except TimeoutError:
                if parent_present:
                    lease.renew(lease.owner.token, ttl_seconds=ttl_seconds)
                elif _parse_iso(lease.owner.renewed_until) <= _utc_now():
                    break
                continue
            with client:
                # Everything about ONE client connection is contained here. A client
                # that times out and hangs up (helpers run for minutes; the CLI used
                # to give them 5 s) makes the reply write fail with EPIPE/ECONNRESET —
                # that is the client's loss, never a reason for the broker to die.
                # 2026-09-01: exactly that write escaped to the fatal handler below,
                # unlinked the socket and released the lease mid-run ("keeper died").
                received = bytearray()
                request: Any = None
                response: dict[str, Any] | None = None
                hard_exit = False
                request_deadline = time.monotonic() + _request_deadline(context)
                try:
                    while b"\n" not in received:
                        remaining = request_deadline - time.monotonic()
                        if remaining <= 0:
                            raise BrokerProtocolError("request not delivered within the broker request deadline")
                        client.settimeout(min(5.0, remaining))
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        received.extend(chunk)
                        if len(received) > 1024 * 1024:
                            raise BrokerProtocolError("broker request exceeds one MiB")
                except OSError as exc:  # includes socket.timeout on a silent client
                    if time.monotonic() >= request_deadline:
                        # the inactivity timeout fired right at the deadline: answer, don't just drop
                        response = {"ok": False, "error": "request not delivered within the broker request deadline", "error_type": "BrokerProtocolError"}
                    else:
                        print(f"broker: dropped a client that sent no complete request ({type(exc).__name__}); continuing", file=sys.stderr)
                        continue
                except BrokerProtocolError as exc:
                    response = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
                if response is None and b"\n" not in received:
                    # EOF before the newline frame: the client gave up mid-send. Never
                    # dispatch an unframed request — a retry would duplicate a mutation
                    # or metered API spend.
                    response = {"ok": False, "error": "incomplete broker request (no newline terminator)", "error_type": "BrokerProtocolError"}
                try:
                    if response is not None:
                        raise _AlreadyAnswered()
                    request = json.loads(bytes(received).split(b"\n", 1)[0].decode("utf-8"))
                    validated = validate_broker_request(request, allowed_helpers=allowed_helpers)
                    if validated["token"] != lease.owner.token:
                        raise BrokerProtocolError("broker authentication failed")
                    if validated["action"] == "release":
                        response = {"ok": True, "result": {"released": True}}
                        running = False
                    else:
                        try:
                            lease.renew(lease.owner.token, ttl_seconds=ttl_seconds)
                        except Exception as exc:
                            # no worker is running yet, so a normal stop is enough:
                            # answer, leave the loop, release what (if anything) is ours
                            raise BrokerStop(f"lease lost before dispatch ({_safe_str(exc)}) — broker stopping")
                        if validated["action"] == "renew":
                            result = {"renewed_until": lease.owner.renewed_until}
                        else:
                            result = _dispatch_keeping_lease_alive(dispatcher, validated, context, lease, ttl_seconds)
                        response = {"ok": True, "result": result}
                        if validated["action"] == "finalize":
                            running = False
                except _AlreadyAnswered:
                    pass
                except BrokerStop as exc:
                    response = {"ok": False, "error": str(exc), "error_type": "BrokerStop"}
                    running = False
                    hard_exit = exc.hard_exit
                except (Exception, SystemExit) as exc:
                    # SystemExit: a helper's argparse rejecting a value exits(2) —
                    # that is the request's failure, not the broker's. (Worker-thread
                    # exceptions of ANY kind arrive here already wrapped.)
                    response = {"ok": False, "error": _safe_str(exc), "error_type": type(exc).__name__}
                try:
                    # default=str: a helper returning a Path/set/dataclass field must
                    # not turn a finished job into a dead broker. Any other failure to
                    # serialize (a __str__ that raises, RecursionError on a pathological
                    # result) is this request's error, never the broker's.
                    encoded = (json.dumps(response, sort_keys=True, default=str) + "\n").encode("utf-8")
                except BaseException as exc:  # a __str__ raising SystemExit is still this request's problem
                    encoded = (json.dumps({"ok": False, "error": f"unserializable broker response: {_safe_str(exc)}", "error_type": "TypeError"}) + "\n").encode("utf-8")
                try:
                    client.sendall(encoded)
                except OSError as exc:
                    if not hard_exit:
                        # Only the reply was lost; whatever this request did (or failed
                        # to do) is recorded in the response we could not deliver.
                        action = request.get("action", "?") if isinstance(request, dict) else "?"
                        outcome = "ok" if response.get("ok") else f"error: {str(response.get('error', ''))[:120]}"
                        print(f"broker: client hung up before the reply for {action!s} could be delivered ({type(exc).__name__}); undelivered outcome = {outcome}; broker {'stopping' if not running else 'continues'}", file=sys.stderr)
                if hard_exit:
                    # A worker thread may still be alive: end the process NOW — whether
                    # or not the reply could be delivered — without releasing the lease
                    # (it ages out via the TTL), so no new owner can overlap a write the
                    # abandoned worker might still make.
                    try:
                        print("broker: hard exit with a live worker — lease left to expire", file=sys.stderr)
                    except Exception:
                        pass
                    try:
                        server.close()
                        if socket_path is not None:
                            socket_path.unlink()
                    except OSError:
                        pass
                    os._exit(70)
        try:
            lease.release(lease.owner.token)
        except LockError as exc:  # already gone (lease lost mid-helper) — nothing left to release
            print(f"broker: lease not released on exit ({exc})", file=sys.stderr)
        lease = None
    except BaseException as exc:
        # Fatal: say why in the broker log (stderr) — a silent death is what made
        # the 2026-09-01 keeper deaths look like a mystery.
        print(f"broker: exiting on {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            connection.send({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        except Exception:
            pass
    finally:
        if server is not None:
            server.close()
        if socket_path is not None:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
        if lease is not None:
            try:
                lease.release(lease.owner.token)
            except LockError:
                pass
        connection.close()


@dataclass
class LocalBrokerClient:
    library: Path
    key: str
    owner: LeaseOwner
    socket_path: Path
    process: multiprocessing.Process

    def request(self, action: str, **payload: Any) -> Any:
        result = broker_request(self.socket_path, self.owner.token, action, **payload)
        if action == "renew" and isinstance(result, dict):
            renewed = result.get("renewed_until")
            if isinstance(renewed, str):
                self.owner = replace(self.owner, renewed_until=renewed)
        return result

    def release(self) -> None:
        if self.process.is_alive():
            self.request("release")
            self.process.join(timeout=5)
        if self.process.is_alive():
            raise BrokerProtocolError("broker did not stop after authenticated release")


# Connecting to and writing into the broker socket is local and instantaneous —
# 5 s is generous. WAITING for the reply is not: invoke-helper runs Exa slices,
# full-text fetches and citation chases that legitimately take minutes, so the
# reply wait defaults to the broker's helper deadline + 300 s (a stuck helper
# still surfaces as a timeout rather than a hung CLI) and is settable per call
# (reply_timeout; None blocks). The default assumes the usual shape — one
# orchestrator issuing helpers one at a time; a request queued behind another
# long helper counts its wait from send, so concurrent clients should pass a
# larger reply_timeout or serialize.
# Until 2026-09-01 a single 5 s timeout covered both, so every long helper
# "timed out" client-side while its work completed server-side.
BROKER_CONNECT_TIMEOUT: float = 5.0
BROKER_REPLY_TIMEOUT: float | None = BROKER_HELPER_DEADLINE + 300.0  # outlast the broker's own give-up point: a client that quits first would retry into a duplicate dispatch
_USE_DEFAULT = object()


def broker_request(
    endpoint: os.PathLike[str] | str,
    token: str,
    action: str,
    *,
    reply_timeout: float | None | object = _USE_DEFAULT,
    **payload: Any,
) -> Any:
    if reply_timeout is _USE_DEFAULT:
        reply_timeout = BROKER_REPLY_TIMEOUT
    if reply_timeout is not None:
        # validate BEFORE anything is sent: a bad value must not become an
        # ambiguous "request delivered, client errored" retry hazard
        if isinstance(reply_timeout, bool) or not isinstance(reply_timeout, (int, float)):
            raise ValueError(f"reply_timeout must be a number of seconds or None, not {reply_timeout!r}")
        if not math.isfinite(reply_timeout) or reply_timeout < 0:
            raise ValueError(f"reply_timeout must be finite and non-negative, not {reply_timeout!r}")
    request = {"action": action, "token": token, **payload}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(BROKER_CONNECT_TIMEOUT)
        client.connect(os.fspath(endpoint))
        client.sendall((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
        client.settimeout(reply_timeout)  # None = block until the helper answers
        received = bytearray()
        while b"\n" not in received:
            chunk = client.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
        response = json.loads(bytes(received).split(b"\n", 1)[0].decode("utf-8"))
    finally:
        client.close()
    if not response.get("ok"):
        message = response.get("error", "broker request failed")
        if response.get("error_type") == "BrokerStop":
            # the broker answered and then STOPPED (lease lost / helper deadline):
            # callers must not read this as an ordinary validation rejection
            raise BrokerStop(message)
        raise BrokerProtocolError(message)
    return response.get("result")


def start_local_broker(
    library: os.PathLike[str] | str,
    key: str,
    *,
    operation: str,
    shared: bool = False,
    ttl_seconds: float = 30.0,
    dispatcher_module: str | None = None,
    dispatcher_name: str | None = None,
    context: Mapping[str, Any] | None = None,
    allowed_helpers: Mapping[str, frozenset[str]] | None = None,
    detached: bool = False,
) -> LocalBrokerClient:
    if detached:
        return _start_detached_broker(
            library,
            key,
            operation=operation,
            shared=shared,
            ttl_seconds=ttl_seconds,
            dispatcher_module=dispatcher_module,
            dispatcher_name=dispatcher_name,
            context=context,
            allowed_helpers=allowed_helpers,
        )
    receive, send = multiprocessing.Pipe(duplex=False)
    parent_pid = os.getpid()
    parent_start = _process_start_identity(parent_pid)
    process = multiprocessing.Process(
        target=_broker_main,
        args=(
            send,
            os.fspath(Path(library).resolve(strict=True)),
            key,
            operation,
            shared,
            ttl_seconds,
            dispatcher_module,
            dispatcher_name,
            dict(context or {}),
            dict(allowed_helpers or {}),
            parent_pid,
            parent_start,
        ),
        daemon=False,
    )
    process.start()
    send.close()
    if not receive.poll(5):
        process.terminate()
        process.join(timeout=2)
        raise BrokerProtocolError("broker did not become ready")
    ready = receive.recv()
    receive.close()
    if not ready.get("ok"):
        process.join(timeout=2)
        raise BrokerProtocolError(ready.get("error", "broker startup failed"))
    # The broker is deliberately lease-owned rather than parent-lifetime-owned. Remove
    # it from multiprocessing's atexit join set so a short-lived CLI can return the
    # endpoint immediately; once orphaned it serves authenticated requests until the
    # renewable lease expires, then removes its holder and socket.
    multiprocessing_process._children.discard(process)
    return LocalBrokerClient(
        Path(library).resolve(strict=True),
        key,
        LeaseOwner(**ready["owner"]),
        Path(ready["socket_path"]),
        process,
    )


@dataclass
class _DetachedProcess:
    popen: subprocess.Popen[Any]

    @property
    def pid(self) -> int:
        return self.popen.pid

    def is_alive(self) -> bool:
        return self.popen.poll() is None

    def join(self, timeout: float | None = None) -> None:
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


class _ReadyFileConnection:
    def __init__(self, ready_path: Path):
        self.ready_path = ready_path
        self.sent = False

    def send(self, payload: Mapping[str, Any]) -> None:
        if not self.sent:
            _atomic_json(self.ready_path, dict(payload))
            self.sent = True

    def close(self) -> None:
        pass


def _serve_detached_broker(config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    connection = _ReadyFileConnection(Path(config["ready_path"]))
    _broker_main(
        connection,
        config["library"],
        config["key"],
        config["operation"],
        bool(config["shared"]),
        float(config["ttl_seconds"]),
        config.get("dispatcher_module"),
        config.get("dispatcher_name"),
        config.get("context", {}),
        {key: frozenset(value) for key, value in config.get("allowed_helpers", {}).items()},
        0,
        None,
    )


def _start_detached_broker(
    library: os.PathLike[str] | str,
    key: str,
    *,
    operation: str,
    shared: bool,
    ttl_seconds: float,
    dispatcher_module: str | None,
    dispatcher_name: str | None,
    context: Mapping[str, Any] | None,
    allowed_helpers: Mapping[str, frozenset[str]] | None,
) -> LocalBrokerClient:
    library_path = Path(library).resolve(strict=True)
    launch_id = str(uuid.uuid4())
    control_root = library_path / ".transactions" / f"broker-{launch_id}"
    control_root.mkdir(parents=True, mode=0o700)
    config_path = control_root / "config.json"
    ready_path = control_root / "ready.json"
    log_path = control_root / "broker.log"
    _atomic_json(
        config_path,
        {
            "library": str(library_path),
            "key": key,
            "operation": operation,
            "shared": shared,
            "ttl_seconds": ttl_seconds,
            "dispatcher_module": dispatcher_module,
            "dispatcher_name": dispatcher_name,
            "context": dict(context or {}),
            "allowed_helpers": {name: sorted(fields) for name, fields in (allowed_helpers or {}).items()},
            "ready_path": str(ready_path),
        },
    )
    repository_root = Path(__file__).resolve().parents[1]
    with log_path.open("ab") as log_handle:
        popen = subprocess.Popen(
            [sys.executable, "-m", "scripts.run_transactions", "--serve-broker", str(config_path)],
            cwd=repository_root,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ready_path.exists():
        if popen.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise BrokerProtocolError(f"detached broker failed to start: {detail.strip()}")
        time.sleep(0.02)
    if not ready_path.exists():
        popen.terminate()
        popen.wait(timeout=2)
        raise BrokerProtocolError("detached broker did not become ready")
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    if not ready.get("ok"):
        popen.wait(timeout=2)
        raise BrokerProtocolError(ready.get("error", "detached broker startup failed"))
    return LocalBrokerClient(
        library_path,
        key,
        LeaseOwner(**ready["owner"]),
        Path(ready["socket_path"]),
        _DetachedProcess(popen),  # type: ignore[arg-type]
    )


def _module_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-broker")
    arguments = parser.parse_args(argv)
    if arguments.serve_broker:
        _serve_detached_broker(Path(arguments.serve_broker))
        return 0
    return 2


@dataclass(frozen=True)
class ImmutableRecord:
    run_id: str
    relative_path: str
    device: int
    inode: int
    collision_key: str
    expected_root: str
    kind: str
    derivation_transaction: str | None
    recorded_at: str


class ImmutableRegistry:
    def __init__(self, library: os.PathLike[str] | str):
        self.library = Path(library).resolve(strict=True)
        self.root = self.library / ".locks" / "immutable"
        self.path = self.root / "registry.json"
        self.guard_path = self.root / ".guard"

    def _read(self) -> list[ImmutableRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return [ImmutableRecord(**item) for item in payload]
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise TransactionConflict("immutable registry is corrupt") from exc

    def _write(self, records: list[ImmutableRecord]) -> None:
        _atomic_json(self.path, [asdict(record) for record in records])

    def _identities(self, path: Path) -> tuple[str, tuple[int, int], str]:
        canonical = path.resolve(strict=True)
        try:
            relative = canonical.relative_to(self.library).as_posix()
        except ValueError as exc:
            raise TransactionConflict("immutable path is outside its library") from exc
        info = os.lstat(canonical)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise TransactionConflict("immutable run root must be an ordinary directory")
        components = relative.split("/")
        if any(part in {"", ".", ".."} or "\x00" in part for part in components):
            raise TransactionConflict("immutable path has an unsafe relative identity")
        # Existing legacy names may themselves be non-portable (for example a trailing
        # dot).  The registry must still be able to index them in order to block aliases.
        collision = "/".join(portable_collision_key(part) for part in components)
        return relative, (info.st_dev, info.st_ino), collision

    def record(
        self,
        path: os.PathLike[str] | str,
        *,
        kind: str,
        expected_root: str,
        run_id: str | None = None,
        derivation_transaction: str | None = None,
        journal: Journal | None = None,
    ) -> ImmutableRecord:
        target = Path(path)
        relative, identity, collision = self._identities(target)
        actual_root = TreeInventory.capture(target).root_digest
        if actual_root != expected_root:
            raise TransactionConflict("immutable record root digest does not match the current tree")
        record = ImmutableRecord(
            run_id or str(uuid.uuid4()),
            relative,
            identity[0],
            identity[1],
            collision,
            expected_root,
            kind,
            derivation_transaction,
            _iso(),
        )
        if journal is not None:
            journal.append("intent", {"operation": "immutable-record", "record": asdict(record)})
        with _registry_guard(self.guard_path):
            records = self._read()
            for existing in records:
                same_identity = (existing.device, existing.inode) == identity
                if existing.relative_path == relative or same_identity:
                    if existing.run_id != record.run_id and run_id is not None:
                        raise TransactionConflict("immutable identity already belongs to another run")
                    return existing
                if existing.collision_key == collision:
                    raise TransactionConflict("immutable collision identity already belongs to another run")
            records.append(record)
            self._write(records)
        if journal is not None:
            journal.append("complete", {"operation": "immutable-record", "run_id": record.run_id})
        return record

    def resolve(self, path: os.PathLike[str] | str) -> ImmutableRecord | None:
        target = Path(path)
        if not target.exists():
            return None
        relative, identity, collision = self._identities(target)
        records = self._read()
        matches = [
            record
            for record in records
            if record.relative_path == relative
            or (record.device, record.inode) == identity
            or record.collision_key == collision
        ]
        if not matches:
            return None
        if len({record.run_id for record in matches}) != 1:
            raise TransactionConflict("conflicting immutable registry identities")
        record = matches[0]
        if TreeInventory.capture(target).root_digest != record.expected_root:
            raise TransactionConflict("immutable run tree no longer matches its recorded root")
        return record


def guard_legacy_mutation(path: os.PathLike[str] | str, library: os.PathLike[str] | str) -> None:
    target = Path(path).resolve(strict=False)
    registry = ImmutableRegistry(library)
    current = target if target.is_dir() else target.parent
    while True:
        if current == registry.library:
            return
        if registry.resolve(current) is not None:
            raise UnsafePathError(f"legacy mutation is below immutable parent {current}")
        if registry.library not in current.parents:
            return
        current = current.parent


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    if os.stat(source.parent).st_dev != os.stat(destination.parent).st_dev:
        raise TransactionConflict("publication source and destination must share a filesystem")
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    result: int | None = None
    if hasattr(libc, "renameat2"):
        libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        libc.renameat2.restype = ctypes.c_int
        result = libc.renameat2(-100, source_bytes, -100, destination_bytes, 1)
    elif hasattr(libc, "renameatx_np"):
        libc.renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        libc.renameatx_np.restype = ctypes.c_int
        result = libc.renameatx_np(-2, source_bytes, -2, destination_bytes, 0x00000004)
    if result is None:
        raise TransactionConflict("atomic no-replace directory publication is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(destination)
        raise OSError(error, os.strerror(error), destination)
    _fsync_directory(destination.parent)


@dataclass(frozen=True)
class RecoveryOutcome:
    status: str
    run_path: Path | None


@dataclass
class CreationTransaction:
    library: Path
    slug: str
    question: str
    transaction_id: str
    run_id: str
    transaction_root: Path
    skeleton: Path
    destination: Path
    journal: Journal
    lease: RunLease
    crash_at: str | None = None

    def _crash(self, point: str) -> None:
        if self.crash_at == point:
            raise SystemExit(91)

    def publish(self) -> Path:
        self.lease.verify(self.lease.owner.token)
        self._crash("after-journal")
        inventory = TreeInventory.capture(self.skeleton)
        self.journal.append("complete", {"operation": "skeleton", "root": inventory.root_digest})
        self._crash("after-skeleton")
        self.journal.append("intent", {"operation": "publish", "destination": self.slug})
        self._crash("before-publish")
        try:
            _rename_directory_no_replace(self.skeleton, self.destination)
        except FileExistsError as exc:
            self.journal.append("abort", {"operation": "publish", "reason": "destination-exists"})
            self.lease.release(self.lease.owner.token)
            raise TransactionConflict(f"destination appeared while publishing: {self.destination}") from exc
        self.journal.append("complete", {"operation": "publish", "destination": self.slug})
        self._crash("after-publish")
        self.lease.release(self.lease.owner.token)
        return self.destination


def create_skeleton_transaction(
    library: os.PathLike[str] | str,
    slug: str,
    *,
    question: str,
    crash_at: str | None = None,
) -> CreationTransaction:
    library_path = Path(library).resolve(strict=True)
    safe_slug = slugify_v1(slug)
    transaction_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    transaction_root = library_path / ".transactions" / transaction_id
    skeleton_parent = transaction_root / "skeleton"
    skeleton = skeleton_parent / safe_slug
    transaction_root.mkdir(parents=True)
    _fsync_directory(transaction_root.parent)
    journal = Journal(transaction_root / "journal.jsonl", transaction_id, run_id)
    journal.append("intent", {"operation": "create", "slug": safe_slug})
    lease = RunLease.acquire(library_path, safe_slug, operation="create")
    skeleton.mkdir(parents=True)
    for relative in ("Sections", "Sources/Extracted", "Process/stages"):
        (skeleton / relative).mkdir(parents=True)
    now = _iso()
    metadata = {
        "layout_version": LAYOUT_VERSION,
        "schema_version": 1,
        "run_id": run_id,
        "slug": safe_slug,
        "slug_version": SLUG_VERSION,
        "unicode_version": UNICODE_VERSION,
        "question": question,
        "question_source": "user",
        "status": "incomplete",
        "completion_profile": "native-v2",
        "sealed": False,
        "frozen_for_derivation": False,
        "frozen_snapshot": None,
        "generation": 1,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "bible": None,
    }
    _atomic_json(skeleton / "Process" / "run.json", metadata)
    inventory = TreeInventory.capture(skeleton)
    _atomic_json(transaction_root / "inventory.json", inventory.to_json())
    _atomic_json(
        transaction_root / "transaction.json",
        {
            "transaction_id": transaction_id,
            "run_id": run_id,
            "slug": safe_slug,
            "question": question,
            "lease_owner": asdict(lease.owner),
            "inventory_root": inventory.root_digest,
        },
    )
    journal.append("intent", {"operation": "skeleton", "root": inventory.root_digest})
    return CreationTransaction(
        library_path,
        safe_slug,
        question,
        transaction_id,
        run_id,
        transaction_root,
        skeleton,
        library_path / safe_slug,
        journal,
        lease,
        crash_at,
    )


def publish_skeleton(library: os.PathLike[str] | str, slug: str, *, question: str) -> Path:
    return create_skeleton_transaction(library, slug, question=question).publish()


def _remove_verified_skeleton(skeleton: Path, expected_root: str) -> bool:
    if not skeleton.exists():
        return True
    try:
        current = TreeInventory.capture(skeleton)
    except UnsafePathError:
        return False
    if current.root_digest != expected_root:
        return False
    shutil.rmtree(skeleton)
    return True


def recover_creation(library: os.PathLike[str] | str, transaction_id: str) -> RecoveryOutcome:
    library_path = Path(library).resolve(strict=True)
    transaction_root = library_path / ".transactions" / transaction_id
    try:
        metadata = json.loads((transaction_root / "transaction.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JournalError(f"creation transaction metadata is unavailable: {transaction_id}") from exc
    journal = Journal.load(transaction_root / "journal.jsonl")
    slug = metadata["slug"]
    skeleton = transaction_root / "skeleton" / slug
    destination = library_path / slug
    RunLease.release_recorded(library_path, slug, metadata["lease_owner"])

    if destination.exists():
        try:
            RunLayout.open(destination)
        except Exception as exc:
            raise TransactionConflict("visible creation destination is invalid") from exc
        if not _remove_verified_skeleton(skeleton, metadata["inventory_root"]):
            raise TransactionConflict("published run is valid but temporary skeleton was modified")
        journal.append("recovered", {"operation": "publish", "status": "already-published"})
        return RecoveryOutcome("already-published", destination)

    if skeleton.exists():
        current = TreeInventory.capture(skeleton)
        if current.root_digest == metadata["inventory_root"]:
            recovery_lease = RunLease.acquire(
                library_path,
                slug,
                operation="recover-create",
                audited_takeover=True,
            )
            try:
                _rename_directory_no_replace(skeleton, destination)
                journal.append("recovered", {"operation": "publish", "status": "published"})
            finally:
                recovery_lease.release(recovery_lease.owner.token)
            return RecoveryOutcome("published", destination)
        if not _remove_verified_skeleton(skeleton, metadata["inventory_root"]):
            raise TransactionConflict("temporary skeleton differs from its manifest")

    journal.append("recovered", {"operation": "create", "status": "aborted"})
    return RecoveryOutcome("aborted", None)


__all__ = [
    "BrokerProtocolError",
    "CreationTransaction",
    "ImmutableRecord",
    "ImmutableRegistry",
    "Journal",
    "JournalError",
    "JournalRecord",
    "LeaseConflict",
    "LeaseOwner",
    "LocalBrokerClient",
    "LockError",
    "RecoveryOutcome",
    "RunLease",
    "TransactionConflict",
    "TreeDiff",
    "TreeEntry",
    "TreeInventory",
    "create_skeleton_transaction",
    "broker_request",
    "guard_legacy_mutation",
    "publish_skeleton",
    "recover_creation",
    "start_local_broker",
    "validate_broker_request",
]


if __name__ == "__main__":
    raise SystemExit(_module_main())
