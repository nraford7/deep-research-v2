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
import multiprocessing
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
        if not isinstance(request.get("logical_destination"), str) or any(
            marker in request["logical_destination"] for marker in ("/", "\\", "..")
        ):
            raise BrokerProtocolError("logical destination must be an allowlisted identifier")
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
            if os.getppid() != parent_pid or _process_start_identity(parent_pid) != parent_start:
                break
            try:
                client, _ = server.accept()
            except TimeoutError:
                lease.renew(lease.owner.token, ttl_seconds=ttl_seconds)
                continue
            with client:
                client.settimeout(5)
                received = bytearray()
                while b"\n" not in received:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)
                    if len(received) > 1024 * 1024:
                        raise BrokerProtocolError("broker request exceeds one MiB")
                try:
                    request = json.loads(bytes(received).split(b"\n", 1)[0].decode("utf-8"))
                    validated = validate_broker_request(request, allowed_helpers=allowed_helpers)
                    if validated["token"] != lease.owner.token:
                        raise BrokerProtocolError("broker authentication failed")
                    if validated["action"] == "release":
                        response = {"ok": True, "result": {"released": True}}
                        running = False
                    else:
                        lease.renew(lease.owner.token, ttl_seconds=ttl_seconds)
                        result = (
                            {"renewed_until": lease.owner.renewed_until}
                            if validated["action"] == "renew"
                            else dispatcher(validated, context, lease)
                        )
                        response = {"ok": True, "result": result}
                except Exception as exc:
                    response = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
                client.sendall((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
        lease.release(lease.owner.token)
        lease = None
    except BaseException as exc:
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
        request = {"action": action, "token": self.owner.token, **payload}
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(5)
            client.connect(str(self.socket_path))
            client.sendall((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
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
            raise BrokerProtocolError(response.get("error", "broker request failed"))
        if action == "renew" and isinstance(response.get("result"), dict):
            renewed = response["result"].get("renewed_until")
            if isinstance(renewed, str):
                self.owner = replace(self.owner, renewed_until=renewed)
        return response.get("result")

    def release(self) -> None:
        if self.process.is_alive():
            self.request("release")
            self.process.join(timeout=5)
        if self.process.is_alive():
            raise BrokerProtocolError("broker did not stop after authenticated release")


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
) -> LocalBrokerClient:
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
    return LocalBrokerClient(
        Path(library).resolve(strict=True),
        key,
        LeaseOwner(**ready["owner"]),
        Path(ready["socket_path"]),
        process,
    )


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
        if registry.resolve(current) is not None:
            raise UnsafePathError(f"legacy mutation is below immutable parent {current}")
        if current == registry.library:
            return
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
    "guard_legacy_mutation",
    "publish_skeleton",
    "recover_creation",
    "start_local_broker",
    "validate_broker_request",
]
