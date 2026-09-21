# Watchover · Docker kurulum rehberi

Watchover Docker'da iki konteyner olarak çalışır: **dashboard** (arayüz 8501, canlı alıcı 8600 ve konteynerin kendini izleyen ajanı) ve **postgres** (ürün veritabanı, PostgreSQL 16). Kod imajın içindedir; ayarlar, token'lar, canlı tampon, sürüm anlık görüntüleri ve loglar `watchover-data` biriminde, veritabanı `watchover-pg` biriminde durur. Güncelleme demek yeni imajı çekip konteyneri yeniden oluşturmak demektir; iki birime de dokunulmaz.

## 1. Ön koşullar ve bağımlılıklar

Sunucuya kurulması gereken **tek şey Docker'dır**. Python, pip, PostgreSQL ya da başka bir paket ana makineye kurulmaz; hepsi imajların içindedir.

| Platform | Kurulum |
|---|---|
| Linux sunucu (Ubuntu / Debian / RHEL) | `curl -fsSL https://get.docker.com \| sudo sh` ve ardından `sudo usermod -aG docker $USER` (yeniden oturum açın). Compose v2 bu paketle gelir: `docker compose version` |
| macOS | Docker Desktop (docker.com/products/docker-desktop); Apple Silicon için arm64 imajı yayınlanır |
| Windows | Docker Desktop + WSL 2; komutlar PowerShell'de aynıdır |

- Kaynak: en az 2 CPU / 4 GB RAM / 10 GB disk. Büyük SAP arşivleri için 4 CPU / 8 GB (bölüm 4).
- Ağ: sunucuda 8501 (arayüz) ve 8600 (ajanlar) portları açık olmalı. İmaj `ghcr.io`'dan çekilir; internet çıkışı olmayan sunucu için bölüm 10.
- İmaj kaydı: `ghcr.io/bannercheck/watchover`. Her `main` gönderimi `:latest` ve `:main`, her `vX.Y` etiketi `:X.Y` ve `:latest` olarak GitHub Actions tarafından yayınlanır (önce testler PostgreSQL ile koşar). Paket özelse çekmek için bir kez `docker login ghcr.io` gerekir (kullanıcı adı: GitHub hesabınız, parola: `read:packages` yetkili bir kişisel erişim belirteci).

## 2. İlk kurulum (beş dakika)

```bash
mkdir -p ~/watchover && cd ~/watchover
curl -fsSLO https://raw.githubusercontent.com/Bannercheck/Operation_Monitoring_tool/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/Bannercheck/Operation_Monitoring_tool/main/.env.docker.example -o .env
nano .env                               # POSTGRES_PASSWORD satırına güçlü bir parola yazın (ilk açılışta veritabanına işlenir)
docker compose up -d                    # imajları çeker, postgres'i başlatır, sağlıklı olunca dashboard'u açar
docker compose ps                       # iki satır da "healthy" olmalı (ilk açılış 30-60 sn)
```

**Arayüze erişim:** tarayıcıda `http://<sunucu-ip>:8501` (aynı makinede `http://localhost:8501`). Sunucunun IP'sini `hostname -I` verir. Uzak bir sunucuda güvenlik duvarı varsa 8501 ve 8600'ü açın (`sudo ufw allow 8501,8600/tcp`). İnternete açık bir sunucuda 8501'i doğrudan açmak yerine bir ters vekil (Nginx / Caddy) arkasına koyup TLS ekleyin; Caddy için tek satır: `watchover.sirket.com { reverse_proxy 127.0.0.1:8501 }`.

**İlk giriş:** yerleşik yönetici hesabı `admin@watchover.local`; başlangıç parolası ayrıca iletilir ve ilk girişte yeni parola istenir. Diğer hesaplar Users sayfasından ya da terminalden açılır:

```bash
docker compose exec dashboard python -m watchover.useradmin add ops@sirket.com --admin
```

