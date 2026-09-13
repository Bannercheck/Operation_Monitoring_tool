# Plan

## Pre-sprint goals (framework)
- [x] Universal ingestion: ZIP/JSON/JSONL/CSV/syslog/KV/plain -> Observation
- [x] Noise reduction: template masking + fingerprints + burst detection
- [x] Correlation: time window + shared entities -> incident candidates + root cause
- [x] Explainability: scored factors + evidence lines per decision
- [x] Action tracker with lifecycle (SQLite)
- [x] Web dashboard: Upload, Overview, Signals, Incident, Actions (embedded, stdlib server)
- [x] Postmortem export + Claude SAKA prompt bundle

## Sprint-day goals (filled in at 14:20 on event day)
- [ ] Adapt parser to the given dataset
- [ ] Tune burst / correlation windows
- [ ] Validate top incidents manually
- [ ] Demo script rehearsed
