# 05 — Streamlit arayüzü ve Datasets sayfası

| Alan | Değer |
|------|-------|
| Amaç | Streamlit arayüzü ve Datasets sayfası |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-14 |

## Prompt

```text
app.py'yi yaz. Koyu tema, ürün gibi görünsün, uzun açıklama metinleri olmasın.
Sol tarafta bir menü: Operasyon (ana sayfa), Datasets, Playbook, ITSM, Connection
Settings, README. Türkçe/İngilizce anlık değiştirilebilsin; tüm metinler tek bir
sözlükte dursun.

Dosya yüklenince analiz hemen başlasın (buton beklemesin). Üstte tıklanabilir özet
kartları: toplam olay, hata, sinyal, olay, ortamlar; karta tıklayınca o kartın
detayı açılsın. Sekmeler: Özet (gerçek grafikler: zaman çizgisi, seviye dağılımı,
en yoğun servis ve sunucular), Sinyaller (filtrelenebilir tablo), Incident'lar,
Aksiyonlar. Birden fazla veri seti yüklenebilsin, dropdown'dan seçilsin,
birbirine karışmasın; iki veri setini yan yana karşılaştıran bir alan olsun.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

app.py sayfa dağıtıcısı, i18n.py TR/EN sözlüğü, kpi2 kartları ve detay panelleri, Altair grafikleri, çoklu veri seti kayıt defteri ve compare.py karşılaştırma görünümü; AppTest ile arayüz testleri.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.