Depoyu klonladıysanız aynı dizinde `docker compose up -d` yeterlidir; `make docker-up`, `make docker-update`, `make docker-logs`, `make docker-backup`, `make docker-down` kısayolları vardır. Yerel imaj derlemek için `make docker-build` (bunun için de yalnızca Docker gerekir; pip kurulumu imaj içinde yapılır).

## 3. Yapılandırma (.env)

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `POSTGRES_PASSWORD` | `change-me` | Veritabanı parolası; **ilk açılıştan önce** değiştirin (birim oluşturulurken işlenir) |
| `POSTGRES_USER` / `POSTGRES_DB` | `watchover` / `watchover` | Veritabanı kullanıcısı ve adı |
| `DATABASE_URL` | boş (paketteki postgres) | Kurumun mevcut PostgreSQL sunucusu kullanılacaksa tam bağlantı (bölüm 6) |
| `WATCHOVER_TAG` | `latest` | Çalıştırılacak sürüm; `1.5` gibi bir etiket sürümü sabitler |
| `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` | `8501` / `8600` | Ana makinede açılan portlar |
| `WATCHOVER_PG_PORT` | `127.0.0.1:5433` | PostgreSQL'in ana makineden erişimi (psql / yedek); yalnız localhost |
| `WATCHOVER_CPUS` / `WATCHOVER_MEMORY` | `4` / `4g` | Dashboard konteynerinin kaynak sınırı |
| `WATCHOVER_PG_CPUS` / `WATCHOVER_PG_MEMORY` | `2` / `2g` | PostgreSQL konteynerinin kaynak sınırı |
| `WATCHOVER_SELFMON` | `1` | Konteynerin kendini izlemesi; ana makinede ayrı ajan varsa `0` |
| `WATCHOVER_PARALLEL` | boş | Çoklu dosya ayrıştırmada paralellik: boş otomatik (8+ çekirdek), `1` zorla, `0` kapalı |
| `TZ` | `Europe/Istanbul` | Konteyner saat dilimi |

Değişiklikten sonra `docker compose up -d` yeni ayarları uygular. `POSTGRES_PASSWORD` sonradan değiştirilecekse önce veritabanında değiştirilir: `docker compose exec postgres psql -U watchover -c "ALTER USER watchover PASSWORD 'yeni'"`, sonra `.env` ve `docker compose up -d`.

## 4. Kaynak yönetimi

- Sınırlar `.env` içindeki `WATCHOVER_CPUS` / `WATCHOVER_MEMORY` (dashboard) ve `WATCHOVER_PG_CPUS` / `WATCHOVER_PG_MEMORY` (veritabanı) ile verilir; `docker stats` anlık kullanımı gösterir.
- Büyük SAP arşivleri için önerilen: dashboard 4 CPU / 4 GB, 1 GB'lık arşivlerde 8 GB. PostgreSQL için 2 CPU / 2 GB yüz binlerce rollup satırına yeter.
- Loglar json-file sürücüsüyle döner (dashboard 20 MB × 5, postgres 10 MB × 3); `docker compose logs --tail 200 dashboard`.
- Sağlık denetimi: dashboard 30 saniyede bir `/_stcore/health`, postgres 10 saniyede bir `pg_isready`. `docker compose ps` her ikisini `healthy` göstermelidir. Uygulama çökerse `restart: unless-stopped` konteyneri geri açar; Sistem › Güncelleme'deki **Uygulamayı yeniden başlat** aynı mekanizmayı kullanır. Dashboard, postgres sağlıklı olmadan başlatılmaz (`depends_on: service_healthy`).

## 5. Güncelleme ve sürüm geçmişi

```bash
cd ~/watchover
docker compose pull && docker compose up -d      # ya da: make docker-update
docker image prune -f                             # eski imaj katmanlarını temizler
```

