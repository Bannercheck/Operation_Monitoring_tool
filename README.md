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

## Modüller

Kural: her dosya/modül için "ne işe yarar / hangi AI aracı / nasıl çalışır"
burada tutulur ve sadece ilgili satır güncellenir. Araç kısaltması:
**CF5.1** = claude-fable-5-1 (Claude Cowork).

| Yol | Ne işe yarar | AI aracı | Nasıl çalışır |
|-----|--------------|----------|---------------|
| `CLAUDE.md` | Claude için çalışma kuralları, her oturumda yüklenir | CF5.1 | Kullanıcı kuralları yazdı, dosyaya aktarıldı |
| `Makefile` | `make install / dev / api / web / test` | CF5.1 | Backend uvicorn :8000, frontend vite :5173 |
| `tools/inspect_dataset.py` | Bilinmeyen veri setini inceler: format, sütunlar, rol tahmini, zaman aralığı, severity dağılımı | CF5.1 | Stdlib only. ZIP/GZ/klasör açar, ilk 200 satırla format sniff eder (json/jsonl/csv/tsv/syslog/kv/plain), kayıtları çıkarır, sütun profili ve rol tahmini yapar. `--json` ile makine çıktısı |
| `backend/app/model/` | Canonical veri modeli: Observation, Signal, Incident (Rationale, Factor), Action | CF5.1 | Dataclass'lar. Her dataset satırı önce Observation olur, analiz katmanları sadece bunu görür |
| `backend/app/ingest/loader.py` | Upload'ı (dosya/ZIP/GZ) metin dosyalarına açar | CF5.1 | Recursive `iter_files(name, bytes)` |
| `backend/app/ingest/detect.py` | Format tespiti | CF5.1 | Her parser'ın `sniff()` skorunu karşılaştırır |
| `backend/app/ingest/schema.py` | Sütun -> rol (timestamp/severity/service/host/message) tahmini | CF5.1 | İsim eşleme heuristiği, UI'da onaylanır |
| `backend/app/ingest/parsers/` | jsonl, csv, syslog, kv, plain parser'ları | CF5.1 | `Parser` arayüzü: `sniff()` ve `parse()`. Gövdeler TODO |
| `backend/app/ingest/pipeline.py` | Upload bytes -> Observation listesi + dosya raporu | CF5.1 | loader -> detect -> parser, zamana göre sıralar |
| `backend/app/reduce/` | Drain şablon madenciliği, fingerprint, burst tespiti | CF5.1 | TODO |
| `backend/app/correlate/` | Ko-oküran graf ve kök neden seçimi | CF5.1 | TODO |
| `backend/app/rank/scoring.py` | Incident skor ağırlıkları | CF5.1 | Tek sözlük, sprint günü burada ayarlanır |
| `backend/app/explain/` | Rationale, şablon anlatı, postmortem export | CF5.1 | TODO |
| `backend/app/actions/tracker.py` | SQLite aksiyon takibi | CF5.1 | TODO |
| `backend/app/api/main.py` | FastAPI uçları | CF5.1 | Şimdilik sadece `/api/health` |
| `backend/tests/` | pytest | CF5.1 | `make test` |
| `frontend/` | React + Vite dashboard: Upload, Overview, Signals, IncidentDetail, Actions | CF5.1 | Router ile 5 sayfa, `src/api/client.js` backend istemcisi, `/api` proxy |
| `samples/` | Demo veri setleri | CF5.1 | TODO |
| `docs/PLAN.md`, `DECISIONS.md`, `DEMO_SCRIPT.md` | Hedef listesi, tasarım kararları, 7 dk demo akışı | CF5.1 | Sunumun üç bölümüne karşılık gelir |
