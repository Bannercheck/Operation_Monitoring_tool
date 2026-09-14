# Signal Sprint

Operational noise in, ranked incidents with evidence out, actions tracked.

Built for AI Hackathon TR 2026 (Signal Sprint). Upload an unknown log /
event / alert dataset; the pipeline normalises it, collapses noise into
signals, correlates signals into incident candidates, explains every
decision with scored factors and evidence lines, and tracks follow-up
actions. No external API is required at runtime.

## Quick start

    pip install -e ".[dev]"
    streamlit run app.py            # sidebar: upload a dataset, or click "Load demo dataset"
    signal-sprint data.zip          # CLI: profile + incidents as text   (--json for machine output)
    signal-sprint data.zip --inspect   # per-file format, roles, columns: first 10 minutes with a new dataset
    make test

Python 3.9+ (3.9 ve 3.11 üzerinde test edildi), Streamlit + pandas + python-dateutil. No LLM, API key or network needed at runtime.
Performans: 300.000 satırlık JSONL ~15 sn (yükleme 6, analiz 7, profil 2).

## Kullanım kılavuzu

### Kurulum ve çalıştırma

    python3 -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev,mcp]"
    streamlit run app.py                 # http://localhost:8501  (canlı alıcı da :8600'de otomatik açılır)

    docker compose up                    # dashboard :8501 + MCP sunucusu :8765 (Docker ile)

### Sol menü

| Bölüm | İçerik |
|---|---|
| ⚙️ Connection Settings → Veri kaynağı | HTTP API (URL, GET/POST, başlıklar, gövde, JSON yolu) veya MCP sunucusu (URL, araç listesi, araç + argümanlar). "Bağlan ve çek" veri setini indirip analiz eder |
| ⚙️ Connection Settings → Canlı alım | Alıcı portu ve API anahtarı; ajan komutu; yerleşik simülatör aç/kapat |
| ⚙️ Connection Settings → LLM | OpenAI uyumlu yerel/uzak LLM: base URL, model, API anahtarı, bağlantı testi. OpenLLM `:3000/v1`, Ollama `:11434/v1`, vLLM `:8000/v1`, LM Studio `:1234/v1`, bulut API'leri |
| 🗄️ Datasets | Dosya / ZIP / TAR.GZ yükleme (1 GB), demo veri seti, canlı tamponu analiz et, yüklü ve son veri setleri |
| 📖 README | Bu dosya, uygulama içinde |

### Veri akışları

1. **Dosya:** yükle → format tespiti → parser → analiz → Özet.
2. **API / MCP:** Connection Settings'ten çek → aynı pipeline.
3. **Canlı:** ajanlar `POST /ingest` ile olay gönderir (herhangi bir format), açılış sayfası ve "Canlı" sekmesi 2 saniyede bir güncellenir, "Canlı tamponu analiz et" ile tam pipeline çalışır.

    python agent.py --url http://<dashboard>:8600/ingest --key SECRET --tail /var/log/app.log   # log dosyasını takip et
    python agent.py --url http://<dashboard>:8600/ingest --key SECRET --file export.csv          # tek seferlik gönder
    python agent.py --url http://<dashboard>:8600/ingest --key SECRET --simulate                 # sentetik trafik

    curl -X POST http://<dashboard>:8600/ingest -H "X-API-Key: SECRET" --data-binary @app.log     # ajan olmadan

4. **MCP sunucusu:** `python mcp_server.py --http --port 8765` ile motor herhangi bir MCP istemcisine açılır (8 araç).

### Teknolojiler

| Katman | Teknoloji | Neden |
|---|---|---|
| Arayüz | Streamlit + Altair | Tek süreç, build yok, 3 saatlik sprintte en düşük risk; Altair grafikler Streamlit ile gelir |
| Veri | pandas | Profil, gruplama, zaman kovaları |
| Ayrıştırma | stdlib `csv` / `json` / `re` / `zipfile` / `tarfile` / `gzip`, python-dateutil | Bağımlılık yok, her ortamda çalışır |
| Zaman | `datetime.fromisoformat` + strptime önbelleği + dateutil (son çare) | 300k satırda 6 sn |
| Depolama | SQLite (aksiyonlar), JSONL spool (canlı olaylar) | Kurulumsuz |
| Bağlantılar | stdlib `urllib` (HTTP API, MCP JSON-RPC istemcisi, OpenAI uyumlu LLM istemcisi) | API anahtarı başlıkla, TLS varsayılan |
| MCP sunucusu | `mcp>=2` (`MCPServer`), streamable HTTP / stdio | İstemciden bağımsız |
| Test | pytest + `streamlit.testing.v1.AppTest` + in-process HTTP sahte sunucular | 20+ test, ağ gerektirmez |
| Konteyner | Dockerfile, docker-compose | dashboard + mcp servisleri |

