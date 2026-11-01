#!/usr/bin/env python3
"""Start a local MCP test server for one protocol revision.

For poking at murl by hand. The automated equivalent is
`pytest tests/test_dual_era_integration.py`, which starts both of these itself.

    python tests/serve_test_server.py 2025          # 2025-era, port 8765
    python tests/serve_test_server.py 2026          # 2026-07-28, port 8767
    python tests/serve_test_server.py 2026 --port 9000
    python tests/serve_test_server.py both          # both at once

Then, in another shell:

    murl http://localhost:8765/mcp tools/list --no-auth
    murl http://localhost:8767/mcp tools/list --no-auth

Both serve the same two tools (`echo`, `weather`) so the only difference between
them is the protocol revision — which is what makes them useful as a pair.

Output is NDJSON on stdout (one object per server started) so a script can read
the ports back; human-readable notes go to stderr. Exit codes: 0 ok, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_test_server import MCPJSONRPCHandler  # noqa: E402
from mcp_test_server_2026 import MCP2026Handler  # noqa: E402

REVISIONS = {
    "2025": {
        "handler": MCPJSONRPCHandler,
        "port": 8765,
        "label": "2025-era (initialize handshake, plain JSON)",
    },
    "2026": {
        "handler": MCP2026Handler,
        "port": 8767,
        "label": "2026-07-28 (server/discover, _meta envelope, Mcp-Method/Mcp-Name)",
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start a local MCP test server for one protocol revision.",
        epilog="Probe it with: murl http://localhost:<port>/mcp tools/list --no-auth",
    )
    parser.add_argument("revision", choices=[*REVISIONS, "both"], help="which revision to serve")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="override the default port (not valid with 'both')",
    )
    args = parser.parse_args(argv)

    if args.revision == "both" and args.port is not None:
        parser.error("--port cannot be used with 'both' (each revision needs its own port)")

    names = list(REVISIONS) if args.revision == "both" else [args.revision]
    servers: list[ThreadingHTTPServer] = []

    for name in names:
        spec = REVISIONS[name]
        port = args.port if args.port is not None else spec["port"]
        # ThreadingHTTPServer is required, not a preference: these handlers speak
        # HTTP/1.1, and a single-threaded server plus keep-alive deadlocks on the
        # second request of a reused connection.
        server = ThreadingHTTPServer(("localhost", port), spec["handler"])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        # NDJSON to stdout so a caller can parse the ports back out.
        print(
            json.dumps(
                {
                    "revision": name,
                    "url": f"http://localhost:{port}/mcp",
                    "port": port,
                    "description": spec["label"],
                }
            ),
            flush=True,
        )

    print("\nServing. Probe with:", file=sys.stderr)
    for server in servers:
        print(
            f"  murl http://localhost:{server.server_address[1]}/mcp tools/list --no-auth",
            file=sys.stderr,
        )
    print("Ctrl-C to stop.", file=sys.stderr)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopping.", file=sys.stderr)
        for server in servers:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
