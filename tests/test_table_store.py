"""Integration tests against the Azurite emulator (npx azurite-table). Skipped if not running."""
import socket
import threading
import uuid

import pytest

CONN = "UseDevelopmentStorage=true"


def emulator_up() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 10002), timeout=0.5).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not emulator_up(), reason="Azurite table service not running")


@pytest.fixture
def store():
    from core.store import TableStore
    return TableStore(CONN)


def test_guest_lifecycle(store):
    label = f"g-{uuid.uuid4().hex[:8]}"
    store.put_guest("h-" + label, label, 7)
    assert store.get_guest("h-" + label) == {"label": label, "monthly_quota": 7, "enabled": True}
    with pytest.raises(ValueError):
        store.put_guest("other-hash", label, 1)
    assert store.set_guest_enabled(label, False)
    assert store.get_guest("h-" + label)["enabled"] is False
    assert not store.set_guest_enabled("missing-" + label, True)
    assert label in [g["label"] for g in store.list_guests()]


def test_counter_never_exceeds_limit_under_concurrency(store):
    pk, rk = f"t-{uuid.uuid4().hex}", "2026-10"
    results = []

    def worker():
        results.append(store.try_increment(pk, rk, 5)[0])

    threads = [threading.Thread(target=worker) for _ in range(25)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) == 5
    assert store.get_count(pk, rk) == 5


def test_decrement_and_zero_limit(store):
    pk = f"t-{uuid.uuid4().hex}"
    assert store.try_increment(pk, "m", 0) == (False, 0)
    assert store.try_increment(pk, "m", 2) == (True, 1)
    store.decrement(pk, "m")
    assert store.get_count(pk, "m") == 0
    store.decrement(pk, "m")
    assert store.get_count(pk, "m") == 0
    store.decrement("never-existed-" + pk, "m")  # no error


def test_quota_change_and_monthly_reset(store):
    label = f"g-{uuid.uuid4().hex[:8]}"
    store.put_guest("h-" + label, label, 1)
    assert store.set_guest_quota(label, 5)
    assert store.get_guest("h-" + label)["monthly_quota"] == 5
    assert not store.set_guest_quota("nobody-" + label, 5)
    pk = "guest:" + label
    store.try_increment(pk, "2026-10", 5)
    store.reset_count(pk, "2026-10")
    assert store.get_count(pk, "2026-10") == 0
    store.reset_count(pk, "2026-10")  # already gone: no error


def test_rotate_guest(store):
    label = f"g-{uuid.uuid4().hex[:8]}"
    store.put_guest("old-" + label, label, 4)
    store.set_guest_enabled(label, False)
    assert store.rotate_guest(label, "new-" + label)
    assert store.get_guest("old-" + label) is None
    assert store.get_guest("new-" + label) == {"label": label, "monthly_quota": 4, "enabled": False}
    assert not store.rotate_guest("nobody-" + label, "x-" + label)
