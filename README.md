# Signal Sprint

## Proje Adı

**Signal Sprint** — operasyonel gürültüyü sinyale, sinyali gerekçeli incident'a, incident'ı takip edilen aksiyona çeviren SRE karar destek uygulaması.

## Problem

Kesinti anında farklı formatlarda (JSON, CSV, syslog, key=value, düz metin, Alertmanager, ticket) on binlerce satır akar. Aynı hatanın tekrarları gerçek sinyali gömer; "neden bu alarm önemli", "ne zaman başladı, düzeldi mi", "kök neden ne" soruları elle ve kanıtsız cevaplanır; aksiyonlar takip edilmez, aynı hata bir sonraki nöbette yeniden öğrenilir.

## Çözüm

Log / olay / alarm / metrik / ticket verisini dosyadan, HTTP API'den, MCP sunucusundan ya da canlı ajanlardan alır; formatı kendisi tanır, sütunları rollere eşler, tek bir kanonik modele indirger. Tekrarları maskeleme + parmak iziyle tek sinyale indirir, patlamaları ölçer, ilişkili sinyalleri incident'ta toplar, kök nedeni ve önem puanını faktörleriyle açıklar; her karar satır düzeyinde kanıt taşır. Incident flashcard'ı (ne oldu / neden / nerede / ne zaman / nasıl düzeldi / ne yapmalı / daha önce görüldü mü), aksiyon kanbanı, playbook, ITSM ticket ilişkilendirmesi ve canlı operasyon sayfası (CPU / GPU / bellek / disk, SLO / SLA, ortam ve sunucu kapsamı, her kartın detayı) sunar. Çekirdek deterministiktir; çalışma zamanında LLM veya bulut servisi gerekmez, LLM isteğe bağlı zenginleştirmedir.

Jüri özeti: [AI_JURI.md](AI_JURI.md) · Mimari: [docs/mimari.md](docs/mimari.md) · Plan ve fazlar: [docs/plan.md](docs/plan.md), [docs/fazlar.md](docs/fazlar.md) · Prompt'lar: [prompts/](prompts/) · Ekran görüntüleri: [demo/](demo/)

## Ekip

| İsim | Rol | İletişim |
|------|-----|----------|
| Tayfur | Ürün ve geliştirme | tayfurozkaras@gmail.com |

AI kaydı kuralı: bu depodaki her dosya için "hangi AI aracıyla üretildi" aşağıdaki dosya referansında tutulur.
**CF5.1** = Claude Fable 5.1 (claude-fable-5-1, Claude Cowork). Adım adım kayıt: `docs/AI_LOG.md`.

## Kurulum

```bash
git clone <repo-url>
cd <repo>
cp .env.example .env                   # isteğe bağlı; uygulama varsayılanlarla çalışır
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mcp]"            # dev: pytest · mcp: MCP sunucusu için SDK
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1            # "running scripts is disabled" derse: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .                       # motor paketini (src\signal_sprint) ortama tanıtır
python -m pytest -q                    # 30 passed beklenir
streamlit run app.py
```

## Kullanım

```bash
streamlit run app.py                   # http://localhost:8501 ; canlı alıcı :8600'de otomatik açılır
python -m pytest -q                    # 30 test, ağ gerektirmez
signal-sprint data.zip --inspect       # CLI: dosya başına format / roller / sütunlar
signal-sprint data.zip                 # CLI: profil + incident özeti (--json ile makine çıktısı)
python mcp_server.py --http --port 8765   # motor MCP sunucusu olarak (http://localhost:8765/mcp)
docker compose up                      # dashboard :8501 + mcp :8765
```

## Teknoloji Yığını

