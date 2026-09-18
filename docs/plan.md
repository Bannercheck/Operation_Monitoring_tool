# Plan

## Hedef

Etkinlik günü verilen bilinmeyen log / olay veri setini 3 saat içinde yükleyip gürültüden arındırılmış, gerekçeli ve aksiyona bağlı incident'lar olarak canlı bir web uygulamasında sunmak.

## Kapsam

**Dahil:**
- Evrensel yükleme ve format tanıma (ZIP / JSON / JSONL / CSV / syslog / key=value / metin)
- Gürültü azaltma, ilişkilendirme, kök neden, faktörlü önem puanı, satır düzeyinde kanıt
- Incident flashcard, düzelme tespiti, aksiyon takibi, playbook
- Canlı operasyon sayfası (ajan, metrikler, SLO / SLA, ortam / sunucu kapsamı, kart detayları)
- ITSM ticket ilişkilendirmesi, HTTP API / MCP veri kaynağı, MCP sunucusu, Docker
- TR / EN arayüz, testler, belgeler, AI kullanım kaydı

**Hariç:**
- Uzun dönem SLO raporlama ve kalıcı zaman serisi veritabanı
- Kimlik doğrulama / çok kullanıcılı yetkilendirme
- LLM'e bağımlı herhangi bir çekirdek işlev

## Başarı Kriterleri

| Kriter | Ölçüt |
|--------|-------|
| Bilinmeyen veri seti parser değişmeden açılır | Format ve roller otomatik; gerekirse yalnız `scenario.MAPPING` |
| Gürültü azalır | Ham olay → parmak izi → anlamlı sinyal → incident hunisi görünür (demo: 916 → 13 → 8 → 2) |
| Her karar açıklanır | Sinyalde "neden bu sinyal", incident'ta faktör katkıları + kanıt satırı |
| Aksiyon izlenir | Öneriden tek tıkla aksiyon, kanban, kendiliğinden düzelenler otomatik done |
| Hız | 100k satır saniyeler içinde; arayüz 2 sn'de bir canlı yenilenir |
| Kalite | `make test` yeşil (30 test), Python 3.9 ve 3.11 |

## Riskler

| Risk | Etki | Önlem |
|------|------|-------|
| Veri seti beklenmedik formatta | Yüksek | Detector + 6 parser + rol eşleme; `watchover data.zip --inspect` ile 10 dk'da teşhis |
| Zaman sütunu yok / bozuk | Orta | Zamansız satırlar işaretlenir ve en erken zamana doldurulur |
| Çok büyük dosya | Orta | Hızlı tarih parse yolu, tek birleşik maske regex'i, 1 GB yükleme sınırı |
| Etkinlikte ağ / API yok | Düşük | Çekirdek çevrimdışı çalışır, LLM isteğe bağlı |
| Demo sırasında hata | Orta | Demo veri seti ve simülatör her zaman elde; prova docs/fazlar.md |

## Demo akışı (7 dk)

1. Operasyon sayfası: canlı kartlar, Kapsam kutusundan bir ortam / sunucu seç, Erişilebilirlik kartının detayında "oranı ne düşürüyor".
2. Datasets → "Demo veri setini yükle": huni, grafikler, ortam kırılımı, canlı log akışı; dil anahtarıyla EN.
3. Sinyaller: 532 satırlık sağlıklı trafik tek satır, timeout sinyali burst 1.0; "NEDEN BU SİNYAL?" ve kanıt satırına tıklayıp ham satır.
4. Incident flashcard: kök neden, ne zaman başladı / düzeldi, restart kanıtı, öneriler; aksiyon aç, kanbanda ilerlet; playbook "daha önce görüldü".
5. Kapanış: ITSM ticket ilişkisi, MCP / ajan entegrasyonu, AI kullanım kaydı (docs/AI_LOG.md).

## Zaman Çizelgesi

Detay için [fazlar.md](fazlar.md).
