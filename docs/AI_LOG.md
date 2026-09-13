# AI Kullanım Kanıt Kaydı

Her prompt için bir satır. Amaç: jürinin "hangi araçla, hangi sürümle, ne üretildi" sorusuna
commit'e kadar izlenebilir cevap vermek. Tarihler UTC. Aynı oturumdaki ardışık küçük düzeltmeler
tek satırda toplanabilir; commit hash'i git geçmişine bağlar.

Araç kısaltmaları:

| Kısaltma | Araç | Sürüm / model | Nasıl kullanıldı |
|---|---|---|---|
| CF5.1 | Claude (Cowork, Claude Code remote oturumu) | claude-fable-5-1 | Mimari tartışması, kod üretimi, test, tarayıcı doğrulaması, commit/push |
| SAKA | Claude SAKA | etkinlik günü doldurulacak | |
| CODEX | Codex | etkinlik günü doldurulacak | |
| İnsan | Ekip üyesi, elle | - | Karar, yönlendirme, doğrulama |

## Hackathon öncesi (framework hazırlığı)

| # | Tarih | Araç | Prompt (özet) | Üretilen / karar | Commit |
|---|---|---|---|---|---|
| 1 | 2026-09-13 15:1x | İnsan → CF5.1 | "AO-Hackathon-2026" ve önceki oturuma devam isteği | Depo boş bulundu; önceki Cowork oturumuna erişilemedi | - |
| 2 | 2026-09-13 15:20 | CF5.1 | Depo açıklamasına göre SAP audit iskeleti | Yanlış yön; kullanıcı "her şeyi unut" dedi, iskelet kaldırıldı | de49e04, dfb4987 |
| 3 | 2026-09-13 15:2x | İnsan → CF5.1 | Hackathon sitesi kuralları, SRE "noise → signal" tezi, kayıt formu ipuçları | Mimari önerisi: universal ingestion + canonical Observation + fingerprint + korelasyon + explainability + action tracker; LLM çekirdek dışı | - |
| 4 | 2026-09-13 15:3x | İnsan (ekran görüntüleri) → CF5.1 | Sitenin sunum yapısı, kurallar (3-4 kişi, Claude SAKA/Codex, 17:30 hard stop, kod paralel değerlendiriliyor) | Repo artefaktları sunumun 3 bölümüne eşlendi; "API garanti değil" → deterministik çekirdek kararı | - |
| 5 | 2026-09-13 15:39 | CF5.1 | "Dosya yapılarını oluştur" | İlk iskelet (backend/frontend dizinleri, docs, Makefile) | ba44f61 |
| 6 | 2026-09-13 18:20 | CF5.1 | "Format detector yaz, gelecek dataseti anlayayım" | tools/inspect_dataset.py (stdlib): format, sütun, rol, zaman aralığı | d49704d |
| 7 | 2026-09-13 18:22 | İnsan → CF5.1 | Çalışma kuralları (token tasarrufu, AI araç kaydı, tek README) | CLAUDE.md + README modül tablosu | b10f634 |
| 8 | 2026-09-13 18:29 | CF5.1 | "Parser'ı hazırla" | 5 parser + ortak yardımcılar + 11 test | 0002563 |
| 9 | 2026-09-13 18:38 | İnsan → CF5.1 | "Hepsini tek py dosyasında yaz" | signal_sprint.py (tek dosya, gömülü web UI) | 083b45c |
| 10 | 2026-09-13 18:58 | İnsan → CF5.1 | "Backend/frontend ayrı, benim verdiğim teknolojilerle": Python + Streamlit + pandas + dateutil + pytest, src/signal_sprint yapısı | Nihai yapı: app.py, src/signal_sprint/{models, loader, format_detector, normalize, parsers/, pipeline, profiler, analysis, actions, cli, scenario/}, 11 test, tarayıcı doğrulaması | 4e709da |
| 11 | 2026-09-13 19:02 | İnsan → CF5.1 | "Python 3.0+ desteklesin" | Gerçekçi taban 3.9 (Streamlit/pandas sınırı); 3.9 ve 3.11'de test | 032d151 |
| 12 | 2026-09-13 20:30 | İnsan → CF5.1 | "Yükleme sınırı 500 MB / 1 GB" | .streamlit/config.toml maxUploadSize=1024 | b9f95aa |
| 13 | 2026-09-13 20:50 | İnsan → CF5.1 | "Frontend daha güzel olsun, yükleme sonrası ekranlar" + "TR/EN dinamik" | Koyu tema, huni kartları, incident pencereli aktivite grafiği, WHY THIS SIGNAL paneli, yayılım Gantt'ı, faktör grafiği, tek tıkla aksiyon, kanban; i18n.py ile TR/EN | 9be5820 |
| 14 | 2026-09-13 21:04 | İnsan → CF5.1 | "Yükleme çok yavaş, hızlandır" | Profil ile darboğaz bulundu (dateutil); hızlı zaman yolları, tek birleşik regex, örneklemli varlık çıkarımı; 300k satır 45 sn → 15 sn | 542cabb |
| 15 | 2026-09-13 21:1x | İnsan → CF5.1 | "Her prompt için kanıt dosyası" | Bu dosya + CLAUDE.md kural güncellemesi | (bu commit) |

## Etkinlik günü (16 Eylül, 14:20 → 17:30)

Her prompt sonrası buraya bir satır ekleyin. Şablon:

| # | Saat | Araç | Prompt (özet) | Üretilen / karar | Commit |
|---|---|---|---|---|---|
| E1 | 14:2x | SAKA / CODEX / CF5.1 | ... | ... | ... |

Zorluklar ve AI'nın yardımcı olamadığı yerler de not edilir (sunumun 3. bölümü):

- ...
