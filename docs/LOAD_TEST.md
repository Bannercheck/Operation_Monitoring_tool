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
    --user admin@watchover.local --password '…' [--key <LIVE_KEY>]
```
