# Watchover · Kurumsal Docker kurulum rehberi

Watchover şirket içindeki **tek bir sunucuda, tamamen Docker'da** çalışır. Kurulum paketi bu klasördür (`watchover.zip`): imaj sunucuda kaynak koddan derlenir (Node aşaması React arayüzünü, Python aşaması motoru kurar); GitHub, imaj kaydı ya da sunucuda Python gerekmez. Üç konteyner vardır: **dashboard** (React arayüzü + HTTP API 8501, canlı alıcı 8600, konteynerin kendini izleyen ajanı), **postgres** (ürün veritabanı, PostgreSQL 16) ve isteğe bağlı **mcp** (kendi MCP sunucumuz, 8765). Eski Streamlit arayüzü `legacy` profiliyle 8502'de bir süre daha açılabilir. Kod imajın içindedir; ayarlar, sırlar, anlık görüntüler ve loglar `watchover-data` biriminde, veritabanı `watchover-pg` biriminde durur. Güncelleme, yeni paketi klasörün üstüne açıp `./watchover.sh update` demektir; birimlere dokunulmaz. Red Hat sunucuda motor Docker CE ya da Podman olabilir (bölüm 1b); aynı ürünün OpenShift kurulumu `docs/KURULUM_OPENSHIFT.md`'dedir, ikisinin karşılaştırması bölüm 1c.

## 0. RHEL 9: tek komutla kurulum (önerilen)

Hedef sürümler **RHEL 9.6, 9.7 ve 9.8**; Rocky Linux ve AlmaLinux 9.6+ aynı şekilde kurulur (çıktıda "RHEL 9 uyumlu"). Daha eski 9.x'te uyarı verip devam eder (önce `sudo dnf -y update` önerilir), RHEL 8 ve 10'u açık bir mesajla reddeder. Motor rootful **Podman 5.x** (AppStream) + crun, netavark / aardvark-dns, container-selinux; SELinux Enforcing kalır. İşlemci x86_64 ya da aarch64.

```bash
unzip -q watchover-<sürüm>.zip && cd watchover-<sürüm>
sudo ./install-rhel.sh          # soru sormaz, parametre gerekmez (exec biti yoksa: sudo bash install-rhel.sh)
sudo ./install-rhel.sh --check  # isteğe bağlı: yalnız ön kontrol, hiçbir şeyi değiştirmez
```

Betik sırayla şunları yapar; her adım tekrarlanabilir: yeniden çalıştırmak **onarır**, daha yeni bir sürüm klasöründen çalıştırmak **yükseltir** (önce yedek alır).