- Belirli bir sürüme geçmek ya da geri dönmek: `.env` içinde `WATCHOVER_TAG=1.4` yazıp aynı komutu çalıştırın.
- Sistem › Güncelleme sekmesi Docker'da zip / git düğmelerini göstermez, bu komutları gösterir; sürüm notları ve anlık görüntüler (Sistem › Güncelleme › Sürüm geçmişi) yine oradadır ve `/data/versions` altında durur. PostgreSQL'de anlık görüntü bütün tabloların JSON dökümüdür (`db.json.gz`); geri dönüş tabloları o dökümle doldurur.
- Sistem başlığındaki çip `Watchover v1.5 · 🐳 1.5` biçiminde imaj etiketini gösterir; Sistem › Durum'daki veritabanı kartı `postgresql` ve bağlantıyı (parolasız) gösterir.

## 6. PostgreSQL: ürün veritabanı

Kullanıcılar, roller, bilgi tabanı, kurallar, aksiyonlar, playbook, ajanlar, kaynaklar, envanter, bildirimler ve SLO rollup'ları **tek bir PostgreSQL veritabanında** tutulur. Compose dosyası `postgres:16-alpine` konteynerini dashboard'un yanında başlatır ve `DATABASE_URL`'i kendisi kurar; ayrıca bir şey yapmak gerekmez.

**Kurumun mevcut PostgreSQL sunucusu** kullanılacaksa (13+):

```sql
CREATE USER watchover WITH PASSWORD 'guclu-parola';
CREATE DATABASE watchover OWNER watchover;
```

```bash
# .env
DATABASE_URL=postgresql://watchover:guclu-parola@db.sirket.com:5432/watchover
docker compose up -d --no-deps dashboard          # paketteki postgres başlatılmaz
```

**Veritabanına bakmak:** `docker compose exec postgres psql -U watchover` (ya da ana makineden `psql -h 127.0.0.1 -p 5433 -U watchover`). Tablolar ilk açılışta uygulama tarafından oluşturulur; şema aracı yoktur.

**Eski kurulumdan (SQLite) geçiş:** dashboard PostgreSQL'e ilk bağlandığında `/data` altında `knowledge.db`, `actions.db`, `playbook.db` görürse bunları **bir kez** kopyalar (hesaplar, roller, bilgi tabanı, aksiyonlar, playbook, ajanlar, kaynaklar, bildirimler, rollup'lar) ve `/data/.migrated-to-postgres` dosyasına raporu yazar. Elle çalıştırmak ya da tekrar etmek için:

```bash
docker compose exec dashboard python -m watchover.migrate --source /data --dry-run    # satır sayıları
docker compose exec dashboard python -m watchover.migrate --source /data              # kopyala (var olan anahtarlar atlanır)
```

Docker dışındaki bir kurulumdan (Windows / macOS) geçerken üç `.db` dosyasını `docker cp DOSYA watchover:/data/` ile birime kopyalayıp konteyneri yeniden başlatmak yeter (`docker compose restart dashboard`). Herkes taşınmadan sonra yeniden giriş yapar (hatırlanan oturumlar taşınmaz).

## 7. Kendi MCP sunucumuz (isteğe bağlı)

Watchover'ın motoru aynı imajdan ayrı bir konteynerde MCP sunucusu olarak açılır; Claude Code, Claude Desktop, Cursor ya da kendi ajanlarınız `analyze_dataset`, `list_incidents`, `get_incident`, `list_signals`, `evidence`, `postmortem`, `create_action`, `list_actions` araçlarını JSON-RPC ile çağırır. Aksiyonlar dashboard ile aynı PostgreSQL'e yazılır.

```bash
# .env
MCP_API_KEY=uzun-rastgele-anahtar        # openssl rand -hex 24
mkdir -p datasets                        # istemcilerin analiz edeceği dosyalar (ZIP / log / CSV) buraya kopyalanır
docker compose --profile mcp up -d       # http://<sunucu>:8765/mcp ; sağlık: /health
```

