# Bulgular — S-A1 "Alarm Fırtınası" veri paketi

Gözlem penceresi 10 Eylül 2026, 01:30–03:30 · 3.000 alarm · 27 servis · 56 sunucu · 32 bağımlılık kaydı · 5 izleme sistemi (OBM, Prometheus, Zabbix, AppDynamics, SyslogNG). Bu belge motorun (`signal-sprint`, yoğunluk kümeleme modu) aynı paket üzerinde ürettiği sonucu anlatır; sayılar `signal-sprint S-A1_alarm_firtinasi.zip --enrich` çıktısıyla birebir aynıdır.

---

## Bölüm A — Özet (nöbetçi mühendis için)

**3.000 alarm → 5 olay kartı.** 1.752 alarm gerekçesiyle gürültü sayıldı, 31 alarm kart için küçük kalan 9 yoğunluk noktasında listelendi, 1.217 alarm beş kartın içinde. Analiz süresi 0,4 saniye.

| # | Zaman | Olay | Kök neden hipotezi | Alarm | Etkilenen servis | Puan | İlk aksiyon → sahip |
|---|---|---|---|---|---|---|---|
| 1 | 01:33–01:56 | **dc1/rack-A kabin ağ olayı** | Aynı kabindeki 9 sunucuda link kopması / flap / paket kaybı; DNS ve auth o kabinde olduğu için 16 servise yayılan zaman aşımı dalgası | 461 | 16 | 0,79 | Rack-A ToR switch / uplink kontrolü, kritik servisleri diğer kabine yönlendir → Ağ / veri merkezi ekibi |
| 2 | 02:33–03:01 | **payment-provider-gw dış servis kesintisi** | Dış ödeme sağlayıcısı erişilemiyor (HTTP 502/504) → payment-service, mobile-bff, order-service zaman aşımı ve işlem hataları | 308 | 5 | 0,78 | Sağlayıcı durum sayfası + SLA, devre kesici / kuyruklama → Ödeme entegrasyon ekibi |
| 3 | 02:04–02:26 | **billing-db disk dolu** | Disk %100 → tablespace genişletilemedi → yazma hatası → bağlantı havuzu → billing / charging / invoice-batch | 263 | 6 | 0,71 | /data alanını genişlet, invoice-batch'i durdur → DBA ekibi |
| 4 | 02:24–03:03 | **session-service bellek sızıntısı** (yavaş gelişen) | gc duraklaması → heap kritik / OOM riski (3 sunucu) → auth-service'in session-service çağrıları zaman aşımı | 38 | 2 | 0,44 | Servisi kontrollü yeniden başlat, heap limitini artır, sızıntıyı araştır → Nöbetçi mühendis |
| 5 | 03:05–03:30 (devam ediyor) | **Toplu iş penceresi çakışması** | batch-scheduler çakışma → reconciliation / report batch aynı anda → subscriber-db bağlantı havuzu tükendi → subscriber-service gecikmesi | 147 | 5 | 0,44 | Çakışan batch'leri durdur / sırala, havuzu geçici artır → Batch operasyon |

