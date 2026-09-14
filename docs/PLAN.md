# Plan

## Pre-sprint goals (framework)
- [x] Universal ingestion: ZIP/JSON/JSONL/CSV/syslog/KV/plain -> Observation
- [x] Noise reduction: template masking + fingerprints + burst detection
- [x] Correlation: time window + shared entities -> incident candidates + root cause
- [x] Explainability: scored factors + evidence lines per decision
- [x] Action tracker with lifecycle (SQLite)
- [x] Web dashboard: Upload, Overview, Signals, Incident, Actions (embedded, stdlib server)
- [x] Postmortem export + Claude SAKA prompt bundle

## Sprint-day goals (filled in at 14:20 on event day)
- [ ] Adapt parser to the given dataset
- [ ] Tune burst / correlation windows
- [ ] Validate top incidents manually
- [ ] Demo script rehearsed


## Demo akışı (7 dk)

1. Sidebar → "Demo veri setini yükle". Özet: huni 914 → 11 → 8 → 2, grafikler (aktivite + incident pencereleri, zamana göre seviye, servisler, kaynaklar), canlı log akışını oynat: 14:31'de hata dalgası akarken görülür. Dil anahtarı ile EN'e geçip aynı ekranı göster.
2. Signals: 532 satırlık sağlıklı trafik tek satır (burst 0), timeout sinyali burst 1.0. "WHY THIS SIGNAL?" ile kanıt + gerekçe + güven.
3. Incidents → INC-1: kök neden postgres gecikmesi, 4 belirti timeline'da, faktör grafiği, kanıt satırı, öneriler.
4. Aksiyon oluştur (P1, owner), Actions sekmesinde in_progress → done. Postmortem indir. LLM prompt'unu göster.
5. Kapanış: plan vs gerçekleşen (docs/PLAN.md), AI kullanımı (docs/AI_LOG.md).


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
