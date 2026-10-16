"""Local dev server: app/ as static files + POST /api/extract on the same origin.

Runs the real handler (function_app.py) with an in-memory store and one guest code, so the
frontend can be tried without Azure Functions Core Tools or Azurite.

    export ANTHROPIC_API_KEY=...
    python scripts/dev_server.py [--port 8000] [--code demo] [--quota 20]

Local use only: no TLS, single process, quota resets on restart.
"""
from __future__ import annotations

import argparse
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import azure.functions as func  # noqa: E402

import function_app  # noqa: E402
from core import gate  # noqa: E402
from core.store import MemoryStore  # noqa: E402


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/api/extract":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        headers = dict(self.headers.items())
        headers.setdefault("x-forwarded-for", self.client_address[0])
        req = func.HttpRequest("POST", "/api/extract", headers=headers,
                               body=self.rfile.read(length))
        resp = function_app.extract_trade.build().get_user_function()(req)
        body = resp.get_body()
        self.send_response(resp.status_code)
        self.send_header("Content-Type", resp.mimetype or "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in resp.headers.items():
            if k.lower() not in ("content-type", "content-length"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # keep request bodies out of the log
        sys.stderr.write("%s %s\n" % (self.command, self.path))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--code", default="demo")
    p.add_argument("--quota", type=int, default=20)
    args = p.parse_args()

    store = MemoryStore()
    store.put_guest(gate.hash_code(args.code), "dev", args.quota)
    function_app._store = store

    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 partial(Handler, directory=str(ROOT / "app")))
    print(f"http://127.0.0.1:{args.port}/  access code: {args.code}")
    server.serve_forever()


if __name__ == "__main__":
    main()
