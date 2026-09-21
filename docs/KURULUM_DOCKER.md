# Watchover · Docker kurulum rehberi

Watchover tek bir imaj olarak çalışır: dashboard (8501), canlı alıcı (8600) ve konteynerin kendini izleyen ajanı aynı konteynerdedir. Kod imajın içindedir; ayarlar, token'lar, veritabanları, canlı tampon, sürüm anlık görüntüleri ve loglar `watchover-data` biriminde durur. Güncelleme demek yeni imajı çekip konteyneri yeniden oluşturmak demektir; veri birimine hiç dokunulmaz.

## 1. Ön koşullar

- Docker Engine 24+ ve Docker Compose v2 (Linux sunucu) ya da Docker Desktop (macOS / Windows).
- İmaj kaydı: `ghcr.io/bannercheck/watchover`. Her `main` gönderimi `:latest` ve `:main`, her `vX.Y` etiketi `:X.Y` ve `:latest` olarak GitHub Actions tarafından yayınlanır. Depo özelse çekmek için bir kez `docker login ghcr.io` gerekir (kullanıcı adı: GitHub hesabınız, parola: `read:packages` yetkili bir kişisel erişim belirteci).

## 2. İlk kurulum (beş dakika)

```bash
mkdir -p ~/watchover && cd ~/watchover
curl -fsSLO https://raw.githubusercontent.com/Bannercheck/Operation_Monitoring_tool/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/Bannercheck/Operation_Monitoring_tool/main/.env.docker.example -o .env
docker compose up -d
docker compose logs -f dashboard        # "You can now view your Streamlit app" satırından sonra Ctrl-C
```

Tarayıcıda `http://<sunucu>:8501`. İlk giriş yerleşik yönetici hesabıyla yapılır (`admin@watchover.local`, başlangıç parolası ayrıca iletilir); ilk girişte yeni parola istenir.

Depoyu klonladıysanız aynı dizinde `docker compose up -d` yeterlidir; `make docker-up`, `make docker-update`, `make docker-logs`, `make docker-down` kısayolları vardır.

## 3. Yapılandırma (.env)

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `WATCHOVER_TAG` | `latest` | Çalıştırılacak sürüm; `1.4` gibi bir etiket sürümü sabitler |
| `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` | `8501` / `8600` | Ana makinede açılan portlar |
| `WATCHOVER_CPUS` / `WATCHOVER_MEMORY` | `4` / `4g` | Konteynerin kaynak sınırı |
| `WATCHOVER_SELFMON` | `1` | Konteynerin kendini izlemesi; ana makinede ayrı ajan varsa `0` |
| `WATCHOVER_PARALLEL` | boş | Çoklu dosya ayrıştırmada paralellik: boş otomatik (8+ çekirdek), `1` zorla, `0` kapalı |
| `TZ` | `Europe/Istanbul` | Konteyner saat dilimi |
| `DATABASE_URL` | boş | PostgreSQL bağlantısı (bölüm 6) |

Değişiklikten sonra `docker compose up -d` yeni ayarları uygular.

## 4. Kaynak yönetimi

- Sınırlar `.env` içindeki `WATCHOVER_CPUS` ve `WATCHOVER_MEMORY` ile verilir; `docker stats watchover` anlık kullanımı gösterir.
- Büyük SAP arşivleri için önerilen: 4 CPU, 4 GB. 1 GB'lık arşivlerde 8 GB.
- Loglar json-file sürücüsüyle döner (20 MB × 5 dosya); `docker compose logs --tail 200 dashboard`.
- Sağlık denetimi 30 saniyede bir çalışır; `docker compose ps` sütununda `healthy` görünmelidir. Uygulama çökerse `restart: unless-stopped` konteyneri geri açar; Sistem › Güncelleme'deki **Uygulamayı yeniden başlat** aynı mekanizmayı kullanır.

## 5. Güncelleme ve sürüm geçmişi

```bash
cd ~/watchover
docker compose pull && docker compose up -d      # ya da: make docker-update
docker image prune -f                             # eski imaj katmanlarını temizler
```

- Belirli bir sürüme geçmek ya da geri dönmek: `.env` içinde `WATCHOVER_TAG=1.3` yazıp aynı komutu çalıştırın.
- Sistem › Güncelleme sekmesi Docker'da zip / git düğmelerini göstermez, bu komutları gösterir; sürüm notları ve veritabanı anlık görüntüleri (Sistem › Güncelleme › Sürüm geçmişi) yine oradadır ve `/data/versions` altında durur.
- Sistem başlığındaki çip `Watchover v1.4 · 🐳 1.4` biçiminde imaj etiketini gösterir.

## 6. PostgreSQL ile çalıştırmak

```bash
# .env
POSTGRES_PASSWORD=guclu-bir-parola
DATABASE_URL=postgresql://watchover:guclu-bir-parola@postgres:5432/watchover
```

```bash
docker compose --profile postgres up -d
```

Bilgi tabanı, kullanıcılar, roller, ajanlar, kaynaklar, bildirimler ve rollup'lar PostgreSQL'e yazılır. SQLite'tan geçişte verinin taşınması ayrı bir adımdır (sonraki sürüm).

## 7. MCP sunucusu (isteğe bağlı)

```bash
docker compose --profile mcp up -d          # http://<sunucu>:8765/mcp
```

## 8. Ajanlar ve kaynaklar

Sunuculardaki ajanlar aynı şekilde kurulur; alıcı adresi Docker ana makinesidir:

```bash
curl -fsSL http://<sunucu>:8600/agent/install.sh | sudo bash -s -- --url http://<sunucu>:8600/ingest --enroll-key wk_… --env prod
```

Konteynerin kendi ajanı konteynerin CPU / bellek / diskini raporlar. Ana makinenin kendisini izlemek için ana makineye normal bir ajan kurun ve `WATCHOVER_SELFMON=0` yapın.

## 9. Yedekleme ve taşıma

```bash
docker run --rm -v watchover_watchover-data:/data -v "$PWD":/backup alpine tar czf /backup/watchover-data.tgz -C /data .   # yedek
docker run --rm -v watchover_watchover-data:/data -v "$PWD":/backup alpine tar xzf /backup/watchover-data.tgz -C /data     # geri yükleme (konteyner durmuşken)
```

`data/.vault.key` sırların anahtarıdır; yedek onsuz açılmaz. Başka bir sunucuya taşımak yedeği oraya açıp `docker compose up -d` demektir.

## 10. Sorun giderme

| Belirti | Çözüm |
|---|---|
| `pull access denied` | `docker login ghcr.io`; depo özelse paket görünürlüğünü kontrol edin |
| Port çakışması | `.env` içinde `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` değiştirin |
| `unhealthy` | `docker compose logs --tail 100 dashboard`; bellek sınırı düşükse `WATCHOVER_MEMORY` artırın |
| Giriş yapılamıyor | `docker compose exec dashboard python -m watchover.useradmin list` (ve `password EMAIL`, `unlock EMAIL`) |
| Yerel imaj ile denemek | `make docker-build` sonra `.env` içinde `WATCHOVER_IMAGE=watchover WATCHOVER_TAG=local` |
