# 02 — Loader ve format detector

| Alan | Değer |
|------|-------|
| Amaç | Loader ve format detector |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-13 |

## Prompt

```text
Bir loader yaz. Kullanıcı tek dosya, ZIP, TAR.GZ, GZ veya bir klasör verebilsin;
iç içe arşivleri de açsın. Dosya kodlaması bozuksa (utf-8 değilse) denemeye devam
etsin, ikili dosyaları atlasın.

Yüklenen dosyanın ne olduğunu kendisi anlasın: JSON (tek büyük belge de olabilir),
satır satır JSON, CSV/TSV (ayracı kendi bulsun), syslog, anahtar=değer satırları
veya düz metin. İlk birkaç yüz satıra bakıp karar versin ve ne kadar emin olduğunu
da söylesin. Yanlış karar verirse kullanıcı elle değiştirebilsin. Testlerini yaz.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

loader.py (arşiv / klasör / kodlama fallback), format_detector.py (6 format + güven skoru), testleri; CLI `--inspect` ile dosya başına format ve sütun teşhisi.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
