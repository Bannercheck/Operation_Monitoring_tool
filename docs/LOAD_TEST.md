# Yük testi · 100 sanal ajan, ERP ağırlıklı karışım

Betik: `scripts/loadtest.py` (SAP, Oracle EBS, Dynamics AX / 365 / NAV, syslog, JSON, nginx; %60 ERP). Ortam: 4 vCPU / 15 GB, PostgreSQL 16, tek Watchover süreci (alıcı + API + anomali tarayıcı). Sonuç dosyaları: `docs/loadtest-2000.json`, `docs/loadtest-4000.json`.

| Ölçüm | v1.29 (önce) | v1.30, 2.000 satır/sn | v1.30, 4.000 satır/sn |
|---|---|---|---|
| Ajanların bastığı hız | 1.886 satır/sn | 1.963 satır/sn | 3.685 satır/sn |
| Alıcının işlediği olay | 1.737 olay/sn | 1.626 olay/sn | 3.035 olay/sn |
| Paket başına alım (40 / 80 satır) | ort. 85 ms, p95 260 ms | ort. 7 ms, p95 16 ms | ort. 16-22 ms, p95 37-54 ms |
| POST gecikmesi p50 / p95 | 165 ms / 2,9 s | 10 ms / 79 ms | 21 ms / 2,0 s |
| Anomali taraması (5 dk pencere, DB'den) | 16,5 s | 4,2 s (165 bin olay) | 12,6 s (250 bin olay) |
| Tarama sırasında alım p95 | 3,6 s | 0,7 s | 4,1 s |
| HTTP hata | 0 | 0 | 0 |

Olay < satır farkı kayıp değildir: ERP loglarındaki stack trace ve devam satırları önceki olaya katlanır (~%17).

**Ne değişti (v1.30):** aynı ajan + dosya adından gelen paketlerde biçim ve aile tespiti bir kez yapılıp önbelleklenir (paket başına ayrıştırma 20 ms → 2,4 ms, tek iş parçacığı tavanı ~16.500 satır/sn); anomali taraması pencereyi fazla başına değil tarama başına bir kez okur, şablon çıkarma tekrar eden mesajlarda tek sefer; gelecek tarihli olaylar varış zamanına çekilir; olay/sn ve tampon süresi ölçümleri düzeltildi.

**Yorum.** 100 sunucu (≈1.000-2.000 satır/sn) tek düğümde rahat. 4.000 satır/sn'de alım sürer ama dakikalık tarama tek Python sürecinde (GIL) alımı saniyeler boyunca duraklatır; bunun kalıcı çözümü Faz 2'deki ayrı worker sürecidir. Çekirdek sayısı tek sürecin tavanını büyütmez; 32 CPU'lu sunucuda da bu sayılar geçerlidir, fazla çekirdekler LLM ve eğitim servisine gider.

## v1.35: Go alıcısı (aynı 4.000 satır/sn, 100 ajan, PostgreSQL 16, 4 vCPU)

8600 artık Go ile yazılmış ayrı bir servis: paketi doğrular, diske sıralı segment olarak yazar ve hemen yanıt verir; ayrıştırma, geçmiş, iz ve anomali işleri panelin arka planında segmentlerden yürür. İki ölçüm aynı makinede art arda alındı (`docs/loadtest-python-4000.json`, `docs/loadtest-go-4000.json`).

| Ölçüm | Python alıcısı | Go alıcısı |
|---|---|---|
| Ajanların bastığı hız | 3.834 satır/sn | 3.907 satır/sn |
| POST gecikmesi p50 / p95 / en kötü | 22 ms / 924 ms / 5,0 s | 1,0 ms / 3,3 ms / 18,6 ms |
| Panelde işlenen olay (5 dk ortalaması) | 2.940 olay/sn | 3.051 olay/sn |
| Paket başına işleme p95 (test sonu) | 2,4 s (tarama sırasında) | 54 ms |
| Test sonu kuyruk | yok (ajan bekliyordu) | 4 segment, 2,2 MB (birkaç saniyelik iş) |
| HTTP hata | 0 | 0 |

**Ne değişti.** Anomali taraması ya da büyük bir ayrıştırma artık ajanın isteğini bekletmiyor: istek, verinin diske yazıldığı anda yanıtlanıyor. İşleme anlık geride kalırsa fark kuyrukta birikir ve saniyeler içinde erir; kuyruk `WATCHOVER_SPOOL_MAX_MB` (varsayılan 2 GB) sınırını aşarsa alıcı 503 + Retry-After döner ve ajan paketi kendi diskinde tutar. Kuyruk durumu Sistem › Kapasite'de ve `http://<sunucu>:8600/health` adresinde görünür.

Kendi ortamınızda:

```
python scripts/loadtest.py --url http://<sunucu>:8600 --api http://<sunucu>:8501 --rate 2000 --agents 100 --duration 90 \
    --user <yönetici e-posta> --password '…' [--key <LIVE_KEY>]
```

## v1.41: 300 sunucu, dakikada 100 bin ve 300 bin satır, operasyon ekranı yük altında

Senaryo: 300 sanal sunucu (15 pool × 20 sunucu, `prd-<pool>-NN`), bunlardan 100'ü "konuşkan" ve satırların %85'ini gönderiyor; her ajan kendi sunucu adını basıyor. Test boyunca operasyon ekranının 5 sn'de bir yaptığı çağrılar ölçüldü (`--probe`). Ortam önceki ölçümlerle aynı: 4 vCPU, PostgreSQL 16, Go alıcısı + tek panel süreci. Sonuç dosyaları: `docs/loadtest-300srv-100k-before.json`, `docs/loadtest-300srv-100k-after.json`, `docs/loadtest-300srv-300k.json`.

| Ölçüm | v1.40, ~98 bin satır/dk | v1.41, ~103 bin satır/dk | v1.41, ~291 bin satır/dk |
|---|---|---|---|
| Alıcı POST p50 / p95 / en kötü | 0,9 / 2,7 / 77 ms | 0,9 / 2,4 / 37 ms | 1,0 / 4,8 / 38 ms |
| Panelde işlenen olay (5 dk ort.) | 1.293 olay/sn | 1.420 olay/sn | 3.950 olay/sn |
| Kuyruk en çok (Go alıcısı) | 3,4 MB | 0,8 MB | 7,5 MB (eriyor) |
| `/live/stats` (15 dk) p50 / p95 | 1.048 / 2.378 ms | 18 / 29 ms | 31 / 73 ms |
| `/live/slo` p50 / p95 | 633 / 2.175 ms | 9 / 14 ms | 16 / 32 ms |
| `/live/hosts` p50 / p95 | 78 / 118 ms | 68 / 386 ms | 51 / 84 ms |
| `/live/pools` (yeni) p50 / p95 | — | 31 / 50 ms | 43 / 61 ms |
| `/live/groups` (yeni, tekilleştirilmiş) p50 / p95 | — | 40 / 54 ms | 53 / 85 ms |
| HTTP hata | 0 | 0 | 0 |

Olay < satır farkı kayıp değildir: çok satırlı ERP kayıtları ve stack trace'ler tek olaya katlanır.

**Ne değişti.** Operasyon ekranı her yenilemede son 15 dakikanın olaylarını (dakikada 100 bin satırda 1,5 milyon) yeniden okuyordu; ring tampon bu hızda yalnız ~30 sn tuttuğu için okuma veritabanına düşüyor ve 1-3 sn sürüyordu. Artık her olay geldiği anda bir kez sayılıyor (`rollup.py`: dakika × sunucu × önem, sunucu × servis, gecikme histogramı, mesaj kalıbı × servis, 5 dakikalık sunucu × kalıp); ekran sayaçlardan yanıtlanıyor. Anomali taramasının sunucu sayımları da aynı sayaçlardan geliyor; pool çözümü envanteri dakikada bir okuyor.

**Yorum ve sınırlar.** 200+ sunucu ve dakikada 100 bin satır tek düğümde rahat (işleme kapasitesinin ~%35'i). Dakikada ~290 bin satırda işleme yetişiyor ama tavana yakın (~%85); bunun üstü için sıradaki adım ayrıştırma işçilerini Go'ya taşımak ya da işçi süreç sayısını artırmaktır (alıcı zaten Go, kuyruk diskte). Desen taraması (anomali) hâlâ olay penceresini okuyor ve 300 bin/dk'da 10 sn sürüyor; bu sırada ekran çağrılarında tek tük 0,2-0,5 sn gecikme görülür.

Kendi ortamınızda (yönetici e-postası ve parolanızla):

```
python scripts/loadtest.py --url http://<sunucu>:8600 --api http://<sunucu>:8501 --rate 1700 --agents 300 --heavy 100 --heavy-share 0.85 \
    --duration 150 --probe --user <yönetici e-posta> --password '…'
```
