# 09 — Ortam / sunucu kapsamı ve kart detayları

| Alan | Değer |
|------|-------|
| Amaç | Ortam / sunucu kapsamı ve kart detayları |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-15 |

## Prompt

```text
Ana sayfada prod / staging / dev / qa ayrımını göreyim. Kartların yanında bir
"Kapsam" kutusu olsun: önce ortam, altında o ortamın sunucuları. Bir sunucuyu
seçtiğimde sayfadaki tüm kartlar ve grafikler sadece o sunucuyu göstersin;
"Filtreyi temizle" ile geri dönsün.

Ana sayfadaki her karta tıklayınca o kartın detayı açılsın. CPU'ya basınca sadece
CPU'nun büyük grafiği, sunucu tablosu ve eşik aşımları; Erişilebilirlik ve SLA'ya
basınca oranı ne düşürüyor (hangi hata kalıpları, hangi servis ve sunucular, dakika
bazında grafik); p95'e basınca hangi servisler yavaş ve en yavaş istekler; hata
bütçesine basınca bütçeyi ne tüketiyor. Servis seviyelerinin neye göre
hesaplandığını da sayfada kısa bir notla yaz.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

LiveStore env / host filtreleri, env_summary, slo_detail; scope_panel (Kapsam kutusu), ops_tile + ops_detail_panel (10 kartın canlı detay paneli), simülatörde ortam etiketleri, agent.py --env.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
