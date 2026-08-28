from __future__ import annotations

from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from scripts.run_layout import LayoutKind, RunLayout
from scripts.run_transactions import (
    BrokerProtocolError,
    ImmutableRegistry,
    Journal,
    JournalError,
    LeaseConflict,
    LockError,
    RunLease,
    TransactionConflict,
    TreeInventory,
    create_skeleton_transaction,
    recover_creation,
    start_local_broker,
)


def _abandon_broker(library: str, connection: object) -> None:
    broker = start_local_broker(
        library,
        "abandoned",
        operation="prepare",
        ttl_seconds=0.1,
    )
    connection.send({"socket": str(broker.socket_path), "pid": broker.process.pid})
    connection.close()
    os._exit(0)


def _hold_shared_lease(library: str, ready: object, release: object) -> None:
    lease = RunLease.acquire(library, "concurrent", operation="reader", shared=True)
    ready.set()
    release.wait(5)
    lease.release(lease.owner.token)


def test_lock_token_is_required_and_stale_pid_reuse_is_not_accepted(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    lock = RunLease.acquire(library, "topic", operation="create")
    try:
        with pytest.raises(LockError, match="ownership token"):
            lock.verify("wrong-token")
        lock.verify(lock.owner.token)
        forged = replace(lock.owner, keeper_process_start="stale-process-instance")
        with pytest.raises(LockError, match="owner identity"):
            lock.verify(lock.owner.token, expected_owner=forged)
    finally:
        lock.release(lock.owner.token)


def test_shared_holders_coexist_and_exclusive_holder_waits(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    first = RunLease.acquire(library, "topic", operation="status", shared=True)
    second = RunLease.acquire(library, "topic", operation="index", shared=True)
    try:
        with pytest.raises(LeaseConflict):
            RunLease.acquire(library, "topic", operation="resume")
    finally:
        first.release(first.owner.token)
        second.release(second.owner.token)
    writer = RunLease.acquire(library, "topic", operation="resume")
    writer.release(writer.owner.token)


def test_cross_process_exclusive_writer_cannot_enter_while_reader_holds(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    reader = multiprocessing.Process(target=_hold_shared_lease, args=(str(library), ready, release))
    reader.start()
    assert ready.wait(5)
    with pytest.raises(LeaseConflict):
        RunLease.acquire(library, "concurrent", operation="writer")
    release.set()
    reader.join(timeout=5)
    assert reader.exitcode == 0
    writer = RunLease.acquire(library, "concurrent", operation="writer")
    writer.release(writer.owner.token)


def test_renewal_extends_lease_and_expired_holder_can_be_taken_over(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    lease = RunLease.acquire(library, "topic", operation="create", ttl_seconds=0.05)
    previous = lease.owner.renewed_until
    time.sleep(0.01)
    lease.renew(lease.owner.token, ttl_seconds=1)
    assert lease.owner.renewed_until > previous
    lease.release(lease.owner.token)

    expired = RunLease.acquire(library, "other", operation="create", ttl_seconds=0.01)
    time.sleep(0.03)
    takeover = RunLease.acquire(library, "other", operation="recover", audited_takeover=True)
    with pytest.raises(LockError):
        expired.verify(expired.owner.token)
    takeover.release(takeover.owner.token)


def test_local_broker_owns_and_renews_lease_with_closed_authenticated_protocol(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    broker = start_local_broker(
        library,
        "topic",
        operation="prepare",
        ttl_seconds=0.15,
        allowed_helpers={"scope": frozenset({"question", "output_path"})},
        context={"run_id": "run"},
    )
    try:
        assert broker.owner.keeper_pid == broker.process.pid
        original_expiry = broker.owner.renewed_until
        time.sleep(0.2)
        renewed = broker.request("renew")
        assert renewed["renewed_until"] >= original_expiry
        result = broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
        assert result["action"] == "invoke-helper"
        with pytest.raises(BrokerProtocolError, match="unknown broker action"):
            broker.request("shell", command="touch /tmp/no")
        with pytest.raises(BrokerProtocolError, match="not allowlisted"):
            broker.request("invoke-helper", helper_id="unknown", args={})
        with pytest.raises(BrokerProtocolError, match="may not carry"):
            broker.request("invoke-helper", helper_id="scope", args={"output_path": "/tmp/out"})
        original = broker.owner
        broker.owner = replace(original, token="wrong")
        with pytest.raises(BrokerProtocolError, match="authentication"):
            broker.request("renew")
        broker.owner = original
    finally:
        broker.release()
    assert not broker.socket_path.exists()


def test_keeper_releases_lease_when_launcher_is_abandoned(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    receive, send = multiprocessing.Pipe(duplex=False)
    launcher = multiprocessing.Process(target=_abandon_broker, args=(str(library), send))
    launcher.start()
    send.close()
    assert receive.poll(5)
    details = receive.recv()
    receive.close()
    launcher.join(timeout=5)
    assert launcher.exitcode == 0

    deadline = time.monotonic() + 5
    acquired = None
    while time.monotonic() < deadline:
        try:
            acquired = RunLease.acquire(library, "abandoned", operation="recover")
            break
        except LeaseConflict:
            time.sleep(0.05)
    assert acquired is not None
    acquired.release(acquired.owner.token)
    assert not Path(details["socket"]).exists()


def test_publish_skeleton_never_replaces_a_racing_destination(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    transaction = create_skeleton_transaction(library, "topic", question="Q")
    (library / "topic").mkdir()
    with pytest.raises(TransactionConflict):
        transaction.publish()
    assert not (library / "topic" / "Process" / "run.json").exists()


def test_publish_skeleton_produces_valid_incomplete_v2_run(tmp_path: Path) -> None:
    library = tmp_path / "research"
    library.mkdir()
    transaction = create_skeleton_transaction(library, "topic", question="Q")
    published = transaction.publish()
    layout = RunLayout.open(published)
    assert layout.kind is LayoutKind.V2
    assert layout.metadata_data["status"] == "incomplete"
    assert layout.metadata_data["question"] == "Q"
    assert not transaction.skeleton.exists()


@pytest.mark.parametrize("crash_at", ["after-journal", "after-skeleton", "before-publish", "after-publish"])
def test_creation_recovery_never_accepts_invalid_visible_directory(tmp_path: Path, crash_at: str) -> None:
    library = tmp_path / "research"
    library.mkdir()
    transaction = create_skeleton_transaction(library, "topic", question="Q", crash_at=crash_at)
    try:
        transaction.publish()
    except BaseException as exc:
        assert isinstance(exc, (TransactionConflict, SystemExit))
    outcome = recover_creation(library, transaction.transaction_id)
    visible = library / "topic"
    if visible.exists():
        assert RunLayout.open(visible).kind is LayoutKind.V2
        assert outcome.status in {"published", "already-published"}
    else:
        assert outcome.status == "aborted"
    leftovers = list((library / ".transactions" / transaction.transaction_id).glob("skeleton/*"))
    assert leftovers == []


def test_inventory_detects_add_remove_change_and_hashes_membership(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    before = TreeInventory.capture(root)
    (root / "a.txt").write_text("changed", encoding="utf-8")
    (root / "extra.txt").write_text("x", encoding="utf-8")
    after = TreeInventory.capture(root)
    diff = before.diff(after)
    assert diff.added == ("extra.txt",)
    assert diff.changed == ("a.txt",)
    assert before.root_digest != after.root_digest


def test_journal_is_hash_chained_monotonic_and_tamper_evident(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl", transaction_id="tx", run_id="run")
    first = journal.append("intent", {"operation": "publish"})
    second = journal.append("complete", {"operation": "publish"})
    assert first.sequence == 1
    assert second.sequence == 2
    assert second.previous_digest == first.record_digest
    assert len(Journal.load(journal.path).records) == 2

    rows = journal.path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    payload["payload"]["operation"] = "tampered"
    rows[0] = json.dumps(payload)
    journal.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(JournalError, match="digest"):
        Journal.load(journal.path)


def test_immutable_registry_detects_conflicting_portable_alias(tmp_path: Path) -> None:
    library = tmp_path / "research"
    first = library / "Topic"
    second = library / "Topic."
    first.mkdir(parents=True)
    second.mkdir()
    registry = ImmutableRegistry(library)
    registry.record(first, kind="sealed", expected_root=TreeInventory.capture(first).root_digest)
    with pytest.raises(TransactionConflict, match="collision identity"):
        registry.record(second, kind="frozen", expected_root=TreeInventory.capture(second).root_digest)
