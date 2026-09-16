# AI Jüri Özeti

## 1. Tek Cümlelik Özet

Signal Sprint, S-A1 alarm fırtınasındaki 3.000 alarmı kök neden hipotezi, karşı olasılıklar, sahipli ilk aksiyon ve gürültü denetimi taşıyan 5 olay kartına indiren, çalışma zamanında LLM gerektirmeyen bir SRE karar destek uygulamasıdır.

## 2. Problem Tanımı

- **Kim:** Operasyon merkezi / SRE / NOC ekipleri ve gece nöbetçisi mühendis.
- **Ne:** 02:14'te alarm ekranı hızlanır; iki saatte 3.000 alarm, 27 servis, 56 sunucu, 5 izleme sistemi. Birden fazla bağımsız olay aynı anda yaşanır, alarm tipleri olaylar arasında paylaşılır, arka plan gürültüsü her servise sabit hızda ve yüksek şiddette de gelir. Alarmlar arasındaki neden-sonuç ilişkisi görünmez.
- **Neden önemli:** Kök neden, türev etki ve gürültü ayırt edilemeyince müdahale sırası yanlış kurulur, çözüm süresi uzar; her dakika SLA ihlali ve müşteri etkisi demektir. Nöbetçinin yedi dakikası vardır.

## 3. Çözüm

Veri paketi (alarms.json / csv, service_dependencies.csv, host_inventory.csv) tek ZIP olarak yüklenir. Ana akış:

1. **Alım:** yan tablolar (bağımlılık, envanter) referans olarak ayrılır; alarms.json ile alarms.csv `alarm_id` ile tekilleştirilir, 3.000 alarmın tamamı işlenir; şiddet ölçeği (1 = bilgi … 5 = kritik) kanonik seviyelere eşlenir.
2. **Yoğunluk kümeleme** (`storm.py`): pencere 5 dk'lık kovalara bölünür; bir servis ya da sunucu kendi medyan hızının 3 katını aşınca hücre "sıcak" olur. Sıcak hücreler aynı servis / tanımlı bağımlılık / aynı kabin (ağ alarmları) ile ve en fazla bir kova arayla bağlanır. Normal hızındaki alarm tipleri gürültüye geri verilir; 15 alarmdan küçük noktalar kart olmaz. Şiddeti kademeli tırmanan neden-tipi zincirler (bellek sızıntısı → gc → oom) ayrı kart olarak çıkarılır.
3. **Kök neden:** alarm tipine göre nedensellik önceliği (ağ > disk > veritabanı > dış servis > kaynak > belirti), şiddet, adet, grubun ilk neden-tipi alarmı, bağımlılık yönü (hedef bozulursa kaynak etkilenir), kabin geneli ağ olayı. En iyi üç rakip hipotez puan ve gerekçesiyle **karşı olasılık** olarak karta yazılır.
4. **Sonuç:** her kart için kök neden gerekçesi, etkilenen servisler, alarm sayısı, zaman aralığı, sahipli ve açık durumlu **ilk aksiyon** (Aksiyonlar sekmesinde kapatılır), Playbook'ta "daha önce görüldü mü", hata haritası; **Gürültü denetimi** sekmesinde elenen her alarmın nedeni ve servis × zaman ısı haritası.

**S-A1 sonucu:** 3.000 alarm → 5 kart · 1.752 alarm gerekçesiyle elendi · analiz 0,4 sn.

