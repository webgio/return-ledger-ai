#!/usr/bin/env python3
"""Manage demo guests (stored as a SHA-256 hash, the code is shown only once).

  # connection string: env AZURE_TABLES_CONNECTION_STRING if set (e.g. UseDevelopmentStorage=true
  # for Azurite), otherwise the macOS keychain item "return-ledger-tables" (production)
  python scripts/guests_admin.py add mario --quota 20
  python scripts/guests_admin.py list
  python scripts/guests_admin.py revoke mario
  python scripts/guests_admin.py enable mario
  python scripts/guests_admin.py rotate mario       # nuovo codice, il vecchio smette di funzionare
  python scripts/guests_admin.py quota mario 50     # nuova quota mensile, vale da subito
  python scripts/guests_admin.py reset mario        # azzera il consumo di questo mese

Il codice non si può rileggere (c'è solo l'hash): se un ospite lo perde, usa rotate.
"""
import argparse
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from core.gate import hash_code  # noqa: E402
from core.store import TableStore  # noqa: E402


KEYCHAIN_SERVICE = "return-ledger-tables"


def connection_string() -> str | None:
    """Env var wins (Azurite, overrides); otherwise the macOS keychain. Never printed."""
    conn = os.environ.get("AZURE_TABLES_CONNECTION_STRING")
    if conn:
        return conn
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("label")
    a.add_argument("--quota", type=int, default=20, help="analisi al mese (default 20)")
    sub.add_parser("list")
    for name in ("revoke", "enable", "reset", "rotate"):
        sub.add_parser(name).add_argument("label")
    q = sub.add_parser("quota")
    q.add_argument("label")
    q.add_argument("monthly_quota", type=int)
    args = p.parse_args()

    conn = connection_string()
    if not conn:
        print(
            "Imposta AZURE_TABLES_CONNECTION_STRING o salvala nel portachiavi "
            f"(servizio '{KEYCHAIN_SERVICE}').",
            file=sys.stderr,
        )
        return 2
    store = TableStore(conn)

    if args.cmd == "add":
        code = secrets.token_urlsafe(18)  # ~144 bit: not guessable
        try:
            store.put_guest(hash_code(code), args.label, args.quota)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Ospite '{args.label}' creato, quota {args.quota}/mese.")
        print(f"Codice (mostrato una sola volta): {code}")
    elif args.cmd == "rotate":
        code = secrets.token_urlsafe(18)
        if not store.rotate_guest(args.label, hash_code(code)):
            print(f"Ospite '{args.label}' non trovato.", file=sys.stderr)
            return 1
        print(f"Ospite '{args.label}': codice sostituito, il vecchio non vale più.")
        print(f"Nuovo codice (mostrato una sola volta): {code}")
    elif args.cmd == "list":
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        for g in sorted(store.list_guests(), key=lambda g: g["label"]):
            used = store.get_count(f"guest:{g['label']}", month)
            state = "attivo" if g["enabled"] else "REVOCATO"
            print(f"{g['label']:<20} {state:<9} {used}/{g['monthly_quota']} questo mese")
    elif args.cmd == "quota":
        if not store.set_guest_quota(args.label, args.monthly_quota):
            print(f"Ospite '{args.label}' non trovato.", file=sys.stderr)
            return 1
        print(f"Ospite '{args.label}': quota {args.monthly_quota}/mese.")
    elif args.cmd == "reset":
        if not any(g["label"] == args.label for g in store.list_guests()):
            print(f"Ospite '{args.label}' non trovato.", file=sys.stderr)
            return 1
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        store.reset_count(f"guest:{args.label}", month)
        print(f"Ospite '{args.label}': consumo di {month} azzerato.")
    else:
        enabled = args.cmd == "enable"
        if not store.set_guest_enabled(args.label, enabled):
            print(f"Ospite '{args.label}' non trovato.", file=sys.stderr)
            return 1
        print(f"Ospite '{args.label}' {'riattivato' if enabled else 'revocato'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
