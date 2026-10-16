"""python -m watchover.api [--host 0.0.0.0] [--port 8000] [--reload]"""
import argparse
import os

import uvicorn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="watchover.api", description="Watchover HTTP API")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args(argv)
    # X-Forwarded-For / -Proto are believed only from WATCHOVER_FORWARDED_ALLOW: the edge (nginx) sets "*" through docker-compose.edge.yml,
    # where the plain port is not published and only the edge reaches it; the OpenShift chart sets it behind the router. Without an edge
    # the port is reached directly, so a client's own header is ignored (default: this host only).
    allow = os.environ.get("WATCHOVER_FORWARDED_ALLOW", "127.0.0.1").strip() or "127.0.0.1"
    uvicorn.run("watchover.api:app", host=a.host, port=a.port, reload=a.reload, log_level="info", proxy_headers=True, forwarded_allow_ips=allow)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
