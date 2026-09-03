"""The smallest process that satisfies orca's managed-server contract.

`LocalServerManager` needs exactly two things from a backend it started: a `GET /api/v1/health`
that answers 200, and `detail.managed_instance_id` echoing the identity orca minted in
`ORCA_MANAGED_INSTANCE_ID`. Everything else in the lifecycle — the receipt, the ownership check,
the graceful stop — is orca's own. So the lifecycle test starts this rather than a real harness,
which is also the honest statement of how little a harness must do to be process-managed.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast, override


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling
        if self.path.split("?")[0] != "/api/v1/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {
                "status": "ok",
                "detail": {"managed_instance_id": os.environ.get("ORCA_MANAGED_INSTANCE_ID", "")},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    options = parser.parse_args()
    host = cast(str, options.host)
    port = cast(int, options.port)
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
