# Watchover · Kurumsal Docker kurulum rehberi

Watchover şirket içindeki **tek bir sunucuda, tamamen Docker'da** çalışır. Kurulum paketi bu klasördür (`watchover.zip`): imaj sunucuda kaynak koddan derlenir (Node aşaması React arayüzünü, Python aşaması motoru kurar); GitHub, imaj kaydı ya da sunucuda Python gerekmez. Üç konteyner vardır: **dashboard** (React arayüzü + HTTP API 8501, canlı alıcı 8600, konteynerin kendini izleyen ajanı), **postgres** (ürün veritabanı, PostgreSQL 16) ve isteğe bağlı **mcp** (kendi MCP sunucumuz, 8765). Eski Streamlit arayüzü `legacy` profiliyle 8502'de bir süre daha açılabilir. Kod imajın içindedir; ayarlar, sırlar, anlık görüntüler ve loglar `watchover-data` biriminde, veritabanı `watchover-pg` biriminde durur. Güncelleme, yeni paketi klasörün üstüne açıp `./watchover.sh update` demektir; birimlere dokunulmaz.

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
| `./watchover.sh legacy on\|off` | Eski Streamlit arayüzünü ek konteyner olarak açar / kapatır (8502) |
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

## 7b. Arayüz ve HTTP API

Ürün arayüzü React'tir ve `dashboard` konteynerinde HTTP API ile birlikte `http://<sunucu>:8501` altında yayınlanır: Operasyon (canlı, log dosyaları, SLO raporu), Anomaliler, Veri setleri (yükleme, incident, sinyal, gürültü, arama, karşılaştırma, postmortem), Hata haritası, Aksiyonlar, Playbook, Watchover'a sor, Bilgi tabanı, ITSM, Bağlantılar (ajanlar, kaynaklar, envanter, keşfedilen sunucular), LLM, Bildirimler (kurallar, alıcılar, SMTP / SMS kanalları), Kullanıcılar ve roller, Sistem (durum, ayarlar, anlık görüntü ve geri dönüş, güncelleme, giriş sağlayıcıları). Telefon ve tablette de çalışır. Motorun bütün işlevleri JSON uçları olarak da açılır; entegrasyonlar bunu kullanır.

```bash
./watchover.sh status                     # http://<sunucu>:8501  (arayüz)  ·  /api/docs (OpenAPI)
curl -s -X POST http://<sunucu>:8000/api/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"admin@watchover.local","password":"…"}'          # -> {"token": "…"}
curl -s http://<sunucu>:8000/api/anomalies -H "Authorization: Bearer <token>"
```

Giriş e-posta + parola iledir; yönetici olmayan hesaplara SMTP ayarlıysa e-posta ile tek kullanımlık kod sorulur (`/api/auth/mfa`). Giriş sayfasındaki **Kayıt ol** düğmesi varsayılan olarak açıktır (Sistem › Giriş sağlayıcıları › otomatik hesap açma). Kayıt olan hesap en kısıtlı rolle (görüntüleyici) açılır ve hemen giriş yapar; yetkiyi yönetici Kullanıcılar sayfasından artırır (operatör / yönetici). SMTP tanımlıysa (Bildirimler › Kanallar) önce e-postaya doğrulama kodu gider; SMTP yoksa doğrulama atlanır. E-posta ile ikinci faktör (MFA) de SMTP ister ve yönetici olmayan hesaplara varsayılan olarak açıktır (Sistem › Giriş sağlayıcıları › MFA). Gmail için `smtp.gmail.com:587`, STARTTLS, kullanıcı = adres, parola = Google "uygulama şifresi"; Microsoft 365 için `smtp.office365.com:587`, STARTTLS. **Google / Microsoft / Apple ile giriş ve kayıt:** giriş sayfasında üç düğme her zaman durur; yapılandırılana kadar soluk görünür. Yeni arayüzde Sistem › *Giriş sağlayıcıları (SSO)* kartına ilgili sağlayıcının istemci bilgileri girilip *etkin* işaretlenince düğme çalışır: tıklayan kişi sağlayıcıya yönlendirilir, geri dönüşte hesabı yoksa otomatik açılır (*operatör* rolüyle; kart üzerinden kapatılabilir ya da e-posta alanı ile sınırlanabilir). Sağlayıcı tarafında tanımlanacak geri dönüş adresi kartta yazar: `http(s)://<sunucu>:8000/api/auth/oidc/<google|microsoft|apple|oidc>/callback`; dış adres farklıysa (ters vekil, TLS) *Dış adres* alanına yazın.

