# 03 — Parser'lar, sütun eşleştirme ve normalleştirme

| Alan | Değer |
|------|-------|
| Amaç | Parser'lar, sütun eşleştirme ve normalleştirme |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-13 |

## Prompt

```text
Her format için ayrı bir parser yaz ama hepsinin çıktısı aynı Gözlem (Observation)
olsun. İstekler: JSON parser iç içe yapılardan kayıt listesini bulsun (örneğin
Alertmanager'ın alerts dizisi); CSV parser başlığı okusun; syslog parser tarih,
sunucu, program adını ayırsın; anahtar=değer parser başındaki zamanı kaçırmasın;
düz metin parser hiç olmazsa zamanı ve seviyeyi yakalasın. Her parser hangi
satırdan ne ürettiğini bilsin ki sonradan ham satırı gösterebilelim.

Farklı adlarla gelen sütunları otomatik tanısın: zaman için ts, time, @timestamp,
tarih; seviye için level, severity, sev, öncelik; servis için service, app,
component; sunucu için host, hostname, node, instance; ortam için env, environment,
stage, ortam; kaynak için source, origin, subsystem, kaynak. Türkçe başlıkları da
tanısın. Sayısal bir sütunu asla "mesaj" sanmasın. Zaman formatlarını hızlı parse
etsin; büyük dosyada yavaşlamasın. Seviyeleri DEBUG/INFO/WARN/ERROR/CRITICAL'e
indirgesin. Ortam sütunu yoksa sunucu adından tahmin etsin (prd-, prod-, test-, dev-).

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

parsers/ (json, jsonl, csv, syslog, kv, text) + common.py; normalize.py (parse_timestamp hızlı yolları, normalize_severity, normalize_environment, auto_map); profiler.py veri seti profili.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
