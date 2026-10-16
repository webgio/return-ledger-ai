import base64
import json
from datetime import datetime, timezone

import pytest

from core import gate
from core.store import MemoryStore

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    s = MemoryStore()
    s.put_guest(gate.hash_code("secret-code"), "mario", 2)
    return s


def auth(store, code=None, ip="1.2.3.4", extra=None):
    h = {"X-Forwarded-For": ip}
    if code:
        h["X-Guest-Code"] = code
    h.update(extra or {})
    return gate.authenticate(h, store, NOW)


def test_valid_code(store):
    s = auth(store, "secret-code")
    assert (s.kind, s.id, s.monthly_quota) == ("guest", "guest:mario", 2)


def test_invalid_missing_and_revoked_code(store):
    for code in ("wrong", None):
        with pytest.raises(gate.GateError) as e:
            auth(store, code)
        assert e.value.status == 401
    store.set_guest_enabled("mario", False)
    with pytest.raises(gate.GateError) as e:
        auth(store, "secret-code")
    assert e.value.status == 401
    store.set_guest_enabled("mario", True)
    assert auth(store, "secret-code").label == "mario"


def test_quota_exhaustion_and_release(store):
    s = auth(store, "secret-code")
    assert gate.reserve(s, store, NOW) == 1
    assert gate.reserve(s, store, NOW) == 0
    with pytest.raises(gate.GateError) as e:
        gate.reserve(s, store, NOW)
    assert (e.value.status, e.value.code) == (429, "quota_exhausted")
    gate.release(s, store, NOW)  # failed call gives the credit back
    assert gate.reserve(s, store, NOW) == 0


def test_denied_quota_does_not_burn_global_cap(store, monkeypatch):
    monkeypatch.setenv("GLOBAL_DAILY_CAP", "5")
    s = auth(store, "secret-code")
    gate.reserve(s, store, NOW); gate.reserve(s, store, NOW)
    with pytest.raises(gate.GateError):
        gate.reserve(s, store, NOW)
    assert store.get_count("global", "2026-10-10") == 2


def test_global_daily_cap(store, monkeypatch):
    monkeypatch.setenv("GLOBAL_DAILY_CAP", "1")
    s = auth(store, "secret-code")
    gate.reserve(s, store, NOW)
    with pytest.raises(gate.GateError) as e:
        gate.reserve(s, store, NOW)
    assert (e.value.status, e.value.code) == (503, "daily_cap")


def test_monthly_quota_resets_next_month(store):
    s = auth(store, "secret-code")
    gate.reserve(s, store, NOW); gate.reserve(s, store, NOW)
    nov = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert gate.reserve(s, store, nov) == 1


def test_failed_attempts_are_throttled_per_ip(store, monkeypatch):
    monkeypatch.setenv("MAX_FAILED_PER_HOUR", "3")
    for _ in range(3):
        with pytest.raises(gate.GateError) as e:
            auth(store, "bad", ip="9.9.9.9")
        assert e.value.status == 401
    with pytest.raises(gate.GateError) as e:
        auth(store, "secret-code", ip="9.9.9.9")  # even a good code is blocked now
    assert (e.value.status, e.value.code) == (429, "too_many_attempts")
    assert auth(store, "secret-code", ip="8.8.8.8").label == "mario"  # other IPs unaffected


def test_login_principal_only_when_enabled(store, monkeypatch):
    principal = base64.b64encode(json.dumps({
        "identityProvider": "google", "userId": "abc", "userDetails": "a@b.it",
        "userRoles": ["anonymous", "authenticated"]}).encode()).decode()
    hdr = {"x-ms-client-principal": principal}
    with pytest.raises(gate.GateError):
        auth(store, extra=hdr)
    monkeypatch.setenv("ALLOW_LOGIN", "true")
    s = auth(store, extra=hdr)
    assert (s.kind, s.id) == ("user", "user:google:abc")


def test_client_ip_parsing():
    assert gate.client_ip({"x-forwarded-for": "1.2.3.4:5678, 10.0.0.1"}) == "1.2.3.4"
    assert gate.client_ip({}) == "unknown"


def test_raising_the_quota_takes_effect_on_the_next_request(store):
    s = auth(store, "secret-code")
    gate.reserve(s, store, NOW); gate.reserve(s, store, NOW)
    with pytest.raises(gate.GateError):
        gate.reserve(s, store, NOW)
    store.set_guest_quota("mario", 3)
    assert gate.reserve(auth(store, "secret-code"), store, NOW) == 0  # 3rd of 3


def test_reset_gives_the_month_back(store):
    s = auth(store, "secret-code")
    gate.reserve(s, store, NOW); gate.reserve(s, store, NOW)
    store.reset_count("guest:mario", "2026-10")
    assert gate.reserve(s, store, NOW) == 1
