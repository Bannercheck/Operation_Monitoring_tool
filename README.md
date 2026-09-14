# Signal Sprint

Operasyonel gürültüyü sinyallere, sinyalleri gerekçeli incident'lara, incident'ları takip edilen aksiyonlara çeviren
SRE karar destek uygulaması. Log / olay / alarm / metrik / ticket verisini dosyadan, API'den, MCP sunucusundan ya da
canlı ajanlardan alır; deterministik bir motorla analiz eder; her kararı kanıt satırlarıyla açıklar. Çalışma zamanında
LLM veya bulut servisi gerekmez; LLM opsiyonel zenginleştirmedir.

AI kaydı kuralı: bu depodaki her dosya için "hangi AI aracıyla üretildi" aşağıdaki dosya referansında tutulur.
**CF5.1** = Claude Fable 5.1 (claude-fable-5-1, Claude Cowork). Adım adım kayıt: `docs/AI_LOG.md`.

## 1. Kurulum ve çalıştırma

    python3 -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev,mcp]"            # dev: pytest · mcp: MCP sunucusu için SDK
    streamlit run app.py                   # http://localhost:8501 ; canlı alıcı :8600'de otomatik açılır

    python -m pytest -q                    # 27 test, ağ gerektirmez
    signal-sprint data.zip --inspect       # CLI: dosya başına format / roller / sütunlar
    signal-sprint data.zip                 # CLI: profil + incident özeti (--json ile makine çıktısı)
    python mcp_server.py --http --port 8765   # motor MCP sunucusu olarak (http://localhost:8765/mcp)
    docker compose up                      # dashboard :8501 + mcp :8765

