# Fazlar

## Faz 0 — Hazırlık (13 Eylül)

- [x] Repo iskeleti, çalışma kuralları (`CLAUDE.md`), AI kayıt düzeni
- [x] Kanonik Observation modeli
- [x] Loader ve format detector

**Çıktı:** Herhangi bir dosyanın ne olduğunu söyleyen iskelet

## Faz 1 — Temel (13–14 Eylül)

- [x] 6 parser, normalizasyon, sütun → rol eşleme
- [x] Gürültü azaltma, ilişkilendirme, kök neden, faktörlü puan
- [x] Streamlit arayüzü, TR / EN, demo veri seti, testler

**Çıktı:** Dosya yükle → sinyal → incident → aksiyon akışı çalışıyor

## Faz 2 — Ana Özellik (14–15 Eylül)

- [x] Tıklanabilir kanıt satırları, incident flashcard, düzelme tespiti
- [x] Playbook (hata kütüphanesi), çoklu veri seti ve karşılaştırma
- [x] Ortam / hata kaynağı rolleri ve ortam kırılımı

**Çıktı:** "Ne oldu, neden, nerede, ne zaman, nasıl düzeldi" tek kartta

## Faz 3 — Entegrasyon (14–15 Eylül)

- [x] Canlı alıcı, ajan, simülatör, metrikler, SLO / SLA
- [x] ITSM (ServiceNow / Jira / OneDesk / REST), HTTP API ve MCP veri kaynağı, LLM ayarı
- [x] MCP sunucusu, Docker Compose
- [x] Operasyon sayfası: kapsam kutusu ve kart detay panelleri

**Çıktı:** Canlı operasyon merkezi

## Faz 4 — Demo & Teslim (16 Eylül)

- [x] Ekran görüntüleri (`demo/`)
- [ ] Demo videosu
- [x] `AI_JURI.md` ve `submission.json`
- [ ] Etkinlik günü veri setine `scenario/` ayarı

**Çıktı:** Teslime hazır paket

## Etkinlik günü (16 Eylül, 14:20 → 17:30)

| Saat | İş |
|---|---|
| 14:20–14:30 | `signal-sprint data.zip --inspect`: dosyalar, formatlar, roller |
| 14:30–14:45 | Dashboard'a yükle, profil ve rol tahminini kontrol et |
| 14:45–15:10 | Gerekirse `scenario.MAPPING` / yeni parser |
| 15:10–15:50 | `scenario`: pencere, ağırlıklar, bağımlılık kelimeleri, maskeler, SLO |
| 15:50–16:25 | Öneriler, isteğe bağlı LLM zenginleştirme |
| 16:25–16:50 | Demo verisi ile aksiyon akışı provası |
| 16:50–17:10 | Edge case, testler |
| 17:10–17:25 | README, AI_JURI, sunum |
| 17:25 | Code freeze, push, GitHub doğrulama |
