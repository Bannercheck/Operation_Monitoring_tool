"""Watchover HTTP API (FastAPI) on top of the engine: phase 1 of the move away from Streamlit.

    python -m watchover.api --port 8000            # http://localhost:8000/api/docs
    uvicorn watchover.api:app --host 0.0.0.0 --port 8000

Sign in with POST /api/auth/login (e-mail + password, optional e-mail MFA step) and send the returned token as
`Authorization: Bearer …`. Permissions are the same RBAC keys the dashboard uses (page.*, act.*, sys.*).
"""
from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__


def create_app(services=None) -> FastAPI:
    from .services import Services
    from .routers import actions, agents, anomalies, auth, datasets, inventory, knowledge, live, notify, playbook, sources, system, users

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
    for r in (auth, datasets, live, actions, anomalies, playbook, agents, sources, inventory, knowledge, notify, users, system):
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

    return app


app = None


def __getattr__(name):
    """`uvicorn watchover.api:app` builds the application lazily so importing the package stays cheap."""
    global app
    if name == "app":
        if app is None:
            app = create_app()
        return app
    raise AttributeError(name)
