# 07 — Canlı operasyon sayfası, ajan ve metrikler

| Alan | Değer |
|------|-------|
| Amaç | Canlı operasyon sayfası, ajan ve metrikler |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-14 |

## Prompt

```text
Ana sayfada dosya yükleme alanı olmasın. Girer girmez CPU, GPU, bellek, disk
kartlarını ve sunucu bazlı grafiklerini göreyim; altında servis seviyeleri:
erişilebilirlik, p95 gecikme, hata bütçesi, SLA durumu, dakikadaki olay, hata sayısı.
Ajan bağlı değilse yerleşik bir simülatör veri üretsin ve ekranda "SİMÜLASYON"
rozeti görünsün. Her şey birkaç saniyede bir kendi kendine yenilensin.

Uzak sunuculara kurulacak küçük bir agent.py yaz: bir log dosyasını takip edip ya
da sunucunun CPU/GPU/bellek/disk değerlerini ölçüp (ek kütüphane gerektirmeden)
uygulamaya HTTP ile göndersin; API anahtarıyla korunsun. Uygulama tarafında gelen
verileri bellekte tutan, diske de yedekleyen bir alıcı olsun. "Canlı tamponu analiz
et" dersem toplanan veri normal analizden geçsin.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

live.py: LiveStore (halka tampon, metrikler, spool), HTTP alıcı, simülatör, metric_stats, slo; agent.py (--tail / --file / --metrics / --simulate); page_ops fragment'ı ile 2 sn'de yenilenen kartlar.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
