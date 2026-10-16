"""Run the real extraction on a local PDF (manual check, not a test).

    export ANTHROPIC_API_KEY=...        # never commit it
    python scripts/try_extract.py tests/MPS_PDF.pdf [--model claude-...]

Prints the extracted trade, the checks and the proposed ledger row. Local use only: the
output contains data from the document.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from core.extract import extract, make_client  # noqa: E402
from core.ledger import to_row, to_valuations  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("pdf", type=Path)
    p.add_argument("--model", default=None)
    args = p.parse_args()

    started = time.monotonic()
    out = extract(args.pdf.read_bytes(), make_client(), model=args.model)
    elapsed = time.monotonic() - started

    print(f"attempts: {out.attempts}  pdf text: {out.pdf_text_available}  {elapsed:.1f}s")
    doc = out.trade or out.statement
    if doc:
        print(json.dumps(doc.model_dump(mode="json"), indent=2, ensure_ascii=False))
    print("ok:" if out.validation.ok else "NEEDS REVIEW:")
    for e in out.validation.errors:
        print(f"  error [{e.code}]: {e.message}")
    for w in out.validation.warnings:
        print(f"  warning [{w.code}]: {w.message}")
    if out.trade and out.trade.operation != "other":
        print("row:", json.dumps(to_row(out.trade), ensure_ascii=False))
    if out.statement:
        print("valuation:", json.dumps(to_valuations(out.statement), ensure_ascii=False))
    return 0 if out.validation.ok else 1


if __name__ == "__main__":
    sys.exit(main())
