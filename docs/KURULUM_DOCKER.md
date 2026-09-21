# Watchover · Kurumsal Docker kurulum rehberi

Watchover şirket içindeki **tek bir sunucuda, tamamen Docker'da** çalışır. Kurulum paketi bu klasördür (`watchover.zip`): imaj sunucuda kaynak koddan derlenir; GitHub, imaj kaydı ya da sunucuda Python gerekmez. Üç konteyner vardır: **dashboard** (arayüz 8501, canlı alıcı 8600, konteynerin kendini izleyen ajanı), **postgres** (ürün veritabanı, PostgreSQL 16) ve isteğe bağlı **mcp** (kendi MCP sunucumuz, 8765). Kod imajın içindedir; ayarlar, sırlar, anlık görüntüler ve loglar `watchover-data` biriminde, veritabanı `watchover-pg` biriminde durur. Güncelleme, yeni paketi klasörün üstüne açıp `./watchover.sh update` demektir; birimlere dokunulmaz.

## 1. Sunucu gereksinimleri

Sunucuya kurulacak **tek bağımlılık Docker'dır** (Engine 24+ ve Compose v2). Python, pip, PostgreSQL ana makineye kurulmaz; hepsi imajların içindedir.

| | |
|---|---|
| İşletim sistemi | Ubuntu 22.04 / 24.04, Debian 12, RHEL / Rocky 9 (Linux sunucu). Deneme için macOS / Windows'ta Docker Desktop da çalışır |
| Kaynak | En az 2 CPU / 4 GB RAM / 20 GB disk. Günlük GB'larca log ve büyük SAP arşivleri için 4 CPU / 8 GB |
| Ağ | Kullanıcılar için 8501, izlenen sunuculardan gelen ajanlar için 8600 (MCP açılırsa 8765). İmaj derlenirken bir kez PyPI ve Docker Hub'a çıkış gerekir; kapalı ağ için bölüm 9 |

Docker kurulumu (Linux):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # sonra oturumu kapatıp açın
docker compose version             # v2.x görünmeli
```

## 2. Kurulum (on dakika)

Paketi sunucuya kopyalayın (scp / WinSCP), açın ve tek komutu çalıştırın:

```bash
scp watchover.zip kullanici@sunucu:~/
ssh kullanici@sunucu
unzip -q watchover.zip && mv hackathon watchover && cd watchover
./watchover.sh install
```

`install` şunları yapar: `.env` dosyasını `.env.docker.example`'dan üretir ve **rastgele veritabanı parolası ile MCP anahtarını** yazar (`.env` 600 izinli, kimseyle paylaşılmaz); imajı derler (ilk seferde 3-5 dakika); PostgreSQL'i başlatır, sağlıklı olunca dashboard'u açar; sonunda adresleri basar:

```
  Watchover v1.6 is running.
    Dashboard      http://10.0.0.12:8501     (same machine: http://localhost:8501)
    Agents post to http://10.0.0.12:8600/ingest
    First sign-in: admin@watchover.local ...
```

**Arayüze erişim:** tarayıcıda `http://<sunucu-ip>:8501`. Güvenlik duvarı varsa `sudo ufw allow 8501,8600/tcp`. Şirket ağında TLS için bölüm 8.

**İlk giriş:** yerleşik yönetici `admin@watchover.local`, başlangıç parolası ayrıca iletilir; ilk girişte yeni parola istenir. Ekip hesapları Users sayfasından ya da terminalden:

```bash
./watchover.sh user add ops@sirket.com --admin
./watchover.sh user list
```

## 3. Günlük işletim: watchover.sh

| Komut | Ne yapar |
|---|---|
| `./watchover.sh status` | Konteynerler, sağlık durumu, anlık CPU / bellek |
| `./watchover.sh logs [dashboard\|postgres\|mcp]` | Canlı log |
| `./watchover.sh stop` / `start` / `restart` | Durdur / başlat / yeniden başlat (veri kalır) |
| `./watchover.sh update` | Yeni paket klasörün üstüne açıldıktan sonra: imajı yeniden derler, konteynerleri yeniler, eski imajı temizler |
| `./watchover.sh backup` | PostgreSQL dökümü + veri birimi + `.env` → `backups/watchover-YYYYMMDD-HHMM.tgz` |
| `./watchover.sh restore DOSYA` | Yedeği geri yükler (veritabanı ve veri birimi; konteynerler bu sırada durur) |
| `./watchover.sh user …` | `list`, `add EMAIL [--admin]`, `promote`, `password EMAIL`, `unlock`, `enable`, `disable`, `delete` |
| `./watchover.sh mcp on\|off` | Kendi MCP sunucumuzu açar / kapatır (bölüm 7) |
| `./watchover.sh api on\|off` | HTTP API konteynerini açar / kapatır (bölüm 7b) |
| `./watchover.sh migrate` | Eski SQLite kurulumunun `.db` dosyalarını PostgreSQL'e kopyalar (bölüm 6) |
| `./watchover.sh save` / `load DOSYA` | İmajları `.tgz` olarak dışa / içe aktarır (kapalı ağ, bölüm 9) |
| `./watchover.sh shell` | Dashboard konteynerinde kabuk |

