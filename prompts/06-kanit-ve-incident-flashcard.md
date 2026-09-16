# 06 — Kanıt satırları ve incident flashcard

| Alan | Değer |
|------|-------|
| Amaç | Kanıt satırları ve incident flashcard |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-14 |

## Prompt

```text
Bir sinyal ya da olayın kanıt satırına tıkladığımda veri setindeki ham satırı
bütünüyle göreyim (dosya adı, satır numarası, tüm alanlar). Olaya tıklayınca tek bir
flashcard açılsın: ne oldu, neden, nerede, ne zaman başladı ve bitti, nasıl çözüldü,
ne yapmalıyız, daha önce görülmüş mü. İlişkili sinyalleri iç kodlarla (L1, L2 gibi)
değil okunur cümlelerle anlat.

Kendiliğinden düzelen olayları da bul: "restart", "recovered", "started" gibi
mesajları iyileşme işareti say; hata ne zaman kesildi, ne kadar sürdü. Bu olaylar
için Aksiyonlar sekmesinde otomatik "tamamlandı" kaydı açılsın ve nedeni yazsın.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

show_row / evidence_table (satır seçimi → tüm alanlar + ham satır), incident_flashcard, explain_incident'ta origin / timing / recovery, record_auto_recoveries, okunur ticket ve sinyal etiketleri.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
