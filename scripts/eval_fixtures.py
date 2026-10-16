"""Run the real extraction on a folder of notices and compare with the expected results.

    export ANTHROPIC_API_KEY=...
    python scripts/eval_fixtures.py ~/Documents/return-ledger-fixtures [--model ...] [--jobs 4]

The folder holds the PDFs plus `expected.json`, keyed by file name:
    {"note.pdf": {"row": {"date": "...", "type": "deposit", "amount": 1.0, "fees": 0, "tax": 0}},
     "statement.pdf": {"valuation": {"date": "...", "total": 1.0, "positions": 3}},
     "other.pdf": {"error": "not_a_trade"}}
Keep the folder out of git: documents and amounts are personal data.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from core.extract import extract, make_client  # noqa: E402
from core.ledger import to_row, to_valuations  # noqa: E402

FIELDS = ("date", "type", "amount", "fees", "tax")


def run_one(client, folder: Path, name: str, expected: dict, model: str | None) -> dict:
    started = time.monotonic()
    try:
        out = extract((folder / name).read_bytes(), client, model=model)
    except Exception as exc:  # report and keep going
        return {"name": name, "ok": False, "got": f"exception: {exc}", "secs": 0, "attempts": 0}
    secs = time.monotonic() - started
    rejected = out.statement is None and (
        out.trade is None or out.trade.operation == "other" or out.trade.multi_fill_detail)
    if out.statement is not None:
        val = to_valuations(out.statement)
        got = {"valuation": {"date": val["date"], "total": val["options"][0]["value"],
                             "positions": len(val["options"]) - 1},
               "status": "ok" if out.validation.ok else "needs_review"}
    elif rejected:
        got = {"error": out.validation.errors[0].code if out.validation.errors else "?"}
    else:
        row = to_row(out.trade)
        got = {"row": {k: row[k] for k in FIELDS}, "status": "ok" if out.validation.ok else "needs_review"}
    if "error" in expected:
        ok = got.get("error") == expected["error"]
    elif "valuation" in expected:
        ok = got.get("valuation") == expected["valuation"] and got["status"] == "ok"
    else:
        want = expected["row"]
        ok = ("row" in got and got["status"] == "ok"
              and all(got["row"][k] == want[k] if k in ("date", "type")
                      else abs(float(got["row"][k]) - float(want[k])) < 0.005 for k in FIELDS))
    notes = [f"{i.code}: {i.message}" for i in out.validation.errors + out.validation.warnings]
    return {"name": name, "ok": ok, "got": got, "secs": secs, "attempts": out.attempts, "notes": notes}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("folder", type=Path)
    p.add_argument("--model", default=None)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--only", default=None, help="substring filter on file names")
    args = p.parse_args()

    expected = json.loads((args.folder / "expected.json").read_text())
    names = [n for n in expected if not args.only or args.only in n]
    missing = [n for n in names if not (args.folder / n).exists()]
    for n in missing:
        print(f"MISSING  {n}")
    names = [n for n in names if n not in missing]

    client = make_client()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda n: run_one(client, args.folder, n, expected[n], args.model), names))

    for r in sorted(results, key=lambda r: (r["ok"], r["name"])):
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"{mark} {r['secs']:4.1f}s x{r['attempts']}  {r['name']}")
        if not r["ok"]:
            print(f"       expected {json.dumps(expected[r['name']])}")
            print(f"       got      {json.dumps(r['got'])}")
        for n in r.get("notes", []):
            if not r["ok"] or n.startswith("model_note"):
                print(f"       - {n}")
    passed = sum(r["ok"] for r in results)
    slowest = max((r["secs"] for r in results), default=0)
    print(f"\n{passed}/{len(results)} passed, {len(missing)} missing, slowest {slowest:.1f}s")
    return 0 if passed == len(results) and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