- Python 3.9+ (3.9 ve 3.11'de doğrulandı)
- Streamlit + Altair (arayüz), pandas (veri), python-dateutil (zaman), pytest (test)
- stdlib: csv / json / re / zipfile / tarfile / gzip / http.server / urllib / sqlite3
- mcp ≥ 2 (isteğe bağlı MCP sunucusu), Docker Compose

## Demo

`demo/` klasörüne bakınız (8 ekran görüntüsü + açıklama). Video linki: _(eklenecek)_

## Lisans

GPL-3.0 (bkz. `LICENSE`)

---

# Uygulama kılavuzu

## 0. S-A1 "Alarm Fırtınası" senaryosu nasıl işleniyor

Veri paketi (alarms.json, alarms.csv, service_dependencies.csv, host_inventory.csv, VERI_SOZLUGU.md) tek bir ZIP olarak Datasets sayfasından yüklenir; paket repoya eklenmez.

1. **Yan tablolar** (`tables.py`): adı `depend` / `inventory` / `sozlugu` / `brifing` geçen dosyalar olay değil referans tablosudur; bağımlılık (kaynak → hedef, tip, kritiklik) ve envanter (host → servis, dc, kabin, kritiklik) motorun içine alınır.
2. **Tekilleştirme** (`pipeline.dedupe`): alarms.json ve alarms.csv aynı 3.000 alarmı taşır; `alarm_id` ile tek kopya tutulur (json tercih), rapor kaç satırın düşüldüğünü yazar. Zorunlu gereksinim 01: 3.000 alarmın tamamı işlenir.
3. **Şiddet ölçeği** (`scenario.SEVERITY_MAP`): sözlükteki 1 = bilgi … 5 = kritik ölçeği DEBUG…CRITICAL'e eşlenir.
4. **Yoğunluk kümeleme** (`storm.py`, `scenario.CLUSTERING = "auto"`: bağımlılık tablosu varsa devreye girer): pencere 5 dk'lık kovalara bölünür; bir servis (veya sunucu) kovada kendi medyan hızının 3 katını (en az 4) aşarsa hücre "sıcak"tır. Sıcak hücreler aynı servis / tanımlı bağımlılık / aynı kabin (ağ tipli alarmlar) ile ve en fazla bir kova arayla bağlanır; bağlı hücreler bir olaydır. Olayın alarmları üye servislerin o zaman aralığındaki alarmlarıdır; kendi normal hızında kalan (servis, alarm tipi) çiftleri gürültüye geri verilir. 15 alarmdan az ya da 3 ERROR+ içermeyen yoğunluk noktaları kart olmaz (LOW-n, gürültü denetiminde listelenir). Kart bütçesi `MAX_INCIDENTS = 15`.
5. **Kök neden** (`analysis.pick_root_cause`, yoğunluk modu): alarm tipine göre nedensellik önceliği (`CAUSE_RANK`: ağ > disk > veritabanı > dış servis > kaynak > uygulama belirtisi) + şiddet + adet + grubun ilk neden-tipi alarmı olma + bağımlılık tablosunda başkalarının ona bağımlı olması − başkasına bağımlı olması − geç başlama. Bir kabinin ≥ 3 sunucusunda ağ alarmı varsa kabin düzeyi ağ olayı sayılır. En iyi 3 rakip hipotez puan ve gerekçesiyle **karşı olasılıklar** olarak karta yazılır.
5b. **Kart puanı** (0–1, `scenario.WEIGHTS`): patlama 0,30 (olayın tüm alarmlarının tepe dakikası / üye servislerin olay dışındaki medyan dakikası), şiddet 0,20 (ERROR+ payı), etki alanı 0,20 (servis + sunucu sayısı / veri setindeki servis + sunucu sayısının yarısı), süre 0,15, iş kritikliği 0,15 (envanterde kritik / yüksek işaretli sunucu payı). Etiket: ≥ 0,6 veya CRITICAL sinyal → critical, ≥ 0,4 high, ≥ 0,2 medium.
6. **İlk aksiyon** (`record_first_actions`): her kart için kök neden şablonuna göre öneri (`scenario.RECOMMENDATIONS`), sahip (`scenario.OWNERS`: DBA / ağ / ödeme entegrasyon / batch / nöbetçi) ve **açık** durumla aksiyon kaydı otomatik açılır; Aksiyonlar sekmesinde durum değiştirilir (gereksinim 04–05).
7. **Gürültü denetimi** sekmesi: ham olay → kart → karttaki / elenen alarm sayıları, indirgeme oranı, her elenen sinyal için neden (normal hız içinde / küçük yoğunluk noktası / kart bütçesi dışı), servis × zaman ısı haritası (sıcak hücreler çerçeveli), kart için küçük kalan gruplar.

8. **Zenginleştirilmiş tek dosya** (`enrich.py`, `signal-sprint paket.zip --enrich alarms_enriched.csv`): her alarm satırı + şiddet etiketi, alarm sınıfı (neden / kaynak / belirti / arka plan) ve nedensellik puanı, mesajda anılan bağımlılık, envanter (dc, kabin, iş kritikliği, envanter–etiket tutarlılığı), bağımlı olduğu / ona bağımlı servisler ve kritiklikleri, altyapı alarmı mı, 5 dk kovası / kovadaki alarm / servis medyanı / sıcak hücre, incident, rol (kök neden / belirti / küçük grup / gürültü) ve gürültü nedeni; yanında `.summary.json` (kart özetleri, kök neden gerekçeleri, karşı olasılıklar, ilk aksiyon ve sahip). Dosya kendi kendine yeter: `tables.from_observations` bağımlılık grafiğini ve envanteri sütunlardan geri kurar, yalnız bu dosya yüklendiğinde aynı 4 kart çıkar (CSV ve JSONL). Format algılayıcı tırnak içindeki virgüllere karşı `csv.reader` ile sayım yapar.

Bu paketle sonuç: 3.000 alarm → 5 kart (yavaş yanma çıkarımı `storm.extract_slow_burns`: şiddeti tırmanan neden-tipi alarm zinciri + o servise yönelik timeout'lar ayrı kart olur; 02:24 session-service bellek sızıntısı → gc → oom, 0,44), diğerleri puan sırasıyla: 01:33 dc1/rack-A kabin ağ olayı (0,79); 02:33 payment-provider-gw dış servis kesintisi (0,77); 02:04 billing-db disk dolu → tablespace (0,71); 03:05 batch penceresi çakışması → subscriber-db bağlantı havuzu (0,44), 1.793 alarm gerekçesiyle elendi, analiz 0,4 sn. Kullanılan ek kütüphane yok; korelasyon tamamen bu depodaki kodla yapılır.

## 1. Kurulum ve çalıştırma

    python3 -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev,mcp]"            # dev: pytest · mcp: MCP sunucusu için SDK
    streamlit run app.py                   # http://localhost:8501 ; canlı alıcı :8600'de otomatik açılır

    python -m pytest -q                    # 30 test, ağ gerektirmez
    signal-sprint data.zip --inspect       # CLI: dosya başına format / roller / sütunlar
    signal-sprint data.zip                 # CLI: profil + incident özeti (--json ile makine çıktısı)
    python mcp_server.py --http --port 8765   # motor MCP sunucusu olarak (http://localhost:8765/mcp)
    docker compose up                      # dashboard :8501 + mcp :8765

Ortam değişkenleri: `ACTIONS_DB` (aksiyon SQLite yolu), `PLAYBOOK_DB` (hata kütüphanesi SQLite yolu), `LIVE_SPOOL` (canlı olay JSONL spool'u), `LIVE_PORT` (alıcı portu).
Streamlit ayarları `.streamlit/config.toml` (1 GB yükleme sınırı, koyu tema). Python 3.9+; 3.9 ve 3.11'de test edildi.

## 2. Sayfalar (sol menü)

| Sayfa | Ne yapar |
|---|---|
| 🏠 Operasyon | Ana sayfa. 2 sn'de bir yenilenir. Kart ızgarasının sağında **Kapsam** kutusu: Ortam açılır listesi (Tümü iken her ortamın erişilebilirlik / hata özeti), altında o ortamın sunucuları radyo listesi olarak; bir sunucu seçilince sayfadaki 10 kartın hepsi (4 altyapı + 6 servis seviyesi), grafikler, olay akışı ve son olaylar o sunucuya daralır, "Filtreyi temizle" ile geri dönülür. **Her kartın altındaki "Detay" düğmesi** canlı bir ayrıntı paneli açar: CPU / GPU / bellek / disk → o metriğin büyük grafiği (eşik çizgisi), host tablosu (son, ort, maks, eşik, durum) ve eşik aşımları; Erişilebilirlik ve SLA → dakika bazında erişilebilirlik (hedef çizgisi), servis bazında ERROR+, oranı düşüren hata kalıpları (kalıp, adet, servis, host, ilk/son, örnek mesaj), host bazında ERROR+; Hata bütçesi → kalan bütçenin zaman içindeki tüketimi ve aynı hata kırılımı; p95 → servis bazında p50/p95/maks tablosu, SLO çizgili çubuk grafik, en yavaş istekler; Olay/dk → servis bazında dakika grafiği, servis ve host tabloları; ERROR+ → son hata olayları. Servis seviyeleri başlığının altındaki not hesaplama dayanağını söyler (seçili kapsamın son 15 dk olayları; erişilebilirlik = 1 − ERROR+/toplam, p95 mesajlardaki `<n>ms`, hata bütçesi = izin verilen hata payının kalanı, hedefler `scenario.SLO` / `SLA`). Sırasıyla: **Altyapı** (host bazlı CPU / GPU / bellek / disk kartları + 15 dk seriler, eşik renkleri), **Servis seviyeleri** (erişilebilirlik vs SLO, p95 gecikme, hata bütçesi, SLA durumu, olay/dk, ERROR+), **olay akışı** (dakika × seviye alan grafiği, en yoğun servisler), **en ilgili 5 ITSM ticket'ı**, **son olaylar** konsolu. Ajan yoksa yerleşik simülatör akar ve SİMÜLASYON rozeti görünür; "Canlı tamponu analiz et" toplanan veriyi tam pipeline'a sokar |
| 🗄️ Datasets | Dosya / ZIP / TAR.GZ yükleme (1 GB), demo veri seti, canlı tamponu analiz et. Her set **ayrı tutulur**; **Aktif veri seti** dropdown'ı seçileni açar, "Kaldır" siler. **Veri setlerini karşılaştır**: A / B seçimi, KPI deltaları, seviye dağılımı, göreli zaman çizgisi, ortak / sadece A / sadece B sinyaller, incident listeleri. Aktif set için **log analizi sekmeleri**: Özet, Sinyaller, Incident'lar, Aksiyonlar (aşağıda) |
| 🕸 Hata haritası | Servis / sunucu bağımlılık grafiği, verinin kendisinden çıkarılır. Kaynak: aktif veri seti ya da canlı tampon; incident seçici (tek incident veya tümü), ortam filtresi, sunucu kümelerini açma/kapama. Kaynak canlı tampon ise harita 5 sn'de bir yeniden çizilir, seçim ve yakınlaştırma korunur. Üstte 5 kart (haritadaki servis, bağımlılık bağı, ilişki bağı, kök neden, kapsamdaki ERROR+). Grafik **etkileşimli** (harici kütüphane yok, kendi SVG çizimi): sürükleyerek kaydırma, tekerlekle yakınlaştırma; **bir düğüme tıklayınca** harita ona yakınlaşır, komşuları dışındaki her şey soluklaşır ve yanında detay paneli açılır (tür, ERROR+ sayısı, bağımlı olduğu / ona bağımlı servisler, sunucuları, sinyalleri); boşluğa tıklamak veya "Görünümü sıfırla" geri döndürür. Ortam ve sunucu filtreleri kök nedeni ve etkilenenleri de o kapsama daraltır. Çizim: **kırmızı ok** = hata mesajlarından çıkarılan bağımlılık ("upstream payment-api", "timeout to db-01"; sunucu adı o sunucudaki servise eşlenir), kalınlık = adet; **mavi kesikli** = motorun sinyal ilişkisi (ortak varlık, saniye farkı); **gri ok** = sunucu → servis ERROR+ sayısı, sunucular ortam kümelerinde. Kök neden servisi kırmızı ve ⚑ ile, etkilenenler turuncu. Altında **etki zinciri** (kök nedenden bağımlılık oklarını geriye izleyerek: postgres → payment-api → checkout-api). Altında okuma anahtarı. Incident flashcard'ındaki "Haritada göster" düğmesi bu sayfayı o incident ile açar |
| 📚 Playbook | **Hata kütüphanesi.** Her analizde ERROR+ / patlayan sinyal şablonları ve incident kök nedenleri otomatik işlenir: kaç veri setinde, ilk/son görülme, olay sayısı, kök neden olma sayısı, gözlenen düzelme türleri (restart / kendiliğinden / durdu / devam), servisler, hostlar. Arama; kayıt seçince **Çözüm notu** ve **Runbook** (ilk bakılacak adımlar; `scenario.RECOMMENDATIONS`'tan otomatik tohumlanır) düzenlenip kaydedilir. Kalıcı (`playbook.db`), oturumlar ve veri setleri arası taşınır |
| 🎫 ITSM | Yapılandırma (ServiceNow / Jira Service Management / OneDesk / Generic REST / demo; URL, kullanıcı-şifre veya token, sorgu, alan eşleme), 60 sn otomatik yenileme, **ilişkilendirilmiş tam ticket tablosu** (ilgililik çubuğu, ilişkili sinyal/metrik/incident, gerekçe) ve ticket detayı |
| ⚙️ Connection Settings | **Veri kaynağı**: HTTP API (URL, GET/POST, başlıklar, gövde, JSON yolu) veya MCP sunucusu (URL, araç listesi, araç + argümanlar) → "Bağlan ve çek". **Canlı alım**: alıcı portu, API anahtarı, simülatör anahtarı, ajan komutları. **LLM**: OpenAI uyumlu base URL, model, anahtar, "Bağlantıyı test et" (OpenLLM, Ollama, vLLM, LM Studio, bulut) |
| 📖 README | Bu dosya |

### Log analizi sekmeleri (Datasets sayfasında, aktif set için)

| Sekme | İçerik |
|---|---|
| Özet | Tıklanabilir metrik kartları (ham olay, parmak izi, anlamlı sinyal, incident, aksiyon, dosya, kayıt, servis, host, hata sınıfı, kapsam, **ortamlar**: hataların yüzde kaçı prod / test / dev; detayında ortam başına olay, hata, hata oranı, tüm hataların payı, ortam × seviye grafiği, hata kaynakları tablosu, ortam × servis hata grafiği), aktivite zaman çizgisi (incident pencereleri gölgeli), zamana göre seviye, en yoğun servisler, kaynak türleri, öne çıkan incident kartları, Parser tablosu + sütun→rol eşleme düzeltme formu, oynatılabilir **canlı log akışı** (veri setini zaman sırasıyla oynatır; hız, satır, seviye ve metin filtresi) |
| Sinyaller | Filtreler (seviye, patlama, anlamlı, servis, ortam), tabloda ortam ve hata kaynağı sütunları, progress sütunlu tablo, **NEDEN BU SİNYAL?** paneli: gerekçe, şablon, gruplama, patlama detayı, sinyalin dakika grafiği; **Nereden / Neden / Zaman / Ne yapmalı** kartı (kaynak dosyalar ve sayıları, ajanlar, servis/host; parmak izi + seviye + patlama gerekçesi; ilk sinyal, son hata, süre; öneriler); **tıklanabilir kanıt tablosu**: satıra tıklayınca tüm alanlar ve veri setindeki ham satır |
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
| `app.py` | CF5.1 | Streamlit arayüzü, tüm sayfalar | `wide()` Streamlit sürümüne göre `width="stretch"` / `use_container_width` seçer. `pill`, `kpi`, `kpi2` HTML kart/rozet üreticileri. `store()` / `live_store()` / `receiver()` / `simulator()` `st.cache_resource` ile süreç başına tekil nesneler (aksiyon DB, canlı tampon, HTTP alıcı, simülatör). `llm_cfg()` oturumdan LLM ayarı. `load()` bir veri setini alır → `Analysis` + `profile` → `st.status` adımlarıyla kayıt defterine (`datasets`) yazar; `activate_dataset()` seçilen seti görünümlere yansıtır; `record_auto_recoveries()` kendiliğinden düzelen incident'lara done aksiyon kaydı açar. `show_row()` bir gözlemin tüm alanları + ham satırı; `evidence_table()` satır seçimli kanıt tablosu (+ satır seçme kutusu); `recovery_label()`; `incident_flashcard()` (tek kartta ne oldu / neden / nerede / ne zaman / nasıl çözüldü / ne yapmalı / daha önce görüldü), `incidents_table()` (satır seçimli incident tablosu → flashcard); `page_playbook()`, `playbook_card()`. `minute_chart()` incident pencereli aktivite grafiği. Sol menü: dil, sayfa radyosu, 5 sn'de bir yenilenen durum satırı (`_status`). `fetch_tickets()` seçili ITSM sistemine göre çeker. `live_signals()` canlı tampondaki ERROR+ olaylardan şablon bazlı hızlı sinyaller. `correlated_tickets()` ticket'ları sinyal + eşik aşımı + incident ile ilişkilendirir; `tickets_df()` tabloya çevirir. `picker()` "Tümü" girişli açılır liste ("Tümü" → None, değer oturumda kalır). `_set_scope()` düğme geri çağrısı (widget'lar kurulmadan önce ortam / host anahtarlarını günceller). `scope_panel()` kartların sağındaki Kapsam kutusu (ortam listesi + ortamın sunucuları, sıfırlama düğmesi) → `(env, host)`; `ops_tile()` "Detay" şeritli kart (`ops_detail` oturum anahtarını açar/kapar), `metric_chart()` eşik çizgili host grafiği, `ops_detail_panel()` tıklanan kartın canlı ayrıntı paneli (metrik / erişilebilirlik / SLA / bütçe / p95 / olay hızı / hatalar); `metrics_block`, `slo_block`, `events_block`, `tail_block` operasyon sayfasının parçaları. `live_analysis()` canlı tampon üzerinde önbellekli Analysis (alınan olay sayısına göre yenilenir), `page_map()` Hata haritası sayfası (kaynak seçimi; canlı kaynakta 5 sn fragment; incident / ortam / sunucu seçimi, 5 kart, `to_html` çıktısını `st.iframe` ile gömme, etki zinciri rozetleri, okuma anahtarı); `page_ops()` (2 sn fragment; ortam ve host seçimi `ops_env_pick` / `ops_host_pick` oturum anahtarlarında tutulur, tüm bloklara `env` / `host` filtresi olarak geçer), `page_itsm()` (yapılandırma + 5 sn fragment tablo + detay), `page_conn()` (üç sekme), `datasets_controls()` (yükleme, demo, canlı tampon, dropdown, kaldır, karşılaştır), `compare_view()` (A/B görünümü). Sayfa dağıtıcı `page` değerine göre çalışır. Analiz sekmeleri: `frames()` (cache'li DataFrame), `tile()` (tıklanabilir kart), detay panelleri, sinyal / incident / aksiyon sekmeleri, `live_panel` (oynatılabilir log akışı fragment'ı) |
| `agent.py` | CF5.1 | Uzak ajan CLI | `host_metrics()` loadavg / `/proc/meminfo` / `shutil.disk_usage` / `nvidia-smi` ile CPU, bellek, disk, GPU yüzdeleri (psutil gerekmez). `post()` `X-API-Key`, `X-Agent`, `X-File-Name` başlıklarıyla gönderir. `tail()` `tail -f` gibi dosyayı izler, 1 sn'lik partiler. `main()` `--tail` / `--file` / `--simulate` / `--metrics` modları; `--env` (veya `AGENT_ENV`) metrik kayıtlarına ortam etiketi ekler |
| `mcp_server.py` | CF5.1 | Motor MCP sunucusu olarak | Araç fonksiyonları düz Python (test edilebilir): `analyze_dataset(path)`, `list_incidents()`, `get_incident(id)`, `list_signals(limit)`, `evidence(ref)`, `postmortem(id)`, `create_action(...)`, `list_actions()`. `build_server()` `mcp>=2` `MCPServer`'a araçları kaydeder; `main()` `--http` ile streamable HTTP (DNS rebinding koruması kapalı, Docker / uzak istemci için) ya da stdio |
| `samples/make_demo.py` | CF5.1 | Sentetik demo veri seti üreticisi | `samples/demo_mixed.zip`: 916 olay, 4 dosya (JSONL, düz log, syslog, CSV); 45 dk arka plan trafiği; gömülü zincir DB gecikmesi → payment timeout → checkout 500 → alarm → 14:36 PostgreSQL restart (systemd stop + "ready to accept connections"); ayrı disk dolu incident'ı |

### Motor (`src/signal_sprint/`)

| Dosya | AI aracı | Ne yapar | Nasıl çalışır (fonksiyonlar) |
|---|---|---|---|
| `models.py` | CF5.1 | Kanonik veri modeli | `Observation` (timestamp, message, kind, severity, service, host, **environment**, **origin**, attributes, source, line_no, parser, parser_confidence, template, fingerprint, **raw** = veri setindeki ham satır; `ref` = `dosya:satır`), `Signal` (`evidence`, `why()` kanıt + gerekçe + güven), `Factor` (ağırlık, değer, katkı, ham veri), `Incident` (kök neden, skor, faktörler, kanıt, zaman çizgisi, bağlar, öneriler, gerekçe kodları, `origin`, `timing`, `recovery`), `Action`. `SEV_RANK` seviye sırası |
| `loader.py` | CF5.1 | Universal loader | `decode()` utf-8-sig → utf-16 → latin-1; `iter_bytes()` ZIP / TAR(.GZ) / GZ'yi özyinelemeli açar, ikili dosyaları atlar; `iter_path()` dosya veya klasör |
| `format_detector.py` | CF5.1 | Format tespiti | `detect_delimiter()` `, ; \| tab`; `detect_format()` ilk 200 satırdan json (tek satırlık belge dahil) / jsonl / syslog / csv / kv / text, güven skoru |
| `normalize.py` | CF5.1 | Zaman, seviye, şema auto-mapper | `parse_timestamp()` epoch s/ms → `fromisoformat` → şekil imzasına göre önbelleklenen strptime → dateutil; hep tz-aware UTC. `normalize_severity()` warn/err/P1/major/syslog 0-7 → DEBUG..CRITICAL; `severity_from_text()`. `auto_map()` açık mapping > isim ipucu (İngilizce + Türkçe + Prometheus) > sonek > değer bazlı tahmin; roller: timestamp, severity, service, host, **environment** (env / stage / tier / ortam; değerleri prod/test/dev… olan sütun otomatik), **origin** (source / origin / component / subsystem / category / kaynak), message; sayısal sütunu mesaj seçmez. `normalize_environment()` Production / PRD / prod-eu → prod, `infer_environment()` sütun yoksa host / servis / dosya adı / mesajdan (prd-api-01 → prod, dev-worker → dev). `flatten()` iç içe JSON'u `a.b` anahtarlarına açar |
| `parsers/base.py` | CF5.1 | Parser arayüzü | `Parser.records(text)` → `(satır_no, dict)`; `parse()` bunları Observation'a çevirir |
| `parsers/common.py` | CF5.1 | Kayıt → Observation | `records_to_observations()` rolleri ilk 50 kaydın anahtar birleşiminden çözer, her kayda ham satırı (`raw`) ekler (JSON dizisinde kaydın kendisi), zamansız satırları işaretler, dosya adına göre kind (alert / incident), rol dışı alanları `attributes`'a koyar |
| `parsers/json_parser.py` | CF5.1 | JSON | `find_records()` belgede iç içe ilk dict listesini bulur (Alertmanager `data.alerts`) |
| `parsers/jsonl_parser.py` | CF5.1 | JSONL / NDJSON | satır başına nesne, bozuk satırı atlar |
| `parsers/csv_parser.py` | CF5.1 | CSV / TSV | ayraç tespiti, başlık satırı 1, kayıtlar 2'den başlar |
| `parsers/syslog_parser.py` | CF5.1 | Syslog | RFC3164 + `<PRI>` (PRI % 8 → seviye), host, program, pid |
| `parsers/kv_parser.py` | CF5.1 | key=value (logfmt) | satır başı zaman / seviye / logger önekini de alır, tırnaklı değerler |
| `parsers/text_parser.py` | CF5.1 | Düz metin | öncü zaman, seviye, logger; kalan mesaj |
| `pipeline.py` | CF5.1 | Ingest orkestrasyonu | `ingest()` loader → detector → parser, dosya raporu (format, güven, satır, tür, anahtarlar, roller), `scenario.MAPPING`; `fill_missing_timestamps()` zamansız satırlara veri setinin en erken zamanı; `ingest_path()`, `ingest_bytes()` |
| `profiler.py` | CF5.1 | Dataset profiler | `bucket_for()` aralığa göre 1 dk / 5 dk / 1 sa / 1 gün; `profile()` pandas ile dosya, kayıt, kaynak türleri, servis/host, hata sınıfı, seviye, ortam başına olay/hata/hata oranı, hata kaynakları, zaman aralığı, zaman serisi, dosyalar arası ilişki önerisi (id/host/service benzeri kolonlarda ≥ %50 değer örtüşmesi); `profile_text()` iki dilli özet |
| `analysis.py` | CF5.1 | Deterministik motor | `template_of()` tek birleşik regex ile uuid / ip / hex / zaman / url / yol / sayı maskeleme (+ `scenario.EXTRA_MASKS`); `entities_of()` servis / host / ip / id varlıkları; `fingerprint()` sha1(şablon + seviye + servis); `build_signals()` + `score_burst()` tepe vs tüm aralıktaki medyan; `interesting()` WARN+ veya burst ≥ 0.5; `correlate()` union-find: onset'ler pencere içinde VE (ortak varlık VEYA ikisi de burst); `pick_root_cause()` en erken onset (-3/dk), altyapı kelimesi (+1.5), fan-out, seviye; `build_incident()` (başlık şablon boşsa ham satırdan) `WEIGHTS` (burst .35, severity .25, blast_radius .25, duration .15) ile faktör katkıları, seviye, zaman çizgisi, kanıt, öneriler; `explain_incident()` her incident için **origin** (dosya sayıları, servis, host, ajan, parser, tür), **timing** (ilk sinyal, son hata, süre, sessiz süre, hata sayısı) ve **recovery** (son hatadan sonra ≥ 2 dk sessizlik; `RESTART_RE` ile restart/başlatma satırı → `restart`; INFO trafiğin dönmesi → `self_healed`; kanıt yoksa `stopped`; hata sona kadar sürüyorsa `ongoing`; kanıt satırı ve saati); `postmortem_md()`, `llm_prompt()`; `Analysis` sınıfı uçtan uca akış, `funnel()`, `signal_by_id`, `incident_by_id`, `obs_by_ref` indeksleri; `signal_dict()`, `incident_dict()` |
| `compare.py` | CF5.1 | İki veri setini karşılaştırma | `kpis()`; `compare()` KPI deltaları (Δ, Δ %), seviye dağılımı, ortak / sadece A / sadece B şablonlar (sayı ve patlama farkı), incident listeleri, göreli dakika zaman çizgisi |
| `actions.py` | CF5.1 | Aksiyon takibi | `ActionStore` SQLite: `list`, `create` (öncelik P1-P4, sorumlu, öneri, kanıt), `update` (open / in_progress / done / suppressed), `delete`, `get` |
| `live.py` | CF5.1 | Canlı alım | `LiveStore` olay halka tamponu + metrik tamponu + JSONL spool; `ingest()` gelen gövdeyi aynı parser'lardan geçirir, `metric_of()` ile cpu / gpu / memory / disk kayıtlarını ayırır; `snapshot()` / `stats()` / `metric_stats()` / `slo()` / `tail()` isteğe bağlı `env` ve `host` filtresi alır; `stats()` dakika × seviye matrisi, servisler, ajanlar; `metric_stats()` host bazlı son değer, dakikalık seri (ortam sütunlu), eşik aşımları, özet, `per_host` (host → ortam + son değerler); `slo()` erişilebilirlik, mesajlardaki `<n>ms` ile p95, hata bütçesi, SLO / SLA durumu; `slo_detail()` servis seviyesi kartlarının dayanağı (servis / host / kalıp bazında ERROR+, servis bazında p50/p95/maks gecikme, en yavaş istekler, dakika bazında erişilebilirlik ve kalan bütçe, hedef altı dakika sayısı, son hatalar); `environments()`, `hosts()` (host → ortam), `env_summary()` (ortam başına olay / hata / erişilebilirlik / sunucu, `ENV_ORDER` sırasıyla); `to_dataset()`; HTTP alıcı `make_handler` / `start_receiver` (`POST /ingest` X-API-Key veya Bearer, `GET /health`); `simulate_batch()`, `simulate_metrics()` (5 host + 2 GPU host, `SIM_ENV` ile prod / staging / dev / qa etiketi, ~2 dk'da bir hata dalgası), `start_simulator()` |
| `itsm.py` | CF5.1 | ITSM entegrasyonu | `Ticket`; `fetch_servicenow()` Table API (basic / token, sysparm_query), `fetch_jira()` REST v3 (JQL, ADF açıklama), `fetch_onedesk()`, `fetch_generic()` (JSON yolu + alan eşleme), `demo_tickets()`; `correlate()` ticket metnindeki servis / host / anahtar kelimelerin canlı sinyaller, metrik eşik aşımları, incident'larla örtüşmesi (0.65) + açılış zamanının bir patlamaya ±30 dk yakınlığı (0.35) → ilgililik 0..1 ve gerekçeler, eşitlikte yeni ticket önce |
| `connectors.py` | CF5.1 | Veri kaynağı bağlayıcıları | `fetch_http()` GET / POST, başlıklar, JSON yolu (`dig()`), içerik türünden dosya adı; `McpClient` MCP streamable-HTTP JSON-RPC (initialize → notifications/initialized → tools/list, tools/call; Mcp-Session-Id, SSE cevap); `fetch_mcp()` bir aracın çıktısını veri seti yapar; `mcp_tools()`; `parse_headers()` |
| `llm.py` | CF5.1 | LLM istemcisi | `LLMConfig` (base_url, model, api_key); `list_models()` `/models`; `chat()` `/chat/completions`; `test_connection()`. Çekirdek buna bağlı değil |
| `graph.py` | CF5.1 | Hata haritası motoru | `DEP_RE` hata mesajlarından bağımlılık hedefi çıkarır (upstream / to / from / calling + servis adı, db-01, postgres, redis…); `dependency_edges()` (servis → hedef) sayaçları; `build_map(analysis, incident_id, env, host, with_hosts)` düğümler (tür: root / affected / erroring / clean, ERROR+ sayısı), bağımlılık bağları (sunucu adı o sunucudaki incident servisine eşlenir), incident `links`'ten servis-servis ilişki bağları, sunucu → servis bağları (ortamıyla), kök nedenden geriye yürüyen `chain`; `layout()` katmanlı yerleşim (sunucular ortam gruplarında solda, bağımlılar → kök neden sağda); `to_html()` harici kütüphanesiz etkileşimli SVG sayfası (kaydırma / yakınlaştırma, düğüme tıklayınca odaklanma + komşuları vurgulama + detay paneli, seçimi `window.name` ile yeniden çizimler arasında koruma); `to_dot()` Graphviz DOT dışa aktarımı |
| `enrich.py` | CF5.1 | Zenginleştirilmiş tek dosya | `enrich(analysis)` alarm başına 30 sütun (envanter, bağımlılık, sınıf, sıcak hücre, incident, rol, gürültü nedeni), `to_csv` / `to_jsonl`, `summary()` kart özetleri; CLI `--enrich` |
| `storm.py` | CF5.1 | Alarm fırtınası kümeleyici | `cluster()`: 5 dk'lık kovalarda servis ve sunucu başına sayım, medyanın 3 katını aşan sıcak hücreler, aynı servis / bağımlılık / aynı kabin ile bağlama (union-find), olay alarmları ve gürültü; `prune_background()` küme içinde normal hızındaki (servis, tip) çiftlerini gürültüye geri verir |
| `tables.py` | CF5.1 | Yan tablolar | `is_side_table()` dosya adından referans tablosu tespiti, `read_table()` CSV / JSON okuma, `dependencies()` (kaynak → hedef, tip, kritiklik; sütun adları `scenario.DEP_COLUMNS`), `inventory()` (host → servis, dc, kabin, ortam, kritiklik) |
| `i18n.py` | CF5.1 | TR / EN metinler | `STRINGS` sözlüğü; `t()` aktif dil; `reason_text()`, `link_text()`, `narrative_text()`, `factor_value()`, `recommendation_text()` motor kodlarını dile çevirir; `RECOMMENDATIONS_TR` |
| `cli.py` | CF5.1 | Komut satırı | `inspect()` dosya başına format, roller, zaman aralığı, seviye, sütun profili; `summary()`; `main()` `signal-sprint <path> [--inspect] [--json]` |
| `scenario/__init__.py` (S-A1 ayarları: SEVERITY_MAP, SIDE_TABLES, DEDUP_KEY, MAX_INCIDENTS, CLUSTERING / BUCKET_MIN / HOT_MIN / HOT_RATIO / HOST_HOT_MIN / PAD_MIN / INFRA_TYPES / RACK_MIN_HOSTS / MIN_CLUSTER_ALARMS / MIN_CLUSTER_ERRORS / PRUNE_BACKGROUND, CAUSE_RANK, DEP_PATTERNS, RECOMMENDATIONS, OWNERS) | CF5.1 | Yapılandırma | `MAPPING`, `EXTRA_MASKS`, `EXTRA_DEPENDENCY_WORDS`, `WINDOW_MIN`, `WEIGHTS`, `RECOMMENDATIONS`, `SLO`, `SLA`, `METRIC_THRESHOLDS` (cpu 85, gpu 95, memory 90, disk 90). Veri setine özel her ayar burada |

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
| `requirements.txt` | Aynı bağımlılıkların düz listesi (`pip install -r requirements.txt` + `pip install -e .`); Windows adımları README Kurulum bölümünde |
| `Makefile` | `make run / test / demo / inspect DS=x / analyze DS=x` |
| `.streamlit/config.toml` | 1 GB yükleme sınırı, koyu tema |
| `Dockerfile`, `docker-compose.yml` | `dashboard` (8501) ve `mcp` (8765) servisleri; `./data` → `/data`, `actions.db` paylaşımı |
| `samples/demo_mixed.zip` | Üretilmiş demo veri seti |
| `docs/bulgular.md` | S-A1 paketi bulgu raporu: özet tablo (5 kart, ilk aksiyon, sahip, müdahale sırası) + olay başına ayrıntı (ne oldu, nerede, kök neden gerekçesi, karşı olasılıklar, zaman aşımı hedefleri, nasıl bitti, ilk aksiyon), gürültü analizi, bağımlılık / envanter katkısı, kısıtlar |
| `docs/plan.md`, `docs/fazlar.md`, `docs/mimari.md`, `docs/AI_LOG.md` | Hedef / kapsam / riskler / demo akışı; fazlar ve etkinlik günü saatleri; mimari, veri modeli, ADR; AI kullanım kaydı |
| `AI_JURI.md`, `submission.json` | Jüri için yapılandırılmış özet; teslim künyesi |
| `prompts/` | Uygulamayı şekillendiren 9 kritik prompt (amaç, model, tarih, prompt, çıktı) |
| `demo/` | Ekran görüntüleri ve açıklamaları |
| `.env.example` | Ortam değişkeni örnekleri (hiçbiri zorunlu değil) |
| `CLAUDE.md` | Bu depoda çalışan Claude için proje kuralları |

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
