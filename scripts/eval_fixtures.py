"""Run the real extraction on a folder of notices and compare with the expected results.

    export ANTHROPIC_API_KEY=...
    python scripts/eval_fixtures.py <fixtures folder> [--model provider:model] [--jobs 4] [--csv run.csv]

The folder holds the PDFs plus `expected.json`, keyed by file name:
    {"note.pdf": {"row": {"date": "...", "type": "deposit", "amount": 1.0, "fees": 0, "tax": 0}},
     "statement.pdf": {"valuation": {"date": "...", "total": 1.0, "positions": 3}},
     "other.pdf": {"error": "not_a_trade"}}
Keep the folder out of git: documents and amounts are personal data.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from core.extract_lc import extract, make_chat_model  # noqa: E402
from core.ledger import to_row, to_valuations  # noqa: E402

FIELDS = ("date", "type", "amount", "fees", "tax")


def run_one(llm, folder: Path, name: str, expected: dict) -> dict:
    started = time.monotonic()
    try:
        out = extract((folder / name).read_bytes(), llm)
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


def write_csv(stream, results: list[dict], expected: dict) -> None:
    out = csv.writer(stream, lineterminator="\n")
    out.writerow(["file", "expected", "correct", "status", "seconds", "attempts", "notes"])
    for r in sorted(results, key=lambda r: r["name"]):
        got = r["got"]
        status = got.get("status", got.get("error", "")) if isinstance(got, dict) else "exception"
        out.writerow([r["name"], next(iter(expected[r["name"]])), "yes" if r["ok"] else "no", status,
                      f"{r['secs']:.2f}", r["attempts"], " | ".join(r.get("notes", []))])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("folder", type=Path)
    p.add_argument("--model", default=None)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--csv", type=Path, default=None, help="write the run statistics to this CSV file")
    p.add_argument("--only", default=None, help="substring filter on file names")
    args = p.parse_args()

    expected = json.loads((args.folder / "expected.json").read_text())
    names = [n for n in expected if not args.only or args.only in n]
    missing = [n for n in names if not (args.folder / n).exists()]
    for n in missing:
        print(f"MISSING  {n}")
    names = [n for n in names if n not in missing]

    llm = make_chat_model(args.model)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda n: run_one(llm, args.folder, n, expected[n]), names))

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
    mean = sum((r["secs"] for r in results), 0) / len(results) if results else 0
    print(f"\n{passed}/{len(results)} passed, {len(missing)} missing, slowest {slowest:.1f}s, mean {mean:.1f}s")

    if args.csv:
        with args.csv.open("w", newline="") as f:
            write_csv(f, results, expected)
        print(f"CSV written to {args.csv}")
    else:
        print("\nCSV")
        write_csv(sys.stdout, results, expected)
    return 0 if passed == len(results) and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