| Kart | Zaman | Kök neden hipotezi | İlk aksiyon sahibi | Puan |
|---|---|---|---|---|
| dc1/rack-A kabin ağ olayı | 01:33–01:56 | 9 sunucuda link kopması / paket kaybı; DNS ve auth o kabinde, 14 servise yayılan zaman aşımı dalgası | Ağ / veri merkezi ekibi | 0,79 |
| payment-provider-gw | 02:33–03:01 | Dış ödeme sağlayıcısı erişilemiyor → payment / mobile-bff / order zaman aşımı ve işlem hataları | Ödeme entegrasyon ekibi | 0,78 |
| billing-db | 02:04–02:26 | Disk dolu → tablespace genişletilemedi → bağlantı havuzu → billing / charging / invoice-batch | DBA ekibi | 0,71 |
| session-service | 02:24–03:03 | Bellek sızıntısı: gc duraklaması → oom riski (3 sunucu) → auth-service zaman aşımı; yavaş gelişen | Nöbetçi mühendis | 0,44 |
| batch-scheduler | 03:05–03:30 | Toplu iş penceresi çakışması → subscriber-db bağlantı havuzu → subscriber-service gecikmesi | Batch operasyon | 0,44 |

Neden 5 kart: veride yoğunluğu servisin kendi medyanının 3 katını aşan dört bağımsız zaman-topoloji bölgesi ve şiddeti tırmanan bir yavaş yanma zinciri var; bunun dışındaki dokuz sıcak nokta 15 alarmdan küçük ve denetim görünümünde listelenir.

## 4. Mimari (Özet)

Detay için [docs/mimari.md](docs/mimari.md).

```
[ZIP: alarms + bağımlılık + envanter] -> [loader · format_detector · parsers · tables · dedupe] -> [Observation]
      -> [storm: sıcak hücreler -> bağlama -> kümeler -> yavaş yanma çıkarımı]
      -> [analysis: sinyaller -> kök neden + karşı olasılıklar -> puan -> ilk aksiyon]
      -> [Streamlit: kartlar · gürültü denetimi · hata haritası · aksiyon kanbanı | CLI --enrich | MCP sunucusu]
```

## 5. Yapay Zekâ Kullanımı

| Alan | Nasıl Kullanıldı |
|------|------------------|
| Geliştirme | Tüm kod, testler ve belgeler Claude Fable 5.1 (Claude Cowork) ile üretildi. Etkinlik günü iş bölümü: veri keşfi (servis × 5 dk yoğunluk tabloları), kümeleme ve kök neden kurallarının tasarımı, kod ve testler AI ile; eşik ve öncelik kararları (3× medyan, 15 alarm, nedensellik sırası, kart sayısı, ağırlıklar) insan onayıyla. İkinci bir AI aracıyla bağımsız çapraz doğrulama yapıldı; kaçırılan beşinci olay ve puanlamadaki doyma sorunu böyle bulundu ve düzeltildi. Her adım `docs/AI_LOG.md`'de araç + sürüm + tarih + iş olarak kayıtlı; dosya bazında "AI aracı" sütunu `README.md`'de. |
| Ürün içi | Çekirdek deterministiktir, LLM zorunlu değildir. İsteğe bağlı: OpenAI uyumlu yerel / bulut LLM ile incident açıklaması ("LLM ile açıkla"), Claude SAKA'ya yapıştırılabilir kanıt paketi (prompt bundle). |
| Entegrasyon | Motor MCP sunucusu olarak dışa açılır (`mcp_server.py`, 8 araç); Claude Desktop veya başka bir MCP istemcisi veri setini analiz ettirebilir. |

Kritik prompt'lar: [prompts/](prompts/)

## 6. Yenilikçilik

- Alarm fırtınasında gruplama şablonla değil **yoğunlukla** yapılır: servis × zaman hücrelerinde medyana göre anomali, bağımlılık tablosu ve kabin bilgisiyle bağlama. Sabit hızlı gürültü, yüksek şiddette bile olsa karta giremez.
- **Yavaş yanma çıkarımı:** şiddeti tırmanan neden-tipi zincirler patlama yapmasa da ayrı kart olur; bağımlılık bağı kullanmadığı için komşu olayları köprüleyemez.
- Kök neden **nedensellik önceliği + bağımlılık yönü + ilk neden-tipi alarm** ile seçilir; karşı olasılıklar puanlarıyla kartta durur, jüri "neden bu?" sorusunun cevabını ekranda görür.
- Her elenen alarmın nedeni vardır (**gürültü denetimi**); indirgeme kararı denetlenebilir.
- Zenginleştirilmiş tek dosya: her alarm satırına envanter, bağımlılık, sıcak hücre, incident, rol ve gürültü nedeni eklenir; yalnız bu dosya yüklendiğinde aynı kartlar çıkar.