| Sağlayıcı | Nereden alınır | Girilecek |
|---|---|---|
| Google | console.cloud.google.com › APIs & Services › Credentials › OAuth client ID (Web application); Authorized redirect URI = geri dönüş adresi | Client ID, Client secret |
| Microsoft | portal.azure.com › Microsoft Entra ID › App registrations › New; Redirect URI (Web) = geri dönüş adresi; Certificates & secrets › New client secret | Tenant (`common` ya da kiracı kimliği), Application (client) ID, secret |
| OIDC (Keycloak, Okta, Authentik…) | Sağlayıcıda yeni istemci, redirect URI = geri dönüş adresi | Issuer URL, Client ID, secret |

Akış standart OpenID Connect yetkilendirme kodu (PKCE) ile API üzerinden yürür, Streamlit'teki ayarlarla aynı `config.json` anahtarlarını kullanır; sırlar şifreli tutulur. Yetkiler dashboard'daki rollerle aynıdır. Uç aileleri: `datasets` (yükleme arka planda, `jobs` ile ilerleme; incident, sinyal, kanıt, postmortem, dışa aktarma), `live`, `actions`, `anomalies`, `playbook`, `agents`, `sources`, `inventory`, `knowledge`, `notify`, `users` / `roles`, `system`. Canlı alıcı (8600) aynı konteynerdedir; ajanlar ve kaynaklar doğrudan arayüzde görünür. Başka bir alan adından çağrılacaksa `WATCHOVER_CORS` ile izinli adresler yazılır. Eski Streamlit arayüzü `./watchover.sh legacy on` ile 8502'de açılır, aynı veritabanını kullanır ve bir sonraki büyük sürümde kaldırılacaktır.

**Tema ve ayrıntı panelleri (v1.11).** Sağ üstte TR / EN'in yanındaki üç düğme temayı seçer: 🌙 koyu, 🖥 sistem (işletim sisteminin tercihini izler), ☀️ açık; seçim tarayıcıda saklanır. Operasyon sayfasında her KPI kartı ve metrik kutusu (olay hızı, ERROR+, erişilebilirlik / SLA / hata bütçesi, p95 gecikme, CPU / bellek / disk / GPU, log dosyaları) tıklanınca kartların altında kapanabilir bir ayrıntı paneli açar; ortam ve sunucu süzgeci tüm kartlara uygulanır. Streamlit'teki incident kartı (flashcard, aksiyon oluştur, geri bildirim, benzer dersler, LLM istemi / açıklama, postmortem), gürültü ısı haritası, örnek veri setleri, uzak kaynaktan (HTTP / MCP) veri çekme, sütun eşleme, bilgi tabanına belge yükleme ve LLM kural önerisi, bakım kartı (canlı akışı temizle, anlık görüntü budama, zip ile dağıtım, git güncelleme) ve README sayfası React arayüzünde de vardır.

## 8. TLS ve şirket ağı: uygulamayı dışarı açmak

Uygulama şirket ağına **tek bir https ucu** üzerinden açılır: `edge` profili Caddy'yi dashboard'un önüne koyar, TLS'i kendi sertifika otoritesiyle (ya da şirket sertifikasıyla) yapar, 80'i 443'e yönlendirir, güvenlik başlıklarını ekler ve `/ingest` yolunu alıcıya (8600) aktarır. `edge on` açıkken düz http portu (8501) hiç yayınlanmaz (Caddy konteynere Docker ağından ulaşır; `docker-compose.edge.yml` `.env`'deki `COMPOSE_FILE` ile devreye girer), 8600 ajanlar için açık kalır; dışarıdan sadece https görünür. Bu yüzden Mac'te launcher'daki Streamlit 8501'i kullanmaya devam edebilir.

