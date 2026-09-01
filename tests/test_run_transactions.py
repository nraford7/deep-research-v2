from __future__ import annotations

from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts.run_layout import LayoutKind, RunLayout
from scripts.run_transactions import (
    BrokerProtocolError,
    BrokerStop,
    broker_request,
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


def _slow_dispatch(request, context, lease):
    """Broker dispatcher standing in for a long helper (slice-search takes minutes)."""
    time.sleep(float(context.get("sleep", 1.0)))
    return {"slept": True, "action": request["action"]}


def test_broker_survives_a_client_that_gave_up_waiting(tmp_path: Path) -> None:
    """A helper that outlives the client's patience must not kill the broker: the
    reply write to the hung-up client fails, and that failure used to escape the
    per-request handler and tear down the whole broker (socket unlinked, lease
    released) — the 'keeper died' seen on 2026-09-01."""
    library = tmp_path / "research"
    library.mkdir()
    broker = start_local_broker(
        library, "topic", operation="prepare", ttl_seconds=5,
        dispatcher_module="tests.test_run_transactions", dispatcher_name="_slow_dispatch",
        allowed_helpers={"scope": frozenset({"question"})}, context={"sleep": "1.0"},
    )
    try:
        with pytest.raises((TimeoutError, OSError)):
            broker_request(broker.socket_path, broker.owner.token, "invoke-helper",
                           helper_id="scope", args={"question": "Q"}, reply_timeout=0.2)
        time.sleep(1.2)  # let the helper finish and the broker try to answer the gone client
        assert broker.process.is_alive(), "broker died after the client hung up"
        assert broker.socket_path.exists()
        assert broker.request("renew")["renewed_until"]
    finally:
        broker.release()


def test_client_waits_for_long_helpers_beyond_the_connect_timeout(tmp_path: Path, monkeypatch) -> None:
    """Connecting and sending get a short timeout; waiting for a helper's reply must
    not — helpers legitimately run for minutes and the old fixed 5 s made every long
    invoke-helper 'time out' client-side."""
    from scripts import run_transactions
    monkeypatch.setattr(run_transactions, "BROKER_CONNECT_TIMEOUT", 0.3)
    library = tmp_path / "research"
    library.mkdir()
    broker = start_local_broker(
        library, "topic", operation="prepare", ttl_seconds=5,
        dispatcher_module="tests.test_run_transactions", dispatcher_name="_slow_dispatch",
        allowed_helpers={"scope": frozenset({"question"})}, context={"sleep": "0.8"},
    )
    try:
        result = broker_request(broker.socket_path, broker.owner.token, "invoke-helper",
                                helper_id="scope", args={"question": "Q"})
        assert result == {"slept": True, "action": "invoke-helper"}
    finally:
        broker.release()


def _exit_dispatch(request, context, lease):
    raise SystemExit(2)  # what argparse does on a bad helper argument


def _unserializable_dispatch(request, context, lease):
    return {"path": Path("/tmp/x"), "tags": {"a", "b"}}


def _marker_dispatch(request, context, lease):
    Path(context["marker"]).write_text("dispatched")
    return {"ok": True}


def _raw(socket_path, payload: bytes, *, wait: float = 0.5, close_first: bool = False) -> bytes:
    """Send raw bytes to the broker. close_first=True hangs up right after sending
    (no reply expected) and then waits, so the server has seen EOF by the time the
    caller inspects side effects."""
    import socket as _socket
    c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    c.settimeout(3)
    try:
        c.connect(str(socket_path))
        c.sendall(payload)
        if close_first:
            c.close()
            time.sleep(wait)
            return b""
        if wait:
            time.sleep(wait)
        try:
            return c.recv(65536)
        except OSError:
            return b""
    finally:
        c.close()


def _broker(tmp_path, dispatcher, **kw):
    library = tmp_path / "research"
    library.mkdir(exist_ok=True)
    return start_local_broker(
        library, "topic", operation="prepare", ttl_seconds=kw.pop("ttl_seconds", 5),
        dispatcher_module="tests.test_run_transactions", dispatcher_name=dispatcher,
        allowed_helpers={"scope": frozenset({"question", "min_chars"})}, **kw)


def test_oversized_request_is_rejected_without_killing_the_broker(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_slow_dispatch", context={"sleep": "0"})
    try:
        reply = _raw(broker.socket_path, b"x" * (1024 * 1024 + 10) + b"\n", wait=0.5)
        assert b"exceeds one MiB" in reply
        assert broker.process.is_alive() and broker.socket_path.exists()
        assert broker.request("renew")["renewed_until"]
    finally:
        broker.release()


def test_helper_raising_systemexit_does_not_kill_the_broker(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_exit_dispatch")
    try:
        with pytest.raises(BrokerProtocolError, match="SystemExit|2"):
            broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
        assert broker.process.is_alive()
        assert broker.request("renew")["renewed_until"]
    finally:
        broker.release()


def test_unserializable_helper_result_is_reported_not_fatal(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_unserializable_dispatch")
    try:
        result = broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
        assert result["path"] == "/tmp/x"  # str()-serialized rather than a dead broker
        assert broker.process.is_alive()
    finally:
        broker.release()


def test_request_without_newline_terminator_is_not_dispatched(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    broker = _broker(tmp_path, "_marker_dispatch", context={"marker": str(marker)})
    try:
        body = json.dumps({"action": "invoke-helper", "token": broker.owner.token,
                           "helper_id": "scope", "args": {"question": "Q"}}).encode()
        _raw(broker.socket_path, body, wait=0.6, close_first=True)   # EOF without the newline frame
        assert not marker.exists(), "an unterminated request must never be dispatched"
        assert broker.process.is_alive()
        # the same request, properly framed, dispatches exactly once
        broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
        assert marker.exists()
    finally:
        broker.release()


def _holder_renewed_until(library: Path, token: str) -> str:
    for f in library.rglob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        holders = data if isinstance(data, list) else data.get("holders", []) if isinstance(data, dict) else []
        for h in holders:
            owner = h.get("owner", {}) if isinstance(h, dict) else {}
            if owner.get("token") == token:
                return owner["renewed_until"]
    raise AssertionError("lease holder not found")


def test_lease_is_renewed_while_a_long_helper_runs(tmp_path: Path) -> None:
    """Production TTL is 300 s and helpers run longer; the lease must be renewed
    DURING the helper, not only before it, or it expires mid-run."""
    broker = _broker(tmp_path, "_slow_dispatch", ttl_seconds=0.6, context={"sleep": "1.5"})
    library = tmp_path / "research"
    try:
        t0 = time.time()
        before = _holder_renewed_until(library, broker.owner.token)
        broker_request(broker.socket_path, broker.owner.token, "invoke-helper",
                       helper_id="scope", args={"question": "Q"})
        after = _holder_renewed_until(library, broker.owner.token)
        assert after > before
        # The helper (1.5 s) outlived the TTL (0.6 s). Without mid-helper renewal the
        # holder would have expired ~0.9 s before this line; it must still be VALID.
        from scripts.run_transactions import _parse_iso, _utc_now
        assert _parse_iso(after) > _utc_now(), "lease expired while the helper ran"
        assert broker.process.is_alive()
    finally:
        broker.release()


def test_default_reply_wait_is_bounded_and_cli_exposes_it() -> None:
    from scripts import run_transactions, run_manager
    assert isinstance(run_transactions.BROKER_REPLY_TIMEOUT, (int, float)) and run_transactions.BROKER_REPLY_TIMEOUT > 300
    parser = run_manager._parser()
    ns = parser.parse_args(["invoke-helper", "--broker-endpoint", "/tmp/x.sock", "--lease-token", "t",
                            "--helper", "scope", "--reply-timeout", "42"])
    assert ns.reply_timeout == 42.0


def _slow_marker_dispatch(request, context, lease):
    if context.get("started"):
        Path(context["started"]).write_text("dispatch entered")
    time.sleep(float(context.get("sleep", 1.0)))
    Path(context["marker"]).write_text("helper finished")
    return {"ok": True}


def _descendant_writer_dispatch(request, context, lease):
    """Stand in for a helper that has launched a subprocess before hanging."""
    ready = Path(context["descendant_ready"])
    release = Path(context["release_descendant"])
    code = (
        "import os,pathlib,time; "
        f"ready=pathlib.Path({str(ready)!r}); release=pathlib.Path({str(release)!r}); "
        "ready.write_text(str(os.getpid())); "
        "\nwhile not release.exists(): time.sleep(0.01); "
        f"\npathlib.Path({context['marker']!r}).write_text('descendant wrote')"
    )
    subprocess.Popen([sys.executable, "-c", code])
    _wait_for(ready)
    Path(context["started"]).write_text("dispatch entered")
    time.sleep(5.0)
    return {"ok": True}


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        assert time.time() < deadline, f"{path} never appeared"
        time.sleep(0.02)


def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> None:
    """Wait until ``pid`` no longer exists; fail instead of relying on a sleep."""
    deadline = time.time() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        assert time.time() < deadline, f"descendant process {pid} survived the broker hard stop"
        time.sleep(0.02)


def _explosive_dispatch(request, context, lease):
    class _Boom:
        def __str__(self):
            raise RuntimeError("str() exploded")
    return {"value": _Boom()}


def _holders_file(library: Path, token: str) -> Path:
    for f in library.rglob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, list) and any(isinstance(h, dict) and h.get("owner", {}).get("token") == token for h in data):
            return f
    raise AssertionError("holders file not found")


def test_lost_lease_during_helper_stops_the_broker_at_once(tmp_path: Path) -> None:
    """If renewal fails mid-helper (holder gone: suspend past the TTL, an audited
    takeover), a new owner may already hold the run, so the worker must NOT be
    allowed to keep writing: the broker answers the client with the lease error
    and hard-exits immediately, taking the daemon worker with it."""
    marker = tmp_path / "marker"
    started = tmp_path / "started"
    broker = _broker(tmp_path, "_slow_marker_dispatch", ttl_seconds=0.4,
                     context={"sleep": "1.2", "marker": str(marker), "started": str(started)})
    library = tmp_path / "research"
    holders = _holders_file(library, broker.owner.token)
    result: dict = {}

    def _call() -> None:
        try:
            broker_request(broker.socket_path, broker.owner.token, "invoke-helper", helper_id="scope", args={"question": "Q"})
        except BrokerProtocolError as exc:
            result["error"] = str(exc); result["type"] = type(exc).__name__

    import threading
    t = threading.Thread(target=_call); t.start()
    _wait_for(started)                     # the helper is provably running now
    holders.unlink()                       # the lease vanishes under the running helper
    t.join(timeout=6)
    assert "error" in result and "lease" in result["error"].lower(), result
    assert result["type"] == "BrokerStop", result   # not an ordinary protocol/validation error
    broker.process.join(timeout=3)
    assert not broker.process.is_alive(), "broker kept serving after losing its lease"
    assert not marker.exists(), "the worker kept writing after the lease was lost"


def test_hung_helper_hits_the_deadline_and_the_broker_stops(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_slow_dispatch", ttl_seconds=0.5, context={"sleep": "5.0", "helper_deadline_seconds": "0.4"})
    try:
        with pytest.raises(BrokerProtocolError, match="deadline"):
            broker_request(broker.socket_path, broker.owner.token, "invoke-helper", helper_id="scope", args={"question": "Q"})
        broker.process.join(timeout=3)
        assert not broker.process.is_alive(), "a broker whose helper hung must not stay immortal"
    finally:
        if broker.process.is_alive():
            broker.process.terminate()


def test_helper_deadline_kills_descendant_processes(tmp_path: Path) -> None:
    """A hard broker stop must kill subprocesses spawned by the helper too.

    Otherwise the retained lease merely delays an orphaned writer: once its TTL
    ages out, that descendant can overlap the next owner of the run.
    """
    started = tmp_path / "started"
    marker = tmp_path / "marker"
    descendant_ready = tmp_path / "descendant-ready"
    release_descendant = tmp_path / "release-descendant"
    broker = _broker(
        tmp_path,
        "_descendant_writer_dispatch",
        ttl_seconds=5,
        context={
            "started": str(started),
            "marker": str(marker),
            "descendant_ready": str(descendant_ready),
            "release_descendant": str(release_descendant),
            "helper_deadline_seconds": "2.0",
        },
    )
    try:
        with pytest.raises(BrokerStop, match="deadline"):
            broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
        _wait_for(started)
        assert descendant_ready.exists(), "the test descendant never started"
        descendant_pid = int(descendant_ready.read_text())
        broker.process.join(timeout=3)
        assert not broker.process.is_alive(), "broker did not stop at the helper deadline"
        _wait_for_process_exit(descendant_pid)
        release_descendant.write_text("go")
        assert not marker.exists(), "a descendant kept running after the broker hard-stopped"
    finally:
        if broker.process.is_alive():
            broker.process.terminate()


def test_result_whose_str_explodes_is_reported_not_fatal(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_explosive_dispatch")
    try:
        with pytest.raises(BrokerProtocolError, match="unserializable"):
            broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
        assert broker.process.is_alive()
        assert broker.request("renew")["renewed_until"]
    finally:
        broker.release()


def test_invalid_reply_timeout_is_rejected_before_anything_is_sent(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    broker = _broker(tmp_path, "_marker_dispatch", context={"marker": str(marker)})
    try:
        for bad in (-1, float("nan"), float("inf"), "soon"):
            with pytest.raises(ValueError):
                broker_request(broker.socket_path, broker.owner.token, "invoke-helper",
                               helper_id="scope", args={"question": "Q"}, reply_timeout=bad)
        time.sleep(0.3)
        assert not marker.exists(), "a request with an invalid timeout must never reach the broker"
    finally:
        broker.release()


def _interrupt_dispatch(request, context, lease):
    raise KeyboardInterrupt()   # a BaseException that is not SystemExit


class _UnprintableError(Exception):
    def __str__(self):
        raise RuntimeError("str() exploded")


def _unprintable_error_dispatch(request, context, lease):
    raise _UnprintableError()


def test_request_delivery_deadline_answers_with_an_error_frame(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_slow_dispatch", context={"sleep": "0", "request_deadline_seconds": "0.5"})
    try:
        reply = _raw(broker.socket_path, b'{"action": "renew", "tok', wait=1.0)   # half a request, held open
        assert b"deadline" in reply, reply
        assert broker.process.is_alive()
        assert broker.request("renew")["renewed_until"]
    finally:
        broker.release()


def test_lost_lease_with_a_hung_helper_stops_at_once(tmp_path: Path) -> None:
    """A hung helper plus a lost lease must not wait for any deadline: the lease
    loss alone ends the broker immediately."""
    started = tmp_path / "started"
    broker = _broker(tmp_path, "_slow_marker_dispatch", ttl_seconds=0.4,
                     context={"sleep": "8.0", "helper_deadline_seconds": "60", "marker": str(tmp_path / "m"), "started": str(started)})
    holders = _holders_file(tmp_path / "research", broker.owner.token)
    result: dict = {}

    def _call() -> None:
        try:
            broker_request(broker.socket_path, broker.owner.token, "invoke-helper", helper_id="scope", args={"question": "Q"})
        except BrokerProtocolError as exc:
            result["error"] = str(exc)

    import threading
    t = threading.Thread(target=_call); t.start()
    _wait_for(started)
    holders.unlink()
    t.join(timeout=6)
    assert "lease" in result.get("error", "").lower(), result
    broker.process.join(timeout=3)
    assert not broker.process.is_alive(), "lost lease + hung helper must not wait forever"


def test_helper_deadline_override_must_be_finite_and_positive() -> None:
    from scripts.run_transactions import _helper_deadline, BROKER_HELPER_DEADLINE
    for bad in ("nan", "inf", "-1", "0", "soon", None):
        assert _helper_deadline({"helper_deadline_seconds": bad}) == BROKER_HELPER_DEADLINE, bad
    assert _helper_deadline({"helper_deadline_seconds": "12.5"}) == 12.5
    assert _helper_deadline({}) == BROKER_HELPER_DEADLINE


def test_any_helper_exception_is_reported_not_fatal(tmp_path: Path) -> None:
    for dispatcher, pattern in (("_interrupt_dispatch", "KeyboardInterrupt"), ("_unprintable_error_dispatch", "_UnprintableError")):
        broker = _broker(tmp_path, dispatcher)
        try:
            with pytest.raises(BrokerProtocolError, match=pattern):
                broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
            assert broker.process.is_alive(), dispatcher
            assert broker.request("renew")["renewed_until"]
        finally:
            broker.release()


def test_default_client_wait_outlasts_the_helper_deadline() -> None:
    from scripts import run_transactions as rt
    assert rt.BROKER_REPLY_TIMEOUT >= rt.BROKER_HELPER_DEADLINE, "a client that gives up before the broker does would retry into a duplicate dispatch"


def test_cli_forwards_reply_timeout_to_broker_request(monkeypatch) -> None:
    from scripts import run_manager
    seen: dict = {}

    def _fake(endpoint, token, action, **kw):
        seen.update(endpoint=endpoint, token=token, action=action, **kw)
        return {"ok": True}

    monkeypatch.setattr(run_manager, "broker_request", _fake)
    rc = run_manager.run(["invoke-helper", "--broker-endpoint", "/tmp/x.sock", "--lease-token", "t",
                          "--helper", "scope", "--args-json", "{}", "--reply-timeout", "42", "--json"])
    assert rc == 0
    assert seen["reply_timeout"] == 42.0 and seen["action"] == "invoke-helper"
    seen.clear()
    run_manager.run(["invoke-helper", "--broker-endpoint", "/tmp/x.sock", "--lease-token", "t", "--helper", "scope", "--json"])
    assert "reply_timeout" not in seen   # library default applies when the flag is absent


class _BaseExplodingStr(Exception):
    def __str__(self):
        raise KeyboardInterrupt()


def _base_exploding_error_dispatch(request, context, lease):
    raise _BaseExplodingStr()


class _BaseExplodingValue:
    def __str__(self):
        raise SystemExit(3)


def _base_exploding_value_dispatch(request, context, lease):
    return {"value": _BaseExplodingValue()}


def test_deadline_hard_exit_keeps_the_lease_even_when_the_client_is_gone(tmp_path: Path) -> None:
    """The client that owned the request may have timed out already; the hard exit
    must not depend on delivering the reply, and it must NOT release the lease
    (a live worker could still write — the holder ages out via the TTL)."""
    broker = _broker(tmp_path, "_slow_dispatch", ttl_seconds=5,
                     context={"sleep": "8.0", "helper_deadline_seconds": "1.0"})
    holders = _holders_file(tmp_path / "research", broker.owner.token)
    with pytest.raises((TimeoutError, OSError)):
        broker_request(broker.socket_path, broker.owner.token, "invoke-helper",
                       helper_id="scope", args={"question": "Q"}, reply_timeout=0.2)   # client gives up first
    broker.process.join(timeout=4)
    assert not broker.process.is_alive(), "broker must hard-exit at the helper deadline"
    assert holders.exists() and broker.owner.token in holders.read_text(), "hard exit released the lease under a live worker"
    assert not broker.socket_path.exists()


def test_pre_dispatch_renewal_failure_stops_the_broker(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "_slow_dispatch", ttl_seconds=5, context={"sleep": "0"})
    _holders_file(tmp_path / "research", broker.owner.token).unlink()
    with pytest.raises(BrokerProtocolError, match="lease"):
        broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
    broker.process.join(timeout=3)
    assert not broker.process.is_alive(), "broker served on without a valid lease"


def test_base_exceptions_from_str_are_contained(tmp_path: Path) -> None:
    for dispatcher, pattern in (("_base_exploding_error_dispatch", "_BaseExplodingStr"),
                                ("_base_exploding_value_dispatch", "unserializable")):
        broker = _broker(tmp_path, dispatcher)
        try:
            with pytest.raises(BrokerProtocolError, match=pattern):
                broker.request("invoke-helper", helper_id="scope", args={"question": "Q"})
            assert broker.process.is_alive(), dispatcher
        finally:
            broker.release()


def test_helper_deadline_overshoot_is_bounded(tmp_path: Path) -> None:
    """A large TTL must not turn a small deadline into a long wait (ttl/3 polling)."""
    broker = _broker(tmp_path, "_slow_dispatch", ttl_seconds=30,
                     context={"sleep": "8.0", "helper_deadline_seconds": "0.5"})
    t0 = time.time()
    try:
        with pytest.raises(BrokerProtocolError, match="deadline"):
            broker_request(broker.socket_path, broker.owner.token, "invoke-helper", helper_id="scope", args={"question": "Q"})
    finally:
        broker.process.join(timeout=3)
        if broker.process.is_alive():
            broker.process.terminate()
    assert time.time() - t0 < 2.5, "deadline overshoot"


def test_cli_reports_a_stopped_broker_as_failed_not_invalid_argument(monkeypatch) -> None:
    from scripts import run_manager
    from scripts.run_transactions import BrokerStop

    def _stopped(*a, **kw):
        raise BrokerStop("lease lost while the helper ran — broker stopping at once")

    monkeypatch.setattr(run_manager, "broker_request", _stopped)
    rc = run_manager.run(["invoke-helper", "--broker-endpoint", "/tmp/x.sock", "--lease-token", "t", "--helper", "scope", "--json"])
    assert rc == int(run_manager.Exit.FAILED)
