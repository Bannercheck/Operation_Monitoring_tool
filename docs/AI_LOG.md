# AI Kullanım Kaydı

Sadece geliştirme adımları. Format: araç + sürüm · tarih · yapılan iş.

| Araç + sürüm | Tarih | Yapılan düzeltme / geliştirme |
|---|---|---|
| Claude Fable 5.1 | 2026-09-13 | Proje iskeleti: app.py, src/signal_sprint paketi, docs, Makefile, pyproject |
| Claude Fable 5.1 | 2026-09-13 | Format detector: dosya / ZIP / GZ / TAR.GZ / klasör açma, json / jsonl / csv-tsv / syslog / kv / text tespiti |
| Claude Fable 5.1 | 2026-09-13 | Parser'lar (6 format), zaman damgası ve severity normalizasyonu, sütun → rol auto-mapper |
| Claude Fable 5.1 | 2026-09-13 | Analiz motoru: fingerprint, burst tespiti, korelasyon, kök neden, skor + faktör gerekçesi, postmortem, LLM prompt paketi |
| Claude Fable 5.1 | 2026-09-13 | Dataset profiler ve SQLite aksiyon takibi |
| Claude Fable 5.1 | 2026-09-13 | Streamlit dashboard: Özet, Sinyaller, Incident'lar, Aksiyonlar |
| Claude Fable 5.1 | 2026-09-13 | Sentetik demo veri seti üreticisi ve 11 test (pytest + Streamlit AppTest) |
| Claude Fable 5.1 | 2026-09-13 | Python 3.9+ desteği, 3.9 ve 3.11 üzerinde test |
| Claude Fable 5.1 | 2026-09-13 | Yükleme sınırı 1 GB, koyu tema (.streamlit/config.toml) |
| Claude Fable 5.1 | 2026-09-13 | Arayüz yeniden tasarımı: huni kartları, incident pencereli aktivite grafiği, WHY THIS SIGNAL paneli, yayılım Gantt'ı, faktör grafiği, tek tıkla aksiyon, kanban |
| Claude Fable 5.1 | 2026-09-13 | TR/EN dil anahtarı; anlatı, gerekçe ve öneriler iki dilde (i18n.py) |
| Claude Fable 5.1 | 2026-09-13 | Performans: hızlı zaman damgası yolları, tek birleşik maske regex'i, örneklemli varlık çıkarımı (300k satır 45 sn → 15 sn) |
| Claude Fable 5.1 | 2026-09-13 | Özet sayfası: metin yerine grafikler (zamana göre seviye, servisler, kaynak türleri) ve oynatılabilir canlı log akışı |
| Claude Fable 5.1 | 2026-09-13 | Açılış sayfası sadeleştirildi: yükleme alanı, demo butonu, ikonlu pipeline şeridi |
| Claude Fable 5.1 | 2026-09-13 | Sol menüde Parser paneli: dosya başına format, güven, satır, roller; sütun → rol eşleme düzeltme formu |
| Claude Fable 5.1 | 2026-09-13 | Streamlit 1.50 uyumluluğu: `wide()` yardımcısı ile width / use_container_width seçimi |
| Claude Fable 5.1 | 2026-09-13 | Gerçek veri şekilleri: iç içe JSON kayıt listesi, Türkçe başlık ipuçları, kv satır öneki, zaman damgası olmayan satırların 1970'e düşmesi düzeltildi, otomatik zaman kovası; sade sol menü, yükleme anında ilerleme, parser tablosu Özet'te |
| Claude Fable 5.1 | 2026-09-14 | Tıklanabilir metrik kartları ve detay panelleri; sol menüde Bağlantı paneli (HTTP API + MCP istemcisi); motor MCP sunucusu (`mcp_server.py`, streamable HTTP / stdio); Dockerfile + docker-compose; 4 bağlayıcı testi |
| Claude Fable 5.1 | 2026-09-14 | Metrik kartları yeniden tasarlandı: ikon, vurgu rengi, gradyan, bağlam satırı, karta yapışık Detay şeridi |
| Claude Fable 5.1 | 2026-09-14 | Canlı alım: HTTP alıcı (API anahtarlı `POST /ingest`), halka tampon + JSONL spool, yerleşik simülatör, `agent.py` (tail / file / simulate); açılış sayfası ve Canlı sekmesinde 2 sn'de bir yenilenen canlı grafikler; sol menü Connection Settings (veri kaynağı, canlı alım, LLM) / Datasets / README; OpenAI uyumlu yerel LLM istemcisi ve incident'ta LLM ile açıkla; README kullanım kılavuzu; 4 yeni test |
| Claude Fable 5.1 | 2026-09-14 | Açılış operasyon merkezi: demo butonu kaldırıldı; SLO/SLA kartları (erişilebilirlik, p95, hata bütçesi), host bazlı CPU/bellek/disk metrikleri (alıcı + simülatör + `agent.py --metrics`), ITSM entegrasyonu (ServiceNow, Jira SM, OneDesk, genel REST, demo) ve ticket ↔ sinyal/metrik/incident ilişkilendirme; auto-mapper sayısal sütunu mesaj seçmiyor; 3 yeni test |
| Claude Fable 5.1 | 2026-09-14 | Sol menü sayfa gezinmesi (Operasyon, Datasets, ITSM, Connection Settings, README); ana sayfa yükleme alanı olmadan önce metriklerle açılıyor; GPU metriği (simülatör, ajan nvidia-smi, eşik); log analizi Datasets sayfasında; ITSM yapılandırma + tam tablo + ticket detayı kendi sayfasında |
| Claude Fable 5.1 | 2026-09-14 | Çoklu veri seti kaydı: her set ayrı tutulur, aktif set dropdown ile seçilir, kaldırılabilir; `compare.py` ile iki setin karşılaştırma bölümü (KPI deltaları, seviye dağılımı, göreli zaman çizgisi, ortak / sadece A / sadece B sinyaller, incident'lar); 2 yeni test |
| Claude Fable 5.1 | 2026-09-14 | README yalnızca uygulama kılavuzu olarak yeniden yazıldı: sayfalar, veri akışları, her .py dosyası fonksiyon düzeyinde, testler, teknolojiler; yarışma takvimi ve demo akışı docs/PLAN.md'ye taşındı |
| Claude Fable 5.1 | 2026-09-14 | Log anlamlandırma: ham satır saklama, incident origin / timing / recovery tespiti (restart, kendiliğinden, durdu, devam ediyor), sinyal ve incident'ta Nereden / Neden / Ne yapmalı / Zaman ve düzelme kartları, tıklanabilir kanıt satırı → tam kayıt + ham satır, kendiliğinden düzelenler için otomatik done aksiyon kaydı ve Aksiyonlar bölümü; demo veri setine restart olayı; 1 yeni test |
| Claude Fable 5.1 | 2026-09-14 | Playbook / hata kütüphanesi: SQLite'ta kalıcı hata desenleri (nerede, ne zaman, kaç kez, düzelme türleri, çözüm notu, runbook), her analizde otomatik kayıt, bulanık eşleşme, Playbook sayfası, incident'ta "Daha önce görüldü" kartı; kanıt tablosuna satır seçme kutusu; 1 yeni test |

## Etkinlik günü

| Araç + sürüm | Tarih | Yapılan düzeltme / geliştirme |
|---|---|---|
| | 2026-09-16 | |
