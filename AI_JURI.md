# AI Jüri Özeti

## 1. Tek Cümlelik Özet

Signal Sprint, SRE ekiplerinin binlerce log / alarm / metrik satırındaki gürültüyü birkaç gerekçeli incident'a indirip her birini kanıtıyla açıklayan ve aksiyona bağlayan, çalışma zamanında LLM gerektirmeyen bir karar destek uygulamasıdır.

## 2. Problem Tanımı

- **Kim:** Operasyon / SRE / NOC ekipleri ve nöbetçi mühendisler.
- **Ne:** Kesinti anında farklı formatlarda (JSON, CSV, syslog, key=value, düz metin, Alertmanager, ticket) on binlerce satır akar; aynı hatanın tekrarları gerçek sinyali gömer, kök neden ve "ne zaman başladı / düzeldi mi" soruları elle cevaplanır.
- **Neden önemli:** Her dakika gecikme SLO / SLA ihlali ve müşteri etkisi demektir; "neden bu alarm önemli" sorusuna kanıtsız cevap verilemez, aksiyonlar takip edilmez, aynı hata bir sonraki nöbette yeniden öğrenilir.

## 3. Çözüm

Ana akış:

1. Kullanıcı dosya / ZIP / klasör yükler, bir HTTP API veya MCP sunucusuna bağlanır ya da sunuculara `agent.py` kurar (canlı olay + CPU / GPU / bellek / disk).
2. Sistem formatı kendisi tanır, sütunları rollere (zaman, seviye, servis, host, ortam, kaynak, mesaj) eşler, her satırı kanonik **Observation** modeline çevirir.
3. Mesajlar maskelenip parmak izine indirilir (gürültü azaltma), patlama skoru hesaplanır, zaman penceresi + ortak varlık ile sinyaller **incident**'ta toplanır, kök neden seçilir, önem puanı faktörleriyle açıklanır.
4. Sonuç: incident flashcard'ı (ne oldu / neden / nerede / ne zaman / nasıl düzeldi / ne yapmalı / daha önce görüldü mü), tıklanabilir kanıt satırları, aksiyon kanbanı, playbook (hata kütüphanesi), ITSM ticket ilişkilendirmesi, postmortem taslağı ve isteğe bağlı LLM açıklaması.

## 4. Mimari (Özet)

Detay için [docs/mimari.md](docs/mimari.md).

```
[Dosya / API / MCP / Ajan] -> [loader + format_detector + parsers] -> [Observation]
      -> [analysis: template -> fingerprint -> signal -> incident -> skor + kanıt]
      -> [Streamlit dashboard | CLI | MCP sunucusu] -> [actions.db, playbook.db]
```

## 5. Yapay Zekâ Kullanımı

| Alan | Nasıl Kullanıldı |
|------|------------------|
| Geliştirme | Tüm kod, testler ve belgeler Claude Fable 5.1 (Claude Cowork) ile üretildi; her adım `docs/AI_LOG.md`'de araç + sürüm + tarih + iş olarak kayıtlı, dosya bazında "AI aracı" sütunu `README.md`'de. |
| Ürün içi | Çekirdek deterministik, LLM zorunlu değil. İsteğe bağlı: OpenAI uyumlu yerel / bulut LLM ile incident açıklaması ("LLM ile açıkla"), Claude SAKA'ya yapıştırılabilir kanıt paketi (prompt bundle). |
| Entegrasyon | Motor bir MCP sunucusu olarak dışa açılır (`mcp_server.py`, 8 araç); Claude Desktop veya başka bir MCP istemcisi veri setini analiz ettirebilir. |

Kritik prompt'lar: [prompts/](prompts/)

## 6. Yenilikçilik

- Veri setini önceden bilmeden çalışır: format tanıma, sütun-rol eşleme ve maskeleme sayesinde etkinlik günü verilen set parser değişikliği olmadan (gerekirse yalnız `scenario/` ayarıyla) işlenir.
- Her karar açıklanabilir: sinyal için "neden bu sinyal", incident için faktör katkıları, kök neden gerekçesi ve satır düzeyinde kanıt.
- Kendiliğinden düzelen incident'lar tespit edilir ve otomatik "done" aksiyon olarak kaydedilir; playbook aynı hatayı bir sonraki veri setinde "daha önce görüldü" diye hatırlatır.
- Canlı operasyon sayfası: ortam / sunucu kapsamı, SLO / SLA / hata bütçesi ve her kartın "oranı ne düşürüyor" detayı.

## 7. Teknik Zorluk

En zor kısım bilinmeyen formatı bozmadan ve hızlı işlemek oldu: iç içe JSON (Alertmanager) tek kayıt sanılıyordu, zamanı olmayan satırlar grafiği 56 yıla yayıyordu, dateutil ile satır başına tarih parse 31 sn sürüyordu. Çözüm: özyinelemeli kayıt bulma, "zamansız" işaretleyip en erken zamana doldurma, `fromisoformat` → şekil önbellekli `strptime` → dateutil sıralı hızlı yol ve dokuz maskeleme regex'ini tek birleşik regex'e indirme (6 sn). İkinci zorluk ticket ilişkilendirmesinin her ticket'ı her sinyale bağlamasıydı; ortak varlık veya en az iki özgül anahtar kelime şartı ve okunur etiketlerle çözüldü.

## 8. Tamamlanma Durumu

| Özellik | Durum |
|---------|-------|
| Loader (ZIP / TAR.GZ / GZ / klasör), format tanıma, 6 parser, sütun-rol eşleme | ✅ Tamam |
| Gürültü azaltma, ilişkilendirme, kök neden, faktörlü puan, kanıt | ✅ Tamam |
| Incident flashcard, düzelme tespiti, aksiyon kanbanı, playbook | ✅ Tamam |
| Canlı operasyon (ajan, simülatör, metrikler, SLO / SLA, kapsam, kart detayları) | ✅ Tamam |
| ITSM (ServiceNow / Jira / OneDesk / REST) ilişkilendirme | ✅ Tamam |
| HTTP API / MCP veri kaynağı, MCP sunucusu, Docker | ✅ Tamam |
| İsteğe bağlı LLM açıklaması | ✅ Tamam |
| TR / EN arayüz, 30 pytest testi | ✅ Tamam |
| Etkinlik günü veri setine `scenario/` ayarı | 🚧 Etkinlik günü |

## 9. Çalıştırma Talimatı

Bkz. [README.md](README.md#kurulum)

## 10. Bilinen Kısıtlar

- Canlı ajan, MCP ve ITSM bağlantıları demo ortamında yerleşik simülatör / demo ticket'larla gösterilir; gerçek sistemlere bağlanmak için yalnız URL ve anahtar gerekir.
- p95 gecikme, mesajlarda `<n>ms` deseni olduğunda hesaplanır; yoksa kart "veri yok" gösterir.
- Servis seviyeleri son 15 dakikalık canlı pencere üzerinden hesaplanır; uzun dönem SLO raporu kapsam dışıdır.
- Playbook benzerlik eşleşmesi token örtüşmesine dayanır (≥ 0.6); anlamsal eşleşme için isteğe bağlı LLM kullanılabilir.