1. **Ön kontrol** (hiçbir şey değiştirilmeden; sorunların hepsi listelenip çıkılır): işletim sistemi sürümü, mimari, en az 2 CPU / 4 GB RAM (kurulu bellek; çekirdeğin ayırdığı kısım düşülmez, 4096 MB sanal makine yeter) / konteyner deposunda 20 GB boş (önerilen 4 CPU / 8 GB / 40 GB; yerel LLM için 16 GB RAM ve 30 GB), SELinux, saat eşitleme, cgroup v2, 80 / 443 / 8600 / 5433 portları (dolu ise hangi servisin tuttuğu), paket depoları (`dnf repolist`; RHEL'de abonelik), aşağıdaki adreslere erişim, `/var/lib/containers`'ın diski (NFS / CIFS ve HDD uyarılır: **yerel SSD olmalı**), sanal makine ve AVX2 notu.
2. **Paketler** `scripts/rhel-prepare.sh --engine podman` ile: podman, container-selinux, crun, netavark, aardvark-dns, openssl (edge sertifikası sunucuda üretilir), Docker deposundan yalnız `docker-compose-plugin` (Docker motoru kurulmaz), `podman.socket`, chrony.
3. **Dosyalar `/opt/watchover`'a** kopyalanır (root:root, SELinux varsayılan etiketi): sabit yol systemd birimlerini, güncelleyiciyi ve desteği basitleştirir; ev dizininden çalışan birimler SELinux'ta engellenir. `.env`, `certs/`, `backups/`, `.releases/` korunur; yeni sürümde olmayan eski dosyalar silinir. Sonraki her işlem: `sudo watchover …` (= `sudo /opt/watchover/watchover.sh …`).
4. **`watchover.sh install --production`**: `.env` (0600, rastgele parolalar), makineye göre boyutlandırma, imajlar, PostgreSQL kendi biriminde ve yalnız `127.0.0.1:5433`.
5. **HTTPS**: `./watchover.sh edge on <fqdn>,<kısa ad>,<ip>,localhost` (Watchover iç CA'sı; `localhost` SSH tüneli için); 8501 yayınlanmaz. Rootless Podman motoru kullanılıyorsa 80 / 443 için `net.ipv4.ip_unprivileged_port_start=80` kalıcı yapılır (`/etc/sysctl.d/90-watchover.conf`); betiğin kurduğu rootful Podman'da gerekmez. Tarayıcılara bir kez yüklenecek CA: `/opt/watchover/certs/watchover-root.crt` (yeniden almak: `sudo watchover edge cert`). Şirket sertifikası: `certs/watchover.crt` + `certs/watchover.key` koyup `sudo watchover edge on` ya da betiği yeniden çalıştırın.
6. **Uygulama içi güncelleme** açık, **açılışta başlama** (`watchover-stack.service`), **gecelik yedek** 02:15, son 14 kopya (`watchover-backup.timer`).
7. **firewalld** (varsayılan rotanın arayüzünün bölgesi): 443 (arayüz ve ajanlar, HTTPS), 80 (https'e yönlendirme, isteğe bağlı), 8600 yalnız ajanların düz http gönderimi açıksa (varsayılan açık; kapatmak: `.env`'de `WATCHOVER_LIVE_PORT=127.0.0.1:8600`, sonra betiği yeniden çalıştırın); 8501 kapatılır. `sudo watchover mcp on` 8765'i, `sudo watchover otlp on` 4317 / 4318'i açar, `off` kapatır. firewalld kapalıysa açılmaz, uyarı verilir.
8. **Yerel LLM**: 16 GB+ RAM, 30 GB+ boş alan ve model kaydına erişim varsa paketli CPU Ollama arka planda kurulur (16-47 GB: qwen2.5 3B + bge-m3; 48 GB+: 7B + 3B + bge-m3; kayıt `/var/log/watchover/llm-setup.log`). Kurulumu bekletmez, tanımlı bir LLM bağlantısına dokunmaz; koşul yoksa atlanır ve nedeni yazılır.
9. **Özet**: açılacak adresler, CA dosyası, ilk giriş (yerleşik yönetici hesabı, ilk girişte yeni parola zorunlu; parola konsoldan da belirlenebilir: `sudo watchover user password <e-posta>`), ajan kurulum komutu (arayüzde Ajanlar paneli), açılan özellikler, devreye alma listesi ve tam kayıt: `/var/log/watchover/install-<zaman>.log`.

**Güvenlik duvarı ekibine çıkış listesi** (HTTPS 443/tcp; ön kontrol ulaşamadığı adresi adıyla yazar):

| Adres | Ne için |
|---|---|
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`, `docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com` | temel imajlar (python, node, golang, alpine, postgres, edge, docker, ollama); `registries.conf`'ta docker.io aynası varsa yalnız ayna |
| `deb.debian.org`, `dl-cdn.alpinelinux.org` | imaj derlemesinde apt / apk |
| `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` | Python ve arayüz paketleri |
| `download.docker.com` | yalnız Compose yoksa (`docker-compose-plugin`); son çare `github.com` + `release-assets.githubusercontent.com` (sha256 doğrulamalı) |
| `cdn.redhat.com` ya da Satellite / yerel ayna | yalnız Podman paketleri kurulu değilse (Rocky / Alma: kendi aynaları) |
| `registry.ollama.ai`, `dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com`, `download.pytorch.org`, `huggingface.co` | yalnız yerel LLM; ulaşılamazsa LLM atlanır, kurulum sürer |

**Yük dengeleyici arkasında, masaüstü olmadan.** Sağlık kontrolü: `GET /healthz` → 200 (HTTPS 443 ya da HTTP 80). Yük dengeleyici `X-Forwarded-For` ve `X-Forwarded-Proto` başlıklarını iletmeli ya da TCP geçişi (TLS passthrough) kullanmalı; dengeleyicinin adresi güvenilir yapılır: `sudo watchover edge lb <IP / CIDR>` (loglarda gerçek istemci adresi; ayrıntı bölüm 8.2); geçişte tarayıcının gördüğü ad sertifikada olmalı: `sudo ./install-rhel.sh --host watchover.firma.com` (LB adı ana adres olur). Sunucuda masaüstü gerekmez: dizüstündeki tarayıcıdan LB VIP'i ya da sunucu adresiyle açın, ya da SSH tüneli: `ssh -L 8443:localhost:443 kullanici@sunucu` → `https://localhost:8443/` (sertifikada `localhost` da vardır).

**Kurumsal proxy.** `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` ortamda ya da `/etc/environment`'ta tanımlıysa (sudo ortamı siler, betik dosyayı da okur) kendiliğinden ayarlanır: `/etc/dnf/dnf.conf` (`proxy=`), boşsa `subscription-manager` proxy'si, Podman çekmeleri (`/etc/containers/containers.conf.d/watchover-proxy.conf` + `podman.service` drop-in; çalışan konteynerlere proxy geçmez), imaj derlemeleri (`.env`'de `WATCHOVER_HTTPS_PROXY`, derlemeye build-arg olarak) ve Ollama / eğitici. `NO_PROXY`'ye iç servis adları, sunucunun adları ve IP'leri eklenir. SSL denetimi yapan proxy'de yukarıdaki adresler denetimden muaf olmalı: derleme konteynerleri şirket CA'sını tanımaz.

**İnternetsiz (air-gapped).** Aynı işlemci mimarisinde internetli bir makinede `./watchover.sh install` (yerel LLM de gidecekse `./watchover.sh llm on`) ve `./watchover.sh bundle` → `watchover-bundle-<sürüm>.tar` (imajlar, Ollama modelleri, Compose ikilisi, kaynak). Zip ile birlikte sunucuya kopyalayın; tar dosyası betiğin yanında ya da bir üst klasördeyse kendiliğinden kullanılır: internete çıkılmaz, derleme yapılmaz. Podman paketleri yine RHEL deposundan gelir (Satellite / yerel ayna / DVD deposu). Pakette Compose yoksa `docker-compose-linux-<x86_64|aarch64>` dosyasını betiğin yanına koyun.

**Sanal makinede deneme.** RHEL 9.8 (Red Hat Developer aboneliğiyle ücretsiz ISO; kurulumdan sonra `sudo subscription-manager register`) ya da Rocky Linux / AlmaLinux 9.6+ minimal kurulum; 4 vCPU, 8 GB RAM (yerel LLM denemesi için 16 GB), 60 GB disk. Apple Silicon Mac'te (UTM / Parallels / VMware Fusion) aarch64 ISO'yu seçin; x86 sanallaştırmada CPU tipini "host" verin (AVX2 yoksa yerel LLM çok yavaş, betik not düşer). Zip'i kopyalayıp `sudo ./install-rhel.sh`; tarayıcıda `https://<VM IP>`, CA dosyasını Mac'te Anahtar Zinciri › Sistem'e alıp "Her zaman güven" yapın. Kontrol: `sudo watchover status`, `sudo watchover doctor`, `sudo watchover checklist`, SELinux engeli olmadığını görmek için `sudo ausearch -m avc -ts recent`.

Eski `scripts/install.sh` ve `scripts/install_linux.sh` emekli Streamlit arayüzünü kurar; kullanılmaz (dosyaların başında not var).

## 1. Sunucu gereksinimleri

Sunucuya kurulacak **tek bağımlılık konteyner motorudur**: Docker (Engine 24+ ve Compose v2) ya da RHEL 9.6+'da Podman 5.x ile Compose v2 (bölüm 0 / 1b). Python, pip, PostgreSQL ana makineye kurulmaz; hepsi imajların içindedir.

| | |
|---|---|
| İşletim sistemi | RHEL 9.6 / 9.7 / 9.8 ve Rocky / Alma 9.6+ (hedef sürümler; tek komut: bölüm 0), Ubuntu 22.04 / 24.04, Debian 12, RHEL 8.8 (Docker CE, bölüm 1b). Deneme için macOS / Windows'ta Docker Desktop da çalışır |
| Kaynak | En az 2 CPU / 4 GB RAM / 20 GB disk. Günlük GB'larca log ve büyük SAP arşivleri için 4 CPU / 8 GB |
| Ağ | Kullanıcılar için 8501, izlenen sunuculardan gelen ajanlar için 8600 (MCP açılırsa 8765; edge açıkken yalnız 443). İmaj derlenirken bir kez PyPI ve Docker Hub'a çıkış gerekir; kapalı ağ için bölüm 9 |

Docker kurulumu (Linux):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # sonra oturumu kapatıp açın
docker compose version             # v2.x görünmeli
```

## 1b. Red Hat Enterprise Linux 8.8 / 9 üretim sunucusu (Docker CE ya da Podman)

RHEL'de Watchover iki motordan biriyle çalışır; compose dosyası, komutlar, veriler ve yedekler ikisinde de aynıdır. `watchover.sh` motoru kendisi bulur (Docker CLI varsa Docker, yoksa Podman) ve `.env`'ye `WATCHOVER_ENGINE=docker|podman` olarak yazar; elle değiştirmek için bu satır yeterlidir. Ana makineye başka bir şey kurulmaz: Python, PostgreSQL, Ollama, nginx imajların içindedir (edge sertifikası için `openssl` RHEL'de zaten vardır).

| | Docker CE (varsayılan) | Podman (Red Hat'in kendi motoru) |
|---|---|---|
| Paketler | `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin` (Docker'ın RHEL deposu; Podman / buildah / runc kaldırılır, çakışır) | `podman`, `container-selinux`, `netavark`, `aardvark-dns` (RHEL depoları) + yalnız `docker-compose-plugin` (tek Compose v2 ikilisi; Docker motoru kurulmaz, Podman'la çakışmaz) |
| Nasıl çalışır | Docker servisi | `podman.socket` (Docker uyumlu API, rootful) + Compose v2 `DOCKER_HOST=unix:///run/podman/podman.sock` ile; imajlar `podman build` ile Docker'ın vereceği adlarla derlenir |
| Komutlar | `./watchover.sh …` (kullanıcı `docker` grubunda) | `sudo ./watchover.sh …` (soket yalnız root'a açık) |
| Açılışta başlama | Docker servisi + `restart: unless-stopped` | `watchover-stack.service` (`install` kendisi açar; `./watchover.sh autostart on` / `off`) |
| SELinux | `enforcing` kalır; Docker CE konteynerleri SELinux dışında çalıştırır | `enforcing` kalır; konteynerler `container_t`, bind mount'lar `:z` ile etiketlenir |
| Hazırlık | `sudo bash scripts/rhel-prepare.sh --user ops [--mcp]` | `sudo bash scripts/rhel-prepare.sh --engine podman [--mcp]` |

Hazırlık betiği her iki motorda da firewalld'de 8501/tcp (arayüz), 8600/tcp (ajanlar), 443/tcp (edge TLS), `--mcp` ile 8765/tcp ve `--otlp` ile 4317-4318/tcp (OpenTelemetry) açar, chrony'yi açar; `--check` yalnız ne yapacağını yazar. Diğer gerekenler: `git` (ya da paket dosyası), `python3`, `tar`, `gzip`, `openssl`, `curl` (RHEL tabanında vardır: `dnf install git python3 tar gzip openssl curl`).

Örnek: 8 CPU / 64 GB / 200 GB sunucu, ham loglar ayrı disklerde:

```bash
sudo dnf -y install git
sudo git clone https://github.com/Bannercheck/Operation_Monitoring_tool.git /opt/watchover-docker   # internetsiz: bundle'ı açın (bölüm "İnternetsiz sunucu")
cd /opt/watchover-docker
sudo bash scripts/rhel-prepare.sh --check                 # ne yapacağını gösterir, değişiklik yapmaz (Podman: --engine podman ekleyin)
sudo bash scripts/rhel-prepare.sh --user ops --mcp        # Docker CE; Podman için:  sudo bash scripts/rhel-prepare.sh --engine podman --mcp
# (Docker: oturumu kapatıp açın, docker grubu)
./watchover.sh install --production                       # Podman: sudo ./watchover.sh install --production
./watchover.sh storage set --hot /srv/watchover/hot --cold /srv/watchover/arsiv   # ham log yerleri ELLE (aşağıda "Ham log yerleri")
sudo ./watchover.sh backup-timer on 02:15                 # gecelik yedek (systemd timer)
./watchover.sh edge on watchover.sirket.local             # TLS: ./certs/watchover.crt + watchover.key şirket sertifikası ya da iç CA
./watchover.sh llm on                                     # yerel LLM istenirse (internetli: modeller iner; internetsiz: paketten gelir)
```

**Üretim kurulumu (`install --production`).** Normal kurulumun aynısı, ek olarak `.env`'ye `WATCHOVER_PRODUCTION=1` yazar ve sonunda devreye alma listesini basar; `./watchover.sh checklist` aynı listeyi istendiğinde, yapılanları işaretleyerek yeniden gösterir:

- gecelik yedek açık mı (`backup-timer on`), yedek klasörü başka bir makineye kopyalanıyor mu,
- açılışta başlama (Podman'da `watchover-stack.service`; Docker'da servis zaten açar),
- yönetici parolası: `admin@watchover.local` ilk girişte parolasını değiştirir ya da `./watchover.sh user password admin@watchover.local`; kişisel yönetici hesapları (`user add … --admin`),
- ham log yerleri elle verildi mi (`storage set --hot DİZİN --cold DİZİN`; verilmediyse "not set" yazar), arşiv süresi (varsayılan 365 gün) ve yıllık ihtiyaç (`./watchover.sh storage show`),
- TLS (edge) açık mı, güvenlik duvarında yalnız gereken portlar mı.

**Ham log yerleri (sıcak ve arşiv) elle verilir.** Sunucu hiçbir diski kendiliğinden seçmez ya da önermez: sıcak loglar (son `live_retention_h` saat, varsayılan 48; satır başına ~65-90 bayt) ve arşiv (sıkıştırılmış, ~7-8 bayt/satır, **varsayılan 365 gün**, denetim için) için dizini yönetici verir: "buraya yaz". Verilmezse loglar veri biriminin içinde kalır (`/data/logs/hot`, `/data/logs/cold`; eski kurulumlar bozulmaz) ve hem `./watchover.sh storage show` hem Sistem › Depolama bunu **"ayarlanmadı / NOT SET"** diye gösterir.

```bash
# önce diskleri bağlayın (örnek LVM: hızlı disk sıcak, ucuz ve büyük disk arşiv)
sudo mkfs.xfs /dev/vg_fast/lv_hot && sudo mkfs.xfs /dev/vg_big/lv_arsiv
sudo mkdir -p /srv/watchover/hot /srv/watchover/arsiv
echo '/dev/vg_fast/lv_hot  /srv/watchover/hot   xfs defaults,noatime 0 0' | sudo tee -a /etc/fstab
echo '/dev/vg_big/lv_arsiv /srv/watchover/arsiv xfs defaults,noatime 0 0' | sudo tee -a /etc/fstab
sudo mount -a

./watchover.sh storage set --hot /srv/watchover/hot --cold /srv/watchover/arsiv   # yerleri ver, var olan günleri taşı
./watchover.sh storage set --cold-days 365 --hot-hours 48                          # yalnız süreler (yer değişmeden)
./watchover.sh storage show                                                        # yerler, süreler, boyutlar, boş alan, yıllık ihtiyaç
```

`storage set` şunları yapar (Podman'da `sudo ./watchover.sh storage set …`):

1. **Denetim**: mutlak yol, yalnız harf / rakam / `. _ - /`; `/`, `/etc`, `/var/lib/docker` gibi sistem dizinleri, kurulum klasörünün içi, sıcak ile arşivin aynı ya da iç içe olması reddedilir. Dizin yoksa açılır, sahibi `10001:0`, kipi `2770` yapılır (yalnız o dizin, `-R` yok); içinde Watchover'a ait olmayan dosyalar varsa (ör. birinin ev dizini) **hiçbir şey değiştirilmeden** reddedilir. Hedefte yer yoksa reddedilir.
2. **Kopyalama + doğrulama** (Watchover çalışırken): var olan gün dosyaları ve arşivler geçici adla kopyalanır; okunan baytların sha256'sı yazılanınkiyle aynı değilse durur.
3. **Geçiş**: log yazan konteynerler kısa süre durur, son geçişte yalnız değişen dosyalar kopyalanır, `.env`'ye `WATCHOVER_LOG_HOT_PATH` / `WATCHOVER_LOG_COLD_PATH` yazılır, konteynerler yeniden oluşturulur, `/api/health` beklenir.
4. **Eski kopyalar** yalnız yeni yer çalıştıktan sonra silinir (`--keep-old` ile kalır). Yeni yer ayağa kalkmazsa eski yollar geri yazılır ve eski veriye dokunulmamış olur.

Uygulama içinden: **Sistem › Depolama › Yerleri ve süreleri ayarla** (sıcak dizin, arşiv dizini, sıcak saat, arşiv gün). Güncelleyici açıksa (bölüm 3b) istek ona gider ve aynı `storage set` adımları (hazırlık, kopyalama + sha256, geçiş, başlatma, sağlık, eski kopyalar) kartta izlenir; `sys.update` yetkisi ve taze kimlik doğrulaması (adım doğrulaması) gerekir, işlem güvenlik kaydına (`sys.storage`) yazılır. Güncelleyici kapalıysa kart çalıştırılacak komutu gösterir. Yalnız süreler değişiyorsa hemen kaydedilir.

**Boyutlandırma.** Arşiv satır başına ~7-8 bayt tutar; Sistem › Depolama ve `storage show` ölçülen hızdan yıllık ihtiyacı yazar ve verilen alanın yetip yetmediğini söyler.

| Dakikada satır | Arşiv / gün | Arşiv / 365 gün | Sıcak (48 saat, ~75 B/satır) |
|---|---|---|---|
| 10 bin | ~0,11 GB | ~40 GB | ~2,2 GB |
| 50 bin | ~0,55 GB | ~200 GB | ~11 GB |
| 100 bin | ~1,1 GB | ~400 GB | ~22 GB |
| 300 bin | ~3,3 GB | ~1,2 TB | ~65 GB |

Arşiv diskine bu tablonun en az %25 fazlasını verin (uyarılar %80'de başlar).

**Disk dolarsa (veri kaybı yok).** Saklama süresi dolmamış bir arşiv **hiçbir boyut sınırı yüzünden silinmez**; yalnız `cold_retention_days` siler ve silişi manifestoya `expire` olarak yazar. `cold_max_gb` artık bir bütçedir: aşıldığında uyarır, silmez. Arşiv diski (ya da bütçe) **%80**'de uyarı, **%90**'da kritik uyarı verir; arşiv diski bir sonraki günü alamayacak durumdaysa (tahmini arşiv boyu + 1 GB / diskin %1'i kadar pay) **arşivleme durur**: gün sıcak diskte kalır, kritik `cold_full` uyarısı gider ve yer açılınca arşivleme kendiliğinden sürer. Sıcak disk de %80 / %90'da uyarır: arşivleme durduğunda günler orada birikir ve sıcak disk de tamamen dolarsa yeni ham satırlar yazılamaz (sayaçlarda reddedilen olarak görünür), bu yüzden kritik uyarıda arşiv diskine yer açmak ya da arşivi `storage set --cold` ile büyük bir diske taşımak gerekir. Uyarılar Sistem › Depolama'da görünür ve bildirim kanallarıyla gider: `log_storage` koşullu bir kural yoksa kritik olanlar yöneticilere e-postayla (ve tanımlıysa webhook'a) gider; Bildirimler'de `log_storage` koşullu kural kurarak alıcıları ve eşiği (80 = uyarılar da, 90 = yalnız kritik) siz belirlersiniz.

**Denetim için doğrulanabilir arşiv.** Her gün arşivlendiğinde dosya **mühürlenir** (`0440`, salt okunur) ve arşiv dizinindeki `MANIFEST.jsonl`'a bir kayıt eklenir: `seq`, `op` (seal / reseal / register / expire / remove), gün, dosya, **sha256**, bayt, satır sayısı, ilk / son zaman damgası ve **bir önceki kaydın karması** (`prev`); kaydın kendi karması (`hash`) diğer alanların sıralı JSON'unun sha256'sıdır. `MANIFEST.head` son kaydı tutar, son `seq` sıcak diskte de saklanır: sondan kesilmiş bir manifesto da fark edilir. Manifestodan önce var olan arşivler ilk çalışmada "registered later" notuyla eklenir; sonradan bilinmeyen bir arşiv sessizce eklenmez, "unregistered" sorunu olarak raporlanır.

```bash
docker compose exec dashboard python -m watchover.logtiers verify                          # tüm arşiv: zincir + her dosyanın sha256'sı
docker compose exec dashboard python -m watchover.logtiers verify --from 2026-01-01 --to 2026-03-31
docker compose exec dashboard python -m watchover.logtiers export --from 2026-01-01 --to 2026-03-31 --out /data/exports
#   --format archive (varsayılan): arşiv dosyaları + MANIFEST.jsonl + SLICE.json + SHA256SUMS + README.txt tek .tar
#   --format ndjson: o günlerin saklanan satırları gzip NDJSON
```

Doğrulama silinen (`missing`), değiştirilen (`altered`), sırası bozulan ya da araya giren / çıkarılan (`chain_broken`, `seq_order`, `entry_altered`), sondan kesilen (`truncated`) ve kaydı olmayan (`unregistered`) günleri bulur; her tam doğrulama arşiv dizinindeki `VERIFY.jsonl`'a yazılır, saatlik iş boyutları ve zinciri her saat denetler (sorun = kritik uyarı). Arayüzde: **Sistem › Depolama › Denetim** (`sys.maint` yetkisi; dışa aktarım ve arama için taze kimlik doğrulaması): *Arşivi doğrula*, *Denetçi için dışa aktar* (tarih aralığı, .tar ya da NDJSON) ve *Denetim araması*. Her dışa aktarımın yanında kendi `.sha256` dosyası vardır, indirme 10 dakikalık tek dosyalık bir bağlantıyla yapılır; dışa aktarımlar veri biriminde `/data/exports` altında 7 gün (en çok 10 dosya) tutulur. Denetçi paketi açınca: `sha256sum -c SHA256SUMS` ve `python -m watchover.logtiers verify --dir archive --manifest MANIFEST.jsonl --from … --to …`.

**Aylar süren aramalar.** Log gezgini arşivi istek başına 1,5 saniye tarar ve en çok 40 istek kendiliğinden devam eder; aralık daha uzunsa durduğu yeri gösterir ve **Denetim araması**na bağlantı verir. Denetim araması aynı sorgu dilini kullanır, sıcak + arşivi süre sınırı olmadan arka planda tarar (ilerleme yüzdesi, durdurma), sonucu indirilebilir NDJSON olarak yazar; aynı anda bir denetim işi çalışır.

- SELinux `enforcing` kalır: bağlamalar `:z` taşır (`ls -Zd /srv/watchover/hot`). Docker CE varsayılanında etiket gerekmez.
- Konteynerler UID 10001 / GID 0 ile yazar; `watchover.sh` her başlatmada dizinin bu kullanıcıya yazılabilir olduğunu denetler, değilse çalıştırılacak `chown` / `chmod` komutunu yazarak durur.
- Log diskleri `./watchover.sh backup` içinde değildir: arşivi LVM / depolama anlık görüntüsüyle ya da düzenli `export` ile koruyun (OpenShift'teki VolumeSnapshot karşılığı).

**Üretim notları (RHEL)**

- **Disk**: imajlar, PostgreSQL, canlı spool ve Ollama modelleri Docker'da `/var/lib/docker`, Podman'da `/var/lib/containers` altındadır. 200 GB'lık sunucuda ham loglar ayrı disklere alınırsa kalan alan bunlara yeter (LLM ile ≥ 100 GB bırakın); betik `/var/lib`'de 40 GB'ın altında uyarır.
- **Kaynak**: `watchover.sh` her install / update / start'ta CPU ve belleği ölçüp payları dağıtır (bölüm 2k); 8 CPU / 64 GB sunucuda sonuç `cpu8` profiliyle aynıdır. 100+ sunucu / 2.000 olay/sn için 16–32 CPU, 64 GB öneri (bkz. "Ölçek").
- **Abonelik / depo**: `dnf` için RHEL aboneliği ya da Satellite gerekir; Docker deposu `download.docker.com/linux/rhel` (Podman'da yalnız `docker-compose-plugin` için). Erişim yoksa: Docker RPM'leri internetli makinede `dnf download --resolve …` ile indirilip `dnf install ./*.rpm`, betik `--no-docker` ile; Podman'da Compose ikilisi (`docker-compose-linux-x86_64`, github.com/docker/compose/releases) `/usr/local/lib/docker/cli-plugins/docker-compose` olarak kopyalanır.
- **Yeniden başlatma**: Docker servisi `enable` edilir, konteynerler `restart: unless-stopped` ile kendiliğinden kalkar, `live-restore` Docker güncellemesinde konteynerleri düşürmez. Podman'da konteynerleri yeniden başlatan bir servis yoktur: `install` `watchover-stack.service`'i açar (`./watchover.sh autostart on|off`), kapanışta yığın düzgünce durdurulur.
- **Yedek**: `sudo ./watchover.sh backup-timer on [SS:DD]` (bölüm 10); `backups/` (ya da `.env` `WATCHOVER_BACKUP_DIR`) merkezi yedeğe alınır.
- **Podman ağı**: konteynerler birbirini adla bulur (`postgres`, `receiver`); bunun için netavark + aardvark-dns gerekir (RHEL 9 varsayılanı). RHEL 8'de eski CNI arka ucu görülürse betik uyarır: `/etc/containers/containers.conf` `[network] network_backend = "netavark"` ve yeni sunucuda `podman system reset --force`.
- **İzlenen RHEL sunucular**: hiçbir paket gerekmez (`python3` de): `curl -fsSL http://<watchover>:8600/agent/install.sh | sudo bash -s -- --url … --enroll-key …` tek Go ikili dosyasını (`wo-agent`, amd64 / arm64) alıcıdan indirir, sha256'sını doğrular ve systemd servisini kurar; ikili dosya eylem ajanınınkiyle aynıdır (`/usr/local/lib/watchover`), ayar, kuyruk ve konumlar `/opt/watchover-agent` altındadır.

## 1c. RHEL sunucu vs OpenShift: aynı ürün, iki kurulum

Kurulum günü hedef hangisi olursa olsun ikisi de hazırdır: aynı imajlar (`Dockerfile`, `Dockerfile.receiver`), aynı veritabanı şeması, aynı `/logs/hot` ve `/logs/cold` yolları, aynı üretim kipi. RHEL sunucuda Docker CE ya da Podman (`docs/KURULUM_DOCKER.md` bölüm 1b), OpenShift'te Helm chart'ı (`docs/KURULUM_OPENSHIFT.md`). Tablo iki rehberde de aynıdır; "Fark" yazan yerde iki kurulum gerçekten ayrışır.

| Konu | RHEL sunucu (Docker CE ya da Podman) | OpenShift |
|---|---|---|
| Kurulum | `sudo bash scripts/rhel-prepare.sh [--engine podman] [--mcp]`, sonra `./watchover.sh install --production` (Podman: `sudo` ile). İmajlar sunucuda kaynak koddan derlenir; kapalı ağda `bundle` / `unbundle` | İmajlar `podman build` + `podman push` ya da `oc new-build` ile kayıt defterine, sonra `helm install … -f degerler.yaml`; Helm'siz: `oc apply -f deploy/openshift/rendered/watchover.yaml` |
| Yükseltme | `./watchover.sh upgrade` (git) ya da yeni paket + `./watchover.sh update`; veri birimleri ve log diskleri yerinde kalır | Yeni etiketli imajlar + `helm upgrade … --set image.tag=X`; pod `Recreate` ile durup yenisi açılır, PVC'ler yerinde kalır |
| Geri alma | Önceki sürümün kaynak ağacı (eski commit ya da paket) + `./watchover.sh update`. Göçler ileriye dönüktür: veritabanı için yükseltmeden önce alınan yedekle `./watchover.sh restore` | `helm rollback watchover <REVİZYON>` (imaj etiketi dahil). Göçler ileriye dönüktür: veritabanı için yükseltmeden önce alınan dump ile `pg_restore` |
| Otomatik boyutlandırma | `watchover.sh` her install / update / start'ta CPU ve belleği ölçüp konteyner sınırlarını yazar (8 CPU / 64 GB = `cpu8` profili); sabit değer: `profile cpu8` / `cpu16` | Fark: ölçme yoktur, istek / sınır değer dosyasındadır (`values.yaml`, tam performans `values-performance.yaml`). Ortak: alıcı kendi cgroup sınırını okuyup Go'nun CPU sayısını (GOMAXPROCS) ve bellek hedefini ona göre ayarlar |
| Ayrı sıcak / soğuk disk | `./watchover.sh storage set --hot DİZİN --cold DİZİN` (ya da Sistem › Depolama, güncelleyiciyle): `.env`'de `WATCHOVER_LOG_HOT_PATH` / `_COLD_PATH`, `/logs/hot`, `/logs/cold` olarak bağlanır, var olan günler sha256 ile doğrulanarak taşınır. Verilmezse veri biriminde ("ayarlanmadı") | Her zaman ayrı PVC: `logs-hot`, `logs-cold`, aynı yollar; boyut ve süre Helm değerleriyle (`persistence.logsCold.size`, `retention.coldRetentionDays`), pod'dan değiştirilmez |
| PostgreSQL | `postgres:16-alpine` konteyneri (birim `watchover-pg`, parola `.env`'de üretilir) ya da `.env` `DATABASE_URL` ile kurumun sunucusu | `rhel9/postgresql-16` StatefulSet (parola Secret'ta üretilir, korunur) ya da `postgresql.enabled: false` + `externalDatabase.existingSecret` |
| LLM (yalnız CPU) | `./watchover.sh llm on`: Ollama + eğitim konteyneri, CPU payı otomatik; modeller indirilir (kapalı ağda paketten gelir) | `llm.enabled: true`: Ollama pod'u, tek model, `llm.lock`, indirdikten sonra `llm.ollama.allowEgress: false`; eğitim `trainer.enabled` |
| MCP sunucusu | `./watchover.sh mcp on`: aynı imaj, `http://<sunucu>:8765/mcp`; edge açıkken nginx `https://<ad>/mcp` yolunu bu konteynere (TLS), `https://<ad>/api/mcp` yolunu arayüzün kendi MCP ucuna gönderir. Anahtarlar: arayüzde adlı, kapsamlı anahtarlar (Ayarlar › Bağlantılar › MCP); `.env` `MCP_API_KEY` kurulum çapında okuma anahtarıdır (ilk kurulumda üretilir; `MCP_API_KEY_WRITE=1` yazma da verir). Canlı ürün araçları arayüze iletilir (`WATCHOVER_API_URL=http://watchover:8501`); dosyalar `./datasets` | `mcp.enabled: true`: ayrı Deployment + Route (`https://<mcp route>/mcp`, edge TLS) + NetworkPolicy; arayüzün kendi ucu `https://<ui route>/api/mcp`. Anahtarlar: aynı adlı anahtarlar; kurulum anahtarı Secret'ta (üretilir, upgrade'de korunur, arayüz pod'una da verilir) ya da `mcp.existingSecret`, yazma `mcp.installKeyWrite`. Canlı araçlar `WATCHOVER_API_URL=http://watchover:8501` ile iletilir; dosyalar kendi PVC'sinde (`/datasets`, `oc cp`). Fark: OpenShift'te MCP pod'u `/data` ve log disklerini görmez, canlı veriye arayüz üzerinden ulaşır; RHEL'de 8765 ayrıca düz http açıktır (`WATCHOVER_MCP_PORT=127.0.0.1:8765` ile yalnız yerel) |
| Yalnız gerçek veri | Simülatör kaldırıldı (ayar yok); eski sürümden kalan demo verisi ilk açılışta kendiliğinden, ya da `./watchover.sh demo-clean`; tüm analiz verisini sıfırlama: `./watchover.sh reset-data` | Aynı; kalan demo verisi ilk açılışta, ya da Sistem › Veri temizliği; sıfırlama: Sistem › Veri temizliği ya da `oc exec … python -m watchover.demo reset --yes` |
| Yedek | `./watchover.sh backup-timer on [SS:DD]`: her gece PostgreSQL dökümü + veri birimi (ham loglar hariç) + `.env` → `backups/`, son 14 (systemd timer; systemd yoksa cron). Geri: `./watchover.sh restore DOSYA`. Log diskleri: LVM / depolama anlık görüntüsü | `backup.enabled: true`: her gece `pg_dump` CronJob'u + kendi PVC'si, `keepDays`. Geri: `pg_restore`. Fark: `data` diski (ayarlar, `.vault.key`) ve log diskleri için VolumeSnapshot gerekir |
| Ajan kayıt adresi | `http://<sunucu>:8600/ingest`, edge açıkken `https://<ad>/ingest`; kayıt anahtarı ve hazır komut Bağlantılar sayfasında | `https://<ingest route>/ingest` (ya da tek adres `routes.ui.host`); chart bu adresi uygulamaya verir (`WATCHOVER_INGEST_URL`), hazır komut aynı sayfada |
| TLS | `./watchover.sh edge on <ad>`: nginx 443'te; şirket sertifikası `./certs/watchover.crt` + `.key` ya da kurulumun kendi CA'sı (`edge cert`); yük dengeleyici: `edge lb <CIDR>`, sağlık `/healthz` | OpenShift router'ı (Route, edge TLS, http → https); kümenin `*.apps` sertifikası ya da `routes.tls` |
| İzleme / sağlık | `./watchover.sh status` ve `doctor`; sağlık uçları `/api/health` (8501), `/health` (8600, MCP 8765); konteynerin kendi ajanı (`WATCHOVER_SELFMON=1`) | Aynı uçlarda startup / readiness / liveness probları; `oc get pods`; pod'un kendi ajanı (`dashboard.selfMonitor`) |
| Açılışta başlama | Docker: servis + `restart: unless-stopped`; Podman: `watchover-stack.service` (install açar, `./watchover.sh autostart on` / `off`) | Kubernetes (Deployment / StatefulSet) |
| Sorun giderme | `./watchover.sh logs [servis]`, `./watchover.sh doctor`, `./watchover.sh storage show`, `./watchover.sh checklist`; Docker rehberi bölüm 11 | `oc logs deploy/watchover -c dashboard` (ya da `-c receiver`), `oc describe pod`, `oc get events`; OpenShift rehberi bölüm 11 |

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

**İlk giriş:** yerleşik yönetici `admin@watchover.local`. Başlangıç parolası her kurulumda rastgele üretilir ve kodda ya da belgelerde yer almaz; kurulumu yapan kişi `./watchover.sh admin-password` (RHEL'de `sudo watchover admin-password`) ile görür. İlk girişte yeni parola istenir; o andan sonra başlangıç parolası silinir ve komut artık göstermez. Ekip hesapları Users sayfasından ya da terminalden:

```bash
./watchover.sh user add ops@sirket.com --admin
./watchover.sh user list
```

## 2b. Alıcı servisi (Go, 8600)

Ajanların bağlandığı 8600 portu v1.35'ten itibaren ayrı bir konteynerde, Go ile yazılmış `watchover-receiver` servisindedir. Paketi doğrular, `/data/spool` altına sıralı segment olarak yazar ve milisaniyeler içinde yanıt verir; panel (dashboard) segmentleri arka planda işler. Böylece anomali taraması ya da yoğun ayrıştırma sırasında ajanlar beklemez, zaman aşımına düşmez.

- Kimlik doğrulama aynıdır: ajan token'ları, ortak anahtar ve ilk kurulumdaki açık mod. Panel izinli token'ların özetini (`auth.json`) birkaç saniyede bir alıcıya yayınlar; iptal edilen token en geç birkaç saniye içinde reddedilir.
- `/enroll`, `/agent.py`, `/agent/install.sh`, `/agent/manifest.json` ve Go ajanının ikili dosyaları (`/agent/wo-agent-linux-amd64`, `-arm64`: Linux'ta log gönderimi ve eylem ajanı; imaj yapılırken test edilip derlenir) de bu servisten sunulur; kayıt isteği panele, eylem ajanlarının iş kanalı (`/agent/v1/`, yalnız `wa_` jetonları) panelin alıcısına aktarılır.
- Kuyruk sınırı `.env` içinde `WATCHOVER_SPOOL_MAX_MB` (varsayılan 2048). Aşılırsa alıcı 503 ve Retry-After döner, ajan paketi kendi diskinde saklar.
- Durum: `curl http://<sunucu>:8600/health`, Prometheus için `/metrics`; log: `./watchover.sh logs receiver`.
- Yükseltme `./watchover.sh upgrade` ile kendiliğinden olur: panel 8600'ü bırakır, alıcı servisi devralır. Güvenlik duvarı kuralı değişmez.
- **Go ile ayrıştırma (v1.46):** alıcı, Python'la aynı sonucu verdiği kanıtlanmış biçimleri kendisi ayrıştırır (`.env` içinde `WATCHOVER_GO_PARSE=1`, varsayılan). Kullanıcı grok kalıpları, öğrenilmiş sütun eşlemeleri ve tanınmayan biçimler yine panelde ayrıştırılır. `WATCHOVER_GO_PARSE=0` eski davranışa birebir döner, `shadow` yalnız karşılaştırır. Ayrıştırılmış segmentler ham halin yaklaşık 3 katı yer kaplar ve kuyruk sınırına dahildir; disk elveriyorsa `WATCHOVER_SPOOL_MAX_MB` değerini 4096-6144 yapın. İşçi sayısı `WO_PARSE_WORKERS` (varsayılan: çekirdek sayısı); 4 çekirdekli makinede ekran yanıtları yavaşlarsa 2 yapın.

## 2b-2. OpenTelemetry (OTLP)

Alıcı OpenTelemetry'nin standart protokolünü (OTLP) doğrudan konuşur: bir OpenTelemetry Collector ya da uygulamadaki OpenTelemetry SDK'sı başka bir değişiklik olmadan Watchover'a gönderir. Loglar log gezginine ve canlı akışa, span'lar izlere (servis metrikleri, servis haritası), sunucu metrikleri (CPU, bellek, disk, GPU) operasyon ekranına düşer. Çözme ve dönüştürme alıcıda, Go ile yapılır.

| Yol | Adres | Biçim |
|---|---|---|
| OTLP/HTTP | `http://<sunucu>:8600/v1/logs`, `/v1/traces`, `/v1/metrics` (her zaman açık) ya da `:4318` | protobuf veya JSON, gzip |
| OTLP/gRPC | `<sunucu>:4317` (şifresiz HTTP/2) | protobuf, gzip |

4317 / 4318 portları varsayılan olarak dışarı açılmaz (aynı sunucuda başka bir collector bu portları kullanıyor olabilir): `./watchover.sh otlp on` açar, `./watchover.sh otlp off` kapatır. RHEL'de güvenlik duvarı için `sudo bash scripts/rhel-prepare.sh --otlp`.

**Kimlik:** collector'ı **Ajanlar** panelinde bir ajan olarak kaydedin ve jetonunu başlıkta gönderin (`Authorization: Bearer wo_…`); kayıtlar o ajanın ortam ve site bilgisini alır. Hiç ajan kayıtlı değilken (açık mod) jeton gerekmez.

Örnek Collector ayarı (`otelcol-contrib`, sunucu metrikleri dahil):

```yaml
receivers:
  otlp:
    protocols: {grpc: {}, http: {}}
  hostmetrics:
    collection_interval: 30s
    scrapers: {cpu: {}, memory: {}, filesystem: {}}
processors:
  batch: {}
  resourcedetection: {detectors: [system]}
exporters:
  otlphttp/watchover:
    endpoint: http://watchover.firma.local:8600
    headers: {Authorization: "Bearer wo_xxxxxxxx"}
    compression: gzip
service:
  pipelines:
    logs:    {receivers: [otlp], processors: [resourcedetection, batch], exporters: [otlphttp/watchover]}
    traces:  {receivers: [otlp], processors: [resourcedetection, batch], exporters: [otlphttp/watchover]}
    metrics: {receivers: [otlp, hostmetrics], processors: [resourcedetection, batch], exporters: [otlphttp/watchover]}
```

SDK'dan doğrudan göndermek için: `OTEL_EXPORTER_OTLP_ENDPOINT=http://watchover.firma.local:8600`, `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20wo_xxxxxxxx` (gRPC için `:4317`, `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`).

Eşlemeler: `service.name` → servis, `host.name` (yoksa `k8s.node.name`, `k8s.pod.name`) → sunucu, `deployment.environment(.name)` → ortam, log kaydının `trace_id`'si → ize kesin bağ. Metriklerden `system.cpu.time` / `system.cpu.utilization`, `system.memory.usage` / `.utilization`, `system.filesystem.usage` / `.utilization` (tmpfs, overlay gibi sanal diskler hariç en dolu disk) ve GPU kullanımı yüzdeye çevrilir; diğer metrikler sayılır ama saklanmaz (alıcının `/health` çıktısında `otlp_points_skipped`). Bir izde hata görülünce o izin bekletilen span'ları da tutulur. Sayaçlar: `curl -s localhost:8600/health` içinde `otlp_*`.

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

Ham log satırları ana veritabanında değil, iki dizinde tutulur: **sıcak** (gün başına bir SQLite dosyası, son `live_retention_h` saat; ölçülen ~65-90 bayt/satır) ve **arşiv** (şablon + sıkıştırma ile ~7-8 bayt/satır, `cold_retention_days` gün, **varsayılan 365**; eski varsayılan 7 olan kurulumlar da 365'e geçer). Süresi dolan gün tek dosya silinerek gider, PostgreSQL şişmez. Log gezgini eski günleri arşivden tarar ("arşivden" rozeti, ilerleme çubuğu). Yerleri yönetici verir (bölüm 1b, "Ham log yerleri"); verilmezse veri biriminde kalır ve "ayarlanmadı" görünür. `hot_max_gb` dolunca biten en eski gün erkenden arşive geçer; `cold_max_gb` yalnız bir bütçedir (%80 / %90 uyarı), arşiv silmez. Arşiv diski dolarsa arşivleme durur, gün sıcakta kalır, kritik uyarı gelir. Her arşiv günü mühürlenir ve karma zincirli manifestoya yazılır; doğrulama, denetçi dışa aktarımı ve aylar süren arka plan araması Sistem › Depolama › Denetim'dedir. İlk açılışta eski `live_batches` tablosu arka planda dosyalara taşınır. Eski davranış için `.env`'de `WATCHOVER_LOG_STORE=db`.

## 2j-b. Anahtar servisi: watchover-keyd (aksiyon ajanı)

Aksiyon ajanının imza ve denetim anahtarları panelde, veritabanında ya da `/data`'da **durmaz**: `keyd` servisi (aynı imaj, `python -m watchover.act.keyd serve`) kendi birimi `watchover-keys`'te tutar, dışarıya port açmaz ve panelle yalnız karşılıklı TLS ile konuşur (istemci sertifikasını ilk açılışta `watchover-secrets` birimine yazar). Kasa anahtarı (`.vault.key`) da ilk açılışta `/data`'dan `watchover-secrets`'e taşınır; böylece `/data` ya da veritabanı yedeği sızsa bile kasa değerleri ve denetim zinciri çözülemez. keyd yalnız bir sonraki denetim kaydını imzalar (geçmiş yeniden imzalanamaz), çapaları kendi başından imzalar, iş imzasında kendi sınırlarını (`/keys/store/keyd.json`) uygular. Anahtarlar `act_mode` ilk kez "off" dışına alınınca oluşur. keyd çalışmazsa sistem yürütmesi ve denetim yazımı durur (503), rehberli inceleme sürer. Yedek: Otomasyon › Anahtarlar'dan iki `act.policy` sahibi, her biri parolanın bir yarısıyla şifreli dışa aktarım alır; ya da sunucuda `docker compose exec keyd python -m watchover.act.keyd export --dir /keys/store --out /keys/yedek.wokx`. Acil durdurma: `docker compose exec keyd python -m watchover.act.keyd kill --dir /keys/store --reason "..."`. Durum: `python -m watchover.act doctor` (paylaşılan disk uyarısı, tetikleyiciler, dış çapa). Tek sunucu, Docker'sız kurulumda keyd ayrı bir işletim sistemi kullanıcısıyla Unix soketinde çalışır: `--unix /run/watchover-keyd/keyd.sock`, panelde `WATCHOVER_KEYD=unix:/run/watchover-keyd/keyd.sock`.

## 2k. Hazır boyut profili ve LLM'i GPU'suz çalıştırmak

**Kaynaklar kendiliğinden ayarlanır:** `watchover.sh` her kurulum, güncelleme ve başlatmada makinenin CPU ve belleğini okur ve payları orantılı dağıtır: panel çekirdeklerin çoğunu (CPU − 2, en az 2), belleğin dörtte birini; alıcı CPU'nun dörtte birini (ayrıştırma işçisi CPU × 3/8); PostgreSQL belleğin sekizde birini (`shared_buffers` bunun dörtte biri); LLM CPU'nun yarısını (v1.59; 4 CPU'da 2) ve belleğin ~%16'sını (14B seçilince en az 16 GB); eğitim CPU'nun yarısını. 8 CPU / 64 GB makinede sonuç: panel 6 CPU / 16 GB, alıcı 2 CPU (3 işçi), PostgreSQL 2 CPU / 8 GB, LLM 3 CPU / 10 GB, eğitim 4 CPU / 12 GB. Makineye CPU ya da bellek eklenince bir sonraki `./watchover.sh start` yeni değerleri yazar. Sabit değer istenirse `./watchover.sh profile cpu8|cpu16` (ya da `.env`'de `WATCHOVER_SIZING=manual`); `profile auto` otomatiğe döner, `profile` geçerli değerleri gösterir. Alıcı ayrıca konteynerin CPU ve bellek sınırını kendisi okur (OpenShift pod sınırı dahil). Tanımlı bir LLM bağlantısı (LLM sayfası / `.env`) değiştirilmez.

LLM'i açmak: `./watchover.sh llm on` (Ollama başlar, modeller iner: 7B, hızlı işler için 3B, gömme için bge-m3). Ollama, llama.cpp ile GGUF (Q4_K_M) modelleri CPU'da çalıştırır; ek motor gerekmez. GPU'suz bir yanıt 3B ile ~20-40 sn, 7B ile ~45-90 sn sürer. Çekirdek (anomali, eşik, log analizi, panolar) LLM'siz çalışır.

**Sohbet modeli hız ölçümüyle seçilir (v1.59).** CPU'da üretim hızı RAM miktarından değil bellek bant genişliğinden ve LLM'e ayrılan CPU payından gelir; bu yüzden ölçülür. `llm on` indirmeden sonra 7B'yi bu makinede ölçer (`src/watchover/llmbench.py`: sabit bir log sorusu, 64 token, ısınma + ölçüm). 14B'nin hızı 7B'den tahmin edilir (ağırlıklar 4,7 / 9,0 GB: her token için iki kat bayt); RAM 60 GB+, tahmin eşiğe yakın (`WATCHOVER_LLM_MIN_TPS`, varsayılan 5 token/sn) ve konteyner deposunda 15 GB boş ise `qwen2.5:14b-instruct` indirilip ölçülür, eşiği geçerse seçilir (LLM bellek payı en az 16 GB olur). 7B `WATCHOVER_LLM_FLOOR_TPS` (2,5) altındaysa 3B seçilir. Sonuç `.env`'de `LLM_MODEL` ve `WATCHOVER_LLM_BENCH` (ör. `7b 8.4 tok/s, 14b 4.6 tok/s`). Tekrar ölçmek: `./watchover.sh llm auto`; tek modeli ölçmek: `./watchover.sh llm bench qwen2.5:7b-instruct`. LLM sayfasında ya da `llm use` ile seçtiğiniz model ve kendi LLM bağlantınız değiştirilmez; `WATCHOVER_LLM_AUTO=0` ölçümü kapatır. İndirilen model silinmez. Kaba beklenti: 4 CPU (LLM'e 2) → 7B ~2-4 token/sn, büyük ihtimalle 3B; 8 CPU (4) → 7B ~5-8, 14B ~3-4 (7B kalır); 14B için ~16+ çekirdek ve yüksek bant genişliği gerekir. Ollama konteyner payını değil sunucunun çekirdek sayısını gördüğü için panel her isteğe `num_thread` = `WATCHOVER_LLM_THREADS` ekler (aşırı iş parçacığı CPU kotasında yavaşlatıyordu). CPU'da uzun bağlam pratik değildir (128k token'lık bir istemi okumak saatler sürer); Watchover modele yalnız çekirdeğin bulduğu kanıt satırlarını verir.

**Eğitim takvimi:** LLM › Kalite › Eğitim takvimi: günler, başlangıç / bitiş saati ve saat dilimi. Takvim açıkken otomatik eğitim yalnız bu pencerede başlar (bitiş başlangıçtan önceyse pencere gece yarısını geçer, ör. 22:00-05:00); "Şimdi eğit" takvimi beklemez, başlamış eğitim pencere bitince kesilmez.

**Simülatör kaldırıldı; yalnız gerçek veri.** Watchover artık sentetik veri üretmez: Operasyon'da simülasyon, Veri setleri'nde demo / örnek veri seti, ITSM'de demo ticket ve aksiyon ajanında demo kontrolleri yoktur (API 403); hiçbir ayar ya da ortam değişkeni bunları açmaz. Eski bir sürümün simülatöründen kalan veri (gönderen `simulator`, `wo.demo` işareti, `demo = 1` sütunu) **yükseltmeden sonraki ilk açılışta kendiliğinden** her depodan silinir: log satırları, metrikler, izler ve RUM, anomaliler, uyarılar, topoloji, iş akışları, aksiyonlar, demo veri setleri, **LLM hafızası** ve demo verisiyle eğitilmiş **ince ayar modelleri** (indirilmiş temel modellere dokunulmaz). Gerçek veriye dokunulmaz; işlem denetim kaydına yazılır. Elle: `./watchover.sh demo-clean --dry-run` (sayar), sonra `./watchover.sh demo-clean`; ya da Sistem › Veri temizliği.

**Tüm analiz verisini sıfırlama (`./watchover.sh reset-data`).** Kim üretmiş olursa olsun her analiz deposunu boşaltır: anomaliler, incident'lerden açılan aksiyonlar ve ajan vakaları, uyarılar ve eşik durumları, öğrenilmiş taban çizgileri / şablonlar / Drain kümeleri, LLM hafızası (sohbetler, kurallar, değerlendirmeler, çağrılar), ince ayar modelleri ve adaptörleri, dakikalık / saatlik geçmiş, izler ve RUM oturumları, keşfedilen topoloji (ağ akışları, teknoloji izleri, namespace'ler), iş akışı olayları, öğrenilmiş sütun profilleri. **Kalanlar:** kullanıcılar, ekipler, roller, ayarlar (LLM bağlantısı dahil), kayıtlı ajanlar ve token'ları, kaynaklar, bildirim ve eşik politikaları, elle girilen envanter / uygulama / iş akışı tanımları, denetim kayıtları, gerçek sunucuların saklanan log satırları (sıcak disk ve bir yıllık arşiv, manifest zinciri bozulmaz) ve indirilen temel modeller. Önce sayımı gösterir; terminalde `SIFIRLA` yazmanızı ister (otomasyon için `--yes`). Sıfırlama dashboard'un **bir sonraki açılışında**, hiçbir iş parçacığı bellekteki veriyi geri yazmadan önce çalışır; betik dashboard'u yeniden başlatır ve sonucu basar. Arayüzde: Sistem › Veri temizliği › *Tüm analiz verisini sıfırla* (kurulum yöneticisi, `SIFIRLA` yazılır ve şifre yeniden sorulur). Geri alınamaz: önce `./watchover.sh backup`.

**Önbelleği temizle:** Sistem › Bakım › *Önbelleği temizle* sunucudaki hesaplanmış önbellekleri (canlı analiz, panolar, APM, log arama, envanter, ITSM, ajan token önbelleği) ve tarayıcının sorgu önbelleğini boşaltır; hiçbir veri ya da öğrenilmiş bilgi silinmez, ilk okumada yeniden dolar.

## 3. Günlük işletim: watchover.sh

| Komut | Ne yapar |
|---|---|
| `./watchover.sh status` | Konteynerler, sağlık durumu, anlık CPU / bellek |
| `./watchover.sh logs [dashboard\|postgres\|mcp]` | Canlı log |
| `./watchover.sh stop` / `start` / `restart` | Durdur / başlat / yeniden başlat (veri kalır) |
| `./watchover.sh update` | Yeni paket klasörün üstüne açıldıktan sonra: imajı yeniden derler, konteynerleri yeniler, eski imajı temizler |
| `./watchover.sh upgrade` | **Yeni sürüme geçmenin tek komutu**: `git fetch` + ileri sarma + `update`. Klasörde elle değiştirilmiş dosya varsa kenara alır (`git stash`) ve sonra geri koyar; düz `git pull`'un "unstaged changes" diye durduğu yerde durmaz. Kendi commit'i olan klasörü elle birleştirmeye bırakır |
| `./watchover.sh install --production` | Kurulum + `WATCHOVER_PRODUCTION=1` + devreye alma listesi (yedek, açılışta başlama, yönetici parolası, disk sınırları, TLS); `./watchover.sh checklist` listeyi yeniden gösterir |
| `./watchover.sh backup [--no-logs] [--keep N]` | PostgreSQL dökümü + veri birimi + `.env` → `backups/watchover-YYYYMMDD-HHMM.tgz` (`--no-logs`: veri birimindeki ham loglar hariç) |
| `./watchover.sh backup-timer on [SS:DD]` / `off` | Gecelik yedek (`backup --no-logs`): systemd timer, systemd yoksa cron; varsayılan 02:15; argümansız durumu gösterir |
| `./watchover.sh autostart on` / `off` | Açılışta başlatan systemd birimi (`watchover-stack.service`); Podman'da `install` kendisi açar |
| `./watchover.sh storage show` | Ham logların yeri (elle verilen dizin ya da "NOT SET": veri birimi), süreler, boyutlar, boş alan, manifesto, ölçülen hızla yıllık ihtiyaç (öneri yapmaz) |
| `./watchover.sh storage set [--hot DİZİN] [--cold DİZİN] [--hot-hours N] [--cold-days N] [--keep-old]` | Yerleri ve süreleri verir; var olan günleri kopyalar, sha256 ile doğrular, geçer, sağlık denetiminden sonra eski kopyaları siler; yer yoksa ya da dizin yabancı dosya içeriyorsa reddeder |
| `./watchover.sh restore DOSYA` | Yedeği geri yükler (veritabanı ve veri birimi; konteynerler bu sırada durur) |
| `./watchover.sh user …` | `list`, `add EMAIL [--admin]`, `promote`, `password EMAIL`, `unlock`, `enable`, `disable`, `delete` |
| `./watchover.sh mcp on\|off` | Kendi MCP sunucumuzu açar / kapatır (bölüm 7) |
| `./watchover.sh legacy on\|off` | Eski Streamlit arayüzünü ek konteyner olarak açar / kapatır (8502) |
| `./watchover.sh migrate` | Eski SQLite kurulumunun `.db` dosyalarını PostgreSQL'e kopyalar (bölüm 6) |
| `./watchover.sh save` / `load DOSYA` | İmajları `.tgz` olarak dışa / içe aktarır (kapalı ağ, bölüm 9) |
| `./watchover.sh shell` | Dashboard konteynerinde kabuk |
| `./watchover.sh updater on\|off\|status` | Uygulama içi güncelleyici (bölüm 3b): varsayılan açık; `off` motor soketini hiçbir konteynerde bırakmaz |
| `./watchover.sh build` / `health [SÜRÜM] [SN]` | Yalnız bu ağacın imajlarını derler (hiçbir şeyi yeniden başlatmaz) / panel `/api/health` ile o sürümü verene kadar bekler |

Bütün bunlar düz `docker compose` komutlarıdır; alışkın olanlar `docker compose ps`, `docker compose logs -f dashboard` ile aynı işleri yapabilir (Podman'da `sudo DOCKER_HOST=unix:///run/podman/podman.sock docker compose ps` ya da `sudo podman ps`).

## 3b. Uygulama içinden güncelleme ve geri dönüş (güncelleyici)

Sistem › **Güncelleme ve sürümler** kartından yeni sürüm zip'i (ya da çevrimdışı paket) yüklenir, uygulanır; bir şey ters giderse önceki sürüme ya da listeden seçilen bir sürüme dönülür. Sunucuda komut gerekmez.

**İlk kez açmak.** Bu özelliği getiren sürüme bir kez her zamanki yoldan geçin; başka adım yok:

```bash
cd ~/watchover-docker
./watchover.sh upgrade        # git ile kurulduysa
# ya da: yeni zip'i klasörün üstüne açın, sonra
./watchover.sh update
```

`update` / `upgrade` / `install` güncelleyici imajını bir kez derler (`watchover-updater:1`: Docker CLI + Compose, bash, Python) ve `updater` konteynerini başlatır; `.env`'e `WATCHOVER_UPDATER=1`, `WATCHOVER_INSTALL_DIR` (bu klasör) ve `WATCHOVER_ENGINE_SOCK` yazılır. Kapatmak: `./watchover.sh updater off`; yeniden açmak: `./watchover.sh updater on`; durum: `./watchover.sh updater status`.

**Mimari.** Yetkili iş paneli taşıyan konteynerde değil, ayrı `updater` konteynerinde yapılır:

| | Panel (`dashboard`) | Güncelleyici (`updater`) |
|---|---|---|
| Motor soketi (`/var/run/docker.sock`, Podman'da `/run/podman/podman.sock`) | **asla** | yalnızca bu konteyner |
| Kurulum klasörü (`~/watchover-docker`) | yok | aynı mutlak yolda bağlı (compose göreli yolları sunucudaki gibi çözer) |
| Ağ / port | 8501 | **ağ yok** (`network_mode: none`), port yok |
| Paylaşılan | `watchover-updates` birimi (`/updates`): paket ve istek yazar, durumu okur | aynı birim: isteği okur, adım adım durumu yazar |

Panel yalnızca paketi denetler, birime koyar ve "uygula / geri dön" isteği yazar; güncelleyici isteği **kendisi yeniden denetler** (boyut, yol, sembolik bağ, imza) ve bütün motor işlerini `./watchover.sh` ile yapar (`backup`, `build`, `unbundle`, `start`, `health`, `restore`): elle yaptığınız güncellemenin aynısıdır. Güncelleme mantığı imajda değil klasördedir; her yeni sürüm kendi güncelleme mantığını getirir.

**Paketler.**
- Kaynak zip: `git archive --format=zip --prefix=<klasör>/ HEAD` ile yapılan dosya; tek bir üst klasör (adı önemsiz, ör. `hackathon/`). Sürüm `src/watchover/__init__.py`'den okunur.
- Çevrimdışı paket: `./watchover.sh bundle` çıktısı `.tar` (imajlar dahil; derleme yerine imajlar yüklenir, kurulum `WATCHOVER_OFFLINE=1` olur).
- Reddedilenler: boyut sınırını aşan (`WATCHOVER_UPDATE_MAX_MB`, varsayılan 8192), mutlak yol / `..` (zip-slip), sembolik bağ, aygıt dosyası, aynı adlı iki dosya, olağan dışı sıkıştırma oranı, eksik zorunlu dosya (`watchover.sh`, `docker-compose.yml`, `Dockerfile`, `pyproject.toml`, `src/watchover/__init__.py`), sürüm bilgileri birbirini tutmayan ve **uygulama içi güncelleyiciyi içermeyen** (bu özellikten önceki bir sürümün) paketi — böyle bir sürüm arayüzden geri alınamazdı; onu elle kurun.
- Ekranda `çalışan → yeni` sürüm, sürüm notları (CHANGELOG), SHA-256 ve imza durumu görünür. Sürüm düşürme serbesttir, belirgin bir uyarıyla.

**İmza (önerilir).** Anahtar tanımlıysa imzasız ya da yanlış imzalı paket reddedilir:

```bash
# sürümü hazırlayan makinede (özel anahtar sunucuya gitmez)
python -m watchover.release keygen                     # watchover-release.key + watchover-release.pub, .env satırını yazdırır
python -m watchover.release package --key watchover-release.key   # watchover-<sürüm>.zip + .zip.sig
python -m watchover.release sign watchover-bundle-1.54.0.tar --key watchover-release.key   # paket için
# sunucuda .env:
WATCHOVER_UPDATE_PUBKEY=<base64 açık anahtar>          # ya da klasöre update-pubkey.pem
```

Yüklerken zip ile `.sig` dosyasını birlikte bırakın. İmza Ed25519'dur ve paketin SHA-256 özetini kapsar.

**Güncelleme akışı** (ekranda adım adım, panel yeniden başlarken sayfa bağlantıyı bekleyip kaldığı yerden devam eder):

1. Alındı → 2. Denetlendi → 3. **Yedek** (`backup --no-logs`: PostgreSQL dökümü + veri birimi, ham loglar hariç; çalışan sürümün klasörüne `pre-update.tgz` olarak) → 4. **Derleme** (yeni sürümün ağacından `watchover:<sürüm>` imajları; çevrimdışı pakette imaj yükleme). Bu adıma kadar çalışan sürüme dokunulmaz → 5. **Geçiş** (klasör yeni sürüm olur; `.env`, `backups/`, `datasets/`, `certs/`, `data/` hiç değişmez; önceki sürümün ağacı ve imajları saklanır) → 6. **Başlatma** (`./watchover.sh start`) → 7. **Sağlık denetimi** (yeni sürüm `/api/health` ile kendi sürüm numarasını varsayılan 240 sn içinde vermeli) → Bitti.

Geçişten sonraki bir adım başarısız olursa ve "otomatik geri dön" açıksa (varsayılan) önceki sürümün kodu ve imajları geri gelir, yeniden başlatılır ve sağlığı denetlenir; ekranda "başarısız, önceki sürüme dönüldü" ve nedeni yazar. Aynı anda tek güncelleme çalışır; o sırada sunucuda `./watchover.sh update / upgrade / restore / unbundle` da beklemeye zorlanır (`.updater/busy`).

**Sürümler ve geri dönüş.** Son N sürüm (varsayılan 5, kartın ayarlarından ya da `WATCHOVER_UPDATE_KEEP`) kaynak ağacı ve imajlarıyla `.releases/<sürüm>/` altında saklanır; çalışan ve bir önceki sürüm hiç silinmez, daha eskileri imajlarıyla birlikte temizlenir. Listede kurulum tarihi, kaynağı (zip / çevrimdışı paket / elle), CHANGELOG notları, imajların varlığı ve o sürümden çıkarken alınan yedek görünür. **Bu sürüme dön**:
- kod ve imajlar seçilen sürüme geçer; imajlar duruyorsa yeniden derleme yapılmaz,
- önce şu anki durumun yedeği alınır (o sürüme sonradan veriyle dönülebilsin diye),
- varsayılan olarak **veriler korunur**,
- isteğe bağlı **"veritabanını da o güncellemeden önceki yedeğe döndür"**: o sürümden çıkılırken alınan yedek geri yüklenir (`./watchover.sh restore`). O andan sonra yazılan her şey (olaylar, incident'lar, aksiyonlar, ayarlar, kullanıcılar) kalıcı olarak silinir; ham log dosyaları yerinde kalır.

**Veritabanı varsayımı.** Şema değişiklikleri yalnızca eklemedir: tablolar `CREATE TABLE IF NOT EXISTS`, yeni sütunlar `ALTER TABLE … ADD COLUMN` (varsayılanlı ya da boş geçilebilir) ile gelir; bu yüzden eski sürüm bugünkü veritabanıyla çalışır, yeni sütunları görmez. Tek istisna v1.48'deki `live_batches` tablosunun, satırları sıcak log katmanına taşındıktan sonra silinmesidir; uygulama içinden dönülebilecek en eski sürüm (güncelleyiciyi getiren sürüm) bundan yenidir. Test takımı (`tests/test_updater.py::test_schema_changes_are_additive`) yeni bir yıkıcı ifade (`DROP TABLE/COLUMN`, `RENAME`, `ALTER COLUMN`) eklenirse kırmızıya döner. Yine de yeni sürümün yazdığı yeni değerleri (ör. yeni bir durum adı) eski sürüm tanımayabilir; böyle bir durumda veritabanlı geri dönüşü kullanın.

**Güvenlik ve bedeli.**
- Motor soketine erişen, sunucuda **root** yetkisine eşdeğerdir. Bu yetki yalnızca `updater` konteynerindedir; ağı yoktur, port açmaz, panel ona yalnızca birim üzerinden istek bırakabilir. Yine de `sys.update` yetkisi ve yeniden kimlik doğrulaması olan bir kullanıcı (ya da panelin kendisini ele geçiren biri) güncelleyiciye paket uygulatabilir; paket içindeki `docker-compose.yml` sunucuda istediğini çalıştırabilir. Bu nedenle üretimde **imza anahtarı tanımlayın**: o zaman yalnızca sizin imzaladığınız paketler kurulur. İstemiyorsanız `./watchover.sh updater off`.
- Yükleme, uygulama ve geri dönüş `sys.update` yetkisi **ve** taze bir adım doğrulaması ister (e-posta kodu, SSO yeniden girişi ya da parola; yöneticiler dahil). Her yükleme, ret, istek ve sonuç güvenlik kaydına (`sys.update` olayı) yazılır. Ayrıntılı kayıtta parola / anahtar değerleri maskelenir; yerleşik yönetici bilgileri hiçbir yerde gösterilmez.
- SELinux (RHEL): güncelleyici `label=disable` ile çalışır (soket erişimi için); hiçbir dosyanın etiketi değişmez.
- Podman: güncelleyici imajı `podman` istemcisiyle derlenir ve aynı sokete uzaktan bağlanır; `sudo ./watchover.sh update` bir kez yeterlidir.

**Nerede ne var?** `.releases/index.json` (çalışan / önceki), `.releases/<sürüm>/tree/` (kaynak), `.releases/<sürüm>/pre-update.tgz` (o sürümden çıkarken alınan yedek), `docker logs watchover-updater`, ayrıntılı iş kaydı kartta "Ayrıntılı kayıt". Elle yapılan bir `./watchover.sh update` da listeye "sunucuda elle" olarak eklenir. Klasör bir git kopyasıysa uygulama içi güncelleme dosyaları git'in dışında değiştirir; ikisini karıştırmayın (git'e dönecekseniz önce `git status`, gerekirse `git checkout -- .` ve sonra `./watchover.sh upgrade`).

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
| `WATCHOVER_LOG_HOT_PATH` / `WATCHOVER_LOG_COLD_PATH` | boş ("ayarlanmadı") | Ham logların elle verilen yerleri (ana makine dizinleri, sahibi `10001:0`); `./watchover.sh storage set` yazar ve verileri taşır; boşsa veri biriminde |
| `WATCHOVER_EXPORT_DIR` | `/data/exports` | Denetim dışa aktarımlarının ve arama sonuçlarının yazıldığı yer (7 gün / 10 dosya tutulur) |
| `WATCHOVER_BACKUP_DIR` / `WATCHOVER_BACKUP_KEEP` | `backups` / `14` | Yedeklerin klasörü (başka disk önerilir) ve saklanacak sayı |
| `WATCHOVER_SELFMON` | `1` | Konteynerin kendini izlemesi; ana makinede ayrı ajan varsa `0` |
| `WATCHOVER_PARALLEL` | boş | Çoklu dosya ayrıştırmada paralellik: boş otomatik (8+ çekirdek), `1` zorla, `0` kapalı |
| `TZ` | `Europe/Istanbul` | Konteyner saat dilimi |
| `DATABASE_URL` | boş | Kurumun mevcut PostgreSQL sunucusu kullanılacaksa (bölüm 6) |

Değişiklikten sonra `./watchover.sh start` yeni ayarları uygular. Kaynak kullanımı: `docker stats`. Loglar json-file sürücüsüyle döner (dashboard 20 MB × 5, postgres 10 MB × 3). Sağlık denetimleri: dashboard 30 sn'de bir, postgres 10 sn'de bir; çöken konteyneri `restart: unless-stopped` geri açar, Sistem › Güncelleme'deki **Uygulamayı yeniden başlat** aynı mekanizmayı kullanır.

## 5. Sunucuları izlemek ve log çekmek

Kurulum bittikten sonra izleme üç yoldan başlar; hepsi dashboard'dan yönetilir:

1. **Ajan (push):** izlenecek her sunucuda tek satır; ajan CPU / bellek / disk / servis durumunu ve seçilen log dosyalarını 8600'e (edge açıkken `https://<ad>`'a) gönderir. Komut Bağlantı ayarları › Ajanlar sekmesinde hazır çıkar (kayıt anahtarıyla, doğrulamalı; ayrıntı: bölüm 5a):
   ```bash
   f=$(mktemp) && curl -fsSL -o "$f" http://<watchover-sunucusu>:8600/agent/boot.sh && echo "<boot.sh sha256>  $f" | sha256sum -c --quiet \
     && sudo bash "$f" --url http://<watchover-sunucusu>:8600/ingest --release-key SHA256:<sürüm anahtarı> --enroll-key wk_… --env prod
   ```
   Windows sunucular için Görev Zamanlayıcı ile aynı ajan (`docs/KURULUM_WINDOWS.md`).

   **Linux'ta tek ikili dosya, python3 gerekmez.** Linux amd64 / arm64 sunucularda bu komut yalnız Go ile yazılmış `wo-agent` dosyasını kurar (`/usr/local/lib/watchover/wo-agent`, servis `watchover-agent`, komut `wo-agent ship`); `python3` aranmaz, kurulmaz. Gönderilen veri `agent.py` ile birebir aynıdır (aynı başlıklar, aynı log satırları, aynı CPU / bellek / disk / GPU metrikleri ve bağlantı kayıtları, `auto` ile aynı uygulama keşfi), ayarlar da aynıdır (`/opt/watchover-agent/agent.env`). Ek olarak ajan her dosyada nerede kaldığını `/opt/watchover-agent/positions.json`'a yazar: yeniden başlatma, güncelleme ya da sunucu açılışı sonrasında satır tekrar gönderilmez, aradaki satırlar da kaybolmaz; logrotate'in iki yöntemi (yeniden adlandırma ve `copytruncate`) desteklenir, ajan kapalıyken döndürülen dosyanın kalanı da (aynı dizinde, sıkıştırılmadan durduğu sürece) gönderilir. Watchover erişilemezken paketler yerel kuyrukta (`spool`, varsayılan üst sınır 512 MB, `WATCHOVER_SPOOL_MAX_MB`) birikir, alıcının `Retry-After` süresine uyulur; servis durdurulurken okunmuş satırlar gönderilir ya da kuyruğa yazılır. Ölçüm (5.000 satır/sn, aynı dosya): `wo-agent` %0,35 CPU ve 13 MB bellek, `agent.py` %0,52 CPU ve 27 MB; 50.000 satır/sn'de 13 MB'a karşı 72 MB.
   - **agent.py kurulu sunucularda geçiş:** aynı komutu yeniden çalıştırmak yeter. Kurulum bağlantı testini geçince eski Python servisini durdurur, yerine `wo-agent`'ı koyar; sunucunun jetonu (dolayısıyla paneldeki kimliği; kayıt anahtarı ikinci kez kullanılmaz), yerel kuyruğu ve ayarları korunur, dosya konumları durma anında kaydedilir (bu andan sonra yazılan her satır bir kez gelir). Eski agent.py konum tutmadığı ve SIGTERM'de belleğindeki son paketi göndermediği için yalnız o paket (en çok ~1 sn'lik satır) geçişte eksik kalabilir.
   - macOS, Windows ve diğer işlemcilerde ajan `agent.py`'dir (Python 3 standart kütüphanesi). Linux'ta da Python ajanı isteniyorsa kurulum komutuna `--python-agent` eklenir; alıcı imajında Go ajanı yoksa kurulum kendiliğinden `agent.py`'ye döner. `--uninstall` her ikisini, eylem ajanını ve kuyruğu kaldırır.

   **Eylem ajanı (aynı komutla, uykuda).** Linux + systemd sunucularda (amd64 / arm64) aynı komut, aksiyon ajanının sunucu tarafını da kurar: aynı `wo-agent` dosyası ikinci bir rolle (`wo-agent act`, ayrı servis `watchover-actuator`, ayrı jeton `wa_…`). İki rol iki ayrı servistir: eylem ajanı kendi kısıtlarıyla (CPU %25, 128 MB, salt okunur sistem) çalışır, durdurulması ya da uykuda olması log gönderimini etkilemez. Otomasyon kapalıyken ajan **uykuda** bekler: beş dakikada bir merhaba der, hiçbir iş almaz, hiçbir şey çalıştırmaz. Kayıt anahtarıyla kurulumda log jetonu (`wo_…`) ve eylem jetonu (`wa_…`) birlikte verilir; panelin imza anahtarları varsa (`dispatch.pub` / `anchor.pub`) kurulumda, yoksa ilk bağlantıda sabitlenir.
   - İkili dosya alıcıdan indirilir ve imzalı manifestteki sha256 ile doğrulanır (bölüm 5a). Düz HTTP ile, ne sürüm anahtarı (`--release-key`, paneldeki komutta hep vardır) ne de `--agent-sha256` verilmeden kurulan sunucu **"güvensiz kurulum"** olarak işaretlenir: orada yalnız teşhis çalışır, değişiklik adımı gönderilmez (paneldeki komutla yeniden kurun; kimlik, jeton ve konumlar korunur).
   - Sunucunun izin listesi `/etc/watchover/actions.d` (root, yalnız okunur) ikili dosyanın içindeki paketlerden yazılır; `local.json` varsayılanı `max_risk: R0` (yalnız teşhis). Değişikliğe izin vermek sunucu yöneticisinin kararıdır: `--actions-max-risk R1` ya da `local.json`. `--criticality high|critical` her zaman R0'dır. Sunucuda `touch /etc/watchover/actions.halt` her şeyi yerelde durdurur.
   - Komutlar kabuk olmadan, şablonun `run_as` hesabıyla (yoksa ayrı kullanıcı `wo-act`) çalışır; ajan her işi yerel defterine (`/var/lib/watchover/actions/ledger.jsonl`) yazar.
   - İlk bağlantıda ajan anahtar parmak izini günlüğe yazar (`sudo /usr/local/lib/watchover/wo-agent act fingerprint`). Bir kişi **Sistem › Durum › Eylem ajanları**'nda aynı parmak izini yapıştırıp onaylayana kadar o sunucuya iş gönderilmez.
   - Kurum içi CA ile imzalı bir https adresi için `--ca /etc/pki/firma-root.pem` (iki ajana da geçer). `--uninstall` ajanı, eylem ajanını, izin listesini, durum dizinini ve `wo-act` kullanıcısını kaldırır.
   - macOS, Windows ve systemd'siz sunucularda eylem ajanı kurulmaz (kurulum bunu açıkça yazar); log gönderimi aynen çalışır.
2. **Kaynaklar (pull):** Elasticsearch / OpenSearch, Grafana Loki, Splunk, Graylog ve HTTP/JSON uçlarından belirli aralıklarla çekim; Bağlantı ayarları › Kaynaklar sekmesinden adres, kimlik ve sorgu girilir, sırlar veritabanında şifreli tutulur.
3. **Dosya yükleme:** Datasets sayfasından ZIP / log / CSV / SAP arşivleri (çoklu dosya, arka planda, ilerleme yüzdesiyle).

Ajanlar ve kaynaklar veri göndermeye başladıktan yaklaşık yarım saat sonra Operasyon sayfasının altındaki **Anomali takibi** çalışır: her sunucu kendi normaline göre izlenir (hata sıçraması, log fırtınası, sessizlik, yeni örüntü, metrik) ve her anomali operasyonel adım listesiyle takip edilir; Sistem › Bildirimler'deki *Yeni anomali açıldı* kuralı e-posta / SMS gönderir.

Konteynerin kendi ajanı Watchover sunucusunun konteynerini raporlar; ana makinenin kendisini izlemek için ona da normal bir ajan kurun ve `WATCHOVER_SELFMON=0` yapın. Uyarılar (e-posta / SMS) Sistem › Bildirimler'den, roller Users sayfasından ayarlanır.

## 5a. Ajan güncellemesi ve güvenli kurulum

**Ajanlar kendini günceller.** Watchover yükseltildiğinde sunuculara bir daha dokunmak gerekmez: Linux + systemd sunucularda kurulum, `wo-agent`'ın yanına kısa ömürlü bir güncelleme servisi koyar (`watchover-update.service` + `watchover-update.timer`, root). Altı saatte bir ve alıcı daha yeni bir sürüm olduğunu bildirdiğinde (yanıt başlığı `X-Watchover-Update`) hemen çalışır:

1. Alıcıya sorar (`GET /agent/update`, sunucunun kendi jetonuyla): panelin anahtarı **"Ajanlar kendini güncellesin"** (varsayılan açık), sunucunun duraklatılıp duraklatılmadığı, kademeli dağıtım payı (%5 / 10 / 25 / 50 / tümü; her sunucunun yeri sürümle birlikte sabit) ve varsa "bu sürüme döndür" sabitlemesi burada uygulanır.
2. İmzalı manifesti doğrular: imza, kurulumda sunucuya sabitlenen **sürüm anahtarıyla** (`WATCHOVER_RELEASE_KEY`, `/opt/watchover-agent/agent.env`) tutmalıdır; başka bir anahtarla, değiştirilmiş ya da imzasız manifest reddedilir.
3. İkili dosyayı indirir; sha256 ve boyut imzalı manifestle, dosyanın içine derlenmiş sürüm (`WO-AGENT-VERSION=…`) manifestin sürümüyle aynı olmalıdır. **Eski bir sürüm kabul edilmez**; tek istisna panelden "bu sürüme döndür" ile o sunucu adına imzalanmış, süreli bir sabitlemedir.
4. Yeni dosya bu sunucuda çalışıyor mu (`wo-agent version`) bakar; çalışan dosyayı `wo-agent.prev` olarak saklar, yenisini tek bir `rename` ile yerine koyar, `watchover-agent`'ı yeniden başlatır (eylem ajanı yalnız çalışıyorsa yeniden başlar: `try-restart`).
5. Yeni ajan 2 dakika içinde alıcıya ulaşmazsa (`/var/lib/watchover/update/hello.json`) **önceki dosyayı kendiliğinden geri koyar**, servisi yeniden başlatır ve durumu bildirir. Aynı derleme üç kez başarısız olursa daha yenisi çıkana dek denenmez.

Güncelleme eylem ajanının yetkisini genişletmez: yalnız ikili dosya değişir; root'a ait izin listesi (`/etc/watchover/actions.d`), `local.json`, servis dosyaları ve `wo-agent.env` (ör. `WO_INSECURE_INSTALL`) olduğu gibi kalır, uykudaki eylem ajanı uykuda kalır.

Ajanlar paneli her sunucunun sürümünü ve güncelleme durumunu gösterir (*güncel, güncelleniyor, sırada, duraklatıldı, başarısız, geri alındı, kendini güncelleyemez*); sunucu başına **Duraklat / Sürdür** ve **Bu sürüme döndür** (keyd'in daha önce imzaladığı sürümlerden biri; ajan saklı önceki dosyasını kullanır) vardır.

**agent.py'den wo-agent'a kendiliğinden geçiş.** Linux amd64 / arm64'te systemd servisi olarak root ile çalışan ve sürüm anahtarı sabitlenmiş `agent.py`, panel izin verdiğinde (aynı anahtar, duraklatma ve kademeli dağıtım) kendini taşır: imzalı manifesti saf Python ile doğrular, kurulum betiğini manifestteki sha256 ile alır ve kendi servisinin dışında (`systemd-run`) çalıştırır. Betik jetonu, kimliği ve yerel kuyruğu korur, dosya konumlarını geçiş anında kaydeder (`wo-agent ship --seed-positions`), servisi yeniden yazar ve `wo-agent` 2 dakika içinde alıcıya ulaşmazsa `agent.py`'yi geri getirir. Root olmayan, systemd'siz, macOS / Windows / başka işlemcili ya da sürüm anahtarı olmadan (eski kurulum betiğiyle) kurulmuş `agent.py` kendini taşımaz; panel nedenini "kendini güncelleyemez" olarak gösterir: o sunucularda paneldeki komutu bir kez çalıştırın.

**Güven zinciri.** İmzayı yalnız anahtar servisi atar (`watchover-keyd`, ayrı `release.key`; aksiyon işlerini imzalayan `dispatch.key`'den bilerek ayrı: bir iş imzası yalnız sunucunun izin listesindeki şablonları çalıştırır, bir sürüm imzası ise o listeyi uygulayan programı değiştirir). keyd imzaladığı dosyaların sha256'sını **kendi imajındaki** dosyalardan kendisi hesaplar (`wo-agent-linux-amd64/arm64`, `agent-install.sh`, `agent-boot.sh`, `agent.py`); panel ya da alıcı ona özet veremez, bu yüzden ele geçirilmiş bir API bile istediği dosyayı imzalatamaz. Panel imzalı manifesti ve bu dosyaların kopyasını alıcının kuyruk dizinine yazar (`/data/spool/agent-update.json`, `/data/spool/release/`); alıcı yalnız sunar, anahtar tutmaz. `release.key` anahtar yedeğine (Otomasyon › Anahtarlar) dahildir; kaybolursa sunucuların sabitlediği anahtar artık tutmaz ve kendini güncelleme durur: paneldeki yeni komutu sunucularda bir kez çalıştırın.

**Güvenli kurulum komutu.** Paneldeki komut her zaman güncel değerleri taşır:
```bash
# düz http (8600): önce boot.sh sha256 ile, sonra imzalı manifest sürüm anahtarıyla (openssl >= 1.1.1) doğrulanır
f=$(mktemp) && curl -fsSL -o "$f" http://<sunucu>:8600/agent/boot.sh && echo "<boot.sh sha256>  $f" | sha256sum -c --quiet \
  && sudo bash "$f" --url http://<sunucu>:8600/ingest --release-key SHA256:<sürüm anahtarı> --enroll-key wk_… --env prod
# edge açıkken https: kurulumun kendi CA'sı sha256 ile sabitlenir ve ajan için kurulur (elle CA kopyalamak yok)
f=$(mktemp) && curl -fsSLk -o "$f" https://<ad>/agent/boot.sh && echo "<boot.sh sha256>  $f" | sha256sum -c --quiet \
  && sudo bash "$f" --url https://<ad>/ingest --ca-sha256 <CA sha256> --release-key SHA256:<sürüm anahtarı> --enroll-key wk_… --env prod
```
- `boot.sh` bilerek küçük ve sabittir: komuttaki sha256'sı Watchover yükseltmelerinde ve ajan güncellemelerinde değişmez; sürüm anahtarı da değişmez. Yani bir kez dağıtılan komut (ör. Ansible'da) geçerli kalır, panel ayrıca hep güncelini gösterir. `boot.sh` kurulum betiğinin sha256'sını imzalı manifestten alır; betik de ikili dosyayı aynı manifestle doğrular ve anahtarı sunucuya sabitler. Böyle kurulan sunucu **güvensiz sayılmaz**, düz http ile bile.
- `-k` yalnız boot.sh'ın indirildiği ilk istekte vardır ve zararsızdır: dosya sha256 ile doğrulanır; CA da (`/agent/ca.crt`) `--ca-sha256` ile. Ondan sonraki her bağlantı bu CA ile normal TLS doğrulamasıyla yapılır; CA `/etc/watchover/ca.pem` olarak kurulur ve iki ajan da onu kullanır (`WATCHOVER_CA`, `WO_CA`).
- Şirket sertifikası (`./certs/watchover.crt`) kullanılıyorsa ve sunucular o CA'ya zaten güveniyorsa komutta `-k` ve `--ca-sha256` yoktur. Güvenmiyorlarsa şirket kök sertifikasını `./certs/watchover-ca.crt` olarak koyun; alıcı onu sunar, komut onu sabitler.
- Sunucuda `openssl` (>= 1.1.1; RHEL 8+, Ubuntu 18.04+, Debian 10+'da var) gerekir; yoksa https komutunu kullanın.
- Eski, güvensiz işaretli kurulumlar kendini güncelleyerek güvenli hale **gelmez**: ilk ikili ve sabitlenen anahtar aynı korumasız kanaldan geldiği için sonraki her "doğrulama" de o kanala dayanır. Bu sunucularda paneldeki komutu bir kez çalıştırmak yeter (kimlik, jeton, kuyruk ve konumlar korunur, güvensiz işareti kalkar).
- Edge açıkken nginx ajan yollarını da alıcıya yönlendirir: `/ingest`, `/enroll`, `/agent.py`, `/agent/*` (kurulum, `boot.sh`, `ca.crt`, manifest, ikili dosyalar, `/agent/update`, eylem ajanlarının `/agent/v1/` iş kanalı), `/rum`, `/rum.js`, `/mobile` ve OTLP/HTTP `/v1/*`. 8600 açık kalır; `./watchover.sh edge on` kurulumun kendi CA'sını oluşturur, kökü `./certs/watchover-root.crt`'dir, alıcı onu `/agent/ca.crt` olarak sunar.

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

Sonra istemciden `analyze_dataset("uretim-log.zip")` demek yeter. Anahtarsız istekler 401 alır. MCP konteyneri canlı ürün araçlarını (loglar, panolar, anomaliler, envanter) arayüzün kendi MCP ucuna iletir (`WATCHOVER_API_URL`, varsayılan `http://watchover:8501`). Edge açıkken (bölüm 8) aynı sunucu TLS ile `https://<ad>/mcp` adresindedir (nginx → `mcp:8765`), arayüzün kendi MCP ucu `https://<ad>/api/mcp`; düz 8765'i `.env`'de `WATCHOVER_MCP_PORT=127.0.0.1:8765` ile yalnız yerelde tutun (MCP kapalıyken `/mcp` 502 döner). Anahtarlar: Ayarlar › Bağlantılar › MCP'de adlı, kapsamlı (hesap ya da ekip, okuma / yazma) anahtarlar; `.env`'deki `MCP_API_KEY` kurulum çapında okuma anahtarıdır, `MCP_API_KEY_WRITE=1` ona yazma da verir. OpenShift'te aynı sunucu `mcp.enabled: true` ile kendi pod'u ve TLS'li Route'uyla gelir (OpenShift rehberi bölüm 5b).

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

Giriş e-posta + parola iledir; yönetici olmayan hesaplara SMTP ayarlıysa e-posta ile tek kullanımlık kod sorulur (`/api/auth/mfa`). Giriş sayfasındaki **Kayıt ol** düğmesi varsayılan olarak açıktır (Sistem › Giriş sağlayıcıları › otomatik hesap açma). Kayıt olan hesap en kısıtlı rolle (görüntüleyici) açılır; yetkiyi yönetici Kullanıcılar sayfasından artırır (operatör / yönetici). SMTP tanımlıysa (Bildirimler › SMTP) önce e-postaya doğrulama kodu gider; SMTP yoksa hesap yönetici onayı bekler (Kullanıcılar › onayla). E-posta ile ikinci faktör (MFA) de SMTP ister ve yönetici olmayan hesaplara varsayılan olarak açıktır (Sistem › Giriş sağlayıcıları › MFA). Gmail için `smtp.gmail.com:587`, STARTTLS, kullanıcı = adres, parola = Google "uygulama şifresi"; Microsoft 365 için `smtp.office365.com:587`, STARTTLS. **Google / Microsoft / Apple ile giriş ve kayıt:** giriş sayfasında üç düğme her zaman durur; yapılandırılana kadar soluk görünür. Yeni arayüzde Sistem › *Giriş sağlayıcıları (SSO)* kartına ilgili sağlayıcının istemci bilgileri girilip *etkin* işaretlenince düğme çalışır: tıklayan kişi sağlayıcıya yönlendirilir, geri dönüşte hesabı yoksa otomatik açılır (*operatör* rolüyle; kart üzerinden kapatılabilir ya da e-posta alanı ile sınırlanabilir). Sağlayıcı tarafında tanımlanacak geri dönüş adresi kartta yazar: `http(s)://<sunucu>:8000/api/auth/oidc/<google|microsoft|apple|oidc>/callback`; dış adres farklıysa (ters vekil, TLS) *Dış adres* alanına yazın.

| Sağlayıcı | Nereden alınır | Girilecek |
|---|---|---|
| Google | console.cloud.google.com › APIs & Services › Credentials › OAuth client ID (Web application); Authorized redirect URI = geri dönüş adresi | Client ID, Client secret |
| Microsoft | portal.azure.com › Microsoft Entra ID › App registrations › New ("Accounts in this organizational directory only"); Redirect URI (Web) = geri dönüş adresi; Certificates & secrets › New client secret | Tenant (şirketin kiracı kimliği ya da alan adı; `common` kabul edilmez), Application (client) ID, secret |
| OIDC (Keycloak, Okta, Authentik…) | Sağlayıcıda yeni istemci, redirect URI = geri dönüş adresi | Issuer URL, Client ID, secret |

**Sağlayıcıyla gelen hesaplar (v1.58.5).** İlk girişte hesap **izleyici** rolüyle açılır (yönetici yükseltir). `gmail.com`, `outlook.com`, `hotmail.com`, `yahoo.com`, `icloud.com`, `yandex.com` gibi herkesin açabildiği adreslerden gelen yeni hesaplar **yönetici onayı bekler** (Kullanıcılar › onayla); şirket alan adından gelenler hemen girer. İzinli alanlar: Sistem › Giriş sağlayıcıları › "İzinli e-posta alanları" (ör. `sirket.com.tr, gmail.com`; boş = hepsi). Sağlayıcının doğrulamadığı adres (`email_verified: false`) reddedilir; yerleşik yönetici (`admin@watchover.local`) yalnız parolayla girer. Microsoft'ta `common` kiracısı kapalıdır: her Entra kiracısı giriş yapıp istediği e-postayı yazabilirdi. Google geri dönüş adresi olarak IP ya da `.local` kabul etmez; `https://watchover.sirket.com.tr/api/auth/oidc/google/callback` gibi gerçek bir alan adı gerekir (ad yalnız şirket içi DNS'te olabilir: dönüşü tarayıcı yapar, Google sunucuya bağlanmaz).

Akış standart OpenID Connect yetkilendirme kodu (PKCE) ile API üzerinden yürür, Streamlit'teki ayarlarla aynı `config.json` anahtarlarını kullanır; sırlar şifreli tutulur. Yetkiler dashboard'daki rollerle aynıdır. Uç aileleri: `datasets` (yükleme arka planda, `jobs` ile ilerleme; incident, sinyal, kanıt, postmortem, dışa aktarma), `live`, `actions`, `anomalies`, `playbook`, `agents`, `sources`, `inventory`, `knowledge`, `notify`, `users` / `roles`, `system`. Canlı alıcı (8600) aynı konteynerdedir; ajanlar ve kaynaklar doğrudan arayüzde görünür. Başka bir alan adından çağrılacaksa `WATCHOVER_CORS` ile izinli adresler yazılır. Eski Streamlit arayüzü `./watchover.sh legacy on` ile 8502'de açılır, aynı veritabanını kullanır ve bir sonraki büyük sürümde kaldırılacaktır.

**Tema ve ayrıntı panelleri (v1.11).** Sağ üstte TR / EN'in yanındaki üç düğme temayı seçer: 🌙 koyu, 🖥 sistem (işletim sisteminin tercihini izler), ☀️ açık; seçim tarayıcıda saklanır. Operasyon sayfasında her KPI kartı ve metrik kutusu (olay hızı, ERROR+, erişilebilirlik / SLA / hata bütçesi, p95 gecikme, CPU / bellek / disk / GPU, log dosyaları) tıklanınca kartların altında kapanabilir bir ayrıntı paneli açar; ortam ve sunucu süzgeci tüm kartlara uygulanır. Streamlit'teki incident kartı (flashcard, aksiyon oluştur, geri bildirim, benzer dersler, LLM istemi / açıklama, postmortem), gürültü ısı haritası, örnek veri setleri, uzak kaynaktan (HTTP / MCP) veri çekme, sütun eşleme, bilgi tabanına belge yükleme ve LLM kural önerisi, bakım kartı (canlı akışı temizle, anlık görüntü budama, zip ile dağıtım, git güncelleme) ve README sayfası React arayüzünde de vardır.

## 7c. Uygulamalar: iş uygulaması bazında izleme

Soldaki **Uygulamalar** menüsü sunucuları ve servisleri iş uygulamalarına (SAP ERP, Ödeme, İK portalı…) toplar; her uygulamanın sağlığı (istek ve hata oranı, p95, SLO, raporlayan / sessiz sunucu, açık alarm ve anomali, en kötü sunucu / servis) ve sekmelerde sunucuları, servis haritası, logları ve canlı kuyruğu, izleri, kullanıcı deneyimi, iş akışları, eşikleri ve olayları görünür. Bir uygulamanın üyeleri: açık sunucu listesi, envanterdeki *uygulama* alanı, pool, servis adı ya da deseni (`hr-*`), OpenTelemetry `service.namespace`, web / mobil uygulama ve iş akışı. **Keşif** düğmesi envanter, OTel namespace ve ortak önekli servis adlarından öneri çıkarır ve hiçbir uygulamaya ait olmayan sunucu / servisleri listeler; öneri var olan bir uygulamaya kendiliğinden eklenmez.

* **Yetkiler:** `page.apps` (görüntüleme) mevcut rollere ve ekip rollerine bir kez eklenir (Operasyon ya da APM görenler). `act.apps` (uygulama tanımlama) **hiçbir role kendiliğinden verilmez**; Kullanıcılar ve roller ya da ekip rolü düzenleyicisinden elle işaretlenir.
* **Ekipler:** bir ekip uygulama sahibi olabilir; sahiplik sunucu görünürlüğünü genişletmez (görünürlük ekibin sunucu kurallarından gelir). Başka ekibin uygulaması hiç görünmez; kurulum geneli bir uygulamada her ekip yalnız kendi sunucularını görür. Uygulama sayfasındaki **Ekibe sunucu ata** üye sunucuları ekip kapısından geçirir (başka ekibin sunucusu gelmez).
* **Log sorgusu:** `app:"SAP ERP"` (ya da `application:`) uygulamanın üye sunucularını ve servislerini seçer; canlı kuyruk, log uyarıları ve MCP `log_search` aynı ifadeyi anlar. Böyle bir uygulama yoksa eski anlamıyla servis adı olarak aranır (uyarı gösterilir).
* **Eşikler:** yeni kapsam *Uygulama*. Öncelik: sunucu > ad deseni > uygulama > pool > ortam > tümü (ekibin kendi politikası önce). Birden çok uygulamadaki bir sunucuda en sıkı uygulama seviyesi geçerlidir; bildirimler sunucunun uygulamalarını yazar.
* **MCP:** salt okunur `list_applications` ve `application_health` araçları anahtarın ekip kapsamıyla çalışır.
* Hiçbir örnek uygulama oluşmaz (simülatör kaldırıldı); eski bir sürümün eklediği örnek uygulamalar ilk açılıştaki temizlikle silinir.

## 8. TLS ve şirket ağı: uygulamayı dışarı açmak

Uygulama şirket ağına **tek bir https ucu** üzerinden açılır: `edge` profili **nginx**'i (resmî `nginx:1.30-alpine` imajı) dashboard'un önüne koyar, TLS'i kurulumun kendi sertifika otoritesiyle (ya da şirket sertifikasıyla) yapar (yalnız TLS 1.2 / 1.3, HSTS ve güvenlik başlıkları), 80'i 443'e yönlendirir ve ajan yollarını alıcıya (8600) aktarır. `edge on` açıkken düz http portu (8501) hiç yayınlanmaz (nginx konteynere konteyner ağından ulaşır; `docker-compose.edge.yml` `.env`'deki `COMPOSE_FILE` ile devreye girer), 8600 ajanlar için açık kalır; dışarıdan sadece https görünür. Yapılandırma `docker/nginx/watchover.conf.template`'tedir; arkadaki konteyner adları her istekte çözülür, bu yüzden bir konteyner yeniden oluşturulduğunda nginx'i yeniden başlatmak gerekmez.

**Sertifika.** `edge on` ilk çalıştığında `scripts/edge-cert.sh` ile bu kuruluma özel bir CA oluşturur (`./certs/.ca/watchover-ca.key`, yalnız sahibi okur; gece yedeği `./certs`'i de alır) ve sunucu sertifikasını verilen adlara ve makinenin IP'sine keser (397 gün; her `start` / `upgrade` bitişe 30 gün kala yeniler, adlar değişince yeniden keser). Kök sertifika `./certs/watchover-root.crt`'dir; tarayıcılar bir kez güvenir, ajanlar onu Ajanlar panelindeki https komutuyla sha256'sından sabitler. Şirket CA'sından alınmış bir sertifika varsa `./certs/watchover.crt` (zincirli) ve `./certs/watchover.key` olarak koyun: `edge on` onu kullanır ve hiç dokunmaz. **Caddy'den geçerken:** v1.57'den önceki kurulumlarda edge Caddy idi; yükseltmede yeni CA oluşur, bu yüzden meslektaşlar yeni `watchover-root.crt`'yi bir kez daha kurar ve https ile kurulmuş ajanlarda paneldeki komut bir kez yeniden çalıştırılır (düz http ile kurulmuş olanlar etkilenmez).

### 8.1 macOS'ta (Docker Desktop) şirket ağına açmak

```bash
cd ~/watchover-docker
./watchover.sh edge on watchover.local    # sanal ad: Mac adı Bonjour ile ağa duyurur; sertifika ada ve Mac'in IP'sine birlikte kesilir
./watchover.sh edge cert                  # kök sertifika ./certs/watchover-root.crt ve sha256'sı (meslektaşlar bir kez kurar)
./watchover.sh status
```

Meslektaşlar `https://watchover.local` yazar. `.local` adları Mac, iPhone / iPad ve Windows 10+ makinelerde ek kurulum olmadan çözülür (mDNS / Bonjour); Linux'ta `avahi-daemon` gerekir. Ad yerine ya da adla birlikte IP de verilebilir: `edge on watchover.local,192.168.1.107`; IP her durumda sertifikaya eklenir, `https://192.168.1.107` de çalışır. Duyuru bir LaunchAgent olarak kurulur (`~/Library/LaunchAgents/local.watchover.mdns.*.plist`): Mac yeniden başlasa, oturum kapansa ya da `update` çalışsa da sürer; `edge off` kaldırır. v1.42.1'den beri duyuruyu küçük bir izleyici yapar: Mac başka bir ağa geçtiğinde (Wi-Fi değişimi, yeni DHCP adresi, uykudan uyanma, USB-C Ethernet) 15 saniye içinde yeni adresi duyurur; elle bir şey yapmak gerekmez. launchd olmayan sistemlerde duyuru arka plan süreci olarak çalışır ve `install` / `update` / `start` kaybolduysa yeniden açar.

Sonra:

1. **Sabit adres.** Şirket DNS'i varsa BT'den `watchover.sirket.com` gibi bir A kaydı isteyip `./watchover.sh edge on watchover.sirket.com` demek en kalıcısıdır (Google girişi de yalnız gerçek alan adıyla çalışır). Bonjour adı yoksa ve IP ile giriliyorsa yönlendiricide Mac'e sabit IP ayırın.
2. **macOS güvenlik duvarı.** Sistem Ayarları › Ağ › Güvenlik Duvarı açıksa ilk bağlantıda Docker için "İzin ver" deyin (443 ve 80).
3. **Uyku.** Mac uyuyunca servis durur: Sistem Ayarları › Pil › Güç adaptörü › "Ekran kapalıyken otomatik uykuyu engelle", ya da terminalde `caffeinate -s &`. Docker Desktop › Settings › General › "Start Docker Desktop when you sign in" açık olsun.
4. **Kök sertifikayı dağıtmak.** `watchover-root.crt` dosyasını meslektaşlarınıza verin: macOS'ta Anahtar Zinciri Erişimi › Sistem'e sürükleyip "Her Zaman Güven"; Windows'ta `certutil -addstore -f Root watchover-root.crt`; RHEL'de `/etc/pki/ca-trust/source/anchors/` altına koyup `update-ca-trust`; Debian / Ubuntu'da `/usr/local/share/ca-certificates/` + `update-ca-certificates`. Kurmayan tarayıcı uyarısıyla yine girer. Şirket sertifikasında dağıtmak gerekmez.
5. **Ajanlar ve kaynaklar.** Diğer sunuculardaki ajanlar `https://<adres>/ingest` adresine gönderir: Ajanlar panelindeki https komutu CA'yı kendisi sabitleyip kurar (bölüm 5a). Mevcut `http://<adres>:8600/ingest` de çalışmaya devam eder (token'lı düz HTTP, sadece şirket ağı içinde).
6. **SSO.** Google / Microsoft / OIDC geri dönüş adresleri `https://<adres>/api/auth/oidc/<sağlayıcı>/callback` olur; Sistem › Giriş sağlayıcıları kartı adresi gösterir. Google özel IP'yi (192.168.x.x) geri dönüş adresi olarak kabul etmez, gerçek bir alan adı (`watchover.sirket.com`) ister; Microsoft daha esnektir.

Kapatmak: `./watchover.sh edge off` (8501 yeniden yayınlanır; Mac'te başka bir program 8501'i tutuyorsa `.env`'de `WATCHOVER_UI_PORT=8511` gibi bir port verin). Portlar: `.env` içinde `WATCHOVER_EDGE_HTTPS_PORT=8443` gibi.

### 8.2 Sunucuda (RHEL, arayüzü olmayan sunucu)

Aynı komutlar; güvenlik duvarında yalnızca 443 (ve yönlendirme / sağlık kontrolü için 80) açılır. RHEL'de `firewall-cmd --permanent --add-service=https --add-service=http && firewall-cmd --reload` (kurulum betiği bunu kendisi yapar); Ubuntu'da `ufw allow 80,443/tcp`. 8501, 8600 ve 5433 dışarıya kapalı kalabilir; ajanlar `https://<adres>/ingest` kullanır. Şirket DNS'inde A kaydı ve şirket CA'sından sertifika (`./certs/`) en temiz kurulumdur. RHEL kurulum betiği Podman'ı root olarak çalıştırır; 443 / 80 doğrudan açılır. Rootless Podman 1024'ün altındaki portları açamaz: o kurulumda betik `net.ipv4.ip_unprivileged_port_start=80` ayarını kalıcı yapar (ya da `.env`'de `WATCHOVER_EDGE_HTTPS_PORT=8443` ve yük dengeleyici 443 → 8443).

**Sunucuda masaüstü yok: arayüze nasıl girilir?** Arayüz tarayıcıdan açılır, sunucuda ekran gerekmez. Üç yol:

1. **Yük dengeleyici ya da DNS adı (kalıcı yol).** Kendi bilgisayarınızın tarayıcısında `https://watchover.sirket.com` (yük dengeleyicinin VIP adı / IP'si). Ayrıntı aşağıda.
2. **Doğrudan sunucu adresi.** Ağ izin veriyorsa `https://<sunucu IP>` (sertifika IP'yi de kapsar).
3. **SSH tüneli (yalnız SSH açıksa, ilk kurulum ve sorun giderme için).** Kendi bilgisayarınızda: `ssh -L 8443:localhost:443 kullanici@sunucu` ve tarayıcıda `https://localhost:8443`. Edge kapalıysa: `ssh -L 8501:localhost:8501 kullanici@sunucu` ve `http://localhost:8501`. Sunucuda sadece terminal varken durum için `./watchover.sh status` ve `./watchover.sh doctor --check`; sağlık için `curl -sk https://localhost/healthz`.

**Yük dengeleyici arkasında.** Önerilen düzen: yük dengeleyici 443'ü dinler ve sunucunun 443'üne gider (TLS'i ister kendisi sonlandırıp yeniden şifreler, ister TCP olarak geçirir); 80 → 443 yönlendirmesi hem dengeleyicide hem nginx'te çalışır.

- **Sağlık kontrolü:** `GET /healthz` → 200 (dashboard yanıt veriyorsa). 443'te HTTPS ya da 80'de HTTP; ikisi de yönlendirme yapmadan cevaplar. Aralık 10-30 sn, zaman aşımı 5 sn, 3 hatada düşür.
- **Gerçek istemci adresi:** dengeleyici `X-Forwarded-For` (ve `X-Forwarded-Proto`) eklesin; sonra sunucuda `./watchover.sh edge lb 10.20.30.0/28` (dengeleyicinin adresleri ya da alt ağı, birden çok verilebilir). Yalnız bu adreslerden gelen `X-Forwarded-For`'a inanılır; giriş kayıtları, kilitlenme ve denetim izi gerçek istemci adresini görür. Liste boşken başlık yok sayılır, sahte bir başlıkla adres değiştirilemez. Kaldırmak: `./watchover.sh edge lb off`. TCP geçişli (TLS passthrough) dengeleyicide başlık eklenemez; o durumda nginx dengeleyicinin adresini görür (PROXY protokolü bu sürümde yok).
- **Sertifika:** dengeleyici TLS'i sonlandırıyorsa kullanıcıların gördüğü sertifika dengeleyicinindir (şirket sertifikası oraya konur); dengeleyiciden sunucuya giden bağlantı kurulumun CA'sıyla şifrelenir; dengeleyiciye `watchover-root.crt`'yi güvenilir CA olarak tanıtın ya da sunucu sertifika doğrulamasını o havuz için kapatın. TCP geçişlide kullanıcılar doğrudan nginx'in sertifikasını görür: `edge on watchover.sirket.com,<VIP adresi>` ile sertifikaya dengeleyicinin adını da ekleyin ya da şirket sertifikasını `./certs/`'e koyun.
- **Ajanlar:** dengeleyici adresiyle de çalışır (`https://watchover.sirket.com/ingest`); büyük gövdeler için dengeleyicide istek boyutu sınırı en az 512 MB, boşta zaman aşımı en az 300 sn olsun (canlı izleme uzun bağlantı kullanır).
- **Oturum yapışkanlığı gerekmez** (tek sunucu); birden çok Watchover sunucusu tek dengeleyici arkasında aynı veriyi paylaşmaz.

Kendi nginx / HAProxy / Traefik'iniz zaten varsa `edge` yerine `docker/nginx/watchover.conf.template`'teki yolları oraya taşıyın: arayüz + API `watchover:8501`, ajan yolları (`/ingest`, `/enroll`, `/agent.py`, `/agent/`, `/rum`, `/rum.js`, `/mobile`, `/v1/`) `receiver:8600`.

**Güvenlik notu.** Bir hesap 15 dakikada 5 hatalı denemede geçici kilitlenir (`./watchover.sh user unlock EMAIL` açar); bir adres de tüm hesaplar için 15 dakikada 20 hatalı denemeden sonra yavaşlatılır. Hatalı girişlerin cevabı hesap var olsun olmasın aynıdır. E-posta ile ikinci faktör SMTP tanımlıyken yönetici olmayan hesaplara sorulur (Sistem › Ayarlar; yöneticiler için ayrı seçenek); kod gönderilemezse giriş reddedilir. Çıkış yapmak hesabın tüm oturumlarını kapatır; parola değişince ve hesap devre dışı kalınca da eski oturumlar biter. SMTP yokken kendi kendine kayıt olan hesap yönetici onaylayana kadar açılmaz. Parolalar scrypt ile, sırlar şifreli tutulur. Yönetici parolasını ilk girişte değiştirin ve `.env`'yi paylaşmayın.

### 8.3 Şirket ağından tek adres (VPN + yük dengeleyici)

Amaç: VPN'e bağlanan ve güvenlik duvarı izni olan herkes aynı linki açar, örneğin `https://watchover.sirket.com.tr`. Kimin ulaşacağına kurumsal güvenlik duvarı ve VPN karar verir; içeri girişi Watchover hesabı (ya da SSO) belirler. Ad yük dengeleyicinin (LB) sanal IP'sinde (VIP) durur, sunucu arkada kalır.

**1. Sunucuda tek komut.** LB'nin sunucuya bağlanırken kullandığı adres(ler)i (SNAT / self IP) ağ ekibinden alın; bilinmiyorsa sonra `edge lb` ile eklenir.

```bash
sudo watchover edge public watchover.sirket.com.tr 10.20.0.10/32
```

Ad sertifikanın ilk adı olur (sunucu adı ve IP de kalır), SSO geri dönüş adresleri ve web / mobil sensör (RUM) kodu bu adı kullanır (`WATCHOVER_PUBLIC_URL`; Sistem › Dış adres doluysa o geçerlidir; Ajanlar panelindeki kurulum komutu panelin açıldığı adresi kullanır, paneli bu adla açın), LB güvenilir yapılır ve ağ ekibine iletilecek liste bu sunucunun değerleriyle yazdırılır.

**2. Ağ ekibinden bir kez istenecekler.**

| Konu | Ayar |
|---|---|
| DNS | `watchover.sirket.com.tr` → LB VIP (A kaydı) |
| LB dinleyici | VIP:443/TCP → havuz üyesi `<sunucu IP>:443`. TLS ya **geçirilir** (passthrough) ya da LB'de sonlandırılıp sunucuya **yeniden şifreli** gider (`X-Forwarded-For` ve `X-Forwarded-Proto: https` eklenir). Sunucunun 80'ine düz http gönderilmez: o port yalnız https'e yönlendirir |
| Sağlık kontrolü | HTTPS `GET /healthz` → 200, `<sunucu IP>:443` (ya da HTTP `GET /healthz`, `:80`); aralık 10-30 sn, zaman aşımı 5 sn |
| Zaman aşımı / boyut | Boşta zaman aşımı en az 300 sn (canlı log izleme, uzun raporlar); istek gövdesi en az 512 MB (log yüklemeleri). Oturum yapışkanlığı gerekmez |
| Güvenlik duvarı | VPN kullanıcı alt ağları → VIP:443; LB'nin self / SNAT adresleri → `<sunucu IP>`:443 ve :80 |
| Ajanlar | İzlenen sunucular → VIP:443 (`https://watchover.sirket.com.tr/ingest`) ya da doğrudan `<sunucu IP>`:8600 |

**LB yapılandırması hazır gelir (v1.59).** `sudo watchover edge lbconf f5 <VIP>` (ya da `netscaler`, `haproxy`, `all`) bu sunucunun adı, IP'si ve portlarıyla gözden geçirilmeye hazır parçaları yazdırır: `/healthz` sağlık kontrolü (yanıt `{"ok":true,...}`), TLS'i LB'de şirket sertifikasıyla açıp sunucuya yeniden şifreleme, `X-Forwarded-For`, 300 sn boşta zaman aşımı, websocket, 80 → 443 yönlendirme; F5'te ayrıca TCP geçişi (passthrough) seçeneği. Uygulandıktan sonra `sudo watchover edge lb <LB'nin SNAT / self IP'leri>`.

**LB provası (v1.59.2).** Gerçek LB tanımlanmadan önce aynı yolu sunucunun kendisinde denemek için `sudo watchover edge lbtest`: `edge lbconf haproxy` yapılandırmasından bir HAProxy konteyneri edge'in önüne 9443 / 9080'de kurulur, sınama için güvenilir yapılır ve gerçek LB'nin geçmesi gerekenler denetlenir: LB üzerinden `/healthz` → 200 (LB'ye ve LB'den sunucuya giden iki TLS bağlantısı da doğrulanır), http → https, edge'in logunda LB'nin değil istemcinin adresi, istemcinin uydurduğu `X-Forwarded-For` yok sayılır. Bir dizüstünden `https://<ad>:9443` ile de açılır (9443 yalnız prova süresince açık). Kaldırmak: `sudo watchover edge lbtest stop` (güvenilir LB listesi eski haline döner).

**TLS sürümleri (v1.59.5).** Edge yalnız TLS 1.2 ve TLS 1.3 konuşur; SSLv3, TLS 1.0 ve 1.1 reddedilir. TLS 1.2'de yalnız ileri gizlilikli AEAD şifreler (ECDHE + AES-GCM / ChaCha20) kabul edilir; CBC, RSA anahtar değişimi, DHE ve SHA-1 kapalıdır. TLS 1.3'ün üç şifresi açıktır, eğriler X25519, P-256, P-384. Yalnız TLS 1.3 istenirse `sudo watchover edge tls 1.3` (geri almak: `edge tls 1.2`); bu durumda LB'nin sunucuya giden profili de TLS 1.3 konuşmalıdır (F5 BIG-IP'de server-ssl tarafında TLS 1.3 sürüme bağlıdır; ağ ekibi doğrular, `edge lbtest` sınar). Hangi sürümlerin kabul edildiğini `sudo watchover edge check` gösterir. `edge lbconf` çıktısındaki F5, NetScaler ve HAProxy parçaları da aynı kuralla gelir: TLS 1.2 + 1.3, aynı şifre listesi, yeniden anlaşma kapalı.

**3. Sertifika: herkesin tarayıcısında uyarısız.** Kurulumun kendi CA'sı yalnız kök sertifikasını yükleyen makinelerde güvenilir olur; "VPN'deki herkes" için şirket CA'sından sertifika alın. Şirket bilgisayarları o köke zaten güvenir.

```bash
sudo watchover edge csr                                   # /opt/watchover/certs/watchover.csr (adlar: LB adı + sunucu adı / IP)
# .csr dosyasını PKI ekibine gönderin (Microsoft AD CS: "Web Server" şablonu); anahtar sunucuda kalır (certs/.company, 0600)
sudo watchover edge company /tmp/watchover.cer /tmp/zincir.p7b   # PEM ya da DER .cer; zincir PEM, DER ya da .p7b
```

`edge company` sertifikayı yalnız son `edge csr` anahtarına aitse ve dış adı içeriyorsa kurar; zinciri sunucu sertifikasının arkasına ekler ve ajanların sabitlemesi için `certs/watchover-ca.crt` olarak saklar. Başka bir ada ya da anahtara kesilmiş sertifika reddedilir, kurulu olan değişmez. LB TLS'i şirket sertifikasıyla kendisi sonlandırıyorsa kullanıcılar LB'nin sertifikasını görür; o durumda LB'den sunucuya giden bağlantı için LB'ye `certs/watchover-root.crt` güvenilir kök olarak yüklenir (ya da aynı şirket sertifikası sunucuya da kurulur).

**4. Kontrol.**

```bash
sudo watchover edge check     # sertifika adı içeriyor mu, bu sunucu yanıt veriyor mu, DNS kaydı var mı, LB üzerinden açılıyor mu
```

Bazı LB'ler havuz üyesinin kendi VIP'ine çıkmasına izin vermez; son madde sunucudan "uyarı" verirse VPN'deki bir dizüstünden `https://watchover.sirket.com.tr` açarak deneyin.

**Gerçek istemci adresi.** TLS'i sonlandırıp yeniden şifreleyen LB `X-Forwarded-For` ekler: giriş kayıtlarında ve loglarda kullanıcının adresi görünür (önerilen). TCP geçişinde (SNAT ile) herkes LB'nin adresinden gelir: Watchover güvenilir LB adresini "adres başına hatalı giriş" yavaşlatmasına sokmaz (yoksa birkaç hatalı parola bütün şirketi 15 dakika dışarıda bırakırdı); hesap başına kilit sürer, kayıtlarda LB adresi görünür.

**Sanal makinede deneme.** Apple M4'lü Mac'te RHEL 9 VM'inde çekirdek işlemciyi yanlış bildirir (`sve2` var, `sve` yok); Watchover bunu kendisi algılayıp OpenSSL'i buna göre ayarlar (v1.58.1). Bir konteyner yine 132 ile duruyorsa: `sudo bash scripts/cpu-check.sh` hangi kütüphanenin çöktüğünü gösterir. Güvenlik duvarını elle `firewall-cmd --reload` ile yenilerseniz konteynerler birbirinin adını çözemeyebilir (edge "unhealthy"): `sudo podman network reload --all` ya da `sudo watchover doctor` onarır; kurulum `netavark-firewalld-reload.service`'i açar, sonraki reload'larda bunu kendisi yapar. VM'in penceresinde kopyala-yapıştır çalışmaz; Mac'in Terminal'inden `ssh kullanici@<VM IP>` ile bağlanın. Parallels / VMware "Shared / NAT" ağında VM yalnız kendi Mac'inizden görünür. Aynı ağdaki başka makinelerden açmak için ağ ayarını "Bridged" yapın (VM ağdan IP alır), sonra `sudo watchover edge on` (yeni IP sertifikaya eklenir) ve `https://<VM IP>`.

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
| Disk doluyor / "Arşivleme durdu" | Sistem › Depolama: hangi katman doluyor ve yıllık ihtiyaç; arşiv diskini büyütün ya da `./watchover.sh storage set --cold YENİ_DİZİN` ile daha büyük bir diske taşıyın (veri kaybı yok, arşivleme yer açılınca kendiliğinden sürer). Arşivi kısaltmak bilinçli bir karardır: `--cold-days N` (silinen günler manifestoya `expire` yazılır) |
| "Arşiv doğrulaması sorun buldu" | Sistem › Depolama › Denetim › Arşivi doğrula: `missing` / `altered` / `chain_broken` / `truncated` / `unregistered` hangi günler; dosyayı yedekten (`export` paketi ya da disk anlık görüntüsü) geri koyun, değişikliği güvenlik ekibine bildirin |
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

**3. Diğer sunucular Watchover'a nasıl bağlanır.** Veri akışı **ajanlardan Watchover'a doğru** çalışır: her izlenen sunucu, Watchover'ın 8600 portuna (edge açıksa 443 üzerinden `https://…/ingest`) kendi loglarını gönderir; Watchover sunuculara bağlanmaz. Linux sunucularda ajan bağımlılığı olmayan tek bir Go ikili dosyasıdır (`wo-agent`), macOS / Windows'ta yalnız Python 3 standart kütüphanesini kullanan `agent.py`; ikisi de **Watchover'ın kendisinden indirilir**, internet gerekmez:

```
curl -fsSL http://<watchover>:8600/agent/install.sh | sudo bash -s -- --url http://<watchover>:8600/ingest --enroll-key <Bağlantılar › Ajanlar> --env prod
```

Linux (amd64 / arm64) sunucularda `curl` (ya da `wget`) dışında hiçbir şey gerekmez; `python3` yalnız macOS'ta, diğer işlemcilerde ve `--python-agent` ile gerekir (yoksa kurulum betiği paket deposundan kurmayı dener). Eylem ajanı (bölüm 5) aynı `wo-agent` dosyasıdır; ek bir paket ya da internet gerekmez. Ajan, Watchover erişilemezken paketleri yerel diskte biriktirir ve bağlantı dönünce boşaltır. Güvenlik duvarında açılması gereken tek kural: izlenen sunuculardan Watchover'a **TCP 8600** (edge ile **443**), arayüz için istemcilerden **443/8501**. Elasticsearch, Grafana, Splunk gibi kaynaklardan **çekme** ise Watchover'dan o sistemlere doğru ilgili portlarda izin ister.

**Öğrenme ve güncellemeler internetsiz sunucuda.** Çalışma zamanında hiçbir özellik internete bağlı değildir:

- *Otomatik öğrenme (LoRA eğitimi)* sunucuda çalışır: eğitici imajı ve taban ağırlıkları (Qwen2.5 1.5B/3B/7B, Hugging Face önbelleği) pakete girer; `bundle` bunu `llm prepare` ile kendisi önbelleğe alır. Eğitim, aday model ve `watchover-ops` yayını tamamen yerel Ollama üzerinde biter. Çevrimdışı işaret varken eğitici Hugging Face'e hiç çıkmaz (`HF_HUB_OFFLINE=1`), önbellekteki taban yoksa eğitim açık hata verir, asla asılı kalmaz. Farklı bir taban seçecekseniz (`Sistem › Eğitim tabanı`) internetli makinede `./watchover.sh llm prepare qwen2.5:3b-instruct` ve yeni paket.
- *Sohbet, inceleme, hafıza (RAG), gömme (bge-m3), zafiyet taraması (gömülü danışma listesi), kural/parser öğrenme* yerel çalışır.
- *Model güncellemeleri* internetli kurulumda da otomatik değildir: Ollama modelleri yalnız `llm on` / `llm pull` ile iner, Watchover kendi kendine model çekmez. Yeni bir taban model ya da yeni Watchover sürümü istendiğinde internetli makinede indirilir ve yeni paketle taşınır; sunucudaki eğitilmiş `watchover-ops` modeli ve verileri korunur.

**Güncelleme.** Yeni sürümde internetli makinede `git pull && ./watchover.sh update && ./watchover.sh bundle`, sunucuda kaynak kodu aynı `tar xf … --strip-components=1 source` ile üzerine açıp `./watchover.sh unbundle <dosya> && ./watchover.sh update`. Veri hacimleri (`watchover-data`, `watchover-pg`) ve `.env` dokunulmaz. Çevrimdışı işareti `.env` içindeki `WATCHOVER_OFFLINE=1` satırıdır; sunucu ileride internete çıkarsa satırı silmek yeterlidir.
