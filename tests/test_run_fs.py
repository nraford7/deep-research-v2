from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile

import pytest

from scripts.run_fs import RootedFS, UnsafePathError
from scripts.run_transactions import ImmutableRegistry, TreeInventory


@pytest.fixture
def rooted_fs(tmp_path: Path) -> RootedFS:
    root = tmp_path / "run"
    root.mkdir()
    return RootedFS(root, lease_token="token")


def test_rooted_read_write_mkdir_and_json_round_trip(rooted_fs: RootedFS) -> None:
    rooted_fs.mkdir("Sources/Extracted", parents=True)
    rooted_fs.atomic_write_text("Sources/Extracted/a.txt", "hello")
    rooted_fs.atomic_write_json("Process/state.json", {"ok": True}, create_parents=True)

    assert rooted_fs.read_text("Sources/Extracted/a.txt") == "hello"
    assert rooted_fs.read_json("Process/state.json") == {"ok": True}
    assert [entry.relative for entry in rooted_fs.iter_regular()] == [
        "Process/state.json",
        "Sources/Extracted/a.txt",
    ]


def test_rooted_write_rejects_component_swapped_to_symlink(rooted_fs: RootedFS, tmp_path: Path) -> None:
    rooted_fs.mkdir("Sources/Extracted", parents=True)
    escape = tmp_path / "escape"
    escape.mkdir()
    original = rooted_fs.root / "Sources"
    displaced = rooted_fs.root / "Sources-original"

    def swap() -> None:
        original.rename(displaced)
        original.symlink_to(escape, target_is_directory=True)

    rooted_fs.test_hook = swap
    with pytest.raises(UnsafePathError, match="identity changed"):
        rooted_fs.atomic_write_text("Sources/Extracted/a.txt", "secret")
    assert not (escape / "Extracted" / "a.txt").exists()


@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "unlink", "rename"])
def test_state_guard_is_applied_to_every_operation(rooted_fs: RootedFS, operation: str) -> None:
    seen: list[tuple[str, str]] = []
    rooted_fs.state_guard = lambda op, path: seen.append((op, path))
    rooted_fs.mkdir("a")
    rooted_fs.atomic_write_text("a/source.txt", "x")
    if operation == "read":
        rooted_fs.read_text("a/source.txt")
    elif operation == "write":
        rooted_fs.atomic_write_text("a/source.txt", "y")
    elif operation == "mkdir":
        rooted_fs.mkdir("b")
    elif operation == "unlink":
        rooted_fs.unlink_regular("a/source.txt")
    else:
        rooted_fs.rename_no_replace("a/source.txt", "a/dest.txt")
    assert any(call[0] == operation for call in seen)


def test_every_mutation_below_inherited_is_rejected_but_rounds_are_writable(rooted_fs: RootedFS) -> None:
    rooted_fs.mkdir("Process/Inherited/parent", parents=True, internal=True)
    rooted_fs.mkdir("Process/round2", parents=True)
    rooted_fs.atomic_write_text("Process/round2/synthesis.md", "ok")
    with pytest.raises(UnsafePathError, match="immutable inherited subtree"):
        rooted_fs.atomic_write_text("Process/Inherited/parent/tamper.txt", "no")
    with pytest.raises(UnsafePathError, match="immutable inherited subtree"):
        rooted_fs.mkdir("Process/Inherited/new")
    with pytest.raises(UnsafePathError, match="immutable inherited subtree"):
        rooted_fs.unlink_regular("Process/Inherited/parent/missing.txt")


def test_reads_and_inventory_reject_symlinks_and_special_files(rooted_fs: RootedFS) -> None:
    rooted_fs.mkdir("Sources")
    outside = rooted_fs.root.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (rooted_fs.root / "Sources" / "link.txt").symlink_to(outside)
    with pytest.raises(UnsafePathError, match="regular file"):
        rooted_fs.read_text("Sources/link.txt")
    with pytest.raises(UnsafePathError, match="symlink"):
        TreeInventory.capture(rooted_fs.root)

    (rooted_fs.root / "Sources" / "link.txt").unlink()
    fifo = rooted_fs.root / "Sources" / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(UnsafePathError, match="special file"):
        TreeInventory.capture(rooted_fs.root)


def test_socket_is_rejected_by_inventory(rooted_fs: RootedFS) -> None:
    with tempfile.TemporaryDirectory(prefix="dr-inventory-", dir="/tmp") as temporary:
        short_root = Path(temporary) / "run"
        short_root.mkdir()
        sock_path = short_root / "sock"
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(sock_path))
            with pytest.raises(UnsafePathError, match="special file"):
                TreeInventory.capture(short_root)
        finally:
            server.close()


def test_exclusive_create_and_no_replace_rename(rooted_fs: RootedFS) -> None:
    rooted_fs.mkdir("Process")
    descriptor = rooted_fs.open_exclusive("Process/new.txt")
    os.write(descriptor, b"new")
    os.close(descriptor)
    rooted_fs.atomic_write_text("Process/dest.txt", "existing")
    with pytest.raises(FileExistsError):
        rooted_fs.rename_no_replace("Process/new.txt", "Process/dest.txt")
    assert rooted_fs.read_text("Process/dest.txt") == "existing"


def test_copy_is_verified_and_never_hard_links(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    source = RootedFS(source_root)
    destination = RootedFS(destination_root)
    source.atomic_write_text("a.txt", "copy me")

    digest = destination.reflink_or_copy_from(source, "a.txt", "copy.txt")

    assert destination.read_text("copy.txt") == "copy me"
    assert digest == TreeInventory.capture(destination_root).entries["copy.txt"].sha256
    assert os.stat(source_root / "a.txt").st_ino != os.stat(destination_root / "copy.txt").st_ino


def test_immutable_registry_blocks_mutation_after_parent_rename(tmp_path: Path) -> None:
    library = tmp_path / "research"
    run = library / "topic"
    run.mkdir(parents=True)
    (run / "scope.json").write_text("{}", encoding="utf-8")
    registry = ImmutableRegistry(library)
    record = registry.record(run, kind="frozen", expected_root=TreeInventory.capture(run).root_digest)
    renamed = run.with_name("TOPIC")
    run.rename(renamed)

    assert registry.resolve(renamed).run_id == record.run_id
    guarded = RootedFS(renamed, immutable_registry=registry)
    with pytest.raises(UnsafePathError, match="immutable run"):
        guarded.atomic_write_text("scope.json", "changed")


def test_cleanup_manifest_removes_only_unchanged_entries(rooted_fs: RootedFS) -> None:
    rooted_fs.atomic_write_text("one.txt", "one")
    rooted_fs.atomic_write_text("two.txt", "two")
    inventory = TreeInventory.capture(rooted_fs.root)
    rooted_fs.atomic_write_text("two.txt", "changed")
    removed, refused = rooted_fs.cleanup_manifest_entries(inventory)
    assert removed == ("one.txt",)
    assert refused == ("two.txt",)
    assert rooted_fs.read_text("two.txt") == "changed"
