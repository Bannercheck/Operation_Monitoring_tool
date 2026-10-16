# Watchover · OpenShift kurulum rehberi

Watchover OpenShift'te **kendi projesinde, kendi PostgreSQL pod'uyla** çalışır ve kümeden yalnız standart `restricted-v2` SCC'yi ister: konteynerler keyfi bir UID ve GID 0 ile, kök dosya sistemi salt okunur, hiçbir yetki (capability) olmadan koşar; özel SCC, ayrıcalıklı pod ya da GPU gerekmez. Kurulum `deploy/openshift/watchover` Helm chart'ıdır (Helm'siz kurulum için hazır YAML: `deploy/openshift/rendered/watchover.yaml`).

| Bileşen | Ne | Disk |
|---|---|---|
| `watchover` pod'u | iki konteyner tek pod'da: **dashboard** (React arayüzü + API, 8501) ve **receiver** (Go alıcı, 8600; ajanların kapısı). İkisi `/data` diskini paylaşır (alıcı kuyruğu `/data/spool`) | `data` (küçük), `logs-hot` (hızlı), `logs-cold` (ayrı, ucuz) |
| `watchover-postgresql` | ürün veritabanı, PostgreSQL 16, StatefulSet, yalnız Watchover pod'u erişir | `pgdata` |
| `watchover-ollama` (isteğe bağlı) | yerel LLM, yalnız CPU, tek küçük model | `ollama` |
| `watchover-mcp` (isteğe bağlı) | kendi MCP sunucumuz (8765), kendi Route'u ve anahtarı (bölüm 5b) | `mcp-datasets` (küçük) |

Ham loglar **son 48 saat hızlı diskte** (`logs-hot`) aranır; daha eskiler saatlik bir işle **ayrı, ucuz diskteki** sıkıştırılmış arşive (`logs-cold`) taşınır ve orada da aranabilir. PostgreSQL ham log tutmaz; dakikalık özetler, izler, anomaliler, envanter ve ayarlar oradadır. Kurulum **üretim modundadır** (`WATCHOVER_DEMO=0`): simülatör açılamaz, demo envanteri / RUM uygulaması / iş akışı hiç oluşmaz. Caddy edge konteyneri ve mDNS OpenShift'te kullanılmaz; TLS'i OpenShift router'ı (Route, edge) sağlar.

## RHEL sunucu vs OpenShift: aynı ürün, iki kurulum

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

## 1. Ön koşullar

| | |
|---|---|
| Araçlar | `oc` (4.12+), `helm` 3.12+, imaj derlemek için `podman` (ya da kümede derleme: `oc new-build`, bölüm 2b) |
| Yetki | Bir proje açabilen kullanıcı (`oc new-project`); küme yöneticisi gerekmez. PVC, Route, NetworkPolicy, CronJob proje yetkisiyle oluşur |
| Depolama | İki **blok** depolama sınıfı önerilir: hızlı (ör. `ocs-storagecluster-ceph-rbd`, `gp3-csi`, `thin-csi`) `data` + `logs-hot` + PostgreSQL için, ucuz/yavaş olan `logs-cold` için. Hepsi `ReadWriteOnce`. Sıcak loglar SQLite gün dosyalarıdır: **NFS / CephFS gibi paylaşımlı dosya sistemi kullanmayın** (kilit ve WAL sorunları) |
| Kaynak (varsayılan) | istek 2 CPU / 3,5 Gi, sınır 8 CPU / 13 Gi (Watchover pod'u + PostgreSQL). LLM açılırsa +2 CPU / 4 Gi istek, 8 CPU / 8 Gi sınır. Boyutlar için bölüm 6 |
| Ağ | Kümenin internet çıkışı: imaj derleme (Docker Hub, npm, PyPI, Go modülleri), `registry.redhat.io` (PostgreSQL imajı) ve LLM açıksa model indirme. İzlenen sunuculardan ingest Route adresine 443/tcp |
| Kota | Projede ResourceQuota varsa sınırlar yukarıdakilere göre ayarlanmalı; her konteynerin istek ve sınırı tanımlıdır |

```bash
oc login https://api.kume.firma.com:6443
oc new-project watchover
oc get storageclass                      # sınıf adlarını not edin (hızlı / ucuz)
```

## 2. İmajları derleyip dahili kayıt defterine göndermek

İki imaj gerekir: `watchover` (Dockerfile) ve `watchover-receiver` (Dockerfile.receiver). Eğitim konteyneri açılacaksa üçüncüsü `watchover-trainer` (Dockerfile.trainer). İmajlar OpenShift için hazırdır: yazılabilir dizinler GID 0'a aittir (`chgrp 0` + `chmod g=u`), 1024 altı port yoktur, başlangıçta root gerektiren adım yoktur. Aynı imajlar Docker Compose kurulumunda da kullanılır. Etiket olarak sürüm numarasını kullanın:

```bash
cd watchover                                          # kaynak ağacı
TAG=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)   # ör. 1.47.1
```

### 2a. podman ile (iş istasyonundan)

```bash
oc patch configs.imageregistry.operator.openshift.io/cluster --type merge -p '{"spec":{"defaultRoute":true}}'   # bir kez, küme yöneticisi
REG=$(oc registry info --public)
podman login -u "$(oc whoami)" -p "$(oc whoami -t)" "$REG"
podman build --platform linux/amd64 -f Dockerfile          -t "$REG/watchover/watchover:$TAG" --build-arg VERSION="$TAG" --build-arg GIT_REV="$(git rev-parse --short HEAD)" .
podman build --platform linux/amd64 -f Dockerfile.receiver -t "$REG/watchover/watchover-receiver:$TAG" .
podman push "$REG/watchover/watchover:$TAG"
podman push "$REG/watchover/watchover-receiver:$TAG"
```

Dış bir kayıt defteri (Quay, Harbor, Nexus) kullanılacaksa aynı komutlarla oraya gönderin ve değer dosyasında `image.registry` / `image.project` ile `imagePullSecrets` verin.

### 2b. Kümede derleme (`oc new-build --binary`, iş istasyonunda podman yoksa)

```bash
git archive --format=tar.gz -o /tmp/watchover-src.tar.gz HEAD        # yalnız kaynak; node_modules, veri, .env gitmez
for img in watchover:Dockerfile watchover-receiver:Dockerfile.receiver; do
  name=${img%%:*}; file=${img#*:}
  oc new-build --name "$name" --binary --strategy docker --to "$name:$TAG" -n watchover
  oc patch bc/"$name" -n watchover --type merge -p "{\"spec\":{\"strategy\":{\"dockerStrategy\":{\"dockerfilePath\":\"$file\"}}}}"
  oc start-build "$name" -n watchover --from-archive=/tmp/watchover-src.tar.gz --build-arg VERSION="$TAG" --follow
done
oc get imagestreamtag -n watchover                                   # watchover:1.47.1, watchover-receiver:1.47.1
```

Sonraki sürümlerde yalnız `oc start-build` satırını yeni arşiv ve yeni `--to` etiketiyle çalıştırırsınız (bc'yi `oc patch bc/watchover --type merge -p '{"spec":{"output":{"to":{"name":"watchover:YENI"}}}}'` ile yeni etikete çevirin).

## 3. Kurulum (helm install)

Kendi değer dosyanızı yazın; `values.yaml` içindeki Türkçe açıklamalar her alanı anlatır. Asgari örnek:

```yaml
# watchover-degerler.yaml
image:
  tag: "1.47.1"
persistence:
  data:     {size: 5Gi,  storageClass: ocs-storagecluster-ceph-rbd}
  logsHot:  {size: 25Gi, storageClass: ocs-storagecluster-ceph-rbd}
  logsCold: {size: 30Gi, storageClass: yavas-blok}
postgresql:
  storage: {size: 10Gi, storageClass: ocs-storagecluster-ceph-rbd}
retention:
  liveRetentionH: 48       # ham loglar sıcak diskte 48 saat
  coldRetentionDays: 30    # arşivde 30 gün (coldMaxGb dolarsa en eski gün daha önce gider)
  hotMaxGb: 20
  coldMaxGb: 25
routes:
  ui:     {host: watchover.apps.kume.firma.com}
  ingest: {host: watchover-ingest.apps.kume.firma.com}
llm:
  enabled: false
```

```bash
helm install watchover deploy/openshift/watchover -n watchover -f watchover-degerler.yaml
oc get pods -n watchover -w          # watchover-xxx 2/2 Running, watchover-postgresql-0 1/1 Running
helm status watchover -n watchover   # adresler ve sonraki adımlar
```

- PostgreSQL parolası ilk kurulumda 32 karakter üretilir ve `watchover-db` Secret'ında kalır; `helm upgrade` aynı parolayı korur, `helm uninstall` Secret'ı ve diskleri silmez (`helm.sh/resource-policy: keep`). Kendi Secret'ınızı vermek için `postgresql.existingSecret` (anahtar `postgres-password`). Parolayı değer dosyasına yazmayın.
- PostgreSQL imajı varsayılan olarak `registry.redhat.io/rhel9/postgresql-16`'dır (kümenin genel pull secret'ı ile çekilir). Erişim yoksa `postgresql.image: quay.io/sclorg/postgresql-16-c9s` (aynı imajın herkese açık eşdeğeri) ya da `postgresql.flavor: official` + `postgresql.image: docker.io/library/postgres:16`.
- Kurumda zaten bir PostgreSQL varsa: `postgresql.enabled: false` ve `externalDatabase.existingSecret` (anahtar `database-url`, `postgresql://kullanici:parola@sunucu:5432/veritabani`).
- Saklama değerleri (`retention`) ve LLM kilidi platform tarafından sabitlenir (`WATCHOVER_SETTINGS`): arayüzde değiştirilse bile Helm değeri geçerlidir. Değiştirmek için değer dosyası + `helm upgrade`.

### 3b. Helm olmadan kurulum (oc apply)

`deploy/openshift/rendered/watchover.yaml`, chart'ın varsayılanlarla `helm template` çıktısıdır ve içinde hiçbir gizli değer yoktur:

```bash
oc new-project watchover
oc create secret generic watchover-db -n watchover --from-literal=postgres-password="$(openssl rand -hex 24)"
cp deploy/openshift/rendered/watchover.yaml /tmp/watchover.yaml
sed -i 's/apps\.example\.com/apps.kume.firma.com/g' /tmp/watchover.yaml      # Route adresleri; gerekirse storageClassName ekleyin
oc apply -n watchover -f /tmp/watchover.yaml
```

## 4. İlk giriş

1. Arayüz adresini açın: `https://watchover.apps.kume.firma.com` (ya da `oc get route watchover -n watchover`).
2. Yerleşik yönetici `admin@watchover.local`'dır; başlangıç parolası kurulum ekibine ayrıca iletilir ve **ilk girişte yeni parola istenir**. Parolayı hiç paylaşmadan doğrudan terminalden belirlemek de mümkündür (yeni parolayı iki kez sorar, ilk parolayı geçersiz kılar):

```bash
oc exec -it -n watchover deploy/watchover -c dashboard -- python -m watchover.useradmin password admin@watchover.local
```

3. Kişisel yönetici hesapları açın ve yerleşik hesabı yalnız acil durum için saklayın:

```bash
oc exec -it -n watchover deploy/watchover -c dashboard -- python -m watchover.useradmin add ops@firma.com --admin
```

Şirket SSO'su (OIDC / Microsoft / Google) Sistem › Giriş sağlayıcıları'ndan bağlanır; dönüş adresi Route adresidir (`public_host` chart tarafından `https://<ui host>` olarak sabitlenir).

## 5. Sunucuları bağlamak (ajanlar)

Ajanlar ingest Route'una https ile gönderir. `routes.ingest.host` verildiğinde bu adreste yalnız alıcı yolları açıktır: `/ingest`, `/enroll`, `/agent.py`, `/agent/install.sh`, `/agent/manifest.json`, `/agent/wo-agent-…` (Go ajanının ikili dosyaları: Linux'ta log gönderimi ve eylem ajanı), `/agent/v1/` (eylem ajanlarının iş kanalı; yalnız `wa_` jetonları), `/mobile`, `/rum`, `/rum.js` (alıcının `/health` ve `/metrics` uçları küme içinde kalır). Kayıt anahtarı Bağlantılar sayfasındadır (ekip anahtarları Ekip sayfasında); aynı sayfa komutu bu adresle hazır gösterir.

```bash
# izlenecek her Linux sunucuda:
curl -fsSL https://watchover-ingest.apps.kume.firma.com/agent/install.sh | sudo bash -s -- \
     --url https://watchover-ingest.apps.kume.firma.com/ingest --enroll-key wk_… --env prod
```

- Route şirket iç CA'sıyla imzalıysa ve sunucular bu CA'ya güvenmiyorsa: sunucuya CA'yı kurun ya da kurulum komutuna `--ca /etc/pki/firma-root.pem` ekleyin (log ajanı `WATCHOVER_CA`, eylem ajanı `WO_CA` olarak saklar).
- Linux (amd64 / arm64) sunucularda komut yalnız Go ile yazılmış `wo-agent` dosyasını kurar; `python3` gerekmez. Log, metrik ve bağlantı gönderimi `watchover-agent` servisidir (`wo-agent ship`); dosya konumlarını saklar (yeniden başlatmada tekrar ya da kayıp olmaz), Route erişilemezken yerel kuyrukta biriktirir. agent.py kurulu bir sunucuda aynı komutu yeniden çalıştırmak geçişi yapar: Python servisi durdurulur, jeton (paneldeki kimlik), kuyruk ve ayarlar korunur. macOS / Windows'ta ve `--python-agent` ile ajan `agent.py`'dir. Ayrıntılar ve ölçümler `docs/KURULUM_DOCKER.md` bölüm 5'te.
- Aynı komut Linux + systemd sunucularda eylem ajanını (aynı `wo-agent` dosyası, ayrı `watchover-actuator` servisi) **uykuda** kurar: otomasyon kapalıyken yalnız merhaba der, iş almaz. Https Route ile kurulduğu için "güvensiz kurulum" sayılmaz; ayrıntılar `docs/KURULUM_DOCKER.md` bölüm 5'te. Sunucuya iş gitmeden önce bir kişi Sistem › Durum › Eylem ajanları'nda anahtar parmak izini onaylar.
- Küme içinden doğrudan alıcıya gönderecek bir bileşen (ör. OpenTelemetry collector) için `networkPolicy.ingestNamespaces: [{name: otel}]` ve adres `http://watchover.watchover.svc:8600/ingest`.
- Web/mobil kullanıcı deneyimi (RUM) betiği de bu adresi kullanır (`https://<ingest host>/rum.js`).

## 5b. MCP sunucusu (isteğe bağlı)

Docker'daki `./watchover.sh mcp on` karşılığıdır: Watchover motoru MCP istemcilerine (Claude, Cursor, kendi ajanlarınız) açılır; `analyze_dataset`, `list_incidents`, `get_incident`, `list_signals`, `evidence`, `postmortem`, `create_action`, `list_actions` araçları çağrılır, aksiyonlar arayüzle aynı veritabanına yazılır. Canlı ürün araçlarını (loglar, panolar, anomaliler, envanter) arayüzün kendi MCP ucuna iletir (`WATCHOVER_API_URL=http://watchover:8501`; NetworkPolicy bu yolu açar, anahtar arayüz pod'una da verilir; kurulum anahtarı yalnız okur, yazma için `mcp.installKeyWrite: true` ya da arayüzdeki yazma yetkili adlı anahtar). Aynı `watchover` imajı ve aynı süreç (`python mcp_server.py --http --port 8765`, tini altında), **kendi Deployment'ı** (`watchover-mcp`: çökmesi ya da yeniden başlaması arayüzü ve ajanları etkilemez), kendi Service'i ve **Route'u** (edge TLS, http → https), kendi NetworkPolicy'si (8765'e yalnız router ve `mcp.clientNamespaces`); PostgreSQL politikası MCP pod'una da izin verir. Güvenlik ayarları diğer pod'larla aynıdır (`restricted-v2`: keyfi UID, salt okunur kök, yetkisiz), istek / sınır `mcp.resources` (100m / 256 Mi → 1 CPU / 1 Gi), sağlık `/health` (startup, readiness, liveness).

```yaml
# watchover-degerler.yaml
mcp:
  enabled: true
  route: {host: watchover-mcp.apps.kume.firma.com}     # boş = kümenin ürettiği adres
  # existingSecret: benim-mcp-anahtarim                 # kendi Secret'ınız (anahtar adı mcp-api-key); boşsa üretilir
  # clientNamespaces: [{name: ajanlar}]                 # küme içinden doğrudan http://watchover-mcp.watchover.svc:8765/mcp
  datasets: {size: 5Gi, storageClass: ""}
```

```bash
helm upgrade watchover deploy/openshift/watchover -n watchover -f watchover-degerler.yaml
oc get secret watchover-mcp -n watchover -o jsonpath='{.data.mcp-api-key}' | base64 -d; echo      # istemcilere verilecek anahtar
POD=$(oc get pod -n watchover -l app.kubernetes.io/component=mcp -o jsonpath='{.items[0].metadata.name}')
oc cp uretim-log.zip watchover/$POD:/datasets/uretim-log.zip                                        # istemcilerin analiz edeceği dosya
```

İstemci ayarı: `{"mcpServers": {"watchover": {"url": "https://watchover-mcp.apps.kume.firma.com/mcp", "headers": {"Authorization": "Bearer <anahtar>"}}}}` (anahtar: Ayarlar › Bağlantılar › MCP'deki adlı bir anahtar ya da Secret'taki kurulum anahtarı); anahtarsız istekler 401 alır.

- **Anahtar** ilk kurulumda 48 karakter üretilir ve `watchover-mcp` Secret'ında kalır: `helm upgrade` aynı anahtarı korur, `helm uninstall` silmez (`helm.sh/resource-policy: keep`). Döndürmek: `oc delete secret watchover-mcp -n watchover`, `helm upgrade …` (yenisi üretilir), `oc rollout restart deploy/watchover-mcp -n watchover`. Anahtarı değer dosyasına yazmayın.
- **Helm'siz (oc apply)**: önce `oc create secret generic watchover-mcp -n watchover --from-literal=mcp-api-key="$(openssl rand -hex 24)"`, sonra hazır YAML'ı MCP ile yeniden üretin: `helm template watchover deploy/openshift/watchover -n watchover --set postgresql.existingSecret=watchover-db --set mcp.enabled=true,mcp.existingSecret=watchover-mcp --set routes.ui.host=… --set routes.ingest.host=… > watchover.yaml` ve `oc apply`.
- **RHEL kurulumundan farkı**: MCP pod'u dashboard'un `/data` diskini ve ham log disklerini görmez (ayrı pod, diskler ReadWriteOnce); olaylar ve aksiyonlar veritabanından, canlı veriler arayüz üzerinden gelir, analiz edilecek dosyalar kendi `/datasets` diskindedir. RHEL'de 8765 düz http'dir; burada Route ile TLS'lidir.
- Arayüzün kendi MCP ucu (`https://<ui route>/api/mcp`) `mcp.enabled` olmadan da vardır; bağımsız MCP sunucusu veri seti araçlarını (`analyze_dataset` …) ekler. Adlı, kapsamlı anahtarlar (hesap ya da ekip, okuma / yazma) Ayarlar › Bağlantılar › MCP'de oluşturulur ve iki uçta da geçer; Secret'taki kurulum anahtarı kurulum çapında okuma anahtarıdır.

## 5c. OpenTelemetry (OTLP)

Alıcı konteyneri OTLP/gRPC (4317) ve OTLP/HTTP (4318) dinler; Service'te `otlp-grpc` ve `otlp-http` portları vardır (`otlp.enabled: true`, varsayılan). Küme içindeki bir OpenTelemetry Collector doğrudan gönderir; ağ politikasında collector'ın ad alanını izinli yapın:

```yaml
networkPolicy:
  ingestNamespaces: [{name: otel}]
```

Collector exporter'ı (küme içi, gRPC):

```yaml
exporters:
  otlp/watchover:
    endpoint: watchover.watchover.svc:4317
    tls: {insecure: true}
    headers: {authorization: "Bearer wo_xxxxxxxx"}
```

Küme dışından OTLP/HTTP, ingest Route'u üzerinden TLS ile gelir: `otlphttp` exporter'ında `endpoint: https://watchover-ingest.apps.<küme alan adı>` (yollar `/v1/logs`, `/v1/traces`, `/v1/metrics`). Jeton, Ajanlar panelinde collector için kaydedilen ajanın jetonudur. Eşlemeler ve desteklenen metrikler için Docker kılavuzunun "OpenTelemetry (OTLP)" bölümüne bakın.

## 6. Boyutlandırma

Ölçülen değerler (yük testi verisi, 300 ajan): sıcak diskte satır başına **~65-90 bayt**, soğuk arşivde **~7-8 bayt** (şablon + lzma). Sıcak loglar gün dosyalarında tutulduğu için sıcak disk 48 değil 48-72 saat taşır; `hotMaxGb` dolarsa biten en eski gün erkenden arşive geçer, `coldMaxGb` dolarsa en eski arşiv günü silinir. İlk günün sonunda Sistem › Depolama kartından (ya da `oc exec deploy/watchover -c dashboard -- du -sh /logs/hot /logs/cold`) gerçek değeri görüp gerekirse disk büyütün (`allowVolumeExpansion: true` olan blok depolama sınıfı).

| Dakikada satır | Sunucu (yaklaşık) | logs-hot | logs-cold (30 gün) | PostgreSQL | data | Watchover pod'u (istek → sınır) | PostgreSQL pod'u |
|---|---|---|---|---|---|---|---|
| 50 bin | ~100 | 30 Gi (`hotMaxGb: 24`) | 25 Gi (`coldMaxGb: 20`) | 15 Gi | 20 Gi | 2 CPU / 6 Gi → 4 CPU / 12 Gi | 1 / 2 Gi → 2 / 4 Gi |
| **100 bin** (varsayılan) | ~200-300 | **50 Gi** (`hotMaxGb: 40`) | **50 Gi** (`coldMaxGb: 40`) | **30 Gi** | **30 Gi** | **2 CPU / 8 Gi → 6 CPU / 16 Gi** | **1 / 4 Gi → 2 / 8 Gi** |
| 300 bin | 300+ | 150 Gi (`hotMaxGb: 120`) | 130 Gi (`coldMaxGb: 105`) | 50 Gi | 40 Gi | 8 CPU / 16 Gi → 12 CPU / 24 Gi | 2 / 8 Gi → 4 / 12 Gi |

Watchover pod'u panel + alıcıdır (alıcı ayrıca 0,5 CPU / 512 Mi → 2 CPU / 2 Gi). Gece arşivleme bir çekirdeği düşük öncelikle birkaç saat kullanır (dakikada 100 bin satırda günde ~2 saat). Dakikada ~300 bin satır tek pod'un tavanına yakındır (docs/LOAD_TEST.md).

**Tam performans profili** — `deploy/openshift/watchover/values-performance.yaml`: istek = sınır (Guaranteed QoS: yoğun anda CPU kısıtlaması yok), panel 6 CPU / 16 Gi, alıcı 2 CPU / 2 Gi, PostgreSQL 2 CPU / 8 Gi (`shared_buffers` 2 GB), diskler tablodaki "100 bin" satırının değerleri. Sıcak loglar ve PostgreSQL için yerel NVMe/SSD blok depolama (LVM Storage ya da Local Storage Operator) seçin, NFS / CephFS kullanmayın; pod'u `nodeSelector` / `tolerations` ile az kalabalık, sabit bir düğüme koyun:

```bash
helm upgrade --install watchover deploy/openshift/watchover -n watchover \
  -f deploy/openshift/watchover/values-performance.yaml -f benim-degerlerim.yaml
```

Proje kotası önerisi (LLM kapalı): CPU 12, bellek 32 Gi, depolama 250 Gi; LLM açılacaksa CPU 20, bellek 40 Gi.

## 7. Yükseltme ve geri alma

```bash
# 1) yeni sürümün imajları (bölüm 2), ör. 1.48.0
# 2) etiketi değiştirip yükseltin; pod Recreate stratejisiyle durup yenisi açılır (kısa kesinti, ajanlar kendi kuyruklarında bekler)
helm upgrade watchover deploy/openshift/watchover -n watchover -f watchover-degerler.yaml --set image.tag=1.48.0
oc rollout status deploy/watchover -n watchover
# geri alma: bir önceki revizyon (imaj etiketi dahil)
helm history watchover -n watchover
helm rollback watchover <REVIZYON> -n watchover
```

Veritabanı göçleri ileriye dönüktür; geri almadan önce yedek alın (bölüm 8). Chart'ı da güncellediyseniz `deploy/openshift` klasörünü yeni sürümden alın.

## 8. Yedekleme

**Veritabanı.** `backup.enabled: true` her gece `pg_dump` (özel biçim, sıkıştırılmış) alan bir CronJob ve ona ait 20 Gi'lik bir disk açar; `backup.keepDays` (14) günden eskiler silinir:

```yaml
backup:
  enabled: true
  schedule: "15 2 * * *"
  keepDays: 14
  storage: {size: 20Gi, storageClass: yavas-blok}
```

```bash
oc create job --from=cronjob/watchover-backup elle-yedek -n watchover        # hemen bir yedek
# geri yükleme: Watchover'ı durdurun, dump'ı PostgreSQL pod'una kopyalayıp pg_restore
oc scale deploy/watchover -n watchover --replicas=0
oc cp ./watchover-20261015-0215.dump watchover/watchover-postgresql-0:/tmp/w.dump
oc exec -n watchover watchover-postgresql-0 -- pg_restore -h 127.0.0.1 -U watchover -d watchover --clean --if-exists /tmp/w.dump
oc scale deploy/watchover -n watchover --replicas=1
```

**Diskler.** CSI sürücüsü destekliyorsa `data` ve `logs-cold` için VolumeSnapshot alın (sıcak loglar 48 saatte zaten arşive geçer):

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata: {name: watchover-logs-cold-20261015, namespace: watchover}
spec:
  volumeSnapshotClassName: ocs-storagecluster-rbdplugin-snapclass
  source: {persistentVolumeClaimName: watchover-logs-cold}
```

Sistem › Anlık görüntü (veritabanı dahil) PostgreSQL'in tamamını JSON olarak `/data`'ya yazar; büyük kurulumda `data` diskini doldurur, üretimde yukarıdaki yolları kullanın.

**Anahtarlar (watchover-keyd).** Aksiyon ajanının anahtarları `keyd` kabının kendi diskinde (`watchover-keys`) durur; o diski yalnız bu kap bağlar, veritabanı ve `data` yedeklerine girmez. Kasa anahtarı `watchover-secrets` diskindedir. `keys` diskinin VolumeSnapshot'ını ayrı bir sorumlu alır ya da Otomasyon › Anahtarlar'dan iki `act.policy` sahibi şifreli dışa aktarım yapar. Denetim zinciri yalnız eklenir: eski bir `pg_dump`'ı `--clean` ile geri yüklemek zincirde bir boşluk olarak görünür (tasarım gereği, hiçbir kayıt silinmez); boşluğu iki `act.policy` sahibi onaylayana dek otomatik çalıştırma L2'de kalır. `keyd.enabled: false` anahtar servisini kapatır (sistem yürütmesi kapalı, yalnız rehberli mod).

## 9. LLM'i kısıtlamak

Watchover'ın çekirdeği deterministiktir; LLM yalnız açıklama ve soru-cevap yardımcısıdır ve kapalıyken her şey çalışır. Üç seçenek:

| Seçenek | Değerler | Sonuç |
|---|---|---|
| Kapalı (varsayılan) | `llm.enabled: false`, `llm.lock: true` | Ollama pod'u yok; LLM ayarları boş sabitlenir, arayüzden dış bir sağlayıcı (OpenAI vb.) da tanımlanamaz |
| Yalnız küme içi model | `llm.enabled: true`, `llm.lock: true`, `llm.model: qwen2.5:3b-instruct` | Ollama pod'u (yalnız CPU, 2→8 CPU / 4→8 Gi), açılışta tek model iner; Watchover yalnız bu modeli kullanır, ayar arayüzden değiştirilemez. Ollama'ya yalnız Watchover pod'u erişir (NetworkPolicy) |
| Model indikten sonra dışa çıkış yok | yukarıdakine ek `llm.ollama.allowEgress: false` | Ollama pod'unun dışarıya hiçbir çıkışı kalmaz (Egress NetworkPolicy). Model diskte kaldığı için çalışmaya devam eder; yeni model için geçici olarak `true` yapın |

Çıkışı yalnız model deposuna sınırlamak isterseniz (OVN-Kubernetes), küme yöneticisi projeye bir EgressFirewall yazar; Ollama model katmanlarını `registry.ollama.ai` üzerinden bir CDN'den indirir, CDN adı değişebildiği için en sağlam yol yukarıdaki "indir, sonra kapat" düzenidir:

```yaml
apiVersion: k8s.ovn.org/v1
kind: EgressFirewall
metadata: {name: default, namespace: watchover}
spec:
  egress:
    - {type: Allow, to: {dnsName: registry.ollama.ai}}
    - {type: Allow, to: {dnsName: smtp.firma.com}}      # Watchover'ın kendi çıkışları (e-posta, ITSM, webhook) da listelenmeli
    - {type: Deny,  to: {cidrSelector: 0.0.0.0/0}}
```

Eğitim konteyneri (`trainer.enabled`) varsayılan kapalıdır; açılırsa Watchover pod'una üçüncü konteyner olarak eklenir, ilk eğitimde Hugging Face'ten temel ağırlıkları indirir ve saatlerce CPU kullanır.

## 10. Demo verisi

OpenShift kurulumu üretim modundadır; simülatör hiç açılamaz ve örnek uygulama (Uygulamalar menüsü) oluşturulmaz; uygulamaları Uygulamalar › Keşif ya da Yeni uygulama ile siz tanımlarsınız (`act.apps` yetkisi rollere elle verilir). Docker'dan taşınan ya da demo modunda çalışmış bir kurulumda kalan simülatör verisini (simülatörün satırları, "demo (simulator)" envanter kayıtları, demo RUM uygulamaları ve oturumları, demo iş akışı ve yalnız simülatörün kullandığı sunucu adlarına ait kayıtlar) Sistem › Demo verisi › **Demo verisini temizle** siler; yalnız kurulum yöneticisi görür, işlem denetim kaydına yazılır. Aynı adı taşıyan gerçek bir sunucunun (ör. kendi `db-01`'iniz) verisine dokunulmaz. Terminalden önce sayım, sonra silme:

```bash
oc exec -n watchover deploy/watchover -c dashboard -- python -m watchover.demo clean --dry-run
oc exec -n watchover deploy/watchover -c dashboard -- python -m watchover.demo clean
oc rollout restart deploy/watchover -n watchover      # çalışan pod'un bellekteki canlı penceresi de unutsun
```

## 11. Sorun giderme

| Belirti | Neden | Çözüm |
|---|---|---|
| Pod `CreateContainerConfigError` / `unable to validate against any security context constraint` | Konteynerde sabit `runAsUser`, ek capability ya da ayrıcalık istendi (kendi değişikliğiniz) | Chart UID vermez; `oc get pod <pod> -o jsonpath='{.metadata.annotations.openshift\.io/scc}'` `restricted-v2` olmalı. `extraEnv` dışında securityContext'e dokunmayın |
| `Permission denied` / `Read-only file system` loglarda | OpenShift uyumlu olmayan eski bir imaj ya da yazılabilir olmayan bir yol | Bu rehberle gelen kaynak ağacından derlenmiş imajları kullanın; yazılan yollar yalnız `/data`, `/logs/hot`, `/logs/cold`, `/tmp` |
| PVC `Pending` | Depolama sınıfı yok / yanlış ad, `WaitForFirstConsumer` (pod planlanınca bağlanır), kota | `oc describe pvc -n watchover`; `oc get storageclass`; değer dosyasında `storageClass` |
| Route `503 Application is not available` | Pod hazır değil (ilk açılışta veritabanı göçü), Service eşleşmiyor, NetworkPolicy router'ı engelliyor | `oc get pods`, `oc logs deploy/watchover -c dashboard`; router ad alanında `network.openshift.io/policy-group: ingress` etiketi olmalı (OpenShift 4 varsayılanı) |
| Ajan `401 invalid or revoked token` | Kayıt anahtarı yanlış/döndürülmüş, ajan iptal edilmiş, alıcı `auth.json`'u henüz okumadı (ilk saniyeler) | Bağlantılar'da anahtarı ve ajan durumunu kontrol edin; `oc logs deploy/watchover -c receiver`; kurulum komutunu yeni anahtarla tekrarlayın |
| Ajan `503 receiver starting` | Dashboard ayağa kalkmadan alıcı istek aldı | Ajan kendi kuyruğunda bekleyip yeniden dener; sürerse dashboard loglarına bakın |
| Ajan TLS hatası | Route kurum CA'sıyla imzalı, sunucu güvenmiyor | Sunucuya CA'yı kurun ya da `--ca` ile verin |
| `ImagePullBackOff` (postgresql) | `registry.redhat.io` için pull secret yok | `postgresql.image: quay.io/sclorg/postgresql-16-c9s` |
| `ImagePullBackOff` (watchover) | Etiket yok ya da başka projedeki imaj | `oc get istag -n watchover`; başka projedeyse `oc policy add-role-to-group system:image-puller system:serviceaccounts:watchover -n <imaj projesi>` |
| `OOMKilled` (dashboard) | Bellek sınırı aşıldı (yoğun hacim, büyük veri seti yüklemesi) | `dashboard.resources.limits.memory` artırın (bölüm 6) |
| Model inmiyor | Ollama'nın dışa çıkışı yok (`allowEgress: false` ya da EgressFirewall) | Geçici `allowEgress: true` + `oc rollout restart deploy/watchover-ollama` |
| MCP istemcisi `401 unauthorized` | Anahtar yanlış / döndürüldü, başlık eksik | anahtar `watchover-mcp` Secret'ının `mcp-api-key` alanındadır (bölüm 5b'deki `oc get secret` komutu); istemci `Authorization: Bearer <anahtar>` göndermeli |
| MCP Route `503` / pod `CrashLoopBackOff` | PostgreSQL'e ulaşamıyor (ilk kurulumda veritabanı hazır değil) ya da `existingSecret` yok | `oc logs deploy/watchover-mcp -n watchover`; `oc get secret`; arayüz ve ajanlar bundan etkilenmez |

Loglar: `oc logs -n watchover deploy/watchover -c dashboard` (arayüz/API), `-c receiver` (alıcı), `oc logs -n watchover watchover-postgresql-0`, `oc logs -n watchover deploy/watchover-mcp` (MCP).
