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

## Etkinlik günü

| Araç + sürüm | Tarih | Yapılan düzeltme / geliştirme |
|---|---|---|
| | 2026-09-16 | |
