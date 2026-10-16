# Watchover · Kurumsal Docker kurulum rehberi

Watchover şirket içindeki **tek bir sunucuda, tamamen Docker'da** çalışır. Kurulum paketi bu klasördür (`watchover.zip`): imaj sunucuda kaynak koddan derlenir (Node aşaması React arayüzünü, Python aşaması motoru kurar); GitHub, imaj kaydı ya da sunucuda Python gerekmez. Üç konteyner vardır: **dashboard** (React arayüzü + HTTP API 8501, canlı alıcı 8600, konteynerin kendini izleyen ajanı), **postgres** (ürün veritabanı, PostgreSQL 16) ve isteğe bağlı **mcp** (kendi MCP sunucumuz, 8765). Eski Streamlit arayüzü `legacy` profiliyle 8502'de bir süre daha açılabilir. Kod imajın içindedir; ayarlar, sırlar, anlık görüntüler ve loglar `watchover-data` biriminde, veritabanı `watchover-pg` biriminde durur. Güncelleme, yeni paketi klasörün üstüne açıp `./watchover.sh update` demektir; birimlere dokunulmaz. Red Hat sunucuda motor Docker CE ya da Podman olabilir (bölüm 1b); aynı ürünün OpenShift kurulumu `docs/KURULUM_OPENSHIFT.md`'dedir, ikisinin karşılaştırması bölüm 1c.

## 1. Sunucu gereksinimleri

Sunucuya kurulacak **tek bağımlılık konteyner motorudur**: Docker (Engine 24+ ve Compose v2) ya da RHEL'de Podman 4.4+ ile Compose v2 (bölüm 1b). Python, pip, PostgreSQL ana makineye kurulmaz; hepsi imajların içindedir.

| | |
|---|---|
| İşletim sistemi | Ubuntu 22.04 / 24.04, Debian 12, RHEL 8.8 / 9 ve Rocky / Alma (Linux sunucu; RHEL için bölüm 1b). Deneme için macOS / Windows'ta Docker Desktop da çalışır |
| Kaynak | En az 2 CPU / 4 GB RAM / 20 GB disk. Günlük GB'larca log ve büyük SAP arşivleri için 4 CPU / 8 GB |
| Ağ | Kullanıcılar için 8501, izlenen sunuculardan gelen ajanlar için 8600 (MCP açılırsa 8765; edge açıkken yalnız 443). İmaj derlenirken bir kez PyPI ve Docker Hub'a çıkış gerekir; kapalı ağ için bölüm 9 |

Docker kurulumu (Linux):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # sonra oturumu kapatıp açın
docker compose version             # v2.x görünmeli
```

## 1b. Red Hat Enterprise Linux 8.8 / 9 üretim sunucusu (Docker CE ya da Podman)

RHEL'de Watchover iki motordan biriyle çalışır; compose dosyası, komutlar, veriler ve yedekler ikisinde de aynıdır. `watchover.sh` motoru kendisi bulur (Docker CLI varsa Docker, yoksa Podman) ve `.env`'ye `WATCHOVER_ENGINE=docker|podman` olarak yazar; elle değiştirmek için bu satır yeterlidir. Ana makineye başka bir şey kurulmaz: Python, PostgreSQL, Ollama, Caddy imajların içindedir.

| | Docker CE (varsayılan) | Podman (Red Hat'in kendi motoru) |
|---|---|---|
| Paketler | `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin` (Docker'ın RHEL deposu; Podman / buildah / runc kaldırılır, çakışır) | `podman`, `container-selinux`, `netavark`, `aardvark-dns` (RHEL depoları) + yalnız `docker-compose-plugin` (tek Compose v2 ikilisi; Docker motoru kurulmaz, Podman'la çakışmaz) |
| Nasıl çalışır | Docker servisi | `podman.socket` (Docker uyumlu API, rootful) + Compose v2 `DOCKER_HOST=unix:///run/podman/podman.sock` ile; imajlar `podman build` ile Docker'ın vereceği adlarla derlenir |
| Komutlar | `./watchover.sh …` (kullanıcı `docker` grubunda) | `sudo ./watchover.sh …` (soket yalnız root'a açık) |
| Açılışta başlama | Docker servisi + `restart: unless-stopped` | `watchover-stack.service` (`install` kendisi açar; `./watchover.sh autostart on` / `off`) |
| SELinux | `enforcing` kalır; Docker CE konteynerleri SELinux dışında çalıştırır | `enforcing` kalır; konteynerler `container_t`, bind mount'lar `:z` ile etiketlenir |
| Hazırlık | `sudo bash scripts/rhel-prepare.sh --user ops [--mcp]` | `sudo bash scripts/rhel-prepare.sh --engine podman [--mcp]` |

Hazırlık betiği her iki motorda da firewalld'de 8501/tcp (arayüz), 8600/tcp (ajanlar), 443/tcp (edge TLS) ve `--mcp` ile 8765/tcp açar, chrony'yi açar; `--check` yalnız ne yapacağını yazar. Diğer gerekenler: `git` (ya da paket dosyası), `python3`, `tar`, `gzip`, `openssl`, `curl` (RHEL tabanında vardır: `dnf install git python3 tar gzip openssl curl`).

Örnek: 8 CPU / 64 GB / 200 GB sunucu, ham loglar ayrı disklerde:

```bash
sudo dnf -y install git
sudo git clone https://github.com/Bannercheck/Operation_Monitoring_tool.git /opt/watchover-docker   # internetsiz: bundle'ı açın (bölüm "İnternetsiz sunucu")
cd /opt/watchover-docker
sudo bash scripts/rhel-prepare.sh --check                 # ne yapacağını gösterir, değişiklik yapmaz (Podman: --engine podman ekleyin)
sudo bash scripts/rhel-prepare.sh --user ops --mcp        # Docker CE; Podman için:  sudo bash scripts/rhel-prepare.sh --engine podman --mcp
# (Docker: oturumu kapatıp açın, docker grubu)
cp .env.docker.example .env                               # kurulumdan önce ayrı log diskleri (isteğe bağlı, önerilir; aşağıda):
#   WATCHOVER_LOG_HOT_PATH=/srv/watchover/hot   WATCHOVER_LOG_COLD_PATH=/srv/watchover/cold
./watchover.sh install --production                       # Podman: sudo ./watchover.sh install --production
sudo ./watchover.sh backup-timer on 02:15                 # gecelik yedek (systemd timer)
./watchover.sh edge on watchover.sirket.local             # TLS: ./certs/watchover.crt + watchover.key şirket sertifikası ya da iç CA
./watchover.sh llm on                                     # yerel LLM istenirse (internetli: modeller iner; internetsiz: paketten gelir)
```

**Üretim kurulumu (`install --production`).** Normal kurulumun aynısı, ek olarak `.env`'ye `WATCHOVER_DEMO=0` yazar (simülatör açılamaz, demo verisi oluşmaz) ve sonunda devreye alma listesini basar; `./watchover.sh checklist` aynı listeyi istendiğinde, yapılanları işaretleyerek yeniden gösterir:

- gecelik yedek açık mı (`backup-timer on`), yedek klasörü başka bir makineye kopyalanıyor mu,
- açılışta başlama (Podman'da `watchover-stack.service`; Docker'da servis zaten açar),
- yönetici parolası: `admin@watchover.local` ilk girişte parolasını değiştirir ya da `./watchover.sh user password admin@watchover.local`; kişisel yönetici hesapları (`user add … --admin`),
- disk üst sınırları: Sistem › Ayarlar'da `hot_max_gb` / `cold_max_gb`; betik diske göre öneri yazar (kendi diskinin %80'i, iki katman aynı diskteyse %40'ı, veri biriminde %30'u),
- ham loglar ayrı disklerde mi, TLS (edge) açık mı, güvenlik duvarında yalnız gereken portlar mı.

