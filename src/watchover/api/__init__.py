"""Watchover HTTP API (FastAPI) on top of the engine: phase 1 of the move away from Streamlit.

    python -m watchover.api --port 8000            # http://localhost:8000/api/docs
    uvicorn watchover.api:app --host 0.0.0.0 --port 8000

Sign in with POST /api/auth/login (e-mail + password, optional e-mail MFA step) and send the returned token as
`Authorization: Bearer …`. Permissions are the same RBAC keys the dashboard uses (page.*, act.*, sys.*).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"

from .. import __version__


def create_app(services=None) -> FastAPI:
    from .services import Services
    from .routers import actions, agents, anomalies, assist, auth, datasets, grok, inventory, itsm, knowledge, live, llm, map as map_, memory, notify, scan, playbook, sources, system, users

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.services.close()

    app = FastAPI(title="Watchover API", version=__version__, docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json", lifespan=lifespan,
                  description="Operational noise -> signals -> explained incidents -> tracked actions. Deterministic engine, no LLM in the loop.")
    app.state.services = services or Services()
    origins = [o.strip() for o in os.environ.get("WATCHOVER_CORS", "").split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if not request.url.path.startswith("/api/"):         # the SPA: allow its own inline styles/scripts, block plugins/embedding
            resp.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                    "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
        return resp
    for r in (auth, datasets, live, actions, anomalies, playbook, agents, sources, inventory, knowledge, notify, users, system, map_, assist, llm, itsm, grok, memory, scan):
        app.include_router(r.router, prefix="/api")

    @app.exception_handler(KeyError)
    async def _not_found(_: Request, e: KeyError):
        return JSONResponse({"detail": f"not found: {e.args[0] if e.args else ''}"}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad_request(_: Request, e: ValueError):
        return JSONResponse({"detail": str(e)[:300]}, status_code=400)

    @app.get("/api/health", tags=["system"])
    def health():
        svc = app.state.services
        return {"ok": True, "version": __version__, "database": svc.kb.backend, "live_events": svc.live.received}

    dist = Path(os.environ.get("WATCHOVER_WEB_DIST") or WEB_DIST)
    if (dist / "index.html").exists():                      # the React build: assets from /assets, every other path -> index.html (client routing)
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        _dist_root = dist.resolve()

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            index = _dist_root / "index.html"
            if not path:
                return FileResponse(str(index))
            try:
                f = (_dist_root / path).resolve()
            except (OSError, ValueError):
                return FileResponse(str(index))
            if f.is_file() and f.is_relative_to(_dist_root):     # never serve a file outside the built SPA (path traversal)
                return FileResponse(str(f))
            return FileResponse(str(index))
    return app


_app = None


def __getattr__(name):
    """`uvicorn watchover.api:app` builds the application lazily so importing the package stays cheap."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
