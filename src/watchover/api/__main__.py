"""python -m watchover.api [--host 0.0.0.0] [--port 8000] [--reload]"""
import argparse

import uvicorn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="watchover.api", description="Watchover HTTP API")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args(argv)
    # behind the edge container (Caddy) X-Forwarded-For / -Proto carry the real client; ./watchover.sh edge on binds the plain port to localhost
    uvicorn.run("watchover.api:app", host=a.host, port=a.port, reload=a.reload, log_level="info", proxy_headers=True, forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