## 7. Teknik Zorluk

Asıl zorluk alarm fırtınasının yapısıydı: bir olay onlarca alarm tipine ve şiddete yayılırken arka plan gürültüsü her servise sabit hızda ve ERROR / CRITICAL seviyede de geliyordu. Mesaj şablonu bazlı gruplama ortak servis adları üzerinden her şeyi tek karta zincirledi (ilk deneme: 3.000 alarm → 1 kart). Çözüm gruplamayı yoğunluğa taşımak oldu. İkinci zorluk yavaş gelişen olaydı: session-service'in 01:35'ten 02:52'ye tırmanan bellek zinciri servis toplamını medyanın 3 katına çıkarmadığı için görünmüyordu. İlk deneme (genel tırmanma dedektörü) uzun hücrelerle iki olayı köprüleyip yanlış birleştirme yaptı; çözüm, çıkarımı normal kümelemeden sonra, bağımlılık bağı kullanmadan ve o servise yönelik zaman aşımlarını diğer kümelerden geri alarak yapmak oldu. Üçüncü zorluk puanlama: etki alanı faktörü tüm kartlarda doyuyordu ve patlama sinyal düzeyinde ölçülüyordu; olay düzeyi patlama, veri setine göre normalize etki alanı ve envanter iş kritikliği faktörü eklendi.

## 8. Tamamlanma Durumu

| Özellik | Durum |
|---------|-------|
| 3.000 alarmın tamamı işlenir; json / csv tekilleştirme; yan tablolar | ✅ Tamam |
| Anlamlı gruplara indirgeme, grup başına tek kart (5 kart ≤ 15) | ✅ Tamam |
| Kartta kök neden hipotezi + gerekçe, etkilenen servisler, alarm sayısı, zaman aralığı | ✅ Tamam |
| Kart başına önerilen ilk aksiyon, sahip ve durumla otomatik kayıt | ✅ Tamam |
| Aksiyonun açılıştan kapanışa izlenmesi (kanban, durum değişimi) | ✅ Tamam |
| Bonus: karşı olasılıklar, gürültü denetimi görünümü, Playbook ile geçmiş örüntü | ✅ Tamam |
| Hata haritası (bağımlılık + hata akışı), zenginleştirilmiş tek dosya, CLI, MCP sunucusu | ✅ Tamam |
| TR / EN arayüz, 32 pytest testi, Docker | ✅ Tamam |
| Yavaş yanma kartının 01:35–02:20 düşük şiddetli başlangıcını kapsaması | 🚧 Kısmi |
| Demo videosu | ⬜ Planlandı |

## 9. Çalıştırma Talimatı

Bkz. [README.md](README.md#kurulum)

## 10. Bilinen Kısıtlar

- Yavaş yanma kartı tırmanmayı ERROR+ seviyesinde yakalar; session-service zincirinin 01:35–02:20 arasındaki düşük şiddetli mem_high başlangıcı kartın dışında kalır.
- Kartlardaki "kendiliğinden düzeldi" bilgisi alarm verisinde iyileşme kanıtı olmadığı için "son alarmdan sonra sessizlik" sezgisine dayanır.
- Eşikler (3× medyan, 15 alarm, 5 dk kova, nedensellik öncelikleri) `scenario/` içinde açıktır ve bu veri setine göre seçilmiştir; başka bir kurumun alarm profili için yeniden ayar gerekebilir.
- Playbook benzerlik eşleşmesi token örtüşmesine dayanır (≥ 0,6); anlamsal eşleşme için isteğe bağlı LLM kullanılabilir.
- Canlı ajan, MCP ve ITSM bağlantıları demoda yerleşik simülatör / demo ticket'larla gösterilir; gerçek sistemler için yalnız URL ve anahtar gerekir.