### 8.1 macOS'ta (Docker Desktop) şirket ağına açmak

```bash
cd ~/watchover-docker
./watchover.sh edge on watchover.local    # sanal ad: Mac adı Bonjour ile ağa duyurur; sertifika ada ve Mac'in IP'sine birlikte kesilir
./watchover.sh edge cert                  # Caddy'nin kök sertifikası -> ./certs/watchover-root.crt (meslektaşlar bir kez kurar)
./watchover.sh status
```

Meslektaşlar `https://watchover.local` yazar. `.local` adları Mac, iPhone / iPad ve Windows 10+ makinelerde ek kurulum olmadan çözülür (mDNS / Bonjour); Linux'ta `avahi-daemon` gerekir. Ad yerine ya da adla birlikte IP de verilebilir: `edge on watchover.local,192.168.1.107`; IP her durumda sertifikaya eklenir, `https://192.168.1.107` de çalışır. Duyuru Mac yeniden başlayınca durur; `./watchover.sh edge on` bir kez daha çalıştırılır (ayarlar `.env`'de durur).

Sonra:

1. **Sabit adres.** Şirket DNS'i varsa BT'den `watchover.sirket.com` gibi bir A kaydı isteyip `./watchover.sh edge on watchover.sirket.com` demek en kalıcısıdır (Google girişi de yalnız gerçek alan adıyla çalışır). Bonjour adı yoksa ve IP ile giriliyorsa yönlendiricide Mac'e sabit IP ayırın.
2. **macOS güvenlik duvarı.** Sistem Ayarları › Ağ › Güvenlik Duvarı açıksa ilk bağlantıda Docker için "İzin ver" deyin (443 ve 80).
3. **Uyku.** Mac uyuyunca servis durur: Sistem Ayarları › Pil › Güç adaptörü › "Ekran kapalıyken otomatik uykuyu engelle", ya da terminalde `caffeinate -s &`. Docker Desktop › Settings › General › "Start Docker Desktop when you sign in" açık olsun.
4. **Sertifika.** `edge cert` ile alınan `watchover-root.crt` dosyasını meslektaşlarınıza verin: macOS'ta Anahtar Zinciri Erişimi › Sistem'e sürükleyip "Her Zaman Güven"; Windows'ta `certutil -addstore -f Root watchover-root.crt`; Linux'ta `/usr/local/share/ca-certificates/` altına koyup `update-ca-certificates`. Kurmayan tarayıcı uyarısıyla yine girer. Şirket CA'sından alınmış bir sertifikanız varsa `./certs/watchover.crt` ve `./certs/watchover.key` olarak koyun; `edge on` onu otomatik kullanır, kök sertifika dağıtmak gerekmez.
5. **Ajanlar ve kaynaklar.** Diğer sunuculardaki ajanlar `https://<adres>/ingest` adresine gönderir: `python3 agent.py --url https://watchover.local/ingest --token … --ca watchover-root.crt` (Linux sunucularda `.local` çözülmüyorsa IP kullanın) (şirket sertifikasında `--ca` gerekmez). Mevcut `http://<adres>:8600/ingest` de çalışmaya devam eder (token'lı düz HTTP, sadece şirket ağı içinde).
6. **SSO.** Google / Microsoft / OIDC geri dönüş adresleri `https://<adres>/api/auth/oidc/<sağlayıcı>/callback` olur; Sistem › Giriş sağlayıcıları kartı adresi gösterir. Google özel IP'yi (192.168.x.x) geri dönüş adresi olarak kabul etmez, gerçek bir alan adı (`watchover.sirket.com`) ister; Microsoft daha esnektir. Apple girişi kaldırıldı (geliştirici üyeliği ve genel alan adı gerektiriyordu).

`curl -vk https://<IP>/api/health` "tlsv1 alert internal error" derse Caddyfile'daki `default_sni` ayarı eksiktir (v1.13.3+ ile gelir; `./watchover.sh update && ./watchover.sh edge on` yeterli): IP ile bağlanan istemciler sunucu adı göndermez, Caddy bu ayarla yine de sertifikayı verir.

Kapatmak: `./watchover.sh edge off` (8501 yeniden yayınlanır; Mac'te başka bir program 8501'i tutuyorsa `.env`'de `WATCHOVER_UI_PORT=8511` gibi bir port verin). `edge on` "address already in use" derse 8501'i tutan programı `lsof -nP -iTCP:8501 -sTCP:LISTEN` ile görebilirsiniz; v1.13.1'den itibaren edge açıkken bu port yayınlanmadığı için hata oluşmaz. Portlar: `.env` içinde `WATCHOVER_EDGE_HTTPS_PORT=8443` gibi.

### 8.2 Sunucuda

Aynı komutlar; ek olarak güvenlik duvarında yalnızca 443 (ve yönlendirme için 80) açılır: `ufw allow 80,443/tcp`. 8501, 8600 ve 5433 dışarıya kapalı kalır; ajanlar `https://<adres>/ingest` kullanır. Şirket DNS'inde A kaydı ve şirket CA'sından sertifika (`./certs/`) en temiz kurulumdur. Kendi Caddy / nginx / Traefik'iniz zaten varsa `edge` yerine `docker/Caddyfile` içindeki iki `reverse_proxy` satırını oraya taşıyın: arayüz + API `watchover:8501`, `/ingest` `watchover:8600`.

**Güvenlik notu.** Giriş 5 hatalı denemede geçici kilitlenir (`./watchover.sh user unlock EMAIL` açar), yönetici olmayan hesaplara e-posta ile ikinci faktör sorulur (Sistem › Ayarlar), parolalar scrypt ile, sırlar şifreli tutulur. Yönetici parolasını ilk girişte değiştirin ve `.env`'yi paylaşmayın.

## 8b. Parser kapsaması: hangi loglar anlaşılır

Sistem › **Parser kapsaması** kartı, 43 log ailesinden birer örnek dosyayı gerçek parser'dan geçirip zaman / seviye / sunucu / servis çıkarım oranını ve gürültü hunisini (ham olay → sinyal → incident, azaltma katsayısı) gösterir. Desteklenen aileler: syslog (RFC 3164 / 5424), CEF / LEEF (ArcSight, QRadar, Palo Alto, Fortinet, Cisco ASA), Apache / nginx / ALB erişim ve hata logları, IIS W3C, Windows Event XML, journald, Docker json-file, Kubernetes CRI, OpenTelemetry, ECS / GELF / logfmt / JSON / CSV, veritabanları (PostgreSQL, MySQL, SQL Server, Oracle alert, MongoDB, Redis), Kafka, HAProxy, Postfix, uygulama logları (Java, Python, .NET Serilog, Go), AWS CloudTrail, Alertmanager ve ERP: SAP (dev trace, HANA, NetWeaver, tp, SM21), Oracle E-Business Suite (concurrent manager, FND), Microsoft Dynamics AX / 365 / NAV-Business Central. Zaman damgasız satırlar (stack trace, ORA- blokları) üstteki olaya katlanır; kayıt servis taşımıyorsa log ailesi servis adı olur. Komut satırından: `python -m watchover.coverage` (Docker'da `./watchover.sh shell` içinden).

**Anlık bildirim:** üst çubuktaki 🔔 zil açık anomalileri 15 saniyede bir çeker; kritik CPU / bellek / disk / GPU (eşikler Sistem › Ayarlar, varsayılan 85 / 90 / 90 / 95 %) sunucu adıyla birlikte aynı listeye düşer; panelde "Tarayıcı bildirimi aç" deyip izin verince yeni anomaliler işletim sistemi bildirimi olarak da gelir (Watchover sekmesi arkada olsa bile). E-posta / SMS kuralları Sistem › Bildirimler sekmesinde.

**Kendi uygulamanızın logları için desen:** Yönetim › **Parser desenleri** sayfasında Test sekmesine birkaç satır yapıştırın, grok deseni yazın (`^%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:severity} \[%{DATA:service}\] %{GREEDYDATA:message}$` gibi), satır satır alanları görün ve "Desen olarak kaydet" deyin; sonraki yüklemelerde o dosya bu desenle ayrıştırılır. "Ayrıştırılamayan satırlar" sekmesi bir veri setinde zamanı ya da seviyesi çıkarılamayan satırları gösterir ve teste taşır. Hazır paketler (Squid, BIND, iptables, Nagios, ESXi, Tomcat, RabbitMQ, Docker, F5, Junos, Check Point, Zeek …) otomatik denenir. **Öğrenen kısım:** canlı akıştaki ve yüklenen setlerdeki satırlar Drain ile şablonlara kümelenir (Öğrenilen şablonlar sekmesi; kümeden tek tıkla desen önerisi), bir kez yapılan sütun eşlemesi aynı biçimdeki sonraki dosyalara kendiliğinden uygulanır (Öğrenilen eşlemeler sekmesi).

**Kurumsal hafıza (Hafıza sayfası):** canlı akış her `learn_min` dakikada, yüklenen veri setleri, yeni anomaliler ve kapatılan aksiyonlar kendiliğinden hafızaya yazılır; arama ve LLM incelemesi bu hafızaya dayanır. Gömme modeli olmadan da çalışır; LLM sayfasında bge-m3 tanımlandıysa Hafıza › Yeniden indeksle ile vektörler arka planda yenilenir. Parser desenleri artık LLM sayfasının bir sekmesidir.

**Zafiyet taraması (menü):** Envanterdeki sunucuların işletim sistemi/uygulama sürümleri güvenlik danışma kataloğuyla eşleştirilir; EOL OS, izlemesiz kritik sunucu gibi yapılandırma zafiyetleri işaretlenir. "Tümünü tara" bulguları önem sırasına dizer. İsteğe bağlı "Ağ kontrolü" (act.scan) açık portları pasif tespit eder; yalnız kendi sunucularınızda kullanın. **Güvenlik notu:** Bu sürümde SPA yol aşımı, SSRF/`file://`, bildirim SQL enjeksiyonu, ReDoS, sıkıştırma bombası ve OIDC açık yönlendirme kapatıldı; ilk admin parolası değişene kadar API kilitlidir. Docker imajında pip/setuptools/wheel yükseltildi. **Genel git geçmişinde kalan eski `.api.key`, cookie secret ve Google client secret'ı döndürün.**

**Yerel LLM (CPU, GPU'suz sunucu):** `./watchover.sh llm on` Ollama'yı compose ağında başlatır ve modelleri indirir; LLM sayfasında zaten tanımlı bir bağlantınız varsa ona dokunmaz (gömülü Ollama `http://ollama:11434` olarak ek seçenek kalır), bağlantı yoksa varsayılan olarak onu kullanır (sohbet/inceleme qwen2.5:7b, hızlı 3b, gömme bge-m3). Kaynak sınırları otomatik: Docker'ın gördüğü tüm CPU'lar ve belleğin %80'i tavan olur (`WATCHOVER_SIZING=manual` ile elle). Modeller `watchover-ollama` hacminde kalıcıdır, cache temizliği silmez. `./watchover.sh llm pull <model>`, `llm list`, `llm off` (off indirilenleri korur).

**Gözetimsiz öğrenme:** `./watchover.sh llm on` ile gelen `trainer` servisi hafıza dolunca (LLM › Kalite › Kendi modelin: eşikler, otomatik/kullan anahtarları) kendi kendine eğitir, kalite kapısından geçen model `watchover-ops` olur ve incident incelemesinde kullanılır; sohbet bağlantınıza dokunulmaz. İlk eğitimde taban ağırlıklar Hugging Face'ten bir kez indirilir (internet gerekir, `watchover-hf` hacminde kalır). Kaynaklar otomatik boyutlanır: `watchover.sh` Docker'ın gördüğü CPU/belleği ölçer ve LLM ile eğitim servisine tüm CPU'ları, belleğin çoğunu tavan olarak yazar (dizüstünde 10, sunucuda 32/64). Kendi değerleriniz için `.env` içinde `WATCHOVER_SIZING=manual`.

**Kendi modeliniz (watchover-ops):** `./watchover.sh llm export training.jsonl` hafızayı eğitim verisi olarak alır; Python 3.10+ ve `pip install -e ".[train]"` olan bir makinede `python scripts/train_lora.py --data training.jsonl --base qwen2.5:1.5b-instruct --out models/watchover-ops` CPU'da LoRA eğitir (1.5B: ~6 GB RAM, 32 çekirdekte birkaç yüz örnek/epoch on dakikalar); `./watchover.sh llm import models/watchover-ops` adaptörü Ollama'ya mevcut modellerin yanına ekler (seçmez); denemek için `./watchover.sh llm use watchover-ops`, geri dönmek için `llm use <eski model>`; `python scripts/train_lora.py --gate --model watchover-ops --base-model qwen2.5:1.5b-instruct --url http://<sunucu>:11434` kalite kapısıdır (FAIL ise `LLM_MODEL`'i tabana geri alın).

## 8c. LLM ile inceleme

LLM sayfasında Ollama (tamamen kapalı ağda çalışır) ya da bir API tanımlandığında: incident kartının üstündeki **LLM yorumu** kutusu modele kartın kanıtını okutur ve özet / kök neden değerlendirmesi / etki / aksiyon / açık sorular başlıklarıyla referanslı bir görüş yazdırır; Veri setleri › **LLM incelemesi** sekmesi tüm seti ya da aradığınız log satırlarını inceletir; **Watchover'a sor** sayfası serbest soru-cevap içindir. Model ne derse desin motorun deterministik kararı değişmez; cevaplardaki atıf oranı ("dayanaklı atıf") gösterilir.

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

## Ölçek: 100+ sunucu, 50+ uygulama (Faz 1)

**Canlı pencere veritabanında.** Ajanlardan gelen her paket `live_batches` tablosuna yazılır (paket başına bir satır). Bellekteki halka tampon anomali penceresini kapsamadığında (yüksek olay hızı ya da yeniden başlatma sonrası) pencere veritabanından okunur; Sistem › Kapasite paneli hangi kaynağın kullanıldığını, olay/sn hızını, disk ve veritabanı büyümesini gösterir ve ilk zorlanacak yeri uyarı olarak işaretler.

**Saklama süreleri** (Sistem › Ayarlar): canlı olay saklama (varsayılan 48 saat; 1.000 olay/sn ≈ 17 GB/gün), rollup saklama (400 gün), halka tampon boyutu (50.000 olay ≈ 100 MB bellek), spool dosyası (64 MB × 3 nesil).

**Boyutlandırma.** 100 sunucu ≈ 1.000 olay/sn; tek düğüm 32 CPU / 64 GB / 500 GB SSD yeterlidir. Yerel LLM'i ayrı bir kutuya almak için LLM › Bağlantı adresini o sunucunun Ollama'sına (`http://<sunucu>:11434`) çevirin; uygulama sunucusundaki yük büyük ölçüde düşer.

**Gece yedeği ve soğuk yedek.** Yedek dosyaları döner (varsayılan son 14 tane; `WATCHOVER_BACKUP_KEEP` ya da `backup --keep N`). Cron örneği:

```
0 2 * * * cd /opt/watchover-docker && ./watchover.sh backup >> backups/backup.log 2>&1
```

Soğuk yedek sunucu: aynı klasörü ve `.env`'i kopyalayın, `./watchover.sh install` ile kurun, son yedeği `./watchover.sh restore backups/watchover-….tgz` ile yükleyin; ajanlar yeni adrese `--url` ile yönlendirilir (kesinti sırasında yerel spool'da biriktirirler, veri kaybı olmaz).

**Yük testi.** `python scripts/loadtest.py --url http://<sunucu>:8600 --api http://<sunucu>:8501 --rate 2000 --agents 100 --duration 90 --user … --password …` kendi sunucunuzda 100 sanal ajanla ERP ağırlıklı yük basar ve alım/tarama ölçümlerini yazar; referans sonuçlar `docs/LOAD_TEST.md` (2.000 satır/sn: POST p50 10 ms; 4.000: alım sürer, tarama 12,6 s).

**Ne zaman Kubernetes.** Sıfır kesinti SLA'sı, çoklu lokasyon ya da GPU düğüm havuzu gerektiğinde. Ondan önce Faz 2 (durumsuz API kopyaları, ayrı alıcı servisi, tek worker) uygulanır.

## İnternetsiz (air-gapped) sunucu

Watchover'ın çalışması için internet gerekmez; yalnız **imajların oluşturulması** ve **LLM modellerinin indirilmesi** internet ister. Bu iki iş internete çıkabilen bir makinede (dizüstü, bastion, CI) yapılır ve tek dosya halinde kurumsal sunucuya taşınır.

**1. İnternetli makinede paketi hazırla** (kurulu bir Watchover klasöründe):

```
git clone https://github.com/Bannercheck/Operation_Monitoring_tool.git watchover-docker && cd watchover-docker
./watchover.sh install            # imajları oluşturur
./watchover.sh llm on             # isteğe bağlı: Ollama modelleri (qwen2.5 7B/3B, bge-m3) ve eğitim tabanı iner
./watchover.sh bundle             # -> watchover-bundle-<sürüm>.tar  (tüm imajlar + Ollama modelleri + eğitim ağırlıkları + kaynak kod)
```

**2. Kurumsal sunucuya taşı** (USB, SCP, dosya paylaşımı; tek dosya):

```
mkdir -p /opt/watchover-docker && cd /opt/watchover-docker
tar xf /mnt/usb/watchover-bundle-<sürüm>.tar --strip-components=1 source      # kaynak kod ve betik
./watchover.sh unbundle /mnt/usb/watchover-bundle-<sürüm>.tar                 # imajları ve model hacimlerini yükler, kurulumu "çevrimdışı" işaretler
./watchover.sh install                                                         # build yapmaz, yüklenen imajla başlatır; .env ve şifreler bu sunucuda üretilir
./watchover.sh llm on                                                          # pakette model varsa yerel LLM (indirme yapmaz)
./watchover.sh edge on watchover.sirket.local                                  # TLS: iç CA (internet gerektirmez) ya da ./certs içine şirket sertifikası
```

Sunucuda yalnız Docker Engine + Compose v2 kurulu olmalı (dağıtımın paket deposundan ya da çevrimdışı .deb/.rpm ile).

**3. Diğer sunucular Watchover'a nasıl bağlanır.** Veri akışı **ajanlardan Watchover'a doğru** çalışır: her izlenen sunucu, Watchover'ın 8600 portuna (edge açıksa 443 üzerinden `https://…/ingest`) kendi loglarını gönderir; Watchover sunuculara bağlanmaz. Ajan yalnız Python 3 standart kütüphanesi kullanır ve **Watchover'ın kendisinden indirilir**, internet gerekmez:

```
curl -fsSL http://<watchover>:8600/agent/install.sh | sudo bash -s -- --url http://<watchover>:8600/ingest --enroll-key <Bağlantılar › Ajanlar> --env prod
```

İzlenen sunucuda `python3` kurulu olmalı (yoksa kurulum betiği paket deposundan kurmayı dener; çevrimdışı sunucuda önceden kurun). Ajan, Watchover erişilemezken paketleri yerel diskte biriktirir ve bağlantı dönünce boşaltır. Güvenlik duvarında açılması gereken tek kural: izlenen sunuculardan Watchover'a **TCP 8600** (edge ile **443**), arayüz için istemcilerden **443/8501**. Elasticsearch, Grafana, Splunk gibi kaynaklardan **çekme** ise Watchover'dan o sistemlere doğru ilgili portlarda izin ister.

**Güncelleme.** Yeni sürümde internetli makinede `git pull && ./watchover.sh update && ./watchover.sh bundle`, sunucuda kaynak kodu aynı `tar xf … --strip-components=1 source` ile üzerine açıp `./watchover.sh unbundle <dosya> && ./watchover.sh update`. Veri hacimleri (`watchover-data`, `watchover-pg`) ve `.env` dokunulmaz. Çevrimdışı işareti `.env` içindeki `WATCHOVER_OFFLINE=1` satırıdır; sunucu ileride internete çıkarsa satırı silmek yeterlidir.