**Müdahale sırası önerisi:** 5 (tek devam eden olay, 03:30'da hâlâ hata basıyor) → 2 (müşteri ödemesi, tüm sunucular kritik) → 1 (en geniş etki, ama 01:54'te kendiliğinden toparlanmış) → 3 (02:26'da durdu, kalıcı düzeltme gerek) → 4 (yavaş sızıntı, tekrar edecek). Puan sıralaması "ne kadar önemli", müdahale sırası "şu an ne yanıyor" sorusuna cevap verir; ikisi farklıdır.

**Gürültü:** 1.752 alarm, her servise sabit hızda dağılmış; %40'ı bilgi (1), %37'si uyarı (2), %26'sı küçük (3), 90'ı büyük (4). Baskın tipler: mem_high 226, cert_expiry 210, latency_high 201, log_rotate 185, cpu_high 179, ntp_drift 177, disk_warn 169, backup_warn 156. Hiçbiri kendi servisinin normal hızını aşmıyor.

---

## Bölüm B — Ayrıntılı bulgular

### B1. Veri paketi ve ön işleme

| Dosya | Satır | İşlem |
|---|---|---|
| alarms.json | 3.000 | Olay akışı; tamamı işlendi |
| alarms.csv | 3.000 | Aynı alarmlar; `alarm_id` ile tekilleştirildi, JSON kopyası tutuldu (paket tek ZIP olarak yüklendiğinde) |
| service_dependencies.csv | 32 | kaynak → hedef (hedef bozulursa kaynak etkilenir), tip, kritiklik; korelasyon ve kök neden yönü için kullanıldı |
| host_inventory.csv | 56 | host → servis, veri merkezi, kabin, iş kritikliği; kabin düzeyi olay tespiti ve kritiklik faktörü için kullanıldı |
| VERI_SOZLUGU.md, SENARYO_BRIFINGI.md | – | Referans; şiddet ölçeği 1 = bilgi … 5 = kritik buradan alındı |

Şiddet dağılımı: 1 → 614, 2 → 709, 3 → 800, 4 → 629, 5 → 248. Etiketlerdeki kabin / veri merkezi bilgisi envanterle 3.000 satırın hepsinde tutarlı.

### B2. Yöntem (kısa)

1. Pencere 5 dakikalık kovalara bölünür (24 kova). Servis ve sunucu başına kovadaki alarm sayısı, o servisin kendi medyanının 3 katını (en az 4) aşarsa hücre **sıcak**tır. Bu pakette 96 sıcak hücre.
2. Sıcak hücreler aynı servis / tanımlı bağımlılık / aynı kabin (ağ tipli alarmlar) ile, en fazla bir kova arayla bağlanır → 14 küme. Küme içinde kendi normal hızında kalan (servis, alarm tipi) çiftleri gürültüye geri verilir. 15 alarmdan küçük ya da 3'ten az ERROR+ içeren kümeler kart olmaz (9 küme).
3. Şiddeti kademeli tırmanan neden-tipi zincirler (15 dakikada ≥ 4 ERROR+, ≥ 2 sunucu) ayrı kart olarak çıkarılır; bağımlılık tablosu kullanmadığı için komşu olayları köprüleyemez (kart 4 böyle bulundu).
4. Kök neden: alarm tipinin nedensellik önceliği (ağ > disk > veritabanı > dış servis > kaynak > belirti), şiddet, adet, grubun ilk neden-tipi alarmı, bağımlılık yönü, kabin geneli ağ olayı. En iyi üç rakip hipotez karta yazılır.
5. Puan: patlama 0,30 (olayın tepe dakikası / üye servislerin olay dışı medyanı), şiddet 0,20, etki alanı 0,20 (servis + sunucu / 41,5), süre 0,15, iş kritikliği 0,15 (envanterde kritik / yüksek sunucu payı).

### B3. Olay 1 — dc1/rack-A kabin ağ olayı (01:33–01:56, 461 alarm, puan 0,79, critical)

- **Ne oldu:** 01:42'de dc1/rack-A kabinindeki sunucularda arayüz kopması (network_down ×12), link flap (×33) ve paket kaybı (×22) başladı. dns-resolver ve auth-service bu kabinde; DNS çözümleme ve kimlik doğrulama bozulunca api-gateway, mobile-bff, web-bff, subscriber-service, order-service, charging-service, billing-service, message-queue, report-batch ve diğerlerinde zaman aşımı (×82), HTTP 5xx (×90), gecikme (×84) ve iş parçacığı havuzu doluluğu (×79) dalgası oluştu. Tepe dakika 55 alarm (normal 1).
- **Nerede:** 16 servis, 34 sunucu; alarmların 237'si dc1/rack-A, kalanı bu kabine bağımlı servislerin diğer kabinlerdeki sunucuları. Sunucuların %85'i iş kritikliği yüksek / kritik.
- **Kök neden hipotezi:** kabin geneli ağ olayı; kart üzerinde temsilci alarm message-queue'daki paket kaybı (01:42:54, kritik). Gerekçe: aynı kabindeki 9 sunucu ağ alarmı bildiriyor, pkt_loss belirti değil neden tipi, gruptaki 31 sinyal ona bağımlı servislerden geliyor.
- **Karşı olasılıklar:** subscriber-db paket kaybı (17,1), dns-resolver paket kaybı (16,9), message-queue ens192 link down (16,4). Hepsi aynı kabinde ve aynı dakikada; hangisinin "ilk" olduğu ağ olayı için önemsiz, kök kabin uplink'idir.
- **Zaman aşımı hedefleri:** api-gateway → auth-service 10, api-gateway → dns-resolver 8, subscriber-service → subscriber-db 8, web-bff → api-gateway 7. Zincir DNS/auth'tan gateway'e, oradan BFF'lere ilerliyor; bağımlılık tablosuyla uyumlu.
- **Nasıl bitti:** 01:54:32'de son hata, sonrasında 95 dakika sessizlik; kendiliğinden (ya da elle) toparlanmış, veri setinde açık bir "link up" kaydı yok.
- **İlk aksiyon:** Rack-A ToR switch / uplink port hataları ve flap kontrolü, yedek uplink; dns-resolver / auth / api-gateway trafiğini diğer kabindeki eşlere yönlendirme; ağ ekibine P1. Sahip: Ağ / veri merkezi ekibi.

### B4. Olay 2 — payment-provider-gw dış servis kesintisi (02:33–03:01, 308 alarm, puan 0,78, critical)

- **Ne oldu:** 02:40:28'de üç payment-provider-gw sunucusu (ao-052/053/054) dış ödeme sağlayıcısına erişemedi (ext_unreach ×11, HTTP 502/504) ve yanıt süresi 500 ms'yi aştı (ext_slow ×13). Ona bağımlı payment-service (93 alarm), mobile-bff (95), order-service (60) zaman aşımı (×79), işlem hatası (×87) ve HTTP 5xx (×67) üretti. Tepe dakika 23 alarm.
- **Nerede:** 5 servis, 14 sunucu, dc1 ve dc2'ye dağılmış (kabin bağımsız → ağ olayı değil). Sunucuların %100'ü kritik.
- **Kök neden hipotezi:** dış sağlayıcı erişilemiyor (02:40:28, kritik). Gerekçe: ext_unreach neden tipi, gruptaki 10 sinyal ona bağımlı servislerden geliyor. Zaman aşımlarının hedefi tutarlı: payment-service → payment-provider-gw 34, mobile-bff → 26, order-service → 19.
- **Karşı olasılıklar:** payment-provider-gw yavaş yanıt (9,1; aynı olayın diğer yüzü), mobile-bff eth1 link flap (7,1; tek sunucu, 2 dakika önce, zayıf), auth-service CPU (4,6; arka plan).
- **Nasıl bitti:** son hata 03:01:16, sonra 29 dakika sessiz; sağlayıcı toparlanmış görünüyor.
- **İlk aksiyon:** sağlayıcı durum sayfası ve sözleşme SLA'sı, sağlayıcıyla iletişim; payment-service'te devre kesici / retry with backoff, müşteriye "ödeme gecikmeli" mesajı; alternatif sağlayıcı yolu. Sahip: Ödeme entegrasyon ekibi.

### B5. Olay 3 — billing-db disk dolu → tablespace (02:04–02:26, 263 alarm, puan 0,71, critical)

- **Ne oldu:** 02:00'de ao-035-billing'de disk uyarısı, 02:05:06'dan itibaren üç billing-db sunucusunda disk %100 (disk_full ×17, kritik), hemen ardından "tablespace genişletilemedi" yazma hataları (db_write_fail ×33) ve bağlantı havuzu tükenmesi (db_conn_pool ×24). billing-service (79 alarm), charging-service (61), payment-service (36), invoice-batch (28), order-service (25) gecikme, işlem hatası ve 5xx üretti. Tepe dakika 18.
- **Nerede:** 6 servis, 15 sunucu, dört kabine dağılmış; sunucuların %93'ü kritik.
- **Kök neden hipotezi:** disk dolu (02:05:06). Gerekçe: disk_full en yüksek nedensellik önceliğinde, grubun ilk neden-tipi alarmı, gruptaki 18 sinyal ona bağımlı servislerden geliyor.
- **Karşı olasılıklar:** tablespace yazma hatası (10,9; diskin sonucu), billing-db eth1 link flap (9,7; tek arayüz, ağ olayı için kabin kanıtı yok), billing-service bağlantı havuzu (6,9; aşağı akış belirtisi).
- **Zaman aşımı hedefleri:** charging-service → billing-service 22, payment-service → billing-service 5; zincir DB → billing-service → onu çağıranlar.
- **Nasıl bitti:** son hata 02:26:36, sonra sessiz; disk alanı açılmış olmalı, kayıt yok.
- **İlk aksiyon:** /data alanını genişlet veya arşiv / log temizliği, tablespace'i yeniden genişlet; yazma hataları durana kadar invoice-batch'i durdur; %85 eşiğinde erken uyarı. Sahip: DBA ekibi.

### B6. Olay 4 — session-service bellek sızıntısı (02:24–03:03, 38 alarm, puan 0,44, critical; yavaş gelişen)

- **Ne oldu:** Zincir aslında 01:35'te başlıyor: üç session-service sunucusunda mem_high şiddeti 2 → 3 → 4'e tırmanıyor (01:35–02:20), 02:11'den itibaren gc_pressure (3 → 4), 02:38'den itibaren oom_risk (4 → 5, "heap kritik, OutOfMemory riski"). 02:30–03:00 arasında auth-service'in session-service çağrıları zaman aşımına uğruyor (×15). Servisin toplam alarm hızı hiçbir kovada medyanın 3 katını aşmadığı için patlama tabanlı tespit bunu görmüyor; yavaş yanma çıkarımı ERROR+ tırmanmasını 02:24'te yakalıyor.
- **Nerede:** session-service (23 alarm, 3 sunucu, 3 farklı kabin → ağ değil, uygulama içi), auth-service (15). Sunucular %100 kritik.
- **Kök neden hipotezi:** gc duraklaması eşik aşımı (02:24:36), grubun ilk neden-tipi alarmı; ardından OOM riski (karşı olasılık 3,1 ve 2,9; aynı zincirin sonraki halkası). auth-service zaman aşımı aşağı akış belirtisi (0,5).
- **Nasıl bitti:** son hata 02:57:52; 03:17–03:30 arasında yeniden mem_high 3 görülüyor, sızıntı kalıcı düzeltilmemiş olabilir.
- **İlk aksiyon:** servisi kontrollü yeniden başlat, heap limitini artır, bellek sızıntısını araştır. Sahip: Nöbetçi mühendis (uygulama ekibine devredilmeli).
- **Not:** Bu olay bağımsız bir çapraz doğrulamada bulundu; ilk sürüm kartı üretmiyordu ve auth-service zaman aşımları olay 2'ye karışıyordu. Kartın 01:35–02:20 düşük şiddetli başlangıcı hâlâ kart dışında (bilinen kısıt).

### B7. Olay 5 — toplu iş penceresi çakışması (03:05–03:30, 147 alarm, puan 0,44, high; devam ediyor)

- **Ne oldu:** 03:05:28'de batch-scheduler "toplu iş penceresi çakışması" (×4, büyük); reconciliation-batch ve report-batch aynı anda çalışıp subscriber-db'yi yükledi: CPU yüksek (×43), bağlantı havuzu tükendi (×48, 03:07'den itibaren), batch_slow (×12); subscriber-service gecikmesi (latency_high ×36). Tepe dakika 9. Pencere sonunda (03:30:06) hâlâ hata basıyor.
- **Nerede:** 5 servis, 9 sunucu; sunucuların %67'si kritik.
- **Kök neden hipotezi:** batch penceresi çakışması (03:05:28); grubun en erken ve ilk neden-tipi alarmı, gruptaki 12 sinyal ona bağımlı servislerden geliyor.
- **Karşı olasılıklar:** subscriber-db bağlantı havuzu (8,0; 4 dakika sonra başlıyor, 20 sinyal ona bağımlı; çakışmanın sonucu olması daha olası), subscriber-db CPU (6,1).
- **Nasıl bitti:** bitmedi; veri seti 03:30'da kesiliyor.
- **İlk aksiyon:** çakışan toplu işleri durdur / sırala, batch-scheduler pencerelerini ayır; subscriber-db havuzunu geçici artır, uzun sorguları sonlandır; zamanlayıcıya çakışma koruması. Sahip: Batch operasyon.

### B8. Kart olmayan yoğunluk noktaları (9 adet, 31 alarm)

message-queue 02:01 (6) ve 02:28 (9), web-bff 02:45 (3), cache-cluster 02:08 (2) ve 02:16 (2), kyc-provider-gw 03:10 (2), object-store 01:37 (3), sms-gateway 02:41 (2), dns-resolver 01:31 (2). Hepsi tek servis, tek sunucu, 15 alarmdan az, çoğunlukla arka plan tipleri; Gürültü denetimi sekmesinde "kart için küçük" nedeniyle listelenir. message-queue 02:28 (9 alarm) izlenmeye değer tek aday.

### B9. Gürültü analizi (1.752 alarm)

- Nedeni tek: servisinin normal alarm hızı içinde, servis ya da sunucuda patlama yok.
- Şiddet: 1 → 555, 2 → 654, 3 → 453, 4 → 90, 5 → 0. Yani "büyük" seviyeli 90 alarm da gürültü; şiddet tek başına ayırt edici değil (senaryo uyarısıyla uyumlu).
- Tip: mem_high, cert_expiry, latency_high, log_rotate, cpu_high, ntp_drift, disk_warn, backup_warn; tümü 27 servise düzgün dağılmış.
- Kart içinden geri verilen alarmlar: üye servislerin olay sırasında da normal hızında çalışan tipleri (örneğin olay 2 sırasında payment-provider-gw'nin sertifika ve yedekleme uyarıları) gürültüye iade edildi.

### B10. Bağımlılık ve envanterden gelen katkı

- Olay 1'in "kabin ağ olayı" teşhisi yalnız envanterle mümkün oldu: alarmlardaki kabin etiketi + aynı kabindeki 9 farklı sunucu.
- Olay 2, 3 ve 5'te kök neden yönü bağımlılık tablosundan doğrulandı: hedef servis bozulunca kaynak servislerin alarm bastığı görüldü (payment-service → payment-provider-gw, charging-service → billing-service, subscriber-service → subscriber-db).
- Olay 4'te auth-service → session-service bağımlılığı, zaman aşımlarının hangi olaya ait olduğunu belirledi.
- İş kritikliği puanlamaya girdi: olay 2 ve 4 sunucuların tamamı kritik; olay 5'te yalnız %67.

### B11. Kısıtlar ve açık sorular

- Olay 4'ün başlangıcı gerçek veride 01:35; kart 02:24'ten itibaren. Düşük şiddetli tırmanma dedektörü eklenirse kart uzar.
- "Kendiliğinden düzeldi" bilgisi sessizliğe dayanır; alarm verisinde iyileşme kaydı yok.
- Doğrulama verisi (alarm → olay etiketi) açıklanınca ölçülecekler: kök neden isabeti (5 hipotez), yanlış birleştirme (olay 1'in 16 servisi tek olay mı), gürültü eleme kesinliği (1.752 alarmın kaçı gerçekten gürültü).
- Eşikler (3× medyan, 15 alarm, 5 dk kova) bu paketin profiline göre seçildi; `src/signal_sprint/scenario/__init__.py` içinde açıktır.