**Ayrı sıcak / soğuk log diskleri.** Ham loglar varsayılan olarak veri biriminin içindedir (`/data/logs/hot`, `/data/logs/cold`). Üretimde sıcak loglar (son 48-72 saat, satır başına ~65-90 bayt; dakikada 100 bin satırda ~13 GB/gün) hızlı bir diske (NVMe / SSD), soğuk arşiv (~7-8 bayt/satır, ~1,1 GB/gün) ayrı ve ucuz bir diske konur. `.env`'de iki ana makine dizini verilir; `watchover.sh` bunları `docker-compose.loghot.yml` / `docker-compose.logcold.yml` üzerinden konteynerlerde `/logs/hot` ve `/logs/cold` olarak bağlar (OpenShift'teki yollarla aynı), `COMPOSE_FILE`'ı kendisi yazar:

```bash
sudo mkfs.xfs /dev/vg_data/lv_hot && sudo mkfs.xfs /dev/vg_data/lv_cold          # örnek LVM: 200 GB'lık sunucuda ör. 60 GB hot, 50 GB cold
sudo mkdir -p /srv/watchover/hot /srv/watchover/cold
echo '/dev/vg_data/lv_hot  /srv/watchover/hot  xfs defaults,noatime 0 0' | sudo tee -a /etc/fstab
echo '/dev/vg_data/lv_cold /srv/watchover/cold xfs defaults,noatime 0 0' | sudo tee -a /etc/fstab
sudo mount -a
sudo chown -R 10001:0 /srv/watchover/hot /srv/watchover/cold && sudo chmod 2770 /srv/watchover/hot /srv/watchover/cold
# .env:  WATCHOVER_LOG_HOT_PATH=/srv/watchover/hot   WATCHOVER_LOG_COLD_PATH=/srv/watchover/cold
./watchover.sh start && ./watchover.sh storage           # bağlama, boş alan ve önerilen hot_max_gb / cold_max_gb
```