Bütün bunlar düz `docker compose` komutlarıdır; alışkın olanlar `docker compose ps`, `docker compose logs -f dashboard` ile aynı işleri yapabilir.

## 4. Yapılandırma (.env)

`install` gerekli her şeyi yazar; değiştirmek isteyenler için:

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `POSTGRES_PASSWORD` | rastgele | Veritabanı parolası; birim ilk oluşturulurken işlenir (sonradan değiştirmek: bölüm 6) |
| `MCP_API_KEY` | rastgele | MCP istemcilerinin göndereceği anahtar |
| `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` / `WATCHOVER_MCP_PORT` | `8501` / `8600` / `8765` | Sunucuda açılan portlar; `127.0.0.1:8765` gibi yazılırsa yalnız localhost |
| `WATCHOVER_CPUS` / `WATCHOVER_MEMORY` | `4` / `4g` | Dashboard konteynerinin sınırı |
| `WATCHOVER_PG_CPUS` / `WATCHOVER_PG_MEMORY` | `2` / `2g` | PostgreSQL konteynerinin sınırı |
| `WATCHOVER_SELFMON` | `1` | Konteynerin kendini izlemesi; ana makinede ayrı ajan varsa `0` |
| `WATCHOVER_PARALLEL` | boş | Çoklu dosya ayrıştırmada paralellik: boş otomatik (8+ çekirdek), `1` zorla, `0` kapalı |
| `TZ` | `Europe/Istanbul` | Konteyner saat dilimi |
| `DATABASE_URL` | boş | Kurumun mevcut PostgreSQL sunucusu kullanılacaksa (bölüm 6) |

Değişiklikten sonra `./watchover.sh start` yeni ayarları uygular. Kaynak kullanımı: `docker stats`. Loglar json-file sürücüsüyle döner (dashboard 20 MB × 5, postgres 10 MB × 3). Sağlık denetimleri: dashboard 30 sn'de bir, postgres 10 sn'de bir; çöken konteyneri `restart: unless-stopped` geri açar, Sistem › Güncelleme'deki **Uygulamayı yeniden başlat** aynı mekanizmayı kullanır.

## 5. Sunucuları izlemek ve log çekmek

Kurulum bittikten sonra izleme üç yoldan başlar; hepsi dashboard'dan yönetilir:

1. **Ajan (push):** izlenecek her sunucuda tek satır; ajan CPU / bellek / disk / servis durumunu ve seçilen log dosyalarını 8600'e gönderir. Komut Bağlantı ayarları › Ajanlar sekmesinde hazır çıkar (kayıt anahtarıyla):
   ```bash
   curl -fsSL http://<watchover-sunucusu>:8600/agent/install.sh | sudo bash -s -- --url http://<watchover-sunucusu>:8600/ingest --enroll-key wk_… --env prod
   ```
   Windows sunucular için Görev Zamanlayıcı ile aynı ajan (`docs/KURULUM_WINDOWS.md`).
2. **Kaynaklar (pull):** Elasticsearch / OpenSearch, Grafana Loki, Splunk, Graylog ve HTTP/JSON uçlarından belirli aralıklarla çekim; Bağlantı ayarları › Kaynaklar sekmesinden adres, kimlik ve sorgu girilir, sırlar veritabanında şifreli tutulur.
3. **Dosya yükleme:** Datasets sayfasından ZIP / log / CSV / SAP arşivleri (çoklu dosya, arka planda, ilerleme yüzdesiyle).

Ajanlar ve kaynaklar veri göndermeye başladıktan yaklaşık yarım saat sonra Operasyon sayfasının altındaki **Anomali takibi** çalışır: her sunucu kendi normaline göre izlenir (hata sıçraması, log fırtınası, sessizlik, yeni örüntü, metrik) ve her anomali operasyonel adım listesiyle takip edilir; Sistem › Bildirimler'deki *Yeni anomali açıldı* kuralı e-posta / SMS gönderir.

