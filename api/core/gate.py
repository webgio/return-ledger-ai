"""Who is calling, and may they spend one more AI call?

Everything downstream works on a `Subject`. Today a subject is a guest identified by an access
code; when the app opens to the public, `ALLOW_LOGIN=true` adds Static Web Apps sign-in as a
second way to obtain a Subject, and quota/limit logic stays untouched.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


class GateError(Exception):
    """`code` is stable (clients translate from it); `message` is the English default text."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Subject:
    kind: str  # "guest" | "user"
    id: str  # stable key used for the usage counter
    label: str
    monthly_quota: int


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def client_ip(headers: Mapping[str, str]) -> str:
    fwd = _lower(headers).get("x-forwarded-for", "")
    first = fwd.split(",")[0].strip()
    return first.rsplit(":", 1)[0] if first.count(":") == 1 else first or "unknown"


def authenticate(headers: Mapping[str, str], store: Any, now: datetime) -> Subject:
    h = _lower(headers)
    ip = client_ip(headers)

    # Throttle failed attempts per IP per hour (codes are long and random, but failed lookups
    # should not be free).
    fail_pk, fail_rk = f"fail:{ip}", now.strftime("%Y-%m-%dT%H")
    if store.get_count(fail_pk, fail_rk) >= int(os.environ.get("MAX_FAILED_PER_HOUR", "20")):
        raise GateError(429, "too_many_attempts", "Too many attempts. Try again later.")

    code = h.get("x-guest-code", "").strip()
    if code:
        guest = store.get_guest(hash_code(code))
        if not guest or not guest["enabled"]:
            store.try_increment(fail_pk, fail_rk, 10**9)
            raise GateError(401, "invalid_code", "Invalid access code.")
        return Subject("guest", f"guest:{guest['label']}", guest["label"], guest["monthly_quota"])

    if os.environ.get("ALLOW_LOGIN", "false").lower() == "true":
        raw = h.get("x-ms-client-principal")
        if raw:
            try:
                p = json.loads(base64.b64decode(raw))
            except Exception:
                raise GateError(401, "invalid_identity", "Invalid identity.")
            if "authenticated" in p.get("userRoles", []):
                uid = f"{p['identityProvider']}:{p['userId']}"
                return Subject(
                    "user",
                    f"user:{uid}",
                    p.get("userDetails", uid),
                    int(os.environ.get("DEFAULT_USER_QUOTA", "10")),
                )

    store.try_increment(fail_pk, fail_rk, 10**9)
    raise GateError(401, "code_required", "An access code is required.")


def reserve(subject: Subject, store: Any, now: datetime) -> int:
    """Take one credit (per-subject monthly + global daily). Returns credits left this month."""
    month = now.strftime("%Y-%m")
    day = now.strftime("%Y-%m-%d")
    cap = int(os.environ.get("GLOBAL_DAILY_CAP", "200"))

    ok, _ = store.try_increment("global", day, cap)
    if not ok:
        raise GateError(503, "daily_cap", "The service reached its daily limit. Try again tomorrow.")
    ok, used = store.try_increment(subject.id, month, subject.monthly_quota)
    if not ok:
        store.decrement("global", day)
        raise GateError(429, "quota_exhausted", "Monthly quota used up.")
    return subject.monthly_quota - used


def release(subject: Subject, store: Any, now: datetime) -> None:
    """Give the credit back (the model call failed through no fault of the user)."""
    store.decrement(subject.id, now.strftime("%Y-%m"))
    store.decrement("global", now.strftime("%Y-%m-%d"))
