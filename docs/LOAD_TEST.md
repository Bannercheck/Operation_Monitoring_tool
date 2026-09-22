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

Kendi ortamınızda:

```
python scripts/loadtest.py --url http://<sunucu>:8600 --api http://<sunucu>:8501 --rate 2000 --agents 100 --duration 90 \
    --user admin@watchover.local --password '…' [--key <LIVE_KEY>]
```
