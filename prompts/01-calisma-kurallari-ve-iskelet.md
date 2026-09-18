# 01 — Çalışma kuralları ve depo iskeleti

| Alan | Değer |
|------|-------|
| Amaç | Çalışma kuralları ve depo iskeleti |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-13 |

## Prompt

```text
Bir SRE aracı yapacağız: operasyonel gürültüyü sinyale, sinyalleri gerekçeli
olaylara (incident), olayları takip edilen aksiyonlara çeviren bir web uygulaması.
Teknoloji benim seçimim ve değişmeyecek: Python 3.9 ve üzeri, arayüz Streamlit,
veri işleme pandas, tarih için python-dateutil, test için pytest. Çalışma zamanında
hiçbir yapay zekâ API'sine bağımlı olmayacak; çekirdek deterministik olacak.

Üç kuralı CLAUDE.md'ye yaz ve hep uygula: (1) her promptta kodu baştan sona okuma,
sadece değişecek yere bak; (2) her geliştirme adımını docs/AI_LOG.md'ye tek satır
olarak kaydet: araç + sürüm, tarih, ne yapıldı; (3) tek bir README olsun ve her
adımda sadece ilgili bölümü güncelle.

Depoyu kur: app.py, src/watchover/ paketi, senaryoya özel ayarlar sadece
src/watchover/scenario/ altında, tests/, samples/, docs/, pyproject.toml,
Makefile, .streamlit/config.toml (koyu tema, 1 GB yükleme sınırı).

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

CLAUDE.md'de üç çalışma kuralı, depo iskeleti, pyproject / Makefile / Streamlit ayarı; kanonik Observation, Signal, Factor, Incident, Action modelleri (models.py).

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
