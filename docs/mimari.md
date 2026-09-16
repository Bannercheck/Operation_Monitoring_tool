# Mimari

## Genel Bakış

```
┌──────────────────────────────┐     ┌─────────────────────────────┐     ┌──────────────────────────┐
│  Kaynaklar                   │ ──► │  Alım katmanı               │ ──► │  Kanonik model           │
│  dosya / ZIP / klasör        │     │  loader · format_detector   │     │  Observation             │
│  HTTP API · MCP · ajan       │     │  parsers/* · normalize      │     │  (zaman, seviye, servis, │
│  ITSM ticket'ları            │     │  auto_map (sütun → rol)     │     │   host, ortam, kaynak…)  │
└──────────────────────────────┘     └─────────────────────────────┘     └───────────┬──────────────┘
                                                                                     │
                                                                          ┌──────────▼──────────────┐
                                                                          │  Motor (analysis)       │
                                                                          │  maske → şablon → parmak│
                                                                          │  izi → Signal (+burst)  │
                                                                          │  → correlate → Incident │
                                                                          │  kök neden · skor · kanıt│
                                                                          └──────────┬──────────────┘
                     ┌────────────────────┬───────────────────────┬──────────────────┤
              ┌──────▼──────┐     ┌───────▼───────┐      ┌────────▼────────┐  ┌──────▼──────┐
              │ Streamlit   │     │ CLI           │      │ MCP sunucusu    │  │ SQLite      │
              │ app.py      │     │ signal-sprint │      │ mcp_server.py   │  │ actions.db  │
              │ (5 sayfa)   │     │               │      │ (8 araç)        │  │ playbook.db │
              └─────────────┘     └───────────────┘      └─────────────────┘  └─────────────┘
```

## Bileşenler

| Bileşen | Sorumluluk | Teknoloji |
|---------|------------|-----------|
| `src/signal_sprint/loader.py` | Dosya / ZIP / TAR.GZ / GZ / klasörü açar, kodlama fallback | stdlib |
| `format_detector.py` | json / jsonl / csv / syslog / kv / text + güven skoru | stdlib |
| `parsers/*` | Her format → Observation; ham satır ve satır numarası korunur | stdlib csv / json |
| `normalize.py` | Zaman, seviye, ortam normalizasyonu; sütun → rol eşleme | dateutil |
| `analysis.py` | Maskeleme, parmak izi, patlama, ilişkilendirme, kök neden, skor, açıklama | Python |
| `profiler.py` | Veri seti profili (kaynaklar, servisler, ortamlar, yoğunluk) | pandas |
| `live.py` | Canlı alıcı (HTTP), halka tampon, metrikler, SLO / SLA, simülatör | stdlib http.server |
| `itsm.py` / `connectors.py` / `llm.py` | ITSM, HTTP API, MCP istemcisi, OpenAI uyumlu LLM | stdlib urllib |
| `playbook.py` / `actions.py` | Hata kütüphanesi ve aksiyon takibi | SQLite |
| `scenario/` | Etkinlik günü değişecek tek yer: eşleme, pencere, ağırlık, öneri, SLO | Python |
| `app.py` | Streamlit arayüzü (Operasyon, Datasets, Playbook, ITSM, Connection Settings) | Streamlit + Altair |
| `agent.py` | Uzak sunucu ajanı: log takibi ve host metrikleri | stdlib |
| `mcp_server.py` | Motoru MCP aracı olarak sunar | mcp ≥ 2 |

## Veri Modeli

```
Observation
  - timestamp: datetime (UTC)      - message: str          - severity: DEBUG..CRITICAL
  - service, host, environment, origin: str
  - attributes: dict               - source: dosya adı     - line_no: int
  - parser, parser_confidence      - template, fingerprint - raw: ham satır

Signal      fingerprint, template, count, first/last, services, hosts, severity, burst, evidence refs
Factor      name, weight, value, contribution, data
Incident    id, title, root_cause, signal_ids, score, factors, evidence, recommendations,
            origin, timing, recovery (restart / self_healed / stopped / ongoing)
Action      id, incident_id, title, priority, owner, status, evidence
```

## Harici Bağımlılıklar

- Streamlit, pandas, Altair — arayüz ve grafikler.
- python-dateutil — zaman parse son çare yolu.
- mcp (isteğe bağlı) — MCP sunucusu.
- OpenAI uyumlu bir LLM ucu (isteğe bağlı) — incident açıklaması; çekirdek ona bağlı değildir.
- ServiceNow / Jira / OneDesk REST (isteğe bağlı) — ticket ilişkilendirmesi.

## Güvenlik

- Sırlar `.env` üzerinden, repoya girmez; canlı alıcı `X-API-Key` / Bearer ister.
- Çalışma zamanında dışarıya veri gönderilmez; LLM çağrısı yalnız kullanıcı isterse ve verdiği uca yapılır.
- Yüklenen dosyalar bellekte işlenir; canlı olaylar yerel JSONL spool'a yazılır.

## Karar Kayıtları (ADR)

| # | Karar | Gerekçe |
|---|-------|---------|
| 1 | Deterministik çekirdek, çalışma zamanında LLM yok | Etkinlikte API garantisi yok; her karar faktör + kanıtla açıklanabilir |
| 2 | Her şey önce kanonik Observation'a indirgenir | Etkinlik günü yalnız parser / scenario katmanı değişir |
| 3 | El yazımı regex yerine maskeleme (uuid / ip / hex / ts / url / path / sayı) | Bilinmeyen mesaj şekilleri de gruplanır |
| 4 | Python + Streamlit + pandas, tek süreç | 3 saatlik sprint için en düşük risk; build adımı yok |
| 5 | Case'e özel kod yalnız `scenario/` | Motor genel kalır, ayar tek dosyada |
| 6 | LLM köprüsü isteğe bağlı (prompt paketi + OpenAI uyumlu uç) | İzinli araçla anahtar gerektirmeden zenginleştirme |
