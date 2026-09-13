# Signal Sprint

Operational noise in, ranked incidents with evidence out, actions tracked.

Built for AI Hackathon TR 2026 (Signal Sprint). Upload an unknown log /
event / alert dataset; the pipeline normalises it, collapses noise into
signals, correlates signals into incident candidates, explains every
decision with scored factors and evidence lines, and tracks follow-up
actions. No external API is required at runtime.

## Quick start

    make install
    make dev        # API on :8000, web on :5173

## Layout

    backend/app/ingest      loader, format detection, parsers -> Observation
    backend/app/reduce      Drain templates, fingerprints, burst detection
    backend/app/correlate   co-occurrence graph, root-cause ranking
    backend/app/rank        incident scoring (weights live in one place)
    backend/app/explain     rationale, narrative, postmortem export
    backend/app/actions     SQLite-backed action tracker
    backend/app/api         FastAPI routes
    frontend/               React + Vite dashboard (5 screens)
    samples/                demo datasets
    docs/                   PLAN, DECISIONS, AI_LOG, DEMO_SCRIPT

See docs/PLAN.md for goals and docs/DECISIONS.md for design rationale.
