"""FastAPI application. Endpoints:

POST /api/upload            -> ingest report + schema guess
POST /api/analyze           -> run pipeline, return overview
GET  /api/signals           -> signal list
GET  /api/incidents         -> incident list
GET  /api/incidents/{id}    -> incident detail with rationale + evidence
GET  /api/incidents/{id}/postmortem
GET  /api/incidents/{id}/prompt   (Claude bridge)
CRUD /api/actions
"""

from fastapi import FastAPI

app = FastAPI(title="Signal Sprint")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