Ortam değişkenleri: `ACTIONS_DB` (aksiyon SQLite yolu), `PLAYBOOK_DB` (hata kütüphanesi SQLite yolu), `LIVE_SPOOL` (canlı olay JSONL spool'u), `LIVE_PORT` (alıcı portu).
Streamlit ayarları `.streamlit/config.toml` (1 GB yükleme sınırı, koyu tema). Python 3.9+; 3.9 ve 3.11'de test edildi.

## 2. Sayfalar (sol menü)

| Sayfa | Ne yapar |
|---|---|
| 🏠 Operasyon | Ana sayfa. 2 sn'de bir yenilenir. Sırasıyla: **Altyapı** (host bazlı CPU / GPU / bellek / disk kartları + 15 dk seriler, eşik renkleri), **Servis seviyeleri** (erişilebilirlik vs SLO, p95 gecikme, hata bütçesi, SLA durumu, olay/dk, ERROR+), **olay akışı** (dakika × seviye alan grafiği, en yoğun servisler), **en ilgili 5 ITSM ticket'ı**, **son olaylar** konsolu. Ajan yoksa yerleşik simülatör akar ve SİMÜLASYON rozeti görünür; "Canlı tamponu analiz et" toplanan veriyi tam pipeline'a sokar |
| 🗄️ Datasets | Dosya / ZIP / TAR.GZ yükleme (1 GB), demo veri seti, canlı tamponu analiz et. Her set **ayrı tutulur**; **Aktif veri seti** dropdown'ı seçileni açar, "Kaldır" siler. **Veri setlerini karşılaştır**: A / B seçimi, KPI deltaları, seviye dağılımı, göreli zaman çizgisi, ortak / sadece A / sadece B sinyaller, incident listeleri. Aktif set için **log analizi sekmeleri**: Özet, Sinyaller, Incident'lar, Aksiyonlar (aşağıda) |
| 📚 Playbook | **Hata kütüphanesi.** Her analizde ERROR+ / patlayan sinyal şablonları ve incident kök nedenleri otomatik işlenir: kaç veri setinde, ilk/son görülme, olay sayısı, kök neden olma sayısı, gözlenen düzelme türleri (restart / kendiliğinden / durdu / devam), servisler, hostlar. Arama; kayıt seçince **Çözüm notu** ve **Runbook** (ilk bakılacak adımlar; `scenario.RECOMMENDATIONS`'tan otomatik tohumlanır) düzenlenip kaydedilir. Kalıcı (`playbook.db`), oturumlar ve veri setleri arası taşınır |
| 🎫 ITSM | Yapılandırma (ServiceNow / Jira Service Management / OneDesk / Generic REST / demo; URL, kullanıcı-şifre veya token, sorgu, alan eşleme), 60 sn otomatik yenileme, **ilişkilendirilmiş tam ticket tablosu** (ilgililik çubuğu, ilişkili sinyal/metrik/incident, gerekçe) ve ticket detayı |
| ⚙️ Connection Settings | **Veri kaynağı**: HTTP API (URL, GET/POST, başlıklar, gövde, JSON yolu) veya MCP sunucusu (URL, araç listesi, araç + argümanlar) → "Bağlan ve çek". **Canlı alım**: alıcı portu, API anahtarı, simülatör anahtarı, ajan komutları. **LLM**: OpenAI uyumlu base URL, model, anahtar, "Bağlantıyı test et" (OpenLLM, Ollama, vLLM, LM Studio, bulut) |
| 📖 README | Bu dosya |

### Log analizi sekmeleri (Datasets sayfasında, aktif set için)

| Sekme | İçerik |
|---|---|
| Özet | Tıklanabilir metrik kartları (ham olay, parmak izi, anlamlı sinyal, incident, aksiyon, dosya, kayıt, servis, host, hata sınıfı, kapsam; her kartın "Detay" paneli), aktivite zaman çizgisi (incident pencereleri gölgeli), zamana göre seviye, en yoğun servisler, kaynak türleri, öne çıkan incident kartları, Parser tablosu + sütun→rol eşleme düzeltme formu, oynatılabilir **canlı log akışı** (veri setini zaman sırasıyla oynatır; hız, satır, seviye ve metin filtresi) |
| Sinyaller | Filtreler (seviye, patlama, anlamlı, servis), progress sütunlu tablo, **NEDEN BU SİNYAL?** paneli: gerekçe, şablon, gruplama, patlama detayı, sinyalin dakika grafiği; **Nereden / Neden / Zaman / Ne yapmalı** kartı (kaynak dosyalar ve sayıları, ajanlar, servis/host; parmak izi + seviye + patlama gerekçesi; ilk sinyal, son hata, süre; öneriler); **tıklanabilir kanıt tablosu**: satıra tıklayınca tüm alanlar ve veri setindeki ham satır |
| Incident'lar | Seçilen incident için **flashcard**: ne oldu (başlık, olay/sinyal sayısı, zincir), neden (kök neden ve gerekçe), nerede, ne zaman (başladı, son hata, süre, sessiz süre), nasıl çözüldü (düzelme türü, saat, kanıt satırı), şimdi ne yapmalı, daha önce görüldü mü. Aynı flashcard Özet'teki incident detay panelinde satıra tıklayınca da açılır ("Incident sekmesinde aç" ile geçiş). Kök neden kartı, **Nereden** kartı (dosyalar, servisler, hostlar, ajanlar, parser, tür) ve **Zaman ve düzelme** kartı (ilk sinyal, son hata, hata süresi, ERROR+ sayısı, son hatadan beri sessiz süre; düzelme türü: restart sonrası / kendiliğinden / durdu-kanıt yok / devam ediyor; düzelme kanıtı satırı ve saati), **📚 Daha önce görüldü** kartı (kök neden şablonu playbook'ta varsa: kaç kez, hangi veri setleri, son görülme, düzelmeler, çözüm notu, runbook; benzer şablon için token örtüşmesi ≥ 0.6 ile bulanık eşleşme; "Playbook'ta aç"), yayılım Gantt'ı, skor faktör grafiği, zaman çizgisi, korelasyon bağları, tıklanabilir kanıt tablosu (tam kayıt + ham satır), önerilerden tek tıkla aksiyon, özel aksiyon formu, postmortem indir, LLM prompt popover'ı, **🤖 LLM ile açıkla** |
| Aksiyonlar | **Kendiliğinden düzelenler** bölümü (restart veya normal trafiğin dönmesiyle düzelen incident'lar, ne olduğu ve kanıtı); bu incident'lar için analiz anında otomatik olarak **done** durumunda bir aksiyon kaydı açılır ("Kendiliğinden düzeldi: … · kanıt", öneri: kalıcı düzeltmeyi doğrula; veri seti + incident başına bir kez). Öncelik renkli kanban: open / in_progress / done / suppressed |

Dil: sol üstte 🇹🇷 / 🇬🇧 anahtarı; arayüz, anlatı, gerekçe ve öneriler iki dilde.

## 3. Veri akışları

    dosya / API / MCP / ajan  ─►  loader  ─►  format_detector  ─►  parsers/*  ─►  Observation (kanonik model)
        ─►  analysis: template_of → fingerprint → Signal (+burst) → correlate → Incident (kök neden, skor, faktörler, kanıt, öneri)
        ─►  actions (SQLite)  ─►  app.py sayfaları

1. **Dosya:** Datasets sayfasından yükle → aynı pipeline → aktif veri seti.
2. **API / MCP:** Connection Settings → Veri kaynağı → "Bağlan ve çek".
3. **Canlı:** ajanlar `POST /ingest` ile olay **ve metrik** gönderir (herhangi bir format). Metrik için `{"host":..,"cpu":..,"gpu":..,"memory":..,"disk":..}` ya da `{"metric":"cpu","value":..}`.

        python agent.py --url http://<dashboard>:8600/ingest --key SECRET --tail /var/log/app.log      # log takibi
        python agent.py --url http://<dashboard>:8600/ingest --key SECRET --metrics --interval 10     # host CPU/GPU/bellek/disk
        python agent.py --url http://<dashboard>:8600/ingest --key SECRET --file export.csv            # tek seferlik
        python agent.py --url http://<dashboard>:8600/ingest --key SECRET --simulate                   # sentetik trafik
        curl -X POST http://<dashboard>:8600/ingest -H "X-API-Key: SECRET" --data-binary @app.log      # ajan olmadan

4. **ITSM:** ITSM sayfasından çekilen ticket'lar canlı hata sinyalleri, metrik eşik aşımları ve incident'larla ilişkilendirilir.
5. **MCP sunucusu:** `mcp_server.py` motoru 8 araçla herhangi bir MCP istemcisine açar.

## 4. Dosya referansı (her .py dosyası)

### Uygulama ve giriş noktaları

| Dosya | AI aracı | Ne yapar | Nasıl çalışır (fonksiyonlar) |
|---|---|---|---|
| `app.py` | CF5.1 | Streamlit arayüzü, tüm sayfalar | `wide()` Streamlit sürümüne göre `width="stretch"` / `use_container_width` seçer. `pill`, `kpi`, `kpi2` HTML kart/rozet üreticileri. `store()` / `live_store()` / `receiver()` / `simulator()` `st.cache_resource` ile süreç başına tekil nesneler (aksiyon DB, canlı tampon, HTTP alıcı, simülatör). `llm_cfg()` oturumdan LLM ayarı. `load()` bir veri setini alır → `Analysis` + `profile` → `st.status` adımlarıyla kayıt defterine (`datasets`) yazar; `activate_dataset()` seçilen seti görünümlere yansıtır; `record_auto_recoveries()` kendiliğinden düzelen incident'lara done aksiyon kaydı açar. `show_row()` bir gözlemin tüm alanları + ham satırı; `evidence_table()` satır seçimli kanıt tablosu (+ satır seçme kutusu); `recovery_label()`; `incident_flashcard()` (tek kartta ne oldu / neden / nerede / ne zaman / nasıl çözüldü / ne yapmalı / daha önce görüldü), `incidents_table()` (satır seçimli incident tablosu → flashcard); `page_playbook()`, `playbook_card()`. `minute_chart()` incident pencereli aktivite grafiği. Sol menü: dil, sayfa radyosu, 5 sn'de bir yenilenen durum satırı (`_status`). `fetch_tickets()` seçili ITSM sistemine göre çeker. `live_signals()` canlı tampondaki ERROR+ olaylardan şablon bazlı hızlı sinyaller. `correlated_tickets()` ticket'ları sinyal + eşik aşımı + incident ile ilişkilendirir; `tickets_df()` tabloya çevirir. `metrics_block`, `slo_block`, `events_block`, `tail_block` operasyon sayfasının parçaları. `page_ops()` (2 sn fragment), `page_itsm()` (yapılandırma + 5 sn fragment tablo + detay), `page_conn()` (üç sekme), `datasets_controls()` (yükleme, demo, canlı tampon, dropdown, kaldır, karşılaştır), `compare_view()` (A/B görünümü). Sayfa dağıtıcı `page` değerine göre çalışır. Analiz sekmeleri: `frames()` (cache'li DataFrame), `tile()` (tıklanabilir kart), detay panelleri, sinyal / incident / aksiyon sekmeleri, `live_panel` (oynatılabilir log akışı fragment'ı) |
| `agent.py` | CF5.1 | Uzak ajan CLI | `host_metrics()` loadavg / `/proc/meminfo` / `shutil.disk_usage` / `nvidia-smi` ile CPU, bellek, disk, GPU yüzdeleri (psutil gerekmez). `post()` `X-API-Key`, `X-Agent`, `X-File-Name` başlıklarıyla gönderir. `tail()` `tail -f` gibi dosyayı izler, 1 sn'lik partiler. `main()` `--tail` / `--file` / `--simulate` / `--metrics` modları |
| `mcp_server.py` | CF5.1 | Motor MCP sunucusu olarak | Araç fonksiyonları düz Python (test edilebilir): `analyze_dataset(path)`, `list_incidents()`, `get_incident(id)`, `list_signals(limit)`, `evidence(ref)`, `postmortem(id)`, `create_action(...)`, `list_actions()`. `build_server()` `mcp>=2` `MCPServer`'a araçları kaydeder; `main()` `--http` ile streamable HTTP (DNS rebinding koruması kapalı, Docker / uzak istemci için) ya da stdio |
| `samples/make_demo.py` | CF5.1 | Sentetik demo veri seti üreticisi | `samples/demo_mixed.zip`: 916 olay, 4 dosya (JSONL, düz log, syslog, CSV); 45 dk arka plan trafiği; gömülü zincir DB gecikmesi → payment timeout → checkout 500 → alarm → 14:36 PostgreSQL restart (systemd stop + "ready to accept connections"); ayrı disk dolu incident'ı |

### Motor (`src/signal_sprint/`)

| Dosya | AI aracı | Ne yapar | Nasıl çalışır (fonksiyonlar) |
|---|---|---|---|
| `models.py` | CF5.1 | Kanonik veri modeli | `Observation` (timestamp, message, kind, severity, service, host, attributes, source, line_no, parser, parser_confidence, template, fingerprint, **raw** = veri setindeki ham satır; `ref` = `dosya:satır`), `Signal` (`evidence`, `why()` kanıt + gerekçe + güven), `Factor` (ağırlık, değer, katkı, ham veri), `Incident` (kök neden, skor, faktörler, kanıt, zaman çizgisi, bağlar, öneriler, gerekçe kodları, `origin`, `timing`, `recovery`), `Action`. `SEV_RANK` seviye sırası |
| `loader.py` | CF5.1 | Universal loader | `decode()` utf-8-sig → utf-16 → latin-1; `iter_bytes()` ZIP / TAR(.GZ) / GZ'yi özyinelemeli açar, ikili dosyaları atlar; `iter_path()` dosya veya klasör |
| `format_detector.py` | CF5.1 | Format tespiti | `detect_delimiter()` `, ; \| tab`; `detect_format()` ilk 200 satırdan json (tek satırlık belge dahil) / jsonl / syslog / csv / kv / text, güven skoru |
| `normalize.py` | CF5.1 | Zaman, seviye, şema auto-mapper | `parse_timestamp()` epoch s/ms → `fromisoformat` → şekil imzasına göre önbelleklenen strptime → dateutil; hep tz-aware UTC. `normalize_severity()` warn/err/P1/major/syslog 0-7 → DEBUG..CRITICAL; `severity_from_text()`. `auto_map()` açık mapping > isim ipucu (İngilizce + Türkçe + Prometheus) > sonek > değer bazlı tahmin; sayısal sütunu mesaj seçmez. `flatten()` iç içe JSON'u `a.b` anahtarlarına açar |
| `parsers/base.py` | CF5.1 | Parser arayüzü | `Parser.records(text)` → `(satır_no, dict)`; `parse()` bunları Observation'a çevirir |
| `parsers/common.py` | CF5.1 | Kayıt → Observation | `records_to_observations()` rolleri ilk 50 kaydın anahtar birleşiminden çözer, her kayda ham satırı (`raw`) ekler (JSON dizisinde kaydın kendisi), zamansız satırları işaretler, dosya adına göre kind (alert / incident), rol dışı alanları `attributes`'a koyar |
| `parsers/json_parser.py` | CF5.1 | JSON | `find_records()` belgede iç içe ilk dict listesini bulur (Alertmanager `data.alerts`) |
| `parsers/jsonl_parser.py` | CF5.1 | JSONL / NDJSON | satır başına nesne, bozuk satırı atlar |
| `parsers/csv_parser.py` | CF5.1 | CSV / TSV | ayraç tespiti, başlık satırı 1, kayıtlar 2'den başlar |
| `parsers/syslog_parser.py` | CF5.1 | Syslog | RFC3164 + `<PRI>` (PRI % 8 → seviye), host, program, pid |
| `parsers/kv_parser.py` | CF5.1 | key=value (logfmt) | satır başı zaman / seviye / logger önekini de alır, tırnaklı değerler |
| `parsers/text_parser.py` | CF5.1 | Düz metin | öncü zaman, seviye, logger; kalan mesaj |
| `pipeline.py` | CF5.1 | Ingest orkestrasyonu | `ingest()` loader → detector → parser, dosya raporu (format, güven, satır, tür, anahtarlar, roller), `scenario.MAPPING`; `fill_missing_timestamps()` zamansız satırlara veri setinin en erken zamanı; `ingest_path()`, `ingest_bytes()` |
| `profiler.py` | CF5.1 | Dataset profiler | `bucket_for()` aralığa göre 1 dk / 5 dk / 1 sa / 1 gün; `profile()` pandas ile dosya, kayıt, kaynak türleri, servis/host, hata sınıfı, seviye, zaman aralığı, zaman serisi, dosyalar arası ilişki önerisi (id/host/service benzeri kolonlarda ≥ %50 değer örtüşmesi); `profile_text()` iki dilli özet |
| `analysis.py` | CF5.1 | Deterministik motor | `template_of()` tek birleşik regex ile uuid / ip / hex / zaman / url / yol / sayı maskeleme (+ `scenario.EXTRA_MASKS`); `entities_of()` servis / host / ip / id varlıkları; `fingerprint()` sha1(şablon + seviye + servis); `build_signals()` + `score_burst()` tepe vs tüm aralıktaki medyan; `interesting()` WARN+ veya burst ≥ 0.5; `correlate()` union-find: onset'ler pencere içinde VE (ortak varlık VEYA ikisi de burst); `pick_root_cause()` en erken onset (-3/dk), altyapı kelimesi (+1.5), fan-out, seviye; `build_incident()` (başlık şablon boşsa ham satırdan) `WEIGHTS` (burst .35, severity .25, blast_radius .25, duration .15) ile faktör katkıları, seviye, zaman çizgisi, kanıt, öneriler; `explain_incident()` her incident için **origin** (dosya sayıları, servis, host, ajan, parser, tür), **timing** (ilk sinyal, son hata, süre, sessiz süre, hata sayısı) ve **recovery** (son hatadan sonra ≥ 2 dk sessizlik; `RESTART_RE` ile restart/başlatma satırı → `restart`; INFO trafiğin dönmesi → `self_healed`; kanıt yoksa `stopped`; hata sona kadar sürüyorsa `ongoing`; kanıt satırı ve saati); `postmortem_md()`, `llm_prompt()`; `Analysis` sınıfı uçtan uca akış, `funnel()`, `signal_by_id`, `incident_by_id`, `obs_by_ref` indeksleri; `signal_dict()`, `incident_dict()` |
| `compare.py` | CF5.1 | İki veri setini karşılaştırma | `kpis()`; `compare()` KPI deltaları (Δ, Δ %), seviye dağılımı, ortak / sadece A / sadece B şablonlar (sayı ve patlama farkı), incident listeleri, göreli dakika zaman çizgisi |
| `actions.py` | CF5.1 | Aksiyon takibi | `ActionStore` SQLite: `list`, `create` (öncelik P1-P4, sorumlu, öneri, kanıt), `update` (open / in_progress / done / suppressed), `delete`, `get` |
| `live.py` | CF5.1 | Canlı alım | `LiveStore` olay halka tamponu + metrik tamponu + JSONL spool; `ingest()` gelen gövdeyi aynı parser'lardan geçirir, `metric_of()` ile cpu / gpu / memory / disk kayıtlarını ayırır; `stats()` dakika × seviye matrisi, servisler, ajanlar; `metric_stats()` host bazlı son değer, dakikalık seri, eşik aşımları, özet; `slo()` erişilebilirlik, mesajlardaki `<n>ms` ile p95, hata bütçesi, SLO / SLA durumu; `to_dataset()`; HTTP alıcı `make_handler` / `start_receiver` (`POST /ingest` X-API-Key veya Bearer, `GET /health`); `simulate_batch()`, `simulate_metrics()` (5 host + 2 GPU host, ~2 dk'da bir hata dalgası), `start_simulator()` |
| `itsm.py` | CF5.1 | ITSM entegrasyonu | `Ticket`; `fetch_servicenow()` Table API (basic / token, sysparm_query), `fetch_jira()` REST v3 (JQL, ADF açıklama), `fetch_onedesk()`, `fetch_generic()` (JSON yolu + alan eşleme), `demo_tickets()`; `correlate()` ticket metnindeki servis / host / anahtar kelimelerin canlı sinyaller, metrik eşik aşımları, incident'larla örtüşmesi (0.65) + açılış zamanının bir patlamaya ±30 dk yakınlığı (0.35) → ilgililik 0..1 ve gerekçeler, eşitlikte yeni ticket önce |
| `connectors.py` | CF5.1 | Veri kaynağı bağlayıcıları | `fetch_http()` GET / POST, başlıklar, JSON yolu (`dig()`), içerik türünden dosya adı; `McpClient` MCP streamable-HTTP JSON-RPC (initialize → notifications/initialized → tools/list, tools/call; Mcp-Session-Id, SSE cevap); `fetch_mcp()` bir aracın çıktısını veri seti yapar; `mcp_tools()`; `parse_headers()` |
| `llm.py` | CF5.1 | LLM istemcisi | `LLMConfig` (base_url, model, api_key); `list_models()` `/models`; `chat()` `/chat/completions`; `test_connection()`. Çekirdek buna bağlı değil |
| `i18n.py` | CF5.1 | TR / EN metinler | `STRINGS` sözlüğü; `t()` aktif dil; `reason_text()`, `link_text()`, `narrative_text()`, `factor_value()`, `recommendation_text()` motor kodlarını dile çevirir; `RECOMMENDATIONS_TR` |
| `cli.py` | CF5.1 | Komut satırı | `inspect()` dosya başına format, roller, zaman aralığı, seviye, sütun profili; `summary()`; `main()` `signal-sprint <path> [--inspect] [--json]` |
| `scenario/__init__.py` | CF5.1 | Yapılandırma | `MAPPING`, `EXTRA_MASKS`, `EXTRA_DEPENDENCY_WORDS`, `WINDOW_MIN`, `WEIGHTS`, `RECOMMENDATIONS`, `SLO`, `SLA`, `METRIC_THRESHOLDS` (cpu 85, gpu 95, memory 90, disk 90). Veri setine özel her ayar burada |

### Testler (`tests/`)

| Dosya | AI aracı | Kapsam |
|---|---|---|
| `test_pipeline.py` | CF5.1 | Format tespiti, 6 parser, değer bazlı auto-map, açık mapping, yardımcılar, ZIP + TAR.GZ, gerçek veri şekilleri (iç içe JSON, Türkçe CSV, kv öneki, zamansız satır), profil, patlama, incident zinciri / kök neden / gerekçe, aksiyon deposu, iki set karşılaştırma |
| `test_smoke.py` | CF5.1 | Streamlit `AppTest`: demo yükleme, dil geçişi, çoklu veri seti kaydı ve karşılaştırma |
| `test_connectors.py` | CF5.1 | HTTP bağlayıcı, MCP istemcisi (sahte JSON-RPC sunucu, SSE, oturum başlığı, hata), `dig`, MCP sunucu araçları |
| `test_live_llm.py` | CF5.1 | Alıcı kimlik doğrulama ve formatlar, simülatör, ajan dosya modu, sahte OpenAI uyumlu sunucuyla LLM istemcisi |
| `test_playbook.py` | CF5.1 | Kayıt, veri seti başına tekil sayım, düzelme türleri, runbook tohumlama, notlar, arama, bulanık eşleşme, silme |
| `test_itsm_metrics.py` | CF5.1 | Sahte ServiceNow / Jira / genel REST sunucularıyla fetch, ticket ilişkilendirme sıralaması, metrik alımı + eşik aşımı + SLO |

### Diğer dosyalar

| Dosya | Ne yapar |
|---|---|
| `pyproject.toml` | Paket tanımı, bağımlılıklar (streamlit, pandas, python-dateutil), `signal-sprint` CLI, `dev` / `mcp` ekstraları |
| `Makefile` | `make run / test / demo / inspect DS=x / analyze DS=x` |
| `.streamlit/config.toml` | 1 GB yükleme sınırı, koyu tema |
| `Dockerfile`, `docker-compose.yml` | `dashboard` (8501) ve `mcp` (8765) servisleri; `./data` → `/data`, `actions.db` paylaşımı |
| `samples/demo_mixed.zip` | Üretilmiş demo veri seti |
| `docs/PLAN.md`, `docs/DECISIONS.md`, `docs/AI_LOG.md` | Hedef listesi ve etkinlik planı, tasarım kararları, AI kullanım kaydı |
| `CLAUDE.md` | Bu depoda çalışan Claude için kurallar |

## 5. Teknolojiler

| Katman | Teknoloji | Neden |
|---|---|---|
| Arayüz | Streamlit + Altair | Tek süreç, build yok; `st.fragment(run_every)` ile canlı yenileme; Altair Streamlit ile gelir |
| Veri | pandas | Profil, gruplama, zaman kovaları |
| Ayrıştırma | stdlib `csv` / `json` / `re` / `zipfile` / `tarfile` / `gzip`, python-dateutil | Bağımlılık yok, her ortamda çalışır |
| Zaman | `fromisoformat` + strptime önbelleği + dateutil (son çare) | 300k satırda ~6 sn |
| Depolama | SQLite (aksiyonlar), JSONL spool (canlı olaylar) | Kurulumsuz |
| Bağlantılar | stdlib `urllib` (HTTP API, MCP JSON-RPC istemcisi, ITSM REST, OpenAI uyumlu LLM) | API anahtarı başlıkla, TLS varsayılan |
| MCP sunucusu | `mcp>=2` (`MCPServer`), streamable HTTP / stdio | İstemciden bağımsız |
| Ajan | stdlib (`urllib`, `/proc`, `shutil`, `nvidia-smi`) | Her host'ta çalışır |
| Test | pytest + `streamlit.testing.v1.AppTest` + in-process sahte HTTP sunucular | 27 test, ağ gerektirmez |
| Konteyner | Dockerfile, docker-compose | dashboard + mcp |

Performans: 300.000 satırlık JSONL ~15 sn (yükleme 6, analiz 7, profil 2).
