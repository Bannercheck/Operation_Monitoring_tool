# 04 — Gürültü azaltma, ilişkilendirme, kök neden ve puan

| Alan | Değer |
|------|-------|
| Amaç | Gürültü azaltma, ilişkilendirme, kök neden ve puan |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-13 |

## Prompt

```text
Aynı hatanın binlerce tekrarını tek sinyale indir. Mesajlardaki değişken kısımları
(sayılar, ID'ler, IP'ler, yollar, tarihler) maskeleyip bir kalıp çıkar, kalıp başına
parmak izi üret. Her sinyal için: kaç kez görüldü, ilk ve son görülme, hangi servis
ve sunucularda, en yüksek seviye ve "patlama" skoru (normalde dakikada kaç, tepede kaç).

Aynı zaman penceresinde ve ortak servis/sunucu paylaşan sinyalleri tek olayda
toplasın. Olayın içinde en olası kök nedeni seç: en erken başlayan, bağımlılık
kelimeleri içeren (timeout, connection refused, upstream), en çok servise yayılan,
seviyesi en yüksek olan öne çıksın. Neden onu seçtiğini kısa maddelerle açıkla.

Her olaya 0-1 arası önem puanı ver: patlama, seviye, etki alanı, süre. Ağırlıklar
senaryo dosyasında değiştirilebilsin. Puanı oluşturan her faktörü ve katkısını
göster. Olay için "ne oldu, neden, nerede, ne zaman, ne yapmalı" metni ve postmortem
taslağı üretsin; LLM'e verilebilecek hazır bir prompt paketi de çıkarsın ama LLM
olmadan her şey çalışsın.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

analysis.py: tek birleşik maske regex'i, template_of / fingerprint, score_burst, union-find correlate, pick_root_cause (kod + gerekçe), WEIGHTS ile Factor listesi, explain_incident, postmortem_md, llm_prompt; actions.py SQLite aksiyon takibi; cli.py.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
