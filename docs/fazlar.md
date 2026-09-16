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
- [x] Etkinlik günü veri setine `scenario/` ayarı (S-A1: şiddet ölçeği, yan tablolar, tekilleştirme, yoğunluk kümeleme, nedensellik öncelikleri, öneriler, sahipler)

**Çıktı:** Teslime hazır paket

## Etkinlik günü (16 Eylül, 14:20 → 17:30)

| Saat | İş |
|---|---|
| 14:30–14:50 | Veri sözlüğü ve briefing okundu; servis × 5 dk yoğunluk tablosuyla 4 olay ve gürültü yapısı çıkarıldı |
| 14:50–15:40 | Yan tablolar, tekilleştirme, şiddet ölçeği; şablon bazlı gruplamanın tek karta yığdığı görüldü → yoğunluk kümeleyici (`storm.py`) |
| 15:40–16:10 | Kök neden: nedensellik önceliği, ilk neden-tipi alarm, kabin ağ olayı; karşı olasılıklar; kart eşiği ve arka plan budama |
| 16:10–16:40 | Arayüz: karşı olasılıklar ve "neden bu grup", sahipli ilk aksiyon kaydı, gürültü denetimi sekmesi ve ısı haritası; tarayıcı doğrulaması |
| 16:40–17:10 | README / AI_JURI / fazlar, ekran görüntüleri, testler (32), push |