- Konteynerler UID 10001 / GID 0 ile yazar; `watchover.sh` her başlatmada dizinin mutlak yol olduğunu, var olduğunu (root ile çalışıyorsa yoksa kendisi açar) ve bu kullanıcıya yazılabilir olduğunu denetler, değilse çalıştırılacak `chown` / `chmod` komutunu yazarak durur. Dizin kök dosya sistemindeyse "ayrı disk değil" uyarısı verir.
- SELinux `enforcing` kalır: bağlamalar `:z` taşır, Podman (ya da `selinux-enabled` açık Docker) dizini ilk başlatmada `container_file_t` olarak etiketler (`ls -Zd /srv/watchover/hot`). Docker CE varsayılanında etiket gerekmez.
- Mevcut bir kurulumu taşımak: `./watchover.sh stop`, sonra eski loglar kopyalanır (Podman'da `sudo podman run … docker.io/library/alpine`), `.env`'ye yollar yazılır, `./watchover.sh start`:
  `sudo docker run --rm -v watchover_watchover-data:/data -v /srv/watchover/hot:/dst:z alpine cp -a /data/logs/hot/. /dst/` (soğuk için `cold`). Birimdeki eski kopya sonra silinebilir.
- Log diskleri `./watchover.sh backup` içinde değildir: soğuk arşivi LVM / depolama anlık görüntüsüyle koruyun (OpenShift'teki VolumeSnapshot karşılığı).

**Üretim notları (RHEL)**

- **Disk**: imajlar, PostgreSQL, canlı spool ve Ollama modelleri Docker'da `/var/lib/docker`, Podman'da `/var/lib/containers` altındadır. 200 GB'lık sunucuda ham loglar ayrı disklere alınırsa kalan alan bunlara yeter (LLM ile ≥ 100 GB bırakın); betik `/var/lib`'de 40 GB'ın altında uyarır.
- **Kaynak**: `watchover.sh` her install / update / start'ta CPU ve belleği ölçüp payları dağıtır (bölüm 2k); 8 CPU / 64 GB sunucuda sonuç `cpu8` profiliyle aynıdır. 100+ sunucu / 2.000 olay/sn için 16–32 CPU, 64 GB öneri (bkz. "Ölçek").
- **Abonelik / depo**: `dnf` için RHEL aboneliği ya da Satellite gerekir; Docker deposu `download.docker.com/linux/rhel` (Podman'da yalnız `docker-compose-plugin` için). Erişim yoksa: Docker RPM'leri internetli makinede `dnf download --resolve …` ile indirilip `dnf install ./*.rpm`, betik `--no-docker` ile; Podman'da Compose ikilisi (`docker-compose-linux-x86_64`, github.com/docker/compose/releases) `/usr/local/lib/docker/cli-plugins/docker-compose` olarak kopyalanır.
- **Yeniden başlatma**: Docker servisi `enable` edilir, konteynerler `restart: unless-stopped` ile kendiliğinden kalkar, `live-restore` Docker güncellemesinde konteynerleri düşürmez. Podman'da konteynerleri yeniden başlatan bir servis yoktur: `install` `watchover-stack.service`'i açar (`./watchover.sh autostart on|off`), kapanışta yığın düzgünce durdurulur.
- **Yedek**: `sudo ./watchover.sh backup-timer on [SS:DD]` (bölüm 10); `backups/` (ya da `.env` `WATCHOVER_BACKUP_DIR`) merkezi yedeğe alınır.
- **Podman ağı**: konteynerler birbirini adla bulur (`postgres`, `receiver`); bunun için netavark + aardvark-dns gerekir (RHEL 9 varsayılanı). RHEL 8'de eski CNI arka ucu görülürse betik uyarır: `/etc/containers/containers.conf` `[network] network_backend = "netavark"` ve yeni sunucuda `podman system reset --force`.
- **İzlenen RHEL sunucular**: ajan yalnız `python3` ister (`dnf install python3`), `curl -fsSL http://<watchover>:8600/agent/install.sh | sudo bash -s -- --url … --enroll-key …` systemd servisini kurar; SELinux'ta `/opt/watchover-agent` altında çalışır, ek etiket gerekmez.

## 1c. RHEL sunucu vs OpenShift: aynı ürün, iki kurulum

Kurulum günü hedef hangisi olursa olsun ikisi de hazırdır: aynı imajlar (`Dockerfile`, `Dockerfile.receiver`), aynı veritabanı şeması, aynı `/logs/hot` ve `/logs/cold` yolları, aynı üretim kipi. RHEL sunucuda Docker CE ya da Podman (`docs/KURULUM_DOCKER.md` bölüm 1b), OpenShift'te Helm chart'ı (`docs/KURULUM_OPENSHIFT.md`). Tablo iki rehberde de aynıdır; "Fark" yazan yerde iki kurulum gerçekten ayrışır.

| Konu | RHEL sunucu (Docker CE ya da Podman) | OpenShift |
|---|---|---|
| Kurulum | `sudo bash scripts/rhel-prepare.sh [--engine podman] [--mcp]`, sonra `./watchover.sh install --production` (Podman: `sudo` ile). İmajlar sunucuda kaynak koddan derlenir; kapalı ağda `bundle` / `unbundle` | İmajlar `podman build` + `podman push` ya da `oc new-build` ile kayıt defterine, sonra `helm install … -f degerler.yaml`; Helm'siz: `oc apply -f deploy/openshift/rendered/watchover.yaml` |
| Yükseltme | `./watchover.sh upgrade` (git) ya da yeni paket + `./watchover.sh update`; veri birimleri ve log diskleri yerinde kalır | Yeni etiketli imajlar + `helm upgrade … --set image.tag=X`; pod `Recreate` ile durup yenisi açılır, PVC'ler yerinde kalır |
| Geri alma | Önceki sürümün kaynak ağacı (eski commit ya da paket) + `./watchover.sh update`. Göçler ileriye dönüktür: veritabanı için yükseltmeden önce alınan yedekle `./watchover.sh restore` | `helm rollback watchover <REVİZYON>` (imaj etiketi dahil). Göçler ileriye dönüktür: veritabanı için yükseltmeden önce alınan dump ile `pg_restore` |
| Otomatik boyutlandırma | `watchover.sh` her install / update / start'ta CPU ve belleği ölçüp konteyner sınırlarını yazar (8 CPU / 64 GB = `cpu8` profili); sabit değer: `profile cpu8` / `cpu16` | Fark: ölçme yoktur, istek / sınır değer dosyasındadır (`values.yaml`, tam performans `values-performance.yaml`). Ortak: alıcı kendi cgroup sınırını okuyup Go'nun CPU sayısını (GOMAXPROCS) ve bellek hedefini ona göre ayarlar |
| Ayrı sıcak / soğuk disk | `.env`: `WATCHOVER_LOG_HOT_PATH`, `WATCHOVER_LOG_COLD_PATH` (ana makine dizinleri, sahibi `10001:0`, SELinux `:z`); `/logs/hot`, `/logs/cold` olarak bağlanır. Boşsa veri biriminin içinde (`/data/logs`) | Her zaman ayrı PVC: `logs-hot` (hızlı sınıf), `logs-cold` (ucuz sınıf), aynı `/logs/hot`, `/logs/cold` yolları. Üst sınırlar `retention.hotMaxGb` / `coldMaxGb` ile sabitlenir |
| PostgreSQL | `postgres:16-alpine` konteyneri (birim `watchover-pg`, parola `.env`'de üretilir) ya da `.env` `DATABASE_URL` ile kurumun sunucusu | `rhel9/postgresql-16` StatefulSet (parola Secret'ta üretilir, korunur) ya da `postgresql.enabled: false` + `externalDatabase.existingSecret` |
| LLM (yalnız CPU) | `./watchover.sh llm on`: Ollama + eğitim konteyneri, CPU payı otomatik; modeller indirilir (kapalı ağda paketten gelir) | `llm.enabled: true`: Ollama pod'u, tek model, `llm.lock`, indirdikten sonra `llm.ollama.allowEgress: false`; eğitim `trainer.enabled` |
| MCP sunucusu | `./watchover.sh mcp on`: aynı imaj, `http://<sunucu>:8765/mcp`; edge açıkken Caddy `https://<ad>/mcp` yolunu bu konteynere (TLS), `https://<ad>/api/mcp` yolunu arayüzün kendi MCP ucuna gönderir. Anahtarlar: arayüzde adlı, kapsamlı anahtarlar (Ayarlar › Bağlantılar › MCP); `.env` `MCP_API_KEY` kurulum çapında okuma anahtarıdır (ilk kurulumda üretilir; `MCP_API_KEY_WRITE=1` yazma da verir). Canlı ürün araçları arayüze iletilir (`WATCHOVER_API_URL=http://watchover:8501`); dosyalar `./datasets` | `mcp.enabled: true`: ayrı Deployment + Route (`https://<mcp route>/mcp`, edge TLS) + NetworkPolicy; arayüzün kendi ucu `https://<ui route>/api/mcp`. Anahtarlar: aynı adlı anahtarlar; kurulum anahtarı Secret'ta (üretilir, upgrade'de korunur, arayüz pod'una da verilir) ya da `mcp.existingSecret`, yazma `mcp.installKeyWrite`. Canlı araçlar `WATCHOVER_API_URL=http://watchover:8501` ile iletilir; dosyalar kendi PVC'sinde (`/datasets`, `oc cp`). Fark: OpenShift'te MCP pod'u `/data` ve log disklerini görmez, canlı veriye arayüz üzerinden ulaşır; RHEL'de 8765 ayrıca düz http açıktır (`WATCHOVER_MCP_PORT=127.0.0.1:8765` ile yalnız yerel) |
| Demo kapalı | `install --production` → `WATCHOVER_DEMO=0` (Docker'ın varsayılanı 1); kalan demo verisi: `./watchover.sh demo-clean` | Her zaman üretim (`demo: false`); kalan demo verisi: Sistem › Demo verisi ya da `oc exec … python -m watchover.demo clean` |
| Yedek | `./watchover.sh backup-timer on [SS:DD]`: her gece PostgreSQL dökümü + veri birimi (ham loglar hariç) + `.env` → `backups/`, son 14 (systemd timer; systemd yoksa cron). Geri: `./watchover.sh restore DOSYA`. Log diskleri: LVM / depolama anlık görüntüsü | `backup.enabled: true`: her gece `pg_dump` CronJob'u + kendi PVC'si, `keepDays`. Geri: `pg_restore`. Fark: `data` diski (ayarlar, `.vault.key`) ve log diskleri için VolumeSnapshot gerekir |
| Ajan kayıt adresi | `http://<sunucu>:8600/ingest`, edge açıkken `https://<ad>/ingest`; kayıt anahtarı ve hazır komut Bağlantılar sayfasında | `https://<ingest route>/ingest` (ya da tek adres `routes.ui.host`); chart bu adresi uygulamaya verir (`WATCHOVER_INGEST_URL`), hazır komut aynı sayfada |
| TLS | `./watchover.sh edge on <ad>`: Caddy 443'te; şirket sertifikası `./certs/watchover.crt` + `.key` ya da Caddy'nin kendi CA'sı (`edge cert`) | OpenShift router'ı (Route, edge TLS, http → https); kümenin `*.apps` sertifikası ya da `routes.tls` |
| İzleme / sağlık | `./watchover.sh status` ve `doctor`; sağlık uçları `/api/health` (8501), `/health` (8600, MCP 8765); konteynerin kendi ajanı (`WATCHOVER_SELFMON=1`) | Aynı uçlarda startup / readiness / liveness probları; `oc get pods`; pod'un kendi ajanı (`dashboard.selfMonitor`) |
| Açılışta başlama | Docker: servis + `restart: unless-stopped`; Podman: `watchover-stack.service` (install açar, `./watchover.sh autostart on` / `off`) | Kubernetes (Deployment / StatefulSet) |
| Sorun giderme | `./watchover.sh logs [servis]`, `./watchover.sh doctor`, `./watchover.sh storage`, `./watchover.sh checklist`; Docker rehberi bölüm 11 | `oc logs deploy/watchover -c dashboard` (ya da `-c receiver`), `oc describe pod`, `oc get events`; OpenShift rehberi bölüm 11 |

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

## 2b. Alıcı servisi (Go, 8600)

Ajanların bağlandığı 8600 portu v1.35'ten itibaren ayrı bir konteynerde, Go ile yazılmış `watchover-receiver` servisindedir. Paketi doğrular, `/data/spool` altına sıralı segment olarak yazar ve milisaniyeler içinde yanıt verir; panel (dashboard) segmentleri arka planda işler. Böylece anomali taraması ya da yoğun ayrıştırma sırasında ajanlar beklemez, zaman aşımına düşmez.

- Kimlik doğrulama aynıdır: ajan token'ları, ortak anahtar ve ilk kurulumdaki açık mod. Panel izinli token'ların özetini (`auth.json`) birkaç saniyede bir alıcıya yayınlar; iptal edilen token en geç birkaç saniye içinde reddedilir.
- `/enroll`, `/agent.py` ve `/agent/install.sh` de bu servisten sunulur; kayıt isteği panele aktarılır.
- Kuyruk sınırı `.env` içinde `WATCHOVER_SPOOL_MAX_MB` (varsayılan 2048). Aşılırsa alıcı 503 ve Retry-After döner, ajan paketi kendi diskinde saklar.
- Durum: `curl http://<sunucu>:8600/health`, Prometheus için `/metrics`; log: `./watchover.sh logs receiver`.
- Yükseltme `./watchover.sh upgrade` ile kendiliğinden olur: panel 8600'ü bırakır, alıcı servisi devralır. Güvenlik duvarı kuralı değişmez.
- **Go ile ayrıştırma (v1.46):** alıcı, Python'la aynı sonucu verdiği kanıtlanmış biçimleri kendisi ayrıştırır (`.env` içinde `WATCHOVER_GO_PARSE=1`, varsayılan). Kullanıcı grok kalıpları, öğrenilmiş sütun eşlemeleri ve tanınmayan biçimler yine panelde ayrıştırılır. `WATCHOVER_GO_PARSE=0` eski davranışa birebir döner, `shadow` yalnız karşılaştırır. Ayrıştırılmış segmentler ham halin yaklaşık 3 katı yer kaplar ve kuyruk sınırına dahildir; disk elveriyorsa `WATCHOVER_SPOOL_MAX_MB` değerini 4096-6144 yapın. İşçi sayısı `WO_PARSE_WORKERS` (varsayılan: çekirdek sayısı); 4 çekirdekli makinede ekran yanıtları yavaşlarsa 2 yapın.

## 2c. Web ve mobil uygulamaları izlemek (Kullanıcı deneyimi)

1. **Uygulama ekleyin:** APM › Kullanıcı deneyimi › Uygulamalar. Ad, platform (web, android, ios, flutter, react-native), web için izinli kökenler (`https://ik.sirket.com.tr`), gerekirse örnekleme yüzdesi. Bir anahtar (`pk_…`) üretilir; **Kurulum** düğmesi platforma göre hazır satırları gösterir.
2. **Web:** sayfanın `<head>` kısmına tek satır: `<script src="https://<watchover>/rum.js" data-key="pk_…" async></script>`. Kullanıcı girişinden sonra `window.watchover.setUser(personelNo)`.
3. **Mobil:** depodaki `sensors/` altındaki SDK dosyası uygulamaya eklenir ve uygulama açılışında `start(endpoint, key)` çağrılır (Android: OkHttp interceptor; iOS: `urlSession()`; Flutter: `httpClient()` ve `observer`; React Native: `start` ve `onNavigationChange`). Uç adres `https://<watchover>/mobile` (edge açık değilse `http://<sunucu>:8600/mobile`). Uygulama mağaza sürümüne girmeden önce test ortamında denenmelidir; SDK'lar başvuru kodudur ve bu depoda derlenmemiştir.
4. **Arka uçla birleştirme:** sensörler her çağrıya `traceparent` başlığı ekler. nginx log biçimine `$http_traceparent`, uygulama loglarına gelen `traceparent` başlığı eklenirse kullanıcının dokunuşu ile sunucudaki adımlar aynı izde kimlikle birleşir; eklenmezse oturum ve zaman üzerinden gösterilir.
5. **Güvenlik duvarı:** telefonlar ve tarayıcılar Watchover'a 443 (edge) ya da 8600 üzerinden ulaşmalıdır. Şirket dışından kullanılan uygulamalar için yalnız `/mobile`, `/rum` ve `/rum.js` yollarını dışarı açan bir ters vekil önerilir.
6. **KVKK:** sensörler form değeri, ekran içeriği, tuş vuruşu, konum ve sorgu dizesi toplamaz; sunucu kullanıcı kimliğini takma ada çevirir ve saklamaz, metinlerde TCKN / telefon / e-posta / IBAN / kart / parola maskeler, IP saklamaz. Bir kişinin oturumlarını bulmak için kimlik girildiğinde gerekçe istenir ve güvenlik günlüğüne yazılır. Aydınlatma metni ve rıza kararı kurumun hukuk birimindedir.

## 2d. Servis haritası ve sunucu bazında entegrasyonlar

- **Trafik (izler):** APM › Servis haritası. Kim kimi çağırıyor, çağrı sayısı, P95 gecikme ve hata oranı izlerden ölçülür; düz çizgi loglarda üst adım kimliğiyle, kesikli çizgi zaman örtüşmesinden bilinir.
- **Ağ (ajan bağlantıları):** ajan `--flows` ile (kurulum betiği bunu varsayılan açar, `--no-flows` kapatır) dakikada bir sunucunun açık TCP bağlantılarını gönderir. Hiçbir logun söylemediği bağımlılıklar görünür: uygulama sunucusundan veritabanına, Kafka'ya, SAP RFC'ye, dış API'ye. Süreç adlarını görmek için ajan root çalışmalıdır (systemd servisi öyle çalışır).
- **Entegrasyonun teknolojisi (v1.46):** her bağlantı "REST API · FastAPI", "Oracle Net · Oracle Database 19c", "SAP RFC · SAP ECC (SAP JCo)" gibi teknolojisiyle ve güven derecesiyle (kesin / yüksek / olası) görünür; tıklayınca kanıtlar açılır, yanlışsa "Elle düzelt" ile düzeltilir. En iyi sonuç için ajanları 0.6.0'a güncelleyin: bölüm 5'teki kurulum komutunu (`agent/install.sh … --url … --token …`, Bağlantılar › Ajanlar'dan) aynı değerlerle yeniden çalıştırmak yeter; ajan süreçlerin çalışma zamanı, çatı ve istemci kütüphanesi etiketlerini gönderir; komut satırının kendisi sunucudan çıkmaz.
- **Entegrasyonlar menüsü:** her sunucu için tek ekran: ona bağlananlar, üzerinde çalışan süreçler ve dinlenen portlar, izlerde görülen servisler, gittiği sistemler tür türüne (veritabanı, mesajlaşma, HTTP API, SAP, kimlik, e-posta, dosya aktarımı). Hedefler envanterdeki IP'lerle adlandırılır; envanterde olmayan adres IP olarak ve kesikli çerçeveyle görünür, envantere eklenince adıyla görünür.
- **Gateway / nginx'te istek kimliği:** izlerin kimlikle birleşmesi için önerilen log biçimi:

```nginx
log_format watchover '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent" '
                     '$request_time $upstream_response_time request_id=$request_id traceparent=$http_traceparent';
proxy_set_header X-Request-ID $request_id;          # arka uç bu kimliği kendi loguna yazarsa aynı iz kimlikle birleşir
access_log /var/log/nginx/access.log watchover;
```

- **Okunan biçimler:** nginx / Apache (süre, upstream süresi, istek kimliği, Apache `%D`), IIS `time-taken`, Envoy / Istio erişim logu (`x-request-id`), AWS ALB, uygulama loglarında `traceparent` / `trace_id` / `request_id` / `correlation_id`, SharePoint ULS (Correlation ve Execution Time), Keycloak olayları (SSO oturumu uygulamalar arası yolculuk olarak).

## 2e. Birden fazla ekip: sunucu bazında görünürlük

Kullanıcılar › Ekipler sekmesinde her ekip için kurallar tanımlanır; ekibe eklenen kullanıcı yalnız o ekibin sunucularını ve onlarda çalışan servisleri görür. Örnekler:

| Ekip | Kural |
|---|---|
| EA | Sunucu adı desen `ea-*`, ya da envanter uygulaması `SAP*` |
| İK | Envanter uygulaması `HR*`, servis / uygulama adı `ik-*` (İK mobil ve web uygulamaları) |
| Veritabanı | Ajan etiketi `dba`, ya da IP ağı `10.40.0.0/16` |

v1.46'dan itibaren her yeni ekip üç hazır rolle gelir (Ekip yöneticisi, Operatör, İzleyici); ekibin Roller bölümünde her rolün görebileceği özellikler menü gruplarına göre işaretlenir, üyeye ekip rolü atanır. Ekip yalnız kendi sunucularının verisini görür (envanter, ajanlar, eşikler, bildirimler, veri setleri, loglar); ekip üyesinin eklediği sunucu ekibe ait olur. Ekip yöneticisi "Ekibim" sayfasından yalnız kendi ekibini, kendi yetkisini aşmadan yönetir. Ekip kayıt anahtarıyla kurulan ajanların sunucuları ekibe katılır; ajan başka ekibin sunucu adıyla log gönderemez. Merkezi NOC ekibine "Tüm sunucuları gör" yetkili bir rol verilir. Ekibe girmemiş kullanıcılar her şeyi görmeye devam eder; kurumda herkesin bir ekibe bağlı olması isteniyorsa Sistem › Ayarlar'da `teams_strict` açılır.

## 2f. İş akışları ve hatalar

APM › İş akışları: "Yeni akış" → anahtar deseni (ör. `sipariş\s+(\d{7})`, parantez içi anahtar) → "Öner" ile adımlar loglardan gelir → Kaydet. Ek ajan veya kod değişikliği gerekmez; anahtar hangi sunucunun logunda geçiyorsa oradan izlenir. Kişisel veri taşıyan anahtarlar (TCKN, sicil) için "Anahtarı maskele" işaretlenir. APM › Hatalar ek ayar istemez.

## 2g. 200+ sunucu: pool'lar ve kapasite

Pool'lar envanterdeki **küme** alanından gelir (Envanter › CSV içe aktarma, `cluster` sütunu); boş bırakılan sunucular adlarının numarasız köküyle gruplanır (`prd-sap-app-01` → `prd-sap-app`). Ek ayar gerekmez. Operasyon ekranında üstteki zaman seçiciyle (ya da zaman çizelgesinde sürükleyerek) herhangi bir aralık seçilir; son 48 saatin logları satır satır, daha eskisi sayı olarak görünür (`live_retention_h`, `history_retention_days` Sistem ayarlarından). Ölçülen kapasite (4 vCPU, PostgreSQL, Go alıcısı): 300 sunucu ve dakikada 100 bin satır rahat, dakikada ~290 bin satır sınırda; daha yüksek hacimde CPU sayısını değil düğüm / işçi sayısını artırmak gerekir. Ayrıntı ve kendi ortamınızda yük testi: `docs/LOAD_TEST.md`.

## 2h. Sunucu bazında eşikler ve uyarılar

Müdahale › Eşikler: "Yeni eşik" ile sunucu, ad deseni (`prd-sap-*`), pool ya da ortam için CPU / bellek / disk / GPU / hata oranı / hata/dk / p95 / sessizlik seviyesi tanımlanır. Bildirim için Uyarılar › Alıcılar'da kişi ve grup, Uyarılar › Kanallar'da SMTP, SMS ya da webhook (Teams kanalının "Incoming Webhook" adresi, Slack uygulama webhook'u) ayarlanır; "Test gönder" ile denenir.

## 2i. Log gezgini

Sinyaller › Loglar. Üstteki sorgu çubuğuna `level:err OR level:crit`, `host:prd-sap-* -service:nginx`, `pool:prd-crm level>=warn "timeout"` gibi sorgular yazılır; soldaki filtrelere tıklamak sorguyu kendisi yazar. **Canlı** düğmesi akışı 2 saniyede bir getirir; aşağı kaydırınca durur ve "N yeni satır" gösterir. Satıra tıklayınca Ayrıntı / Bağlam / Ham / JSON açılır; her değerde süz, dışla, kopyala vardır. **Kaydet** aramayı kişiye (istenirse ekibe) kaydeder, **Uyarı** "M dakikada eşleşen satır ≥ N" kuralını mevcut bildirim kanallarına bağlar (`act.logalert` yetkisi). Son 48 saat satır satır aranır (`live_retention_h`); yoğun yükte uzun aralıklardaki serbest metin aramaları "≈ tahmini" sayı gösterir ve "Daha geriye tara" ile devam eder. Kişisel veri (e-posta, TCKN, kart, IBAN, telefon) içeren aramalar reddedilir; satırlar maskeli görünür.

## 2j. Log depolama: sıcak ve soğuk (v1.48)

Ham log satırları artık ana veritabanında değil, veri biriminde iki dizinde tutulur: **sıcak** (`/data/logs/hot`, gün başına bir dosya, son `live_retention_h` saat; ölçülen ~65-90 bayt/satır) ve **soğuk** (`/data/logs/cold`, şablon + sıkıştırma ile ~7-8 bayt/satır, `cold_retention_days` gün, varsayılan 7). Süresi dolan gün tek dosya silinerek gider, PostgreSQL şişmez. Log gezgini eski günleri arşivden tarar ("arşivden" rozeti, ilerleme çubuğu). Disk dolmasın diye Sistem › Ayarlar'da üst sınır verin: `hot_max_gb` (dolunca biten en eski gün erkenden arşive geçer) ve `cold_max_gb` (dolunca en eski arşiv günü silinir); Sistem › Depolama kartı doluluğu gösterir ve %85'te uyarır. Dakikada 100 bin satırda sıcak ~13 GB/gün, soğuk ~1,1 GB/gün. İlk açılışta eski `live_batches` tablosu arka planda dosyalara taşınır. Eski davranış için `.env`'de `WATCHOVER_LOG_STORE=db`. Sunucuda iki katmanı **ayrı disklere** almak için `.env`'de `WATCHOVER_LOG_HOT_PATH` / `WATCHOVER_LOG_COLD_PATH` (bölüm 1b); `./watchover.sh storage` nerede durduklarını ve önerilen üst sınırları gösterir.

## 2j-b. Anahtar servisi: watchover-keyd (aksiyon ajanı)

Aksiyon ajanının imza ve denetim anahtarları panelde, veritabanında ya da `/data`'da **durmaz**: `keyd` servisi (aynı imaj, `python -m watchover.act.keyd serve`) kendi birimi `watchover-keys`'te tutar, dışarıya port açmaz ve panelle yalnız karşılıklı TLS ile konuşur (istemci sertifikasını ilk açılışta `watchover-secrets` birimine yazar). Kasa anahtarı (`.vault.key`) da ilk açılışta `/data`'dan `watchover-secrets`'e taşınır; böylece `/data` ya da veritabanı yedeği sızsa bile kasa değerleri ve denetim zinciri çözülemez. keyd yalnız bir sonraki denetim kaydını imzalar (geçmiş yeniden imzalanamaz), çapaları kendi başından imzalar, iş imzasında kendi sınırlarını (`/keys/store/keyd.json`) uygular. Anahtarlar `act_mode` ilk kez "off" dışına alınınca oluşur. keyd çalışmazsa sistem yürütmesi ve denetim yazımı durur (503), rehberli inceleme sürer. Yedek: Otomasyon › Anahtarlar'dan iki `act.policy` sahibi, her biri parolanın bir yarısıyla şifreli dışa aktarım alır; ya da sunucuda `docker compose exec keyd python -m watchover.act.keyd export --dir /keys/store --out /keys/yedek.wokx`. Acil durdurma: `docker compose exec keyd python -m watchover.act.keyd kill --dir /keys/store --reason "..."`. Durum: `python -m watchover.act doctor` (paylaşılan disk uyarısı, tetikleyiciler, dış çapa). Tek sunucu, Docker'sız kurulumda keyd ayrı bir işletim sistemi kullanıcısıyla Unix soketinde çalışır: `--unix /run/watchover-keyd/keyd.sock`, panelde `WATCHOVER_KEYD=unix:/run/watchover-keyd/keyd.sock`.

## 2k. Hazır boyut profili ve LLM'i GPU'suz çalıştırmak

**Kaynaklar kendiliğinden ayarlanır:** `watchover.sh` her kurulum, güncelleme ve başlatmada makinenin CPU ve belleğini okur ve payları orantılı dağıtır: panel çekirdeklerin çoğunu (CPU − 2, en az 2), belleğin dörtte birini; alıcı CPU'nun dörtte birini (ayrıştırma işçisi CPU × 3/8); PostgreSQL belleğin sekizde birini (`shared_buffers` bunun dörtte biri); LLM CPU × 3/8 ve belleğin ~%16'sını; eğitim CPU'nun yarısını. 8 CPU / 64 GB makinede sonuç: panel 6 CPU / 16 GB, alıcı 2 CPU (3 işçi), PostgreSQL 2 CPU / 8 GB, LLM 3 CPU / 10 GB, eğitim 4 CPU / 12 GB. Makineye CPU ya da bellek eklenince bir sonraki `./watchover.sh start` yeni değerleri yazar. Sabit değer istenirse `./watchover.sh profile cpu8|cpu16` (ya da `.env`'de `WATCHOVER_SIZING=manual`); `profile auto` otomatiğe döner, `profile` geçerli değerleri gösterir. Alıcı ayrıca konteynerin CPU ve bellek sınırını kendisi okur (OpenShift pod sınırı dahil). Tanımlı bir LLM bağlantısı (LLM sayfası / `.env`) değiştirilmez.

LLM'i açmak: `./watchover.sh llm on` (Ollama başlar, modeller iner: günlük kullanım için qwen2.5 3B, gece işleri için 7B). GPU'suz bir yanıt 3B ile ~20-40 sn, 7B ile ~45-90 sn sürer (LLM › Kalite'deki ölçüm kesin süreyi gösterir). Çekirdek (anomali, eşik, log analizi, panolar) LLM'siz çalışır.

**Eğitim takvimi:** LLM › Kalite › Eğitim takvimi: günler, başlangıç / bitiş saati ve saat dilimi. Takvim açıkken otomatik eğitim yalnız bu pencerede başlar (bitiş başlangıçtan önceyse pencere gece yarısını geçer, ör. 22:00-05:00); "Şimdi eğit" takvimi beklemez, başlamış eğitim pencere bitince kesilmez.

**Demo verisi:** simülatörün ürettiği veriyi silmek için `./watchover.sh demo-clean --dry-run` (ne silineceğini gösterir), sonra `./watchover.sh demo-clean`; ya da Sistem › Demo verisi. Gerçek kaynakların kullandığı sunucu adlarına dokunmaz. Üretimde simülatörü tamamen kapatmak için `.env`'de `WATCHOVER_DEMO=0`.

## 3. Günlük işletim: watchover.sh

| Komut | Ne yapar |
|---|---|
| `./watchover.sh status` | Konteynerler, sağlık durumu, anlık CPU / bellek |
| `./watchover.sh logs [dashboard\|postgres\|mcp]` | Canlı log |
| `./watchover.sh stop` / `start` / `restart` | Durdur / başlat / yeniden başlat (veri kalır) |
| `./watchover.sh update` | Yeni paket klasörün üstüne açıldıktan sonra: imajı yeniden derler, konteynerleri yeniler, eski imajı temizler |
| `./watchover.sh upgrade` | **Yeni sürüme geçmenin tek komutu**: `git fetch` + ileri sarma + `update`. Klasörde elle değiştirilmiş dosya varsa kenara alır (`git stash`) ve sonra geri koyar; düz `git pull`'un "unstaged changes" diye durduğu yerde durmaz. Kendi commit'i olan klasörü elle birleştirmeye bırakır |
| `./watchover.sh install --production` | Kurulum + `WATCHOVER_DEMO=0` + devreye alma listesi (yedek, açılışta başlama, yönetici parolası, disk sınırları, TLS); `./watchover.sh checklist` listeyi yeniden gösterir |
| `./watchover.sh backup [--no-logs] [--keep N]` | PostgreSQL dökümü + veri birimi + `.env` → `backups/watchover-YYYYMMDD-HHMM.tgz` (`--no-logs`: veri birimindeki ham loglar hariç) |
| `./watchover.sh backup-timer on [SS:DD]` / `off` | Gecelik yedek (`backup --no-logs`): systemd timer, systemd yoksa cron; varsayılan 02:15; argümansız durumu gösterir |
| `./watchover.sh autostart on` / `off` | Açılışta başlatan systemd birimi (`watchover-stack.service`); Podman'da `install` kendisi açar |
| `./watchover.sh storage` | Ham logların yeri (veri birimi ya da ayrı diskler), boş alan, önerilen `hot_max_gb` / `cold_max_gb` |
| `./watchover.sh restore DOSYA` | Yedeği geri yükler (veritabanı ve veri birimi; konteynerler bu sırada durur) |
| `./watchover.sh user …` | `list`, `add EMAIL [--admin]`, `promote`, `password EMAIL`, `unlock`, `enable`, `disable`, `delete` |
| `./watchover.sh mcp on\|off` | Kendi MCP sunucumuzu açar / kapatır (bölüm 7) |
| `./watchover.sh legacy on\|off` | Eski Streamlit arayüzünü ek konteyner olarak açar / kapatır (8502) |
| `./watchover.sh migrate` | Eski SQLite kurulumunun `.db` dosyalarını PostgreSQL'e kopyalar (bölüm 6) |
| `./watchover.sh save` / `load DOSYA` | İmajları `.tgz` olarak dışa / içe aktarır (kapalı ağ, bölüm 9) |
| `./watchover.sh shell` | Dashboard konteynerinde kabuk |

Bütün bunlar düz `docker compose` komutlarıdır; alışkın olanlar `docker compose ps`, `docker compose logs -f dashboard` ile aynı işleri yapabilir (Podman'da `sudo DOCKER_HOST=unix:///run/podman/podman.sock docker compose ps` ya da `sudo podman ps`).

## 4. Yapılandırma (.env)

`install` gerekli her şeyi yazar; değiştirmek isteyenler için:

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `POSTGRES_PASSWORD` | rastgele | Veritabanı parolası; birim ilk oluşturulurken işlenir (sonradan değiştirmek: bölüm 6) |
| `MCP_API_KEY` | rastgele | MCP istemcilerinin göndereceği kurulum anahtarı (yalnız okur) |
| `MCP_API_KEY_WRITE` | boş | `1`: kurulum anahtarı yazabilir de (`create_action`; OpenShift'te `mcp.installKeyWrite`) |
| `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` / `WATCHOVER_MCP_PORT` | `8501` / `8600` / `8765` | Sunucuda açılan portlar; `127.0.0.1:8765` gibi yazılırsa yalnız localhost |
| `WATCHOVER_CPUS` / `WATCHOVER_MEMORY` | otomatik | Dashboard konteynerinin sınırı (bölüm 2k; `WATCHOVER_SIZING=manual` ile elle) |
| `WATCHOVER_PG_CPUS` / `WATCHOVER_PG_MEMORY` | otomatik | PostgreSQL konteynerinin sınırı |
| `WATCHOVER_ENGINE` | algılanır | `docker` ya da `podman` (RHEL, rootful Podman: bölüm 1b) |
| `WATCHOVER_LOG_HOT_PATH` / `WATCHOVER_LOG_COLD_PATH` | boş | Ham logların ayrı diskleri (ana makine dizinleri, sahibi `10001:0`); boşsa veri biriminde |
| `WATCHOVER_BACKUP_DIR` / `WATCHOVER_BACKUP_KEEP` | `backups` / `14` | Yedeklerin klasörü (başka disk önerilir) ve saklanacak sayı |
| `WATCHOVER_DEMO` | `1` | `0` = üretim: simülatör yok, demo verisi yazılmaz (`install --production` yazar) |
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

Sonra istemciden `analyze_dataset("uretim-log.zip")` demek yeter. Anahtarsız istekler 401 alır. MCP konteyneri canlı ürün araçlarını (loglar, panolar, anomaliler, envanter) arayüzün kendi MCP ucuna iletir (`WATCHOVER_API_URL`, varsayılan `http://watchover:8501`). Edge açıkken (bölüm 8) aynı sunucu TLS ile `https://<ad>/mcp` adresindedir (Caddy → `mcp:8765`), arayüzün kendi MCP ucu `https://<ad>/api/mcp`; düz 8765'i `.env`'de `WATCHOVER_MCP_PORT=127.0.0.1:8765` ile yalnız yerelde tutun (MCP kapalıyken `/mcp` 502 döner). Anahtarlar: Ayarlar › Bağlantılar › MCP'de adlı, kapsamlı (hesap ya da ekip, okuma / yazma) anahtarlar; `.env`'deki `MCP_API_KEY` kurulum çapında okuma anahtarıdır, `MCP_API_KEY_WRITE=1` ona yazma da verir. OpenShift'te aynı sunucu `mcp.enabled: true` ile kendi pod'u ve TLS'li Route'uyla gelir (OpenShift rehberi bölüm 5b).

**Canlı ürünün araçları ve adlı anahtarlar.** Arayüzün kendisi de MCP sunucusudur: `https://<sunucu>/api/mcp` (doğrudan `http://<sunucu>:8501/mcp`). Araçlar: `whoami`, `log_search` (sorgu dili + zaman aralığı), `log_tail`, `dash_infra` / `dash_alarms` / `dash_pools`, `host_detail` (özet, alarm metrikleri, eşik seviyeleri), `thresholds_state`, `list_anomalies` / `get_anomaly`, `integration_hosts` / `host_integrations` (teknolojiyle), `search_traces` / `get_trace` / `trace_session`, `list_flows` / `flow_funnel` / `flow_items`, `inventory_lookup`, `list_actions` / `get_action`; yazma: `create_action`, `ack_anomaly`, `set_threshold`. Anahtarları **Ayarlar › Bağlantılar › MCP**'de oluşturun: her anahtar bir hesaba (o hesabın gördüğü kadar) ya da bir ekibe (yalnız ekibin sunucuları) bağlıdır, varsayılan salt okunurdur, yazma araçları yalnız "yazma" izniyle açılır; iptal anında geçerlidir. Kişisel veri (KVKK) maskelenir; her çağrı kim / araç / kapsam olarak denetim kaydına düşer (argüman değerleri yazılmaz). `MCP_API_KEY` kurulum geneli **okuma** anahtarı olarak çalışmaya devam eder (`MCP_API_KEY_WRITE=1` eski yazma iznini geri verir). 8765 konteyneri adlı anahtarları da kabul eder; `.env`'e `WATCHOVER_API_URL=http://watchover:8501` yazılırsa canlı araçları da çağıranın anahtarıyla API'ye iletir.

**Dış MCP sunucularından veri çekmek.** **Ayarlar › Veri kaynakları › MCP kaynakları**: streamable HTTP ya da HTTP+SSE adresi (konteynerde stdio yok), bearer token ya da özel başlık (sırlar gibi şifreli saklanır). "Araçları listele" ile araç / kaynaklar görünür; bir çekme, bir aracı sabit argümanlarla aralıkla (en az 30 sn) çağırır ve sonucu log satırı (kaynak adıyla, JSON alanları korunur), envanter satırı ya da ITSM kaydı olarak alır. Zaman ve boyut sınırı, son çalışma, satır sayısı ve hata durumu listede görünür; ekibe ait kaynak yalnız ekibin sunucularına yazar. Kaynakta izin verilen salt okunur araçları "Watchover'a sor" da çağırır ve cevapta [M1] diye kaynak gösterir.

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

Meslektaşlar `https://watchover.local` yazar. `.local` adları Mac, iPhone / iPad ve Windows 10+ makinelerde ek kurulum olmadan çözülür (mDNS / Bonjour); Linux'ta `avahi-daemon` gerekir. Ad yerine ya da adla birlikte IP de verilebilir: `edge on watchover.local,192.168.1.107`; IP her durumda sertifikaya eklenir, `https://192.168.1.107` de çalışır. Duyuru bir LaunchAgent olarak kurulur (`~/Library/LaunchAgents/local.watchover.mdns.*.plist`): Mac yeniden başlasa, oturum kapansa ya da `update` çalışsa da sürer; `edge off` kaldırır. v1.42.1'den beri duyuruyu küçük bir izleyici yapar: Mac başka bir ağa geçtiğinde (Wi-Fi değişimi, yeni DHCP adresi, uykudan uyanma, USB-C Ethernet) 15 saniye içinde yeni adresi duyurur; elle bir şey yapmak gerekmez. Eski kurulumlardaki sabit adresli kayıt `upgrade` / `start` ile kendiliğinden yenilenir. launchd olmayan sistemlerde duyuru arka plan süreci olarak çalışır ve `install` / `update` / `start` kaybolduysa yeniden açar.

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

Gecelik yedek: `sudo ./watchover.sh backup-timer on 02:15` (systemd `watchover-backup.timer`: sunucu o gece kapalıysa yedek açılışta alınır; systemd yoksa kullanıcının crontab'ına tek satır yazar); `backup-timer off` kapatır, `backup-timer` durumu gösterir. Gecelik yedek `--no-logs` ile alınır: ham loglar büyüktür ve ayrı disklerdeyse zaten yedeğin dışındadır; böyle bir yedeği geri yüklemek mevcut ham loglara dokunmaz. Son 14 yedek kalır (`WATCHOVER_BACKUP_KEEP`), klasör `WATCHOVER_BACKUP_DIR` ile başka diske alınır. Yedek dosyasını başka bir diske / sunucuya kopyalayın; `data/.vault.key` sırların anahtarıdır, yedek onsuz açılmaz. Başka bir sunucuya taşımak: yeni sunucuda `install`, sonra `restore`.

## 11. Sorun giderme

| Belirti | Çözüm |
|---|---|
| `Docker is not installed / not reachable` | Bölüm 1; `sudo systemctl start docker`; kullanıcı `docker` grubunda mı (`groups`) |
| Derleme `pip` adımında takılıyor | Sunucunun PyPI'ye çıkışı yok: bölüm 9 (imajı başka makinede derleyip taşıyın) ya da şirket proxy'sini Docker'a tanıtın |
| Tarayıcı bağlanamıyor | `./watchover.sh status` (healthy?), `ss -ltnp \| grep 8501`, güvenlik duvarı; uzak sunucuda `localhost` değil sunucu IP'si |
| Güncelleme yapıldı ama ekran eski | `./watchover.sh version` klasörün sürümünü, kenar çubuğunun altındaki "Watchover vX" çalışan sürümü gösterir; ikisi aynıysa tarayıcıda bir kez Cmd+Shift+R (Windows: Ctrl+F5). v1.42.3'ten beri sayfa önbelleğe alınmaz ve açık sekmede "Yeni sürüm yüklendi · Yenile" çıkar; `update` sonunda çalışan sürümü yazar, farklıysa uyarır (`./watchover.sh doctor` da kontrol eder) |
| `https://watchover.local` açılmıyor | `./watchover.sh doctor`: konteynerleri, edge'in bu makinede yanıt verip vermediğini ve adın bu makinenin IP'sine çözülüp çözülmediğini sırayla dener, kapalı konteyneri açar, eski adresli Bonjour kaydını yeniler (`--check` yalnız rapor). Hâlâ açılmıyorsa Mac'in DNS önbelleği: `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`; ağda adı başka bir cihaz duyuruyorsa `dns-sd -G v4 watchover.local` kimin yanıt verdiğini gösterir |
| Port çakışması | `.env` içinde `WATCHOVER_UI_PORT` / `WATCHOVER_LIVE_PORT` / `WATCHOVER_PG_PORT` |
| `unhealthy` | `./watchover.sh logs dashboard`; bellek sınırı düşükse `WATCHOVER_MEMORY` |
| `password authentication failed` | `.env` parolası birim oluşturulduktan sonra değişti: bölüm 6'daki `ALTER USER` |
| Giriş yapılamıyor | `./watchover.sh user list`, `user password EMAIL`, `user unlock EMAIL` |
| Entegrasyonlarda teknoloji "olası" ya da yalnız protokol görünüyor | Ajan 0.6.0'dan eski: ajanı yeniden kurun (bölüm 2d); ajan root çalışmıyorsa süreç adları ve parmak izleri okunamaz. Kesin bilgi için "Elle düzelt" |
| Disk doluyor | Sistem › Depolama: hangi katman doluyor; Sistem › Ayarlar'da `hot_max_gb` / `cold_max_gb` ya da `cold_retention_days` düşürün; `./watchover.sh storage` öneri verir; ham logları ayrı disklere alın (bölüm 1b) |
| `WATCHOVER_LOG_HOT_PATH=… belongs to …` | Log dizini konteyner kullanıcısına ait değil: mesajdaki `sudo chown -R 10001:0 DİZİN && sudo chmod 2770 DİZİN`, sonra `./watchover.sh start` |
| `the Podman socket … is not there` / `needs root` | `sudo systemctl enable --now podman.socket`; Podman'da komutlar `sudo ./watchover.sh …` ile |
| Podman'da konteynerler birbirini bulamıyor (`postgres` adı çözülmüyor) | Ağ arka ucu CNI: `podman info --format '{{.Host.NetworkBackend}}'`; netavark + aardvark-dns'e geçin (bölüm 1b, Podman ağı) |
| Yeniden başlatmadan sonra Podman'da Watchover kalkmadı | `systemctl status watchover-stack.service`; kapalıysa `sudo ./watchover.sh autostart on` |
| LLM sorusu sorulunca ekranlar yavaşlıyor | `./watchover.sh profile cpu8` (LLM'e CPU payı) ve `./watchover.sh start` |
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

**Öğrenme ve güncellemeler internetsiz sunucuda.** Çalışma zamanında hiçbir özellik internete bağlı değildir:

- *Otomatik öğrenme (LoRA eğitimi)* sunucuda çalışır: eğitici imajı ve taban ağırlıkları (Qwen2.5 1.5B/3B/7B, Hugging Face önbelleği) pakete girer; `bundle` bunu `llm prepare` ile kendisi önbelleğe alır. Eğitim, aday model ve `watchover-ops` yayını tamamen yerel Ollama üzerinde biter. Çevrimdışı işaret varken eğitici Hugging Face'e hiç çıkmaz (`HF_HUB_OFFLINE=1`), önbellekteki taban yoksa eğitim açık hata verir, asla asılı kalmaz. Farklı bir taban seçecekseniz (`Sistem › Eğitim tabanı`) internetli makinede `./watchover.sh llm prepare qwen2.5:3b-instruct` ve yeni paket.
- *Sohbet, inceleme, hafıza (RAG), gömme (bge-m3), zafiyet taraması (gömülü danışma listesi), kural/parser öğrenme* yerel çalışır.
- *Model güncellemeleri* internetli kurulumda da otomatik değildir: Ollama modelleri yalnız `llm on` / `llm pull` ile iner, Watchover kendi kendine model çekmez. Yeni bir taban model ya da yeni Watchover sürümü istendiğinde internetli makinede indirilir ve yeni paketle taşınır; sunucudaki eğitilmiş `watchover-ops` modeli ve verileri korunur.

**Güncelleme.** Yeni sürümde internetli makinede `git pull && ./watchover.sh update && ./watchover.sh bundle`, sunucuda kaynak kodu aynı `tar xf … --strip-components=1 source` ile üzerine açıp `./watchover.sh unbundle <dosya> && ./watchover.sh update`. Veri hacimleri (`watchover-data`, `watchover-pg`) ve `.env` dokunulmaz. Çevrimdışı işareti `.env` içindeki `WATCHOVER_OFFLINE=1` satırıdır; sunucu ileride internete çıkarsa satırı silmek yeterlidir.