Konteynerin kendi ajanı Watchover sunucusunun konteynerini raporlar; ana makinenin kendisini izlemek için ona da normal bir ajan kurun ve `WATCHOVER_SELFMON=0` yapın. Uyarılar (e-posta / SMS) Sistem › Bildirimler'den, roller Users sayfasından ayarlanır.

## 6. PostgreSQL: ürün veritabanı

Kullanıcılar, roller, bilgi tabanı, kurallar, aksiyonlar, playbook, ajanlar, kaynaklar, envanter, bildirimler ve SLO rollup'ları **tek PostgreSQL veritabanında** tutulur; `postgres` konteyneri dashboard'un yanında çalışır, ayrıca bir şey gerekmez.

- Bakmak: `docker compose exec postgres psql -U watchover` (ana makineden: `psql -h 127.0.0.1 -p 5433 -U watchover`).
- Parola değiştirmek: `docker compose exec postgres psql -U watchover -c "ALTER USER watchover PASSWORD 'yeni'"`, sonra `.env` ve `./watchover.sh start`.
- **Kurumun mevcut PostgreSQL sunucusu** (13+): `CREATE USER watchover WITH PASSWORD '…'; CREATE DATABASE watchover OWNER watchover;` sonra `.env` içine `DATABASE_URL=postgresql://watchover:…@db.sirket.com:5432/watchover` ve `docker compose up -d --no-deps dashboard`.
- **Eski kurulumdan (SQLite) geçiş:** eski `knowledge.db`, `actions.db`, `playbook.db` dosyalarını bu klasörde `data/` altına koyup `./watchover.sh migrate` (önce `--dry-run` ile satır sayıları). Dashboard PostgreSQL'e ilk bağlandığında `/data` altında bu dosyaları görürse zaten bir kez kendisi kopyalar (`/data/.migrated-to-postgres`). Herkes taşınmadan sonra yeniden giriş yapar.
- Sürüm anlık görüntüleri (Sistem › Güncelleme › Sürüm geçmişi) PostgreSQL'de bütün tabloların JSON dökümüdür; geri dönüş tabloları o dökümle doldurur.

## 7. Kendi MCP sunucumuz

Watchover'ın motoru aynı imajdan üçüncü bir konteynerde MCP sunucusu olarak açılır; Claude Code, Claude Desktop, Cursor ya da kendi ajanlarınız `analyze_dataset`, `list_incidents`, `get_incident`, `list_signals`, `evidence`, `postmortem`, `create_action`, `list_actions` araçlarını çağırır. Aksiyonlar dashboard ile aynı veritabanına yazılır.

```bash
./watchover.sh mcp on              # http://<sunucu>:8765/mcp ; sağlık: /health ; anahtar .env içindeki MCP_API_KEY
cp uretim-log.zip datasets/        # istemcilerin analiz edeceği dosyalar
```

İstemci ayarı (`mcpServers` bloğu):

```json
{"mcpServers": {"watchover": {"url": "http://<sunucu>:8765/mcp", "headers": {"Authorization": "Bearer <MCP_API_KEY>"}}}}
```

Sonra istemciden `analyze_dataset("uretim-log.zip")` demek yeter. Anahtarsız istekler 401 alır.

## 7b. Yeni arayüz (React) ve HTTP API

Streamlit arayüzünün yerini alacak yeni arayüz aynı imajda, `api` konteynerinde yayınlanır: `http://<sunucu>:8000`. Giriş, Operasyon, Anomaliler, Veri setleri (yükleme, incident, sinyal, gürültü, postmortem), Aksiyonlar, Playbook, Bilgi tabanı, Bağlantılar (ajanlar, kaynaklar, envanter), Bildirimler, Kullanıcılar ve roller, Sistem sayfaları hazırdır; telefon ve tablette de çalışır. Motorun bütün işlevleri JSON uçları olarak da açılır; entegrasyonlar bunu kullanır. Dashboard'la aynı PostgreSQL'i ve aynı hesapları kullanır.

```bash
./watchover.sh api on                     # http://<sunucu>:8000  (yeni arayüz)  ·  /api/docs (OpenAPI)
curl -s -X POST http://<sunucu>:8000/api/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"admin@watchover.local","password":"…"}'          # -> {"token": "…"}
curl -s http://<sunucu>:8000/api/anomalies -H "Authorization: Bearer <token>"
```

