"""Persistence for guests and usage counters.

Two implementations of the same small interface:
- MemoryStore: for tests and local experiments
- TableStore: Azure Table Storage (cheap, enough for guests + counters)

Interface:
  get_guest(code_hash) -> dict | None      guest rows: label, monthly_quota, enabled
  put_guest(code_hash, label, monthly_quota)
  set_guest_enabled(label, enabled) -> bool
  rotate_guest(label, new_code_hash) -> bool         new code, same label/quota/enabled/usage
  list_guests() -> list[dict]
  try_increment(pk, rk, limit) -> (allowed, count)   atomic, never exceeds `limit`
  decrement(pk, rk)                                  give a credit back
  get_count(pk, rk) -> int
"""
from __future__ import annotations

import threading
from typing import Any, Optional

GUEST_PK = "guest"


class MemoryStore:
    def __init__(self) -> None:
        self._guests: dict[str, dict[str, Any]] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def get_guest(self, code_hash: str) -> Optional[dict[str, Any]]:
        g = self._guests.get(code_hash)
        return dict(g) if g else None

    def put_guest(self, code_hash: str, label: str, monthly_quota: int) -> None:
        if any(g["label"] == label for g in self._guests.values()):
            raise ValueError(f"Esiste già un ospite '{label}'.")
        self._guests[code_hash] = {
            "label": label,
            "monthly_quota": monthly_quota,
            "enabled": True,
        }

    def _update_guest(self, label: str, **fields: Any) -> bool:
        for g in self._guests.values():
            if g["label"] == label:
                g.update(fields)
                return True
        return False

    def set_guest_enabled(self, label: str, enabled: bool) -> bool:
        return self._update_guest(label, enabled=enabled)

    def set_guest_quota(self, label: str, monthly_quota: int) -> bool:
        return self._update_guest(label, monthly_quota=monthly_quota)

    def rotate_guest(self, label: str, new_code_hash: str) -> bool:
        for old_hash, g in list(self._guests.items()):
            if g["label"] == label:
                self._guests[new_code_hash] = self._guests.pop(old_hash)
                return True
        return False

    def list_guests(self) -> list[dict[str, Any]]:
        return [dict(g) for g in self._guests.values()]

    def try_increment(self, pk: str, rk: str, limit: int) -> tuple[bool, int]:
        with self._lock:
            n = self._counts.get((pk, rk), 0)
            if n >= limit:
                return False, n
            self._counts[(pk, rk)] = n + 1
            return True, n + 1

    def decrement(self, pk: str, rk: str) -> None:
        with self._lock:
            n = self._counts.get((pk, rk), 0)
            self._counts[(pk, rk)] = max(0, n - 1)

    def get_count(self, pk: str, rk: str) -> int:
        return self._counts.get((pk, rk), 0)

    def reset_count(self, pk: str, rk: str) -> None:
        with self._lock:
            self._counts.pop((pk, rk), None)


class TableStore:
    """Azure Table Storage. Counters use optimistic concurrency (ETag) so two parallel
    requests can never push a counter past its limit."""

    def __init__(self, connection_string: str) -> None:
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        self._guests = service.create_table_if_not_exists("Guests")
        self._usage = service.create_table_if_not_exists("Usage")

    # -- guests -----------------------------------------------------------
    def get_guest(self, code_hash: str) -> Optional[dict[str, Any]]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            e = self._guests.get_entity(GUEST_PK, code_hash)
        except ResourceNotFoundError:
            return None
        return {
            "label": e["label"],
            "monthly_quota": int(e["monthly_quota"]),
            "enabled": bool(e["enabled"]),
        }

    def put_guest(self, code_hash: str, label: str, monthly_quota: int) -> None:
        if any(g["label"] == label for g in self.list_guests()):
            raise ValueError(f"Esiste già un ospite '{label}'.")
        self._guests.create_entity(
            {
                "PartitionKey": GUEST_PK,
                "RowKey": code_hash,
                "label": label,
                "monthly_quota": monthly_quota,
                "enabled": True,
            }
        )

    def _update_guest(self, label: str, **fields: Any) -> bool:
        for e in self._guests.query_entities(f"PartitionKey eq '{GUEST_PK}'"):
            if e["label"] == label:
                self._guests.update_entity(
                    {"PartitionKey": GUEST_PK, "RowKey": e["RowKey"], **fields}, mode="merge"
                )
                return True
        return False

    def set_guest_enabled(self, label: str, enabled: bool) -> bool:
        return self._update_guest(label, enabled=enabled)

    def set_guest_quota(self, label: str, monthly_quota: int) -> bool:
        return self._update_guest(label, monthly_quota=monthly_quota)

    def rotate_guest(self, label: str, new_code_hash: str) -> bool:
        # The row is keyed by the code hash: create the new row first, then drop the old one,
        # so the guest is never left without a valid code if the second step fails.
        for e in self._guests.query_entities(f"PartitionKey eq '{GUEST_PK}'"):
            if e["label"] == label:
                self._guests.create_entity(
                    {
                        "PartitionKey": GUEST_PK,
                        "RowKey": new_code_hash,
                        "label": label,
                        "monthly_quota": int(e["monthly_quota"]),
                        "enabled": bool(e["enabled"]),
                    }
                )
                self._guests.delete_entity(GUEST_PK, e["RowKey"])
                return True
        return False

    def list_guests(self) -> list[dict[str, Any]]:
        return [
            {
                "label": e["label"],
                "monthly_quota": int(e["monthly_quota"]),
                "enabled": bool(e["enabled"]),
            }
            for e in self._guests.query_entities(f"PartitionKey eq '{GUEST_PK}'")
        ]

    # -- counters ---------------------------------------------------------
    def try_increment(self, pk: str, rk: str, limit: int) -> tuple[bool, int]:
        from azure.core import MatchConditions
        from azure.core.exceptions import (
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )

        for _ in range(8):
            try:
                e = self._usage.get_entity(pk, rk)
            except ResourceNotFoundError:
                if limit < 1:
                    return False, 0
                try:
                    self._usage.create_entity({"PartitionKey": pk, "RowKey": rk, "count": 1})
                    return True, 1
                except ResourceExistsError:
                    continue  # someone created it first: re-read
            n = int(e["count"])
            if n >= limit:
                return False, n
            try:
                self._usage.update_entity(
                    {"PartitionKey": pk, "RowKey": rk, "count": n + 1},
                    mode="merge",
                    etag=e.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True, n + 1
            except ResourceModifiedError:
                continue
        raise RuntimeError("Troppa contesa sul contatore, riprova.")

    def decrement(self, pk: str, rk: str) -> None:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

        for _ in range(8):
            try:
                e = self._usage.get_entity(pk, rk)
            except ResourceNotFoundError:
                return
            n = max(0, int(e["count"]) - 1)
            try:
                self._usage.update_entity(
                    {"PartitionKey": pk, "RowKey": rk, "count": n},
                    mode="merge",
                    etag=e.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return
            except ResourceModifiedError:
                continue

    def get_count(self, pk: str, rk: str) -> int:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return int(self._usage.get_entity(pk, rk)["count"])
        except ResourceNotFoundError:
            return 0

    def reset_count(self, pk: str, rk: str) -> None:
        self._usage.delete_entity(pk, rk)  # no error if it does not exist