### Parser'lar (`src/signal_sprint/parsers/`)

| Parser | Ne okur | Örnek |
|---|---|---|
| `json_parser` | JSON dizisi veya belgede iç içe ilk dict listesi | Alertmanager `{"data":{"alerts":[…]}}` |
| `jsonl_parser` | Satır başına JSON nesnesi (NDJSON) | uygulama logları |
| `csv_parser` | CSV / TSV, ayraç tespiti (`,` `;` `|` tab) | ITSM dışa aktarımı, Türkçe başlıklar |
| `syslog_parser` | RFC3164 + `<PRI>` | `Sep 16 14:31:02 db-01 postgres[4412]: …` |
| `kv_parser` | key=value (logfmt), satır başı zaman/seviye/logger öneki | `ts=… level=warn svc=checkout msg="…"` |
| `text_parser` | Düz metin: öncü zaman, seviye, logger | Java/Python uygulama logları |

Format seçimi `format_detector.py` ile ilk 200 satırdan; sütun → rol eşlemesi `normalize.auto_map` ile
(isim ipuçları İngilizce + Türkçe + Prometheus, sonra değer bazlı tahmin), gerekirse Özet'teki Parser tablosundan elle düzeltilir.

### Üreteçler ve örnek veri

| Üreteç | Ne üretir |
|---|---|
| `samples/make_demo.py` | `samples/demo_mixed.zip`: 914 olay, 4 dosya (JSONL, düz log, syslog, CSV); DB gecikmesi → payment timeout → checkout 500 → alarm zinciri + disk dolu incident'ı + arka plan gürültüsü |
| `signal_sprint.live.simulate_batch` | Canlı simülatör: 5 servis, 5 host, ~2 dakikada bir hata dalgası |
| `agent.py --simulate` | Aynı üreteci uzaktan ajan olarak çalıştırır |

