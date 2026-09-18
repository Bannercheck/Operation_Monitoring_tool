<p align="left"><img src="assets/logo-wordmark.png" alt="Watchover" width="300"></p>

# Watchover

## Proje Adı

**Watchover** — operasyonel gürültüyü sinyale, sinyali gerekçeli incident'a, incident'ı takip edilen aksiyona çeviren SRE karar destek uygulaması.

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
pip install -e .                       # motor paketini (src\watchover) ortama tanıtır
python -m pytest -q                    # 30 passed beklenir
streamlit run app.py
```

## Kullanım

```bash
streamlit run app.py                   # http://localhost:8501 ; canlı alıcı :8600'de otomatik açılır
python -m pytest -q                    # 30 test, ağ gerektirmez
watchover data.zip --inspect       # CLI: dosya başına format / roller / sütunlar
watchover data.zip                 # CLI: profil + incident özeti (--json ile makine çıktısı)
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

8. **Zenginleştirilmiş tek dosya** (`enrich.py`, `watchover paket.zip --enrich alarms_enriched.csv`): her alarm satırı + şiddet etiketi, alarm sınıfı (neden / kaynak / belirti / arka plan) ve nedensellik puanı, mesajda anılan bağımlılık, envanter (dc, kabin, iş kritikliği, envanter–etiket tutarlılığı), bağımlı olduğu / ona bağımlı servisler ve kritiklikleri, altyapı alarmı mı, 5 dk kovası / kovadaki alarm / servis medyanı / sıcak hücre, incident, rol (kök neden / belirti / küçük grup / gürültü) ve gürültü nedeni; yanında `.summary.json` (kart özetleri, kök neden gerekçeleri, karşı olasılıklar, ilk aksiyon ve sahip). Dosya kendi kendine yeter: `tables.from_observations` bağımlılık grafiğini ve envanteri sütunlardan geri kurar, yalnız bu dosya yüklendiğinde aynı 4 kart çıkar (CSV ve JSONL). Format algılayıcı tırnak içindeki virgüllere karşı `csv.reader` ile sayım yapar.