Giriş e-posta + parola iledir; yönetici olmayan hesaplara SMTP ayarlıysa e-posta ile tek kullanımlık kod sorulur (`/api/auth/mfa`). SSO: Sistem › Giriş'te Google / Microsoft / OIDC istemci bilgileri girilmişse giriş sayfasında sağlayıcı düğmesi çıkar (`/api/auth/oidc/<sağlayıcı>/start`, PKCE'li yetkilendirme kodu akışı; sağlayıcıda geri dönüş adresi `http(s)://<sunucu>/api/auth/oidc/<sağlayıcı>/callback`). Yetkiler dashboard'daki rollerle aynıdır. Uç aileleri: `datasets` (yükleme arka planda, `jobs` ile ilerleme; incident, sinyal, kanıt, postmortem, dışa aktarma), `live`, `actions`, `anomalies`, `playbook`, `agents`, `sources`, `inventory`, `knowledge`, `notify`, `users` / `roles`, `system`. Canlı akış bu fazda dashboard konteynerinde toplanır; API kendi alıcısını `WATCHOVER_API_RECEIVER=1` ile açabilir (o zaman 8600 portu API'ye taşınır). Tarayıcıdan çağrılacaksa `WATCHOVER_CORS` ile izinli adresler yazılır.

## 8. TLS ve şirket ağı

8501'i doğrudan açmak yerine bir ters vekil arkasına koyup TLS ekleyin. Caddy ile tek satır (`/etc/caddy/Caddyfile`):

```
watchover.sirket.local {
    tls internal                    # şirket CA'sı varsa: tls /etc/ssl/watchover.crt /etc/ssl/watchover.key
    reverse_proxy 127.0.0.1:8501
}
```

Bu durumda `.env` içinde `WATCHOVER_UI_PORT=127.0.0.1:8501` yazıp portu dışarıya kapatın. Ajan trafiği (8600) HTTP'dir ve token'lıdır; şirket ağı dışına çıkacaksa aynı vekille `reverse_proxy` ekleyin. SSO (Google / Microsoft / Apple / OIDC) Sistem › Giriş sekmesinden; geri dönüş adresi vekil adresiyle aynı olmalıdır.

## 9. Kapalı ağ (internet çıkışı olmayan sunucu)

İnternetli bir makinede imajı derleyip taşıyın:

```bash
./watchover.sh install && ./watchover.sh save          # watchover-images-1.6.tgz
scp watchover.zip watchover-images-1.6.tgz kullanici@kapali-sunucu:~/
# kapalı sunucuda:
unzip -q watchover.zip && mv hackathon watchover && cd watchover
./watchover.sh load ~/watchover-images-1.6.tgz && ./watchover.sh install
```

## 10. Yedekleme ve taşıma

```bash
./watchover.sh backup                                   # backups/watchover-YYYYMMDD-HHMM.tgz (pg_dump + veri birimi + .env)
./watchover.sh restore backups/watchover-20260921-0200.tgz
```

Gecelik yedek: `crontab -e` → `0 2 * * * cd /home/kullanici/watchover && ./watchover.sh backup`. Yedek dosyasını başka bir diske / sunucuya kopyalayın; `data/.vault.key` sırların anahtarıdır, yedek onsuz açılmaz. Başka bir sunucuya taşımak: yeni sunucuda `install`, sonra `restore`.

## 11. Sorun giderme

| Belirti | Çözüm |
|---|---|
| `Docker is not installed / not reachable` | Bölüm 1; `sudo systemctl start docker`; kullanıcı `docker` grubunda mı (`groups`) |
| Derleme `pip` adımında takılıyor | Sunucunun PyPI'ye çıkışı yok: bölüm 9 (imajı başka makinede derleyip taşıyın) ya da şirket proxy'sini Docker'a tanıtın |
| Tarayıcı bağlanamıyor | `./watchover.sh status` (healthy?), `ss -ltnp \| grep 8501`, güvenlik duvarı; uzak sunucuda `localhost` değil sunucu IP'si |
| Port çakışması | `.env` içinde `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` / `WATCHOVER_PG_PORT` |
| `unhealthy` | `./watchover.sh logs dashboard`; bellek sınırı düşükse `WATCHOVER_MEMORY` |
| `password authentication failed` | `.env` parolası birim oluşturulduktan sonra değişti: bölüm 6'daki `ALTER USER` |
| Giriş yapılamıyor | `./watchover.sh user list`, `user password EMAIL`, `user unlock EMAIL` |
| Ajan gönderemiyor | Sunucudan `curl -s http://<watchover>:8600/health`; 8600 açık mı; token / kayıt anahtarı Bağlantı ayarları › Ajanlar'da geçerli mi |