## Mimari

    UNKNOWN DATASET -> loader (zip/gz/tar/dir, encoding fallback) -> format_detector -> parsers/* -> Observation
      -> analysis: fingerprint (masked template) -> Signal + burst -> correlation (time + shared entities)
      -> Incident: root cause, weighted score, factors, evidence, recommendations -> actions (SQLite) -> app.py

Generic core: `src/signal_sprint/`. Case-specific code on hackathon day: **only** `src/signal_sprint/scenario/`.

## Modüller

Kural: her dosya/modül için "ne işe yarar / hangi AI aracı / nasıl çalışır"
burada tutulur ve sadece ilgili satır güncellenir. Araç kısaltması:
**CF5.1** = claude-fable-5-1 (Claude Cowork).

| Yol | Ne işe yarar | AI aracı | Nasıl çalışır |
|-----|--------------|----------|---------------|
| `CLAUDE.md` | Claude için çalışma kuralları, her oturumda yüklenir | CF5.1 | Kullanıcı kuralları yazdı, dosyaya aktarıldı |
| `.streamlit/config.toml` | Streamlit sunucu ve tema ayarları | CF5.1 | `maxUploadSize = 1024` (MB): 1 GB'a kadar dosya; koyu tema (yeşil vurgu); kullanım istatistiği kapalı |
| `src/signal_sprint/i18n.py` | TR/EN metinler ve yerelleştirilmiş anlatı | CF5.1 | `t(key)` aktif dili session state'ten okur (varsayılan TR). Kök neden gerekçesi ve korelasyon bağları motorda **kod** olarak tutulur (`root_cause_codes`, `links[].ents/gap`), metin dile göre burada üretilir: `reason_text`, `link_text`, `narrative_text`, `factor_value`, `recommendation_text`. CLI/postmortem İngilizce |
| `pyproject.toml`, `Makefile` | Paket tanımı, `signal-sprint` CLI girişi, `make run / test / inspect DS=x` | CF5.1 | `pip install -e .` ile kurulur, `src/` layout |
| `app.py` | Streamlit dashboard (frontend), TR/EN | CF5.1 | `wide()` yardımcısı Streamlit sürümüne göre `width="stretch"` ya da `use_container_width=True` seçer (1.50 ile 1.63 arası uyumlu). Açılış: sadece yükleme alanı, demo butonu ve 4 adımlık ikonlu pipeline şeridi (metin yok). Metrik kartları tasarımlı (ikon, metriğe özel vurgu rengi, gradyan, bağlam satırı: ERROR+ oranı, azaltma katsayısı, en güçlü sinyal, kritik sayısı, açık aksiyon, format listesi, en büyük dosya, en yoğun servis / host, en sık hata, zaman aralığı) ve tıklanabilir: her kartın altındaki "Detay" o alanın panelini açar (tüm gözlemler, parmak izleri, anlamlı sinyaller, incident'lar, aksiyonlar, dosyalar + parser sonuçları, servis / host başına olay-hata tablosu ve grafiği, hata sınıfları, zaman kapsamı ve dosya başına zaman aralığı Gantt'ı). Sol menü üç bölüm: **Connection Settings** (Veri kaynağı HTTP/MCP · Canlı alım port/anahtar/ajan komutu/simülatör · LLM base URL/model/anahtar/test), **Datasets** (yükle, demo, canlı tamponu analiz et, son yüklenenler), **README**. Açılış sayfası ve "⚡ Canlı" sekmesi: 2 sn'de bir yenilenen canlı akış (ajan/olay/hata/tampon kartları, dakika × seviye alan grafiği, servisler, son olaylar konsolu; ajan yoksa SİMÜLASYON rozeti). Incident'ta "🤖 LLM ile açıkla" yapılandırılmış LLM'e kanıt paketini gönderir. Yükleme anında `st.status` ile adım adım ilerleme (ayrıştırma → analiz → profil) ve otomatik olarak Özet'e geçiş. Özet altında katlanır **Parser** tablosu (dosya, format, güven, satır, tür, roller) ve **eşleme düzeltme formu**. **Özet**: huni kartları, profil kartları (dosya/kayıt/servis/host/hata sınıfı/kapsam), incident pencereleri gölgeli aktivite grafiği, zamana göre seviye alan grafiği, en yoğun servisler (seviye renkli), kaynak türleri halkası, öne çıkan incident kartları, **canlı log akışı** (veri setini zaman sırasıyla oynatır: oynat/duraklat/başa al, hız, satır sayısı, seviye ve metin filtresi, konum kaydırıcısı; `st.fragment(run_every)` ile saniyede bir ilerler). **Sinyaller**: seviye/patlama/servis filtreleri, progress sütunlu tablo, "NEDEN BU SİNYAL?" paneli (gerekçe, şablon, gruplama, patlama, dakika grafiği, kanıt tablosu). **Incident'lar**: kök neden kartı, yayılım Gantt'ı, skor faktör grafiği, zaman çizgisi, korelasyon bağları, ham kanıt satırı, tek tıkla öneriden aksiyon, özel aksiyon formu, postmortem indir, LLM prompt popover. **Aksiyonlar**: öncelik renkli kanban (open / in_progress / done / suppressed) |
| `src/signal_sprint/models.py` | `Observation`, `Signal` (+`why()`), `Factor` (+`data`), `Incident` (+`root_cause_codes`), `Action` | CF5.1 | Dataclass'lar. Her satır önce Observation olur; `ref` = `dosya:satır` kanıt adresi, `parser_confidence` taşır |
| `src/signal_sprint/loader.py` | Universal loader | CF5.1 | Dosya / ZIP / GZ / TAR.GZ / klasör, recursive; utf-8-sig → utf-16 → latin-1 encoding fallback; ikili dosyaları atlar |
| `src/signal_sprint/format_detector.py` | Format tespiti | CF5.1 | İlk 200 satırla json / jsonl / csv-tsv / syslog / kv / text seçer, güven skoru döner; ayraç tespiti |
| `src/signal_sprint/normalize.py` | Zaman, severity, **schema auto-mapper** | CF5.1 | `parse_timestamp` hızlı yollar: epoch s/ms → `fromisoformat` → şekil imzasına göre önbelleklenen strptime formatı → son çare dateutil (300k satırda 31 sn → 6 sn). `normalize_severity` warn/err/P1/major/syslog 0-7 → DEBUG..CRITICAL. `auto_map`: açık mapping > isim ipucu (İngilizce + Türkçe: zaman, öncelik, uygulama, sunucu, özet…; Prometheus: activeAt, labels.severity, labels.job, annotations.summary) > sonek > değer bazlı tahmin |
| `src/signal_sprint/parsers/` | `base.py` arayüz, `common.py` kayıt → Observation, json / jsonl / csv / kv / syslog / text parser'ları | CF5.1 | Her parser `(satır_no, dict)` üretir. json: belgede iç içe ilk dict listesini bulur (Alertmanager `data.alerts` gibi). kv: satır başındaki zaman / seviye / logger önekini de alır. `common` rolleri ilk 50 kaydın anahtar birleşiminden çözer, zamanı olmayan satırı işaretler (pipeline veri setinin en erken zamanıyla doldurur, 1970'e düşmez), dosya adına göre kind (alert/incident) atar |
| `src/signal_sprint/pipeline.py` | Ingest orkestrasyonu | CF5.1 | loader → detector → parser; `scenario.MAPPING` uygulanır; zamana göre sıralı Observation listesi + dosya raporu (format, güven, satır, tür, anahtarlar, roller) |
| `src/signal_sprint/profiler.py` | **Dataset profiler** | CF5.1 | pandas ile: dosya/kayıt sayısı, olası kaynak türleri, servis/host/hata sınıfı sayısı, zaman aralığı, dakikalık seri, dosyalar arası ilişki önerisi (id/host/service benzeri kolonlarda ≥ %50 değer örtüşmesi). zaman kovası aralığa göre otomatik (1 dk / 5 dk / 1 sa / 1 gün) |
| `src/signal_sprint/analysis.py` | Deterministik motor | CF5.1 | `template_of` uuid/ip/hex/ts/url/path/sayı maskelerini **tek birleşik regex** ile uygular (+`scenario.EXTRA_MASKS`); varlık çıkarımı sinyal başına 40 örnek satırda; fingerprint = sha1(template+severity+service). `score_burst`: peak vs tüm aralıktaki medyan. `correlate`: union-find, onset'ler pencere içinde VE (ortak varlık VEYA ikisi de burst). `pick_root_cause`: en erken onset (-3/dk), altyapı kelimesi (+1.5), fan-out, severity. `build_incident`: `WEIGHTS` burst .35 / severity .25 / blast_radius .25 / duration .15, faktör katkıları toplamı = skor, öneriler `scenario.RECOMMENDATIONS`'tan. `postmortem_md`, `llm_prompt` (opsiyonel, SAKA'ya yapıştırılır) |
| `src/signal_sprint/actions.py` | Aksiyon takibi (SQLite) | CF5.1 | title, priority P1-P4, status open/in_progress/done/suppressed, owner, recommendation, evidence |
| `src/signal_sprint/connectors.py` | Veri kaynağı bağlayıcıları: HTTP API ve MCP istemcisi | CF5.1 | Stdlib. `fetch_http` (GET/POST, başlıklar, JSON yolu ile daraltma, içerik türünden dosya adı). `McpClient`: MCP streamable-HTTP JSON-RPC (initialize → notifications/initialized → tools/list, tools/call; Mcp-Session-Id ve SSE cevapları desteklenir). `fetch_mcp` bir aracın metin çıktısını veri seti olarak döner |
| `mcp_server.py` | Motor bir **MCP sunucusu** olarak: analyze_dataset, list_incidents, get_incident, list_signals, evidence, postmortem, create_action, list_actions | CF5.1 | `mcp>=2` SDK (`MCPServer`). `--http --port 8765` ile streamable HTTP (`/mcp`, DNS rebinding koruması kapalı, Docker/uzak istemci için), argümansız stdio. İstemciden bağımsız: Claude SAKA / Desktop, Cursor, özel ajanlar veya dashboard'un kendi Bağlantı paneli |
| `Dockerfile`, `docker-compose.yml` | Konteyner: `dashboard` (8501) ve `mcp` (8765) servisleri | CF5.1 | `docker compose up` ikisini de kaldırır; `./data` klasörü `/data` olarak bağlanır, `actions.db` paylaşılır. Bu ortamda Docker olmadığından build burada denenmedi |
| `src/signal_sprint/live.py` | Canlı alım: `LiveStore` (halka tampon + JSONL spool), HTTP alıcı (`POST /ingest`, X-API-Key / Bearer, `GET /health`), `simulate_batch`, `start_simulator` | CF5.1 | Gelen gövde aynı parser'lardan geçer; zamansız satırlara alınma anı yazılır; `stats()` son 15 dk'nın dakika × seviye matrisini, servisleri ve ajanları döner; `to_dataset()` tamponu JSONL yapar |
| `agent.py` | Ajan CLI: `--tail` (log takibi, 1 sn'lik partiler), `--file` (tek seferlik), `--simulate` | CF5.1 | Stdlib; X-Agent ve X-File-Name başlıkları |
| `src/signal_sprint/llm.py` | OpenAI uyumlu LLM istemcisi: `list_models`, `chat`, `test_connection` | CF5.1 | Opsiyonel zenginleştirme; çekirdek buna bağlı değil, hata olursa deterministik anlatı kalır |
| `src/signal_sprint/cli.py` | `signal-sprint <path> [--inspect] [--json]` | CF5.1 | `--inspect`: dosya başına format, roller, zaman aralığı, severity, kolon profili. Varsayılan: profil özeti + incident'lar |
| `src/signal_sprint/scenario/__init__.py` | **Hackathon günü dokunulacak tek yer** | CF5.1 | MAPPING, EXTRA_MASKS, EXTRA_DEPENDENCY_WORDS, WINDOW_MIN, WEIGHTS, RECOMMENDATIONS |
| `samples/make_demo.py` | Sentetik demo veri seti (`demo_mixed.zip`) | CF5.1 | 914 olay, 4 dosya (jsonl, text log, syslog, csv). Zincir: DB gecikmesi → payment timeout → checkout 500 → alarm; ayrıca worker-02 disk dolu; arka plan gürültüsü |
| `tests/test_smoke.py` | Paket + pipeline + Streamlit AppTest | CF5.1 | Demo yükle, huni 914, TR→EN dil geçişi |
| `tests/test_connectors.py` | 4 test: HTTP bağlayıcı (ZIP + JSON yolu), MCP istemcisi (sahte JSON-RPC sunucu, SSE cevap, oturum başlığı, hata), `dig`, MCP sunucu araçları doğrudan | CF5.1 | In-process HTTP sunucu ile |
| `tests/test_pipeline.py` | 10 test: tespit, parser'lar, auto-map, yardımcılar, ZIP + TAR.GZ, profil, burst, incident zinciri/kök neden/gerekçe, aksiyonlar | CF5.1 | `make test` |
| `docs/PLAN.md`, `docs/DECISIONS.md` | Hedef listesi ve tasarım kararları | CF5.1 | Sunumun "planlama" bölümüne kaynak |
| `docs/AI_LOG.md` | AI kullanım kaydı: araç + sürüm, tarih, yapılan geliştirme | CF5.1 | Sadece geliştirme adımları; etkinlik günü bölümü hazır. Sunumun "AI stratejisi" bölümünün kaynağı |

## Demo akışı (7 dk)

1. Sidebar → "Demo veri setini yükle". Özet: huni 914 → 11 → 8 → 2, grafikler (aktivite + incident pencereleri, zamana göre seviye, servisler, kaynaklar), canlı log akışını oynat: 14:31'de hata dalgası akarken görülür. Dil anahtarı ile EN'e geçip aynı ekranı göster.
2. Signals: 532 satırlık sağlıklı trafik tek satır (burst 0), timeout sinyali burst 1.0. "WHY THIS SIGNAL?" ile kanıt + gerekçe + güven.
3. Incidents → INC-1: kök neden postgres gecikmesi, 4 belirti timeline'da, faktör grafiği, kanıt satırı, öneriler.
4. Aksiyon oluştur (P1, owner), Actions sekmesinde in_progress → done. Postmortem indir. LLM prompt'unu göster.
5. Kapanış: plan vs gerçekleşen (docs/PLAN.md), AI kullanımı (bu tablo).

## Hackathon günü (14:20 → 17:30)

| Saat | İş |
|---|---|
| 14:20–14:30 | `signal-sprint data.zip --inspect`: dosyalar, formatlar, roller |
| 14:30–14:45 | Dashboard'a yükle, profil ve rol tahminini kontrol et |
| 14:45–15:10 | Gerekirse `scenario.MAPPING` / yeni parser |
| 15:10–15:50 | `scenario`: pencere, ağırlıklar, bağımlılık kelimeleri, maskeler |
| 15:50–16:25 | Öneriler, opsiyonel LLM zenginleştirme |
| 16:25–16:50 | Demo verisi ile aksiyon akışı provası |
| 16:50–17:10 | Edge case, testler |
| 17:10–17:25 | README, AI kullanım hikâyesi, sunum |
| 17:25 | Code freeze, push, GitHub doğrulama |