Bu paketle sonuç: 3.000 alarm → 5 kart (yavaş yanma çıkarımı `storm.extract_slow_burns`: şiddeti tırmanan neden-tipi alarm zinciri + o servise yönelik timeout'lar ayrı kart olur; 02:24 session-service bellek sızıntısı → gc → oom, 0,44), diğerleri puan sırasıyla: 01:33 dc1/rack-A kabin ağ olayı (0,79); 02:33 payment-provider-gw dış servis kesintisi (0,77); 02:04 billing-db disk dolu → tablespace (0,71); 03:05 batch penceresi çakışması → subscriber-db bağlantı havuzu (0,44), 1.793 alarm gerekçesiyle elendi, analiz 0,4 sn. Kullanılan ek kütüphane yok; korelasyon tamamen bu depodaki kodla yapılır.

## 1. Kurulum ve çalıştırma

    python3 -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev,mcp]"            # dev: pytest · mcp: MCP sunucusu için SDK
    streamlit run app.py                   # http://localhost:8501 ; canlı alıcı :8600'de otomatik açılır

    python -m pytest -q                    # 30 test, ağ gerektirmez
    watchover data.zip --inspect       # CLI: dosya başına format / roller / sütunlar
    watchover data.zip                 # CLI: profil + incident özeti (--json ile makine çıktısı)
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
| Özet | **Funnel** tek kart (ham olay → parmak izi → anlamlı sinyal → incident → aksiyon; logaritmik oranlı çubuklar, başlıkta azaltma katsayısı), **olgu şeridi** tek kart (dosya, kayıt, servis, sunucu, hata sınıfı, zaman kapsamı, ortam) ve 12 ayrı "Detay" düğmesi yerine tek **ayrıntı seçici** (`st.pills`); seçilen ayrıntı **modal pencerede** (`st.dialog`) açılır (ham olay, parmak izi, anlamlı sinyal, incident, aksiyon, dosya, kayıt, servis, host, hata sınıfı, kapsam, **ortamlar**: hataların yüzde kaçı prod / test / dev; detayında ortam başına olay, hata, hata oranı, tüm hataların payı, ortam × seviye grafiği, hata kaynakları tablosu, ortam × servis hata grafiği), aktivite zaman çizgisi (incident pencereleri gölgeli), zamana göre seviye, en yoğun servisler, kaynak türleri, öne çıkan incident kartları, Parser tablosu + sütun→rol eşleme düzeltme formu, oynatılabilir **canlı log akışı** (veri setini zaman sırasıyla oynatır; hız, satır, seviye ve metin filtresi) |
| Sinyaller | Filtreler (seviye, patlama, anlamlı, servis, ortam), tabloda ortam ve hata kaynağı sütunları, progress sütunlu tablo, **NEDEN BU SİNYAL?** paneli: gerekçe, şablon, gruplama, patlama detayı, sinyalin dakika grafiği; **Nereden / Neden / Zaman / Ne yapmalı** kartı (kaynak dosyalar ve sayıları, ajanlar, servis/host; parmak izi + seviye + patlama gerekçesi; ilk sinyal, son hata, süre; öneriler); **tıklanabilir kanıt tablosu**: satıra tıklayınca tüm alanlar ve veri setindeki ham satır |
| Arama | **Log arama**: tek kutu, boşluk = VE, `alan:değer` sütun filtresi (service / host / severity / source / environment, Türkçe adları da), `-terim` hariç tutma, `"tam ifade"`, Regex anahtarı. Vektörel `str.contains` ile önceden kurulmuş küçük harfli haystack sütununda arar ve sonucu `st.cache_data` ile önbeller (100 bin satırda on milisaniyeler). Sonuç kümesinin **facet**'leri (seviye, servis, sunucu, dosya) sayılarıyla hap olarak listelenir, tıklayınca filtre olur (facet içinde VEYA, facet'ler arası VE); aktif filtreler çip olarak durur, ✕ ile kalkar. Eşleşmelerin dakika bazında zaman çizgisi. Tablodan bir satır seçilince **kayıt kartı** açılır: ham satır eşleşen terimler vurgulu, kanıt referansı, ait olduğu incident; kartın altında her alan için **+ filtre** ve **− hariç tut** düğmeleri, incident'a gitme düğmesi |
| Incident'lar | Incident **açılır listeden** seçilir (id · seviye · puan · başlık; 99 incident'ta da tek satır). Seçilen incident için **flashcard**: ne oldu (başlık, olay/sinyal sayısı, zincir), neden (kök neden ve gerekçe), nerede, ne zaman (başladı, son hata, süre, sessiz süre), nasıl çözüldü (düzelme türü, saat, kanıt satırı), şimdi ne yapmalı, daha önce görüldü mü. Aynı flashcard Özet'teki incident detay panelinde satıra tıklayınca da açılır ("Incident sekmesinde aç" ile geçiş). Kök neden kartı, **Nereden** kartı (dosyalar, servisler, hostlar, ajanlar, parser, tür) ve **Zaman ve düzelme** kartı (ilk sinyal, son hata, hata süresi, ERROR+ sayısı, son hatadan beri sessiz süre; düzelme türü: restart sonrası / kendiliğinden / durdu-kanıt yok / devam ediyor; düzelme kanıtı satırı ve saati), **📚 Daha önce görüldü** kartı (kök neden şablonu playbook'ta varsa: kaç kez, hangi veri setleri, son görülme, düzelmeler, çözüm notu, runbook; benzer şablon için token örtüşmesi ≥ 0.6 ile bulanık eşleşme; "Playbook'ta aç"), yayılım Gantt'ı, skor faktör grafiği, zaman çizgisi, korelasyon bağları, tıklanabilir kanıt tablosu (tam kayıt + ham satır), önerilerden tek tıkla aksiyon, özel aksiyon formu, postmortem indir, LLM prompt popover'ı, **🤖 LLM ile açıkla** |
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
| `app.py` | CF5.1 | Streamlit arayüzü, tüm sayfalar | `wide()` Streamlit sürümüne göre `width="stretch"` / `use_container_width` seçer. `pill`, `kpi`, `kpi2` HTML kart/rozet üreticileri. `store()` / `live_store()` / `receiver()` / `simulator()` `st.cache_resource` ile süreç başına tekil nesneler (aksiyon DB, canlı tampon, HTTP alıcı, simülatör). `llm_cfg()` oturumdan LLM ayarı. `load()` bir veri setini alır → `Analysis` + `profile` → `st.status` adımlarıyla kayıt defterine (`datasets`) yazar; `activate_dataset()` seçilen seti görünümlere yansıtır; `record_auto_recoveries()` kendiliğinden düzelen incident'lara done aksiyon kaydı açar. `show_row()` bir gözlemin tüm alanları + ham satırı; `evidence_table()` satır seçimli kanıt tablosu (+ satır seçme kutusu); `recovery_label()`; `incident_flashcard()` (tek kartta ne oldu / neden / nerede / ne zaman / nasıl çözüldü / ne yapmalı / daha önce görüldü), `incidents_table()` (satır seçimli incident tablosu → flashcard); `page_playbook()`, `playbook_card()`. `minute_chart()` incident pencereli aktivite grafiği. Sol menü: dil, sayfa radyosu, 5 sn'de bir yenilenen durum satırı (`_status`). `fetch_tickets()` seçili ITSM sistemine göre çeker. `live_signals()` canlı tampondaki ERROR+ olaylardan şablon bazlı hızlı sinyaller. `correlated_tickets()` ticket'ları sinyal + eşik aşımı + incident ile ilişkilendirir; `tickets_df()` tabloya çevirir. `picker()` "Tümü" girişli açılır liste ("Tümü" → None, değer oturumda kalır). `_set_scope()` düğme geri çağrısı (widget'lar kurulmadan önce ortam / host anahtarlarını günceller). `scope_panel()` kartların sağındaki Kapsam kutusu (ortam listesi + ortamın sunucuları, sıfırlama düğmesi) → `(env, host)`; `ops_tile()` "Detay" şeritli kart (`ops_detail` oturum anahtarını açar/kapar), `metric_chart()` eşik çizgili host grafiği, `ops_detail_panel()` tıklanan kartın canlı ayrıntı paneli (metrik / erişilebilirlik / SLA / bütçe / p95 / olay hızı / hatalar); `metrics_block`, `slo_block`, `events_block`, `tail_block` operasyon sayfasının parçaları. `live_analysis()` canlı tampon üzerinde önbellekli Analysis (alınan olay sayısına göre yenilenir), `page_map()` Hata haritası sayfası (kaynak seçimi; canlı kaynakta 5 sn fragment; incident / ortam / sunucu seçimi, 5 kart, `to_html` çıktısını `st.iframe` ile gömme, etki zinciri rozetleri, okuma anahtarı); `page_ops()` (2 sn fragment; ortam ve host seçimi `ops_env_pick` / `ops_host_pick` oturum anahtarlarında tutulur, tüm bloklara `env` / `host` filtresi olarak geçer), `page_itsm()` (yapılandırma + 5 sn fragment tablo + detay), `page_conn()` (üç sekme), `datasets_controls()` (yükleme, demo, canlı tampon, dropdown, kaldır, karşılaştır), `compare_view()` (A/B görünümü). Sayfa dağıtıcı `page` değerine göre çalışır. Analiz sekmeleri: `frames()` (cache'li DataFrame), `tile()` (tıklanabilir kart), detay panelleri, sinyal / incident / aksiyon sekmeleri, `live_panel` (oynatılabilir log akışı fragment'ı) **Tema:** `.streamlit/config.toml` (lacivert zemin `#0e1420`, yüzey `#151d2b`, kenar `#243044`, vurgu turkuaz `#2dd4bf` / mavi `#60a5fa`, Inter + JetBrains Mono, 10 px köşe, minimal araç çubuğu, ayrı kenar çubuğu rengi) + `CSS` bloğu (kart / KPI / zaman çizgisi / aksiyon stilleri, marka SVG logosu) + `_wo_altair()` grafik teması (şeffaf zemin, soluk eksenler, marka kategori renkleri); `upper()` Türkçe'ye duyarlı büyük harf (İ), KPI etiketleri CSS yerine Python'da büyütülür **Kenar çubuğu**: marka + TR/EN anahtarı, gezinti listesi (radyo düğmesi CSS ile gerçek nav satırlarına çevrildi, aktif satır vurgulu), durum kartı (alıcı, veri seti, ticket'lar), sürüm damgası |
| `agent.py` | CF5.1 | Uzak ajan CLI | `host_metrics()` loadavg / `/proc/meminfo` / `shutil.disk_usage` / `nvidia-smi` ile CPU, bellek, disk, GPU yüzdeleri (psutil gerekmez). `post()` `X-API-Key`, `X-Agent`, `X-File-Name` başlıklarıyla gönderir. `tail()` `tail -f` gibi dosyayı izler, 1 sn'lik partiler. `main()` `--tail` / `--file` / `--simulate` / `--metrics` modları; `--env` (veya `AGENT_ENV`) metrik kayıtlarına ortam etiketi ekler |
| `mcp_server.py` | CF5.1 | Motor MCP sunucusu olarak | Araç fonksiyonları düz Python (test edilebilir): `analyze_dataset(path)`, `list_incidents()`, `get_incident(id)`, `list_signals(limit)`, `evidence(ref)`, `postmortem(id)`, `create_action(...)`, `list_actions()`. `build_server()` `mcp>=2` `MCPServer`'a araçları kaydeder; `main()` `--http` ile streamable HTTP (DNS rebinding koruması kapalı, Docker / uzak istemci için) ya da stdio |
| `samples/make_demo.py` | CF5.1 | Sentetik demo veri seti üreticisi | `samples/demo_mixed.zip`: 916 olay, 4 dosya (JSONL, düz log, syslog, CSV); 45 dk arka plan trafiği; gömülü zincir DB gecikmesi → payment timeout → checkout 500 → alarm → 14:36 PostgreSQL restart (systemd stop + "ready to accept connections"); ayrı disk dolu incident'ı |

### Motor (`src/watchover/`)

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
| `parsers/sap_parser.py` | CF5.1 | SAP log ailesi | Tek durumlu satır parser'ı, uzantıdan bağımsız (`.log .lst .jvm .out .trc .file` ya da uzantısız `dev_w0`): **ABAP kernel developer trace** (`M Wed Sep 16 02:14:09:501 2026` zaman satırı sonraki satırlara miras; `*** ERROR =>` / `*** WARNING =>` / `*** LOG Q0I=>`; `[Thr n]`, bileşen harfi, `sid` → servis `sap-prd`, `profile` → host, pid, release), **HANA trace** (`[thr]{conn}[tx/user] zaman i/w/e/f/d bileşen kaynak(satır) : mesaj`), **NetWeaver Java defaultTrace / applications.log** (`#2.0 #yyyy mm dd hh:mm:ss:ms#+0300#Error#logger#` çok satırlı kayıt, kategori `BC-JAS-DPL`, son alan mesaj), **java.util.logging iki satır** (`Sep 16, 2026 2:14:20 AM logger method` + `SEVERE: mesaj`), **JVM GC** (birleşik `[ts][..][info][gc]` ve klasik `ts: n: [Full GC ...]`; duraklama ≥ 1 sn WARN, ≥ 5 sn ERROR, `gc.pause_ms`), **tp / R3trans / SUM** (`4 ETW000 date&time : 16.09.2026 - 02:14:35` zaman taşıma, `2EETW125` E/W harfi, `exit code : "12"`), **transport ALOG / SLOG** (`ERR imp all PRD 0008 20260916021445`), **SM21 sistem günlüğü dışa aktarımı** (`02:14:51 UPD 003 100 USER TCODE R6 8 mesaj`; başlıktaki tarih taşınır, mesaj sınıfı rakamı → seviye), **sapstartsrv erişilebilirlik günlüğü** (`Unavailable 24.10.2021 18:51:33 - 24.10.2021 19:34:33` → başlangıç zamanlı ERROR / INFO, `avail.duration_min`), **SAP JVM özellik anlık görüntüsü** (`bootstrap.jvm`, `datcol.jvm`: `# created at …` + `key = value` → dosya başına tek INFO kaydı, `jvm.*`), **class prefetch listesi** (`class_prefetch.lst` → tek DEBUG kaydı, paket sayımı; 1.000 sahte olay üretmez). Gerçek NetWeaver 7.50 `deploy.log` doğrulandı: `#2.0<BS>#` sürüm alanı (backspace karakteri), `<!--LOGHEADER-->` başlıkları meta olur (`sap.log_name` → sid / instance), üç satırlık kayıt boş satırda biter, `sap.msg_id` / `sap.category` / `sap.application` / `sap.user` alanları. `sap_score()` format tespitinde kullanılır (`trc file:`, `<!--LOGHEADER`, `# created at` gibi güçlü işaretler tek başına yeter; yoksa satırların ≥ %50'si SAP ailesinden olmalı). Tüm alanlar `sap.*` / `gc.*` attribute'larında kanıt olarak kalır |
| `parsers/kv_parser.py` | CF5.1 | key=value (logfmt) | satır başı zaman / seviye / logger önekini de alır, tırnaklı değerler |
| `parsers/text_parser.py` | CF5.1 | Düz metin | öncü zaman (ISO, `2026/09/16`, `16.09.2026 02:14:07`, `Wed Sep 16 02:14:07 2026`, `Sep 16, 2026 2:14:07 AM`), seviye (SEVERE / CONFIG / FINE dahil), logger; kalan mesaj |
| `knowledge.py` | CF5.1 | Bilgi tabanı | `Knowledge` (SQLite / PostgreSQL): `record()` kök neden örüntüsü başına tek ders + tekrar sayacı, `add_note()`, `add_doc()` (parçalama), `add_feedback()` (karar → kural önerisi), `search()` hibrit sıralama (kapsama + örtüşme + embedding), `related()`, `propose()` / `decide()` / `apply_rules()` (scenario katmanı), `stats()` (boyut) |
| `assistant.py` | CF5.1 | Watchover'a sor | `build_context()` incident satırları + kanıt + dersler + kurallar, `answer()` LLM ile (kaynak zorunlu sistem istemi, son 6 mesaj) ya da `fallback_answer()` deterministik derleme; `rule_context()` + `propose_rules()` modelden JSON kural önerisi, `validate_rules()` veriyle doğrulama |
| `assets/` | CF5.1 | Marka | `logo.svg` (göz + radar süpürmesi + sinyal yayları, turkuaz → mavi), `logo-wordmark.svg`, PNG türevleri (64 / 256 / wordmark). Kenar çubuğu markası, sekme simgesi, sohbet avatarı, Watchover'a sor başlığı ve README bu dosyaları kullanır |
| `llm.py` | CF5.1 | Sağlayıcıdan bağımsız LLM | `LLMConfig(provider, base_url, model, api_key, embed_model)`, `kind` (URL'den tahmin), `chat_messages()` / `chat()` / `embed()` / `list_models()` dört uç biçimi için, `set_sink()` çağrı metrikleri |
| `ollama.py` | CF5.1 | Yerel model yöneticisi | `discover()`, `installed()`, `running()`, `pull()` (ilerleme akışı), `remove()`, `RECOMMENDED` |
| `llm_eval.py` | CF5.1 | LLM kalitesi | `grounding()` atıf doğrulama, `run_benchmark()` kök neden testi (deterministik karıştırma, motorla uyum) |
| `scripts/install_linux.sh`, `scripts/watchover.service` | CF5.1 | Linux kurulum | venv + isteğe bağlı Ollama + modeller + systemd |
| `settings.py` | CF5.1 | Kalıcı ayarlar | `load()` / `save()` (0600), `setup_done()`, `SESSION_KEYS` oturum varsayılanları; `WATCHOVER_HOME` |
| `agents.py` | CF5.1 | Ajan kimliği | `AgentRegistry`: `enroll()` (tek seferlik token), `verify()` (sha256, önbellek), `revoke()` / `rotate()` / `delete()` / `touch()`; alıcı `make_handler(store, api_key, registry)` kimliği çözer |
| `scripts/install.sh`, `scripts/watchover-launcher.sh` | CF5.1 | Tek satır kurulum | macOS / Linux kullanıcı kurulumu, Ollama + modeller, `watchover` komutu (`update [zip]` ile git olmadan güncelleme), LaunchAgent / user systemd |
| `agent.py` | CF5.1 | Sunucu ajanı | Standart kütüphane; `Sender` (Bearer token, 0700/0600 disk spool, kilitli drain, kalıcı 4xx düşürülür), `hello()` / `--test`, `tail_loop()` (`tail -F` gibi: glob, yeniden adlandırma ve copytruncate rotasyonu, yalnız tam satır), `metrics_loop()`, iş parçacığı ölürse çıkış (servis yeniden başlatır), ortam değişkenleriyle yapılandırma (`WATCHOVER_URL/TOKEN/LOGS/METRICS/INTERVAL/SPOOL`) |
| `scripts/agent-install.sh` | CF5.1 | Ajan kurucu | Alıcıdan indirilir; python3 kontrolü, `/opt/watchover-agent`, tırnaklı `agent.env` 0600, olmayan log yolu uyarısı, bağlantı testi, systemd (gerçekten çalışıyorsa; yeniden kurulumda restart) / macOS root'ta LaunchDaemon / arka plan (eski pid öldürülür), `--uninstall` |
| `stamp.py` | CF5.1 | Tekrarlanabilirlik damgaları | `engine_id()` paketteki tüm .py dosyalarının sha256'sı (kural değişince değişir), `scenario_id()` senaryo ayarları, `git_rev()` checkout sürümü (git çalıştırmadan), `file_id()` dosya içeriği (klasör, `\` / `/` ve satır sonu bağımsız), `input_id()` yüklenen setin kimliği (sıra bağımsız), `result_id()` incident listesinin kimliği, `stale_package()` içe aktarılan motorun repo `src/` kopyası olmadığını yakalar. Kenar çubuğunda `v0.3.0 · motor · git · senaryo`, veri seti satırında `girdi · motor · sonuç`; CLI ve `.summary.json` aynı damgayı taşır |
| `pipeline.py` | CF5.1 | Ingest orkestrasyonu | `ingest()` loader → detector → parser, dosya raporu (format, güven, satır, tür, anahtarlar, roller), `scenario.MAPPING`; `fill_missing_timestamps()` zamansız satırlara veri setinin en erken zamanı; `ingest_path()`, `ingest_bytes()` |
| `profiler.py` | CF5.1 | Dataset profiler | `bucket_for()` aralığa göre 1 dk / 5 dk / 1 sa / 1 gün; `profile()` pandas ile dosya, kayıt, kaynak türleri, servis/host, hata sınıfı, seviye, ortam başına olay/hata/hata oranı, hata kaynakları, zaman aralığı, zaman serisi, dosyalar arası ilişki önerisi (id/host/service benzeri kolonlarda ≥ %50 değer örtüşmesi); `profile_text()` iki dilli özet |
| `analysis.py` | CF5.1 | Deterministik motor | `template_of()` tek birleşik regex ile uuid / ip / hex / zaman / url / yol / sayı maskeleme (+ `scenario.EXTRA_MASKS`); `entities_of()` servis / host / ip / id varlıkları; `fingerprint()` sha1(şablon + seviye + servis); `build_signals()` + `score_burst()` tepe vs tüm aralıktaki medyan (sessiz dakikalar sayılır ama liste olarak üretilmez; 4 yıllık SAP günlüğünde 60 sn → 1 sn); `interesting()` WARN+ veya burst ≥ 0.5; `correlate()` union-find: onset'ler pencere içinde VE (ortak varlık VEYA ikisi de burst); `pick_root_cause()` en erken onset (-3/dk), altyapı kelimesi (+1.5), fan-out, seviye; `build_incident()` (başlık şablon boşsa ham satırdan) `WEIGHTS` (burst .35, severity .25, blast_radius .25, duration .15) ile faktör katkıları, seviye, zaman çizgisi, kanıt, öneriler; `explain_incident()` her incident için **origin** (dosya sayıları, servis, host, ajan, parser, tür), **timing** (ilk sinyal, son hata, süre, sessiz süre, hata sayısı) ve **recovery** (son hatadan sonra ≥ 2 dk sessizlik; `RESTART_RE` ile restart/başlatma satırı → `restart`; INFO trafiğin dönmesi → `self_healed`; kanıt yoksa `stopped`; hata sona kadar sürüyorsa `ongoing`; kanıt satırı ve saati); `postmortem_md()`, `llm_prompt()`; `Analysis` sınıfı uçtan uca akış, `funnel()`, `signal_by_id`, `incident_by_id`, `obs_by_ref` indeksleri; `signal_dict()`, `incident_dict()` |
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
| `cli.py` | CF5.1 | Komut satırı | `inspect()` dosya başına format, roller, zaman aralığı, seviye, sütun profili; `summary()`; `main()` `watchover <path> [--inspect] [--json]` |
| `scenario/__init__.py` (S-A1 ayarları: SEVERITY_MAP, SIDE_TABLES, DEDUP_KEY, MAX_INCIDENTS, CLUSTERING / BUCKET_MIN / HOT_MIN / HOT_RATIO / HOST_HOT_MIN / PAD_MIN / INFRA_TYPES / RACK_MIN_HOSTS / MIN_CLUSTER_ALARMS / MIN_CLUSTER_ERRORS / PRUNE_BACKGROUND, CAUSE_RANK, DEP_PATTERNS, RECOMMENDATIONS, OWNERS) | CF5.1 | Yapılandırma | `MAPPING`, `EXTRA_MASKS`, `EXTRA_DEPENDENCY_WORDS`, `WINDOW_MIN`, `WEIGHTS`, `RECOMMENDATIONS`, `SLO`, `SLA`, `METRIC_THRESHOLDS` (cpu 85, gpu 95, memory 90, disk 90). Veri setine özel her ayar burada |

### Testler (`tests/`)

| Dosya | AI aracı | Kapsam |
|---|---|---|
| `test_pipeline.py` | CF5.1 | Format tespiti, 7 parser (SAP ailesi: 12 örnek dosya, 12 biçim, gerçek deploy.log kesiti), aralıktan bağımsız patlama puanı, değer bazlı auto-map, açık mapping, yardımcılar, ZIP + TAR.GZ, gerçek veri şekilleri (iç içe JSON, Türkçe CSV, kv öneki, zamansız satır), profil, patlama, incident zinciri / kök neden / gerekçe, aksiyon deposu, iki set karşılaştırma |
| `test_smoke.py` | CF5.1 | Streamlit `AppTest`: demo yükleme, dil geçişi, çoklu veri seti kaydı ve karşılaştırma |
| `test_connectors.py` | CF5.1 | HTTP bağlayıcı, MCP istemcisi (sahte JSON-RPC sunucu, SSE, oturum başlığı, hata), `dig`, MCP sunucu araçları |
| `test_live_llm.py` | CF5.1 | Alıcı kimlik doğrulama ve formatlar, simülatör, ajan dosya modu, sahte OpenAI uyumlu sunucuyla LLM istemcisi |
| `test_playbook.py` | CF5.1 | Kayıt, veri seti başına tekil sayım, düzelme türleri, runbook tohumlama, notlar, arama, bulanık eşleşme, silme |
| `test_itsm_metrics.py` | CF5.1 | Sahte ServiceNow / Jira / genel REST sunucularıyla fetch, ticket ilişkilendirme sıralaması, metrik alımı + eşik aşımı + SLO |

### Diğer dosyalar

| Dosya | Ne yapar |
|---|---|
| `pyproject.toml` | Paket tanımı, bağımlılıklar (streamlit, pandas, python-dateutil), `watchover` CLI, `dev` / `mcp` ekstraları |
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

### Watchover'a sor: sohbet, bilgi tabanı, kurallar

Motor karar verir; asistan açıklar, hatırlar ve önerir. Üç parça:

- **Bilgi tabanı** (`knowledge.py`, SQLite; `DATABASE_URL` verilirse PostgreSQL). Her analizden **kök neden örüntüsü başına tek ders** yazılır: aynı örüntü tekrar görülünce satır çoğalmaz, `occurrences` artar ve (veri seti, incident) referansı eklenir. Ham alarm asla kopyalanmaz; ders yalnız kanıt referansı (`dosya:satır`) ve girdi kimliği taşır. Notlar ve belgeler içerik hash'iyle tekilleştirilir; belgeler ~900 karakterlik parçalara bölünür. Embedding isteğe bağlıdır ve float16 olarak ders satırının yanında durur (768 boyut ≈ 1,5 KB). Bin incident'lık bir yıl birkaç yüz satır, birkaç MB'dir. Arama hibrittir: sorgu kapsaması + token örtüşmesi, embedding varsa kosinüs, tekrar sayısı için küçük bir öncelik.
- **Öğretme.** Not ekle, runbook / postmortem yükle, "bilgi öğret" formuyla sahip / neden önceliği / gürültü tipi / bağımlılık / ilk aksiyon önerisi. Incident kartının altında **Daha önce görüldü** (bilgi tabanından benzer dersler) ve **karar** paneli: 👍/👎, doğru kök neden (karşı olasılıklardan seçim, "gürültü", "başka"), not. 👎 kararlar kural önerisi üretir.
- **Kurallar.** Üç kaynaktan gelir: ekip kararları, elle öğretilen bilgiler ve **modelin kendi önerileri** (Kurallar › "Modelden öneri iste": model alarm tiplerini, servisleri, incident'ları ve karşı olasılıklarını, ekip kararlarını, notları ve mevcut kuralları okur, JSON döndürür; `validate_rules` bilinmeyen alarm tipi / servis, aralık dışı değer ve izinsiz tür içerenleri eler; kalanlar `llm:<model>` kaynağıyla **öneri** olarak kaydedilir, 🤖 rozetiyle listelenir). Öneriler bir insan onaylayana kadar uygulanmaz. Onaylanan kural `scenario` üzerine bellek içi bir katman olarak biner (`apply_rules`: OWNERS, CAUSE_RANK, NOISE_TYPES, NOISE_TEMPLATES, EXTRA_DEPENDENCIES, RECOMMENDATIONS) ve yüklü tüm veri setleri yeniden analiz edilir. Geri alınan kural etkisiyle birlikte kalkar (her uygulama temiz tabandan). Gürültü kuralıyla elenen alarmlar huni ve denetimde `n_rule` gerekçesiyle görünür.
- **Sohbet** (`assistant.py`). Soru → bağlam (yüklü veri setinin incident satırları; soruda geçen incident'ın kanıt satırları ve karşı olasılıkları; bilgi tabanından geri alınan dersler; aktif kurallar) → OpenAI uyumlu LLM (Ollama, vLLM, LM Studio, bulut). Sistem istemi her iddiaya kaynak zorunlu kılar: `[INC-3]`, `[L12]`, `[dosya:satır]`. Cevabın altında kaynak kartları ve modele gönderilen bağlam görülebilir; cevap ders olarak kaydedilebilir. LLM bağlı değilse aynı bağlam deterministik olarak derlenir, sohbet hiç kapanmaz. Embedding modeli Bağlantı ayarları › LLM'de (`/embeddings`).

### Tek satır kurulum, ilk açılış sihirbazı, ajan kimliği

- **macOS / Linux, sudo'suz:** `curl -fsSL https://raw.githubusercontent.com/<org>/watchover/main/scripts/install.sh | bash -s -- --with-ollama`. Betik Python'ı bulur (yoksa macOS'ta Homebrew ile kurar), uygulamayı `~/Library/Application Support/Watchover` (macOS) ya da `~/.local/share/watchover` (Linux) altına klonlar, venv kurar, istenirse Ollama'yı ve modelleri indirir, `watchover` komutunu (`start|stop|status|open|update|logs|agent`) bağlar, macOS'ta LaunchAgent / Linux'ta kullanıcı systemd servisi ile girişte başlatır ve tarayıcıyı açar. Downloads/Desktop altındaki gizlilik korumasına takılmamak için uygulama kullanıcı kütüphanesine kurulur. Sunucu kurulumu için `scripts/install_linux.sh` (root, `/opt/watchover`, sistem servisi) ayrı durur.
- **İlk açılış sihirbazı:** ayar dosyası (`WATCHOVER_HOME/config.json`) yoksa uygulama dört adımlı sihirbazı gösterir: çalışma alanı (dil, ad, alıcı portu, simülatör) → yerel model (Ollama keşfi, sohbet / embedding modeli seçimi, eksikleri ilerleme çubuğuyla indirme ya da harici uç) → ajanlar (ilk toplayıcıların kaydı) → bitir (özet, demo veri seti). Ayarlar diske yazılır (0600) ve yeniden başlatmada korunur; LLM ve Canlı alım sekmelerindeki "Ayarları kaydet" de aynı dosyaya yazar. Bağlantı ayarlarından sihirbaz yeniden çalıştırılabilir; testler ve otomasyon için `WATCHOVER_SKIP_SETUP=1`.
- **Ajan kimliği** (`agents.py`): her toplayıcı kaydedilir (ad, ortam, site, etiketler) ve bir kez gösterilen kendi token'ını alır (`wo_…`, yalnız sha256'sı saklanır). Alıcı token'ı kayda çözer ve her olayı **kayıtlı** ad / ortam / site ile damgalar; ajanın gönderdiği X-Agent başlığı kimliği değiştiremez, yanlış yapılandırılmış bir ajan başka sunucu adına veri basamaz. Son görülme, IP ve olay sayısı izlenir; token iptal edilir, yenilenir, ajan silinir. Bilinmeyen ya da iptal edilmiş `wo_` token'ı ortak anahtara düşmez, 401 alır. Eski ortak anahtar `legacy` olarak çalışmaya devam eder. Ajan tarafında `--token` (`WATCHOVER_TOKEN`).

**Güncelleme (kaldır-yükle gerekmez).** Kod klasörü git ile klonlandıysa `watchover update`; zip olarak indirildiyse `watchover update ~/Downloads/watchover.zip`. Her iki yol da yalnız kodu değiştirir, bağımlılıkları kurar ve uygulamayı yeniden başlatır; `data/` altındaki ayarlar, ajan token'ları, bilgi tabanı ve `.env` korunur.

### Sunucuya ajan kurulumu: ne gerekir, ne gerekmez

Sorunun cevabı: **IP/port gerekir, Prometheus gerekmez.** Sunucular veriyi Watchover'ın çalıştığı makinedeki alıcıya (varsayılan TCP 8600) gönderir; alıcı adresi sihirbazın 3. adımında ve Bağlantı ayarları › Canlı alım › Ajanlar panelinde seçilir (LAN IP'si, makine adı ya da DNS adı; `local_addresses()` adayları listeler, seçim `public_host` olarak kalıcıdır) ve her kurulum komutunun içine yazılır. Bu makinede gelen TCP 8600 açık olmalıdır.

Sunucuda gereken: python3 (3.9+), o adrese ağ erişimi, servis için sudo, sunucunun kendi token'ı. Gerekmeyen: Prometheus, Zabbix, Docker, pip, git. Akış:

1. Ajanlar panelinde sunucuyu kaydet (ad, ortam, site, izlenecek log dosyaları) → tek seferlik token ve **tek satır komut** çıkar.
2. Sunucuda komutu çalıştır: `curl -fsSL http://<alıcı>:8600/agent/install.sh | sudo bash -s -- --url http://<alıcı>:8600/ingest --token wo_… --logs "/var/log/syslog,/var/log/app/*.log"`. Alıcı `agent.py` ve `agent-install.sh` dosyalarını kendisi sunar (`GET /agent.py`, `GET /agent/install.sh`); sunucunun GitHub'a erişmesi gerekmez. Kurucu python3'ü doğrular (root ise kurar), `/opt/watchover-agent` altına ajanı indirir, `agent.env` (0600) yazar, **bağlantı testini** yapar (`agent.py --test`: adres/port/token'ı kanıtlayan bir merhaba olayı), systemd (Linux) / launchd (macOS) servisini kurar; `--uninstall` ile kaldırır, `--no-service` ile yalnız dosyaları kurar.
3. Panelde satır ilk pakette yeşile döner (5 sn'de bir yenilenir): çevrimiçi / 2 dk'dır sessiz / ilk paket bekleniyor / iptal.

Ajan (`agent.py`, yalnız standart kütüphane, `--metrics` + tekrarlanabilir `--tail` glob'ları aynı süreçte): N saniyede bir CPU / bellek / disk / GPU, log dosyalarını `tail -f` gibi izler (`tail -F` gibi: yeniden adlandırılan ve yerinde sıfırlanan dosyayı yakalar, yarım satırı tamamlanana dek bekler, yeni eşleşen dosyaları 30 sn'de bir alır; olmayan yol beklenir), ulaşamadığında `--spool` dizinine (0700) biriktirip alıcı dönünce tek iş parçacığıyla sırayla gönderir (401/403 ve kalıcı 4xx asla biriktirilmez), `--test` ile kendini doğrular. Varsayılan log listesi `/var/log/syslog,/var/log/messages` (Debian ve RHEL aileleri). Prometheus / Zabbix'ten veri **okunmaz**; genel bir HTTP/JSON ucu veya MCP aracı varsa Bağlantı ayarları › Veri kaynağı'ndan alarm listesi çekilebilir.

**Toplu kurulum: sunucular kendini kaydeder.** Sunucu başına token kopyalamak yerine Ajanlar panelindeki **filo kayıt anahtarı** (`wk_…`) kullanılır: her sunucuda aynı komut çalışır (`… | sudo bash -s -- --url … --enroll-key wk_… --env prod`), kurucu `POST /enroll` ile sunucuyu makine adıyla kaydettirir, alıcı o sunucuya özel `wo_…` token'ı üretir ve kurucu bunu `agent.env` içine yazar. Anahtar yalnız kayıt yapar, veri gönderemez (`/ingest` onu reddeder); aynı ad ikinci kez kaydolamaz (409), anahtar panelden yenilenir ya da kapatılır. Ansible / Terraform / cloud-init gibi araçlarla dağıtım için tasarlandı.

**Güvenlik sınırları.** Alıcı düz HTTP'dir: güvenilir LAN/VPN içinde tutun ya da önüne TLS reverse proxy koyun (arayüz ve sihirbaz bunu uyarı olarak gösterir). İlk ajan kaydedildiği andan itibaren alıcı token'sız gönderimi reddeder (açık mod yalnız hiç ajan yokken). Gövde sınırı 16 MB (413), bozuk `Content-Length` 400, bağlantı zaman aşımı 30 sn, ortak anahtar sabit zamanlı karşılaştırılır. Dashboard arayüzü `install.sh` ile yalnız `127.0.0.1`'e bağlanır; dışarı açmak için `data/.env` içine `WATCHOVER_UI_ADDRESS=0.0.0.0` yazın (önüne TLS/SSO koyun). `data/live/events.jsonl` 64 MB'ta döndürülür, 3 nesil tutulur.

### LLM: her sağlayıcı, yerel çalıştırma, kalite ölçümü

- **Sağlayıcıdan bağımsız istemci** (`llm.py`, yalnız stdlib): OpenAI uyumlu sunucular (Ollama `/v1`, vLLM, LM Studio, LocalAI, OpenLLM, OpenAI, Groq, Mistral, Together, Azure), **Ollama yerel API** (`/api/chat`, `/api/embeddings`), **Anthropic** Messages, **Google Gemini** generateContent. `provider=auto` URL'den tahmin eder. Her çağrı süresi ve sonucuyla bilgi tabanındaki `llm_calls` tablosuna yazılır.
- **Yerel model** (`ollama.py`): olağan portlarda Ollama'yı bulur, ilk açılışta bağlantıyı kendisi doldurur; LLM › Modeller sekmesi kurulu ve bellekteki modelleri gösterir, önerilen modelleri ilerleme çubuğuyla indirir (`/api/pull` akışı), siler. Öneri: sohbet `qwen2.5:7b-instruct` (Türkçe), `llama3.1:8b`, `gemma2:9b`; yalnız CPU'da `qwen2.5:3b-instruct`; embedding `bge-m3`.
- **Linux sunucuya Docker'sız kurulum**: `sudo bash scripts/install_linux.sh --with-ollama` → sistem paketleri, `/opt/watchover` altında venv, isteğe bağlı Ollama (resmi kurulum betiği) + modeller, `.env` doldurma, `watchover.service` systemd birimi (:8501, `journalctl -u watchover -f`). Seçenekler: `--dir --user --port --models "…" --no-service`.
- **Kalite sekmesi**: başarı oranı (hatasız çağrı), gecikme p50 / p95, **kaynak doğruluğu** (cevaptaki `[INC-3]` / `[L12]` / `[dosya:satır]` atıflarının modele gönderilen bağlamda var olma payı; geçersiz atıf sayısı halüsinasyon ölçerine en yakın şey), atıf oranı, **kök neden testi** (yüklü veri setinin her incident'ı için kanıt + karıştırılmış aday sinyaller; modelin seçimi deterministik motorla karşılaştırılır, uyum oranı raporlanır; `llm_eval.py`), cevap onayı (sohbette 👍/👎), kural kabulü (modelin önerdiği kurallardan onaylananlar), tür bazında çağrı sayıları, model bazında tablo, son çağrıların gecikme grafiği.
- **Operasyon kartları canlı**: her kart artık son 15 dakikanın satır içi grafiğini (SVG sparkline, eşik / hedef çizgisi, nabız noktası) taşır ve 2 saniyede bir yenilenir; ayrıntı yine modal pencerede.

### Tekrarlanabilirlik: iki makine farklı sonuç gösteriyorsa

Motor deterministiktir: saat, yerel saat dilimi, hash sırası, dosya sırası, `\` / `/` yol ayracı, CRLF ve BOM sonucu değiştirmez (`tests/test_determinism.py` bunları üç ayrı süreçte ve farklı `PYTHONHASHSEED` ile doğrular). Aynı **girdi** kimliği + aynı **motor** kimliği her makinede aynı **sonuç** kimliğini vermek zorundadır. İki makine farklı incident sayısı gösteriyorsa sırayla:

1. **Motor kimliği farklı mı?** Kenar çubuğundaki `motor xxxxxxxxxx · git xxxxxxx`. Farklıysa makinelerden biri eski kod çalıştırıyor: `git pull` yapılmamış, eski zip açılmış ya da paket `pip install .` ile (`-e` olmadan) kurulup sonra kaynak güncellenmiş. Uygulama repo `src/` kopyasını her zaman öne alır ve eski kurulum saptanırsa uyarı basar; komut satırı için `pip install -e .` şart.
2. **Girdi kimliği farklı mı?** Veri seti satırındaki `girdi xxxxxxxxxx`. Farklıysa aynı dosyalar yüklenmemiştir: yan tablo (bağımlılık / envanter) eksik → yoğunluk modu yerine parmak izi modu; alarms.csv yalnız → tekilleştirme yok; farklı sürüm paket. Dosya raporundaki `sha` sütunu hangi dosyanın farklı olduğunu gösterir.
3. İkisi de aynı ve sonuç farklıysa bu bir motor hatasıdır: `watchover <paket> --json` çıktılarını karşılaştırıp issue açın.; filo kayıt anahtarı (`enroll_key` / `rotate` / `disable`, `self_enroll` → `/enroll`) |
