"""Descriptor-relative, no-follow filesystem operations for managed research runs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Any, Callable, Iterator, Protocol

from scripts.run_layout import safe_relpath


class UnsafePathError(RuntimeError):
    pass


class _ImmutableRegistry(Protocol):
    def resolve(self, path: os.PathLike[str] | str) -> Any | None: ...


@dataclass(frozen=True)
class RegularFile:
    relative: str
    size: int
    sha256: str


@dataclass
class _Walk:
    descriptors: list[int]
    links: list[tuple[int, str, tuple[int, int]]]
    parent_fd: int
    leaf: str
    root_device: int


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


@dataclass
class RootedFS:
    root: Path
    lease_token: str | None = None
    state_guard: Callable[[str, str], None] | None = None
    immutable_registry: _ImmutableRegistry | None = None
    test_hook: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve(strict=True)
        if not self.root.is_dir():
            raise UnsafePathError(f"root is not a directory: {self.root}")

    @staticmethod
    def _secure_primitives_available() -> bool:
        required = ("O_NOFOLLOW", "O_DIRECTORY")
        return os.name == "posix" and all(hasattr(os, name) for name in required)

    def _require_secure_primitives(self) -> None:
        if not self._secure_primitives_available():
            raise UnsafePathError("reliable descriptor-relative no-follow primitives are unavailable")

    def _guard(self, operation: str, relative: str, *, internal: bool = False) -> PurePosixPath:
        parsed = safe_relpath(relative)
        mutation = operation in {"write", "mkdir", "unlink", "rmdir", "rename", "copy"}
        if mutation and not internal:
            parts = tuple(part.casefold() for part in parsed.parts)
            if len(parts) >= 2 and parts[:2] == ("process", "inherited"):
                raise UnsafePathError("mutation below immutable inherited subtree is forbidden")
            if self.immutable_registry is not None and self.immutable_registry.resolve(self.root) is not None:
                raise UnsafePathError("mutation of an immutable run is forbidden")
        if self.state_guard is not None:
            self.state_guard(operation, parsed.as_posix())
        return parsed

    def _open_root(self) -> tuple[int, int]:
        self._require_secure_primitives()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            raise UnsafePathError("managed root is no longer a directory")
        return descriptor, info.st_dev

    def _walk_parent(self, relative: PurePosixPath, *, create_parents: bool = False) -> _Walk:
        root_fd, root_device = self._open_root()
        descriptors = [root_fd]
        links: list[tuple[int, str, tuple[int, int]]] = []
        current = root_fd
        try:
            for component in relative.parts[:-1]:
                try:
                    before = os.stat(component, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    os.mkdir(component, mode=0o755, dir_fd=current)
                    os.fsync(current)
                    before = os.stat(component, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise UnsafePathError(f"path component is not a directory: {component}")
                if before.st_dev != root_device:
                    raise UnsafePathError(f"path component crosses a device boundary: {component}")
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                opened = os.fstat(child)
                if _identity(opened) != _identity(before):
                    os.close(child)
                    raise UnsafePathError(f"path component identity changed while opening: {component}")
                links.append((current, component, _identity(opened)))
                descriptors.append(child)
                current = child
            return _Walk(descriptors, links, current, relative.parts[-1], root_device)
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @staticmethod
    def _close_walk(walk: _Walk) -> None:
        for descriptor in reversed(walk.descriptors):
            os.close(descriptor)

    def _trigger_and_verify(
        self,
        walk: _Walk,
        *,
        final_identity: tuple[int, int] | None = None,
        final_must_exist: bool = False,
    ) -> None:
        hook, self.test_hook = self.test_hook, None
        if hook is not None:
            hook()
        for parent, component, expected in walk.links:
            try:
                current = os.stat(component, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise UnsafePathError(f"path component identity changed: {component}") from exc
            if _identity(current) != expected or not stat.S_ISDIR(current.st_mode):
                raise UnsafePathError(f"path component identity changed: {component}")
        if final_identity is not None:
            try:
                current = os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise UnsafePathError(f"final path identity changed: {walk.leaf}") from exc
            if _identity(current) != final_identity:
                raise UnsafePathError(f"final path identity changed: {walk.leaf}")
        elif final_must_exist:
            try:
                os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise UnsafePathError(f"final path disappeared: {walk.leaf}") from exc

    @contextmanager
    def _parent(self, relative: PurePosixPath, *, create_parents: bool = False) -> Iterator[_Walk]:
        walk = self._walk_parent(relative, create_parents=create_parents)
        try:
            yield walk
        finally:
            self._close_walk(walk)

    @staticmethod
    def _ensure_regular(info: os.stat_result, relative: str) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePathError(f"path is not a regular file: {relative}")

    def read_bytes(self, relative: str) -> bytes:
        parsed = self._guard("read", relative)
        with self._parent(parsed) as walk:
            try:
                descriptor = os.open(walk.leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=walk.parent_fd)
            except OSError as exc:
                raise UnsafePathError(f"path is not a readable regular file: {relative}") from exc
            try:
                info = os.fstat(descriptor)
                self._ensure_regular(info, relative)
                if info.st_dev != walk.root_device:
                    raise UnsafePathError(f"file crosses a device boundary: {relative}")
                self._trigger_and_verify(walk, final_identity=_identity(info))
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def read_text(self, relative: str, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative).decode(encoding)

    def read_json(self, relative: str) -> Any:
        return json.loads(self.read_text(relative))

    def open_exclusive(self, relative: str, *, mode: int = 0o600, create_parents: bool = False) -> int:
        parsed = self._guard("write", relative)
        with self._parent(parsed, create_parents=create_parents) as walk:
            self._trigger_and_verify(walk)
            return os.open(
                walk.leaf,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                mode,
                dir_fd=walk.parent_fd,
            )

    def atomic_write_bytes(
        self,
        relative: str,
        data: bytes,
        *,
        mode: int = 0o600,
        create_parents: bool = False,
        internal: bool = False,
    ) -> None:
        parsed = self._guard("write", relative, internal=internal)
        with self._parent(parsed, create_parents=create_parents) as walk:
            try:
                existing = os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                self._ensure_regular(existing, relative)
                if existing.st_dev != walk.root_device:
                    raise UnsafePathError(f"file crosses a device boundary: {relative}")
            self._trigger_and_verify(walk, final_identity=_identity(existing) if existing else None)
            temporary = f".{walk.leaf}.tmp-{secrets.token_hex(12)}"
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                mode,
                dir_fd=walk.parent_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=walk.parent_fd)
                finally:
                    os.close(descriptor)
                raise
            os.close(descriptor)
            try:
                os.replace(temporary, walk.leaf, src_dir_fd=walk.parent_fd, dst_dir_fd=walk.parent_fd)
                os.fsync(walk.parent_fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=walk.parent_fd)
                except FileNotFoundError:
                    pass
                raise

    def atomic_write_text(
        self,
        relative: str,
        text: str,
        *,
        encoding: str = "utf-8",
        create_parents: bool = False,
        internal: bool = False,
    ) -> None:
        self.atomic_write_bytes(
            relative,
            text.encode(encoding),
            create_parents=create_parents,
            internal=internal,
        )

    def atomic_write_json(
        self,
        relative: str,
        payload: Any,
        *,
        create_parents: bool = False,
        internal: bool = False,
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.atomic_write_text(relative, serialized, create_parents=create_parents, internal=internal)

    def mkdir(
        self,
        relative: str,
        *,
        parents: bool = False,
        exist_ok: bool = True,
        mode: int = 0o755,
        internal: bool = False,
    ) -> None:
        parsed = self._guard("mkdir", relative, internal=internal)
        with self._parent(parsed, create_parents=parents) as walk:
            self._trigger_and_verify(walk)
            try:
                os.mkdir(walk.leaf, mode=mode, dir_fd=walk.parent_fd)
                os.fsync(walk.parent_fd)
            except FileExistsError:
                if not exist_ok:
                    raise
                info = os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or info.st_dev != walk.root_device:
                    raise UnsafePathError(f"existing path is not a safe directory: {relative}")

    def stat_regular(self, relative: str) -> os.stat_result:
        parsed = self._guard("read", relative)
        with self._parent(parsed) as walk:
            info = os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
            self._ensure_regular(info, relative)
            self._trigger_and_verify(walk, final_identity=_identity(info))
            return info

    def unlink_regular(self, relative: str, *, internal: bool = False) -> None:
        parsed = self._guard("unlink", relative, internal=internal)
        with self._parent(parsed) as walk:
            info = os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
            self._ensure_regular(info, relative)
            self._trigger_and_verify(walk, final_identity=_identity(info))
            os.unlink(walk.leaf, dir_fd=walk.parent_fd)
            os.fsync(walk.parent_fd)

    def rmdir_empty(self, relative: str, *, internal: bool = False) -> None:
        parsed = self._guard("rmdir", relative, internal=internal)
        with self._parent(parsed) as walk:
            info = os.stat(walk.leaf, dir_fd=walk.parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafePathError(f"path is not a directory: {relative}")
            self._trigger_and_verify(walk, final_identity=_identity(info))
            os.rmdir(walk.leaf, dir_fd=walk.parent_fd)
            os.fsync(walk.parent_fd)

    def rename_no_replace(self, source: str, destination: str, *, internal: bool = False) -> None:
        source_path = self._guard("rename", source, internal=internal)
        destination_path = self._guard("rename", destination, internal=internal)
        with self._parent(source_path) as source_walk, self._parent(destination_path) as destination_walk:
            source_info = os.stat(source_walk.leaf, dir_fd=source_walk.parent_fd, follow_symlinks=False)
            self._ensure_regular(source_info, source)
            self._trigger_and_verify(source_walk, final_identity=_identity(source_info))
            self._trigger_and_verify(destination_walk)
            libc = ctypes.CDLL(None, use_errno=True)
            source_bytes = os.fsencode(source_walk.leaf)
            destination_bytes = os.fsencode(destination_walk.leaf)
            result: int | None = None
            if hasattr(libc, "renameat2"):
                libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                libc.renameat2.restype = ctypes.c_int
                result = libc.renameat2(
                    source_walk.parent_fd,
                    source_bytes,
                    destination_walk.parent_fd,
                    destination_bytes,
                    1,
                )
            elif hasattr(libc, "renameatx_np"):
                libc.renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                libc.renameatx_np.restype = ctypes.c_int
                result = libc.renameatx_np(
                    source_walk.parent_fd,
                    source_bytes,
                    destination_walk.parent_fd,
                    destination_bytes,
                    0x00000004,
                )
            if result is None:
                raise UnsafePathError("atomic no-replace rename is unavailable")
            if result != 0:
                error = ctypes.get_errno()
                if error in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise FileExistsError(destination)
                raise OSError(error, os.strerror(error), destination)
            os.fsync(destination_walk.parent_fd)
            os.fsync(source_walk.parent_fd)

    def copy_regular_from(self, source: "RootedFS", source_relative: str, destination_relative: str) -> str:
        self._guard("copy", destination_relative)
        data = source.read_bytes(source_relative)
        digest = hashlib.sha256(data).hexdigest()
        self.atomic_write_bytes(destination_relative, data, create_parents=True)
        if hashlib.sha256(self.read_bytes(destination_relative)).hexdigest() != digest:
            raise UnsafePathError("copied file failed digest verification")
        return digest

    def reflink_or_copy_from(self, source: "RootedFS", source_relative: str, destination_relative: str) -> str:
        # A verified byte copy is the portable copy-on-write fallback.  Never use a hard
        # link: inherited sources must not share writable inode identity with their parent.
        return self.copy_regular_from(source, source_relative, destination_relative)

    def _scan_directory(self, descriptor: int, prefix: str, root_device: int) -> Iterator[RegularFile]:
        for name in sorted(os.listdir(descriptor)):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISLNK(info.st_mode):
                raise UnsafePathError(f"symlink encountered while enumerating: {relative}")
            if info.st_dev != root_device:
                raise UnsafePathError(f"device boundary encountered while enumerating: {relative}")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    yield from self._scan_directory(child, relative, root_device)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                data = self.read_bytes(relative)
                yield RegularFile(relative, len(data), hashlib.sha256(data).hexdigest())
            else:
                raise UnsafePathError(f"special file encountered while enumerating: {relative}")

    def iter_regular(self) -> tuple[RegularFile, ...]:
        root_fd, root_device = self._open_root()
        try:
            return tuple(self._scan_directory(root_fd, "", root_device))
        finally:
            os.close(root_fd)

    def cleanup_manifest_entries(self, inventory: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        removed: list[str] = []
        refused: list[str] = []
        for relative, expected in sorted(inventory.entries.items(), reverse=True):
            if getattr(expected, "kind", "file") != "file":
                continue
            try:
                data = self.read_bytes(relative)
                info = self.stat_regular(relative)
            except (FileNotFoundError, UnsafePathError):
                refused.append(relative)
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected.sha256 or info.st_dev != expected.device or info.st_ino != expected.inode:
                refused.append(relative)
                continue
            self.unlink_regular(relative, internal=True)
            removed.append(relative)
        return tuple(sorted(removed)), tuple(sorted(refused))


__all__ = ["RegularFile", "RootedFS", "UnsafePathError"]