İstemci ayarı (Claude Code / Claude Desktop için `mcpServers` bloğu):

```json
{"mcpServers": {"watchover": {"url": "http://<sunucu>:8765/mcp", "headers": {"Authorization": "Bearer uzun-rastgele-anahtar"}}}}
```

Sonra istemciden `analyze_dataset("uretim-log.zip")` demek yeter; göreli ad `./datasets` klasöründe aranır. `MCP_API_KEY` boşsa uç nokta açıktır: o durumda portu yalnız localhost'a bağlayın (`WATCHOVER_MCP_PORT=127.0.0.1:8765`). Dashboard'un Bağlantı ayarları › MCP sekmesi de aynı adrese bağlanabilir.

## 8. Ajanlar ve kaynaklar

Sunuculardaki ajanlar aynı şekilde kurulur; alıcı adresi Docker ana makinesidir:

```bash
curl -fsSL http://<sunucu>:8600/agent/install.sh | sudo bash -s -- --url http://<sunucu>:8600/ingest --enroll-key wk_… --env prod
```

Konteynerin kendi ajanı konteynerin CPU / bellek / diskini raporlar. Ana makinenin kendisini izlemek için ana makineye normal bir ajan kurun ve `WATCHOVER_SELFMON=0` yapın.

## 9. Yedekleme ve taşıma

```bash
make docker-backup                                                     # PostgreSQL: watchover-YYYYMMDD.sql.gz (pg_dump)
docker compose exec -T postgres pg_dump -U watchover watchover | gzip > watchover-$(date +%Y%m%d).sql.gz    # aynı şey elle
docker run --rm -v watchover_watchover-data:/data -v "$PWD":/backup alpine tar czf /backup/watchover-data.tgz -C /data .   # ayarlar, sırlar, anlık görüntüler
```

Geri yükleme (yeni sunucuda `docker compose up -d postgres` sonrası):

```bash
gunzip -c watchover-YYYYMMDD.sql.gz | docker compose exec -T postgres psql -U watchover watchover
docker run --rm -v watchover_watchover-data:/data -v "$PWD":/backup alpine tar xzf /backup/watchover-data.tgz -C /data
docker compose up -d
```

`data/.vault.key` sırların anahtarıdır; yedek onsuz açılmaz. Gecelik yedek için `crontab -e`: `0 2 * * * cd ~/watchover && make docker-backup`.

## 10. Sorun giderme

| Belirti | Çözüm |
|---|---|
| `pull access denied` | `docker login ghcr.io`; depo özelse paket görünürlüğünü kontrol edin |
| Tarayıcı bağlanamıyor | `docker compose ps` (healthy?), `ss -ltnp \| grep 8501`, güvenlik duvarında 8501; uzak sunucuda `localhost` değil sunucu IP'si |
| Port çakışması | `.env` içinde `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` / `WATCHOVER_PG_PORT` değiştirin |
| `unhealthy` | `docker compose logs --tail 100 dashboard`; bellek sınırı düşükse `WATCHOVER_MEMORY` artırın |
| `password authentication failed` | `.env` parolası birim oluşturulduktan sonra değişti: bölüm 3'teki `ALTER USER` ya da (veri yoksa) `docker compose down -v` |
| dashboard postgres'i bekliyor | `docker compose logs postgres`; disk dolu ya da `watchover-pg` birimi bozuksa yedekten dönün |
| Giriş yapılamıyor | `docker compose exec dashboard python -m watchover.useradmin list` (ve `password EMAIL`, `unlock EMAIL`) |
| İnternet çıkışı yok | İnternetli bir makinede `docker pull ghcr.io/bannercheck/watchover:latest postgres:16-alpine`, `docker save … \| gzip > imajlar.tgz`, sunucuda `docker load` |
| Yerel imaj ile denemek | `make docker-build` sonra `.env` içinde `WATCHOVER_IMAGE=watchover WATCHOVER_TAG=local` |
