# Watchover kurulum ve devreye alma rehberi

Bu rehber üç işi adım adım anlatır: (1) dashboard'u kurmak, (2) sunuculara ajan kurmak, (3) yeni bir sistemi (SAP, veritabanı, uygulama kümesi, yeni bir veri merkezi) Watchover'a dahil etmek. Komutlar macOS ve Linux içindir; Windows sunucular için ajan şimdilik WSL veya bir Linux atlama sunucusu üzerinden log paylaşımıyla bağlanır.

## 1. Genel resim

| Bileşen | Nerede çalışır | Port | Görev |
|---|---|---|---|
| Dashboard (Streamlit) | Sizin Mac'iniz veya bir Linux sunucu | 8501 (yalnız `127.0.0.1`) | Arayüz, analiz, bilgi tabanı, LLM |
| Alıcı (receiver) | Dashboard ile aynı süreç | 8600 (tüm arayüzler) | Ajanların veri gönderdiği HTTP ucu |
| Ajan (`agent.py`) | Her izlenen sunucu | dışa port açmaz | CPU / bellek / disk / GPU + log satırları gönderir |
| LLM (isteğe bağlı) | Yerel Ollama veya OpenAI uyumlu uç | 11434 | "Watchover'a sor", kural önerisi, özetler |

Ajan tek dosyalık standart kütüphane Python'udur. Sunucuda **python3 (3.9+)** ve dashboard'un alıcı portuna ağ erişimi yeter. Prometheus, Zabbix, Docker, pip gerekmez. Motor deterministiktir; LLM olmadan her sayfa çalışır.

## 2. Dashboard kurulumu

### 2a. Kişisel makine (macOS / Linux, sudo'suz)

```bash
cd hackathon                          # kod klasörü (zip'ten açılmış veya git ile klonlanmış)
bash scripts/install.sh --with-ollama # --with-ollama: Ollama + önerilen modeller de kurulur (sihirbazın 2. adımı)
```

Kurucu Python'ı bulur, sanal ortamı `~/Library/Application Support/Watchover` (Linux: `~/.local/share/watchover`) altına kurar, `watchover` komutunu ekler, uygulamayı giriş öğesi (LaunchAgent / user systemd) olarak başlatır ve tarayıcıyı açar. Ayarlar, ajan token'ları, bilgi tabanı `…/Watchover/data` altındadır; kod klasörüne dokunulmaz.

Günlük komutlar:

```bash
watchover status | start | stop | restart | open | logs
watchover update ~/Downloads/watchover.zip   # yeni sürüm zip'ten (git yoksa)
watchover update                             # git ile klonlandıysa
```

### 2b. Linux sunucu (kurumsal, systemd, root)

```bash
sudo bash scripts/install_linux.sh --with-ollama   # /opt/watchover, kullanıcı watchover, servis :8501
sudo systemctl status watchover
```

Arayüzü dışarı açmak gerekiyorsa önüne TLS ve kimlik doğrulaması olan bir reverse proxy (nginx / Traefik / kurumun SSO ağ geçidi) koyun; Streamlit'in kendi kimlik doğrulaması yoktur.

### 2c. İlk açılış sihirbazı (4 adım)

1. **Çalışma alanı**: dil, ad, alıcı portu (8600), simülatör. Simülatör demo trafiği üretir; gerçek ajanlar bağlanınca kapatın.
2. **Yerel model**: Ollama keşfi, sohbet ve embedding modeli, eksikleri indir. LLM istemiyorsanız atlayın.
3. **Ajanlar**: alıcı adresini seçin ve ilk sunucuyu kaydedin (bölüm 4). Bu adım atlanabilir, aynı panel Bağlantı ayarları › Canlı alım altında da vardır.
4. **Bitir**: özet ve kaydet. Sihirbaz `Bağlantı ayarları › Kurulum sihirbazını yeniden çalıştır` ile her zaman geri gelir.

### 2.1 İlk giriş

Uygulama giriş yapılmadan hiçbir bilgi göstermez. İlk açılışta yerleşik yönetici hesabı hazırdır:

| Alan | Değer |
|---|---|
| E-posta | `admin@watchover.local` |
| Başlangıç parolası | Kuruluma ayrıca iletilir (giriş ekranında ve belgelerde yazmaz) |

İlk girişin hemen ardından yeni bir parola istenir. **Beni hatırla** işaretliyse tarayıcı 30 gün boyunca oturumu açık tutar; çıkış yapmak veya parolayı değiştirmek bunu sonlandırır. **MFA:** yönetici dışındaki hesaplar girişte kayıtlı e-posta adreslerine 6 haneli kod alır; bunun için Sistem › Bildirimler › Kanallar altında SMTP tanımlı olmalıdır. Google, Microsoft 365 / Exchange, Apple ve kurumsal OpenID Connect girişleri Sistem › Giriş sekmesinden açılır. Diğer hesaplar Sistem › Kullanıcılar sekmesinden açılır (kendi kendine kayıt varsayılan olarak kapalıdır). Parolalar yalnız scrypt karması olarak, SMTP / SMS / LLM / SSO sırları ise `data/.vault.key` anahtarıyla şifreli saklanır; yedek alırken `data/` klasörünü anahtar dosyasıyla birlikte alın.

## 3. Alıcı: adres, port, güvenlik duvarı

Sunucular veriyi **dashboard makinesindeki** alıcıya gönderir. Üç şeyi netleştirin:

- **Adres**: sunucuların dashboard'a ulaşabildiği IP veya DNS adı. Ajanlar panelindeki "Alıcı adresi" listesinden seçin (LAN IP, makine adı) ya da "Başka adres" ile yazın; seçim her kurulum komutunun içine yazılır. Docker'da konteyner IP'si değil, host'un adresi gerekir.
- **Port**: varsayılan 8600 (`LIVE_PORT` ile değişir). Dashboard makinesinde gelen TCP 8600 açık olmalı:

```bash
sudo ufw allow 8600/tcp                                   # Ubuntu / Debian
sudo firewall-cmd --add-port=8600/tcp --permanent && sudo firewall-cmd --reload   # RHEL / Rocky
# macOS: Sistem Ayarları › Ağ › Güvenlik Duvarı › python'a gelen bağlantılara izin ver
```

- **Taşıma**: alıcı düz HTTP'dir. Güvenilir LAN / VPN içinde tutun ya da önüne TLS reverse proxy koyun. İlk ajan kaydedildiği andan itibaren alıcı token'sız gönderimi reddeder.

Kontrol: `curl http://<alıcı>:8600/health` → `{"status":"ok", …, "agents": N}`.

## 4. Sunuculara ajan kurulumu

Her sunucunun **kendi kimliği** vardır: ad, ortam (prod / staging / test …), site (veri merkezi), etiketler. Olaylar bu kimlikle damgalanır; Operasyon ve Hata haritası sayfaları ortam ve sunucuya göre ayrı ayrı gösterir. İki yol vardır.

### Yol A: tek sunucu, sunucuya özel token

1. Dashboard › Bağlantı ayarları › Canlı alım › **Ajanı kaydet**: ad (`db-01`), ortam, site, log dosyaları (`/var/log/syslog,/var/log/messages` varsayılan; virgülle ayrılır, glob olur).
2. Panel bir kez gösterilen token'ı ve tek satır komutu verir. Komutu sunucuda root olarak çalıştırın:

```bash
curl -fsSL http://<alıcı>:8600/agent/install.sh | sudo bash -s -- --url http://<alıcı>:8600/ingest --token wo_… --logs "/var/log/syslog,/var/log/app/*.log" --env prod
```

3. Kurucu şunları yapar: python3 kontrolü (root ise kurar), `agent.py` indirir (`/opt/watchover-agent`), `agent.env` (0600) yazar, **bağlantı testi** (`agent.py --test`: adres, port ve token'ı kanıtlayan bir merhaba olayı), systemd (Linux) / LaunchDaemon (macOS) servisi kurar ve başlatır.
4. Dashboard'daki satır 30 saniye içinde **çevrimiçi** olur. Sarı "sessiz" = 2 dakikadır veri yok; gri "bekleniyor" = hiç gelmedi.

### Yol B: toplu kurulum, sunucular kendini kaydeder (10+ sunucu, Ansible / cloud-init)

1. Ajanlar panelindeki **Toplu kurulum** bloğundan filo anahtarını (`wk_…`) ve komutu alın.
2. Aynı komutu her sunucuda çalıştırın:

```bash
curl -fsSL http://<alıcı>:8600/agent/install.sh | sudo bash -s -- --url http://<alıcı>:8600/ingest --enroll-key wk_… --env prod --site IST-DC1
```

3. Kurucu `POST /enroll` ile sunucuyu **makine adıyla** kaydettirir, alıcı o sunucuya özel `wo_…` token'ı üretir, kurucu bunu `agent.env` içine yazar. Sonrası Yol A ile aynıdır.
4. Filo anahtarı yalnız kayıt yapar, veri gönderemez. Aynı ad ikinci kez kaydolamaz (HTTP 409; panelden ↻ ile yeni token verin). Dağıtım bitince **Yeni anahtar** ile eskisini geçersiz kılın ya da **Kapat** ile kendi kendine kaydı kapatın.

İsteğe bağlı parametreler: `--name db-01` (makine adı yerine), `--tags "oracle,core"`, `--logs "…"`, `--interval 10` (metrik aralığı sn), `--dir`, `--no-service` (yalnız dosyalar), `--no-test`.

### Ajanı yönetmek

```bash
sudo systemctl status watchover-agent      # Linux; log: journalctl -u watchover-agent -f
sudo launchctl print system/com.watchover.agent   # macOS; log: /opt/watchover-agent/agent.log
cat /opt/watchover-agent/agent.env         # adres, token, log listesi (0600)
curl -fsSL http://<alıcı>:8600/agent/install.sh | sudo bash -s -- --uninstall   # kaldır
```

Kurucuyu yeniden çalıştırmak ajanı günceller ve servisi yeniden başlatır (token'ı yenilediyseniz yeni token'la çalıştırın). Dashboard'dan **⏏** token'ı iptal eder (ajan 401 alır, hiçbir şey biriktirmez), **🗑** kaydı siler.

## 5. Yeni bir sistemi dahil etmek (adım adım)

Örnek: yeni bir SAP ortamı (`TPD`, 3 uygulama sunucusu, 1 HANA) Ankara veri merkezinden ekleniyor.

1. **Adlandırma standardı koyun.** Ad = makine adı (`tpd-app-01`), ortam = `prod`, site = `ANK-DC2`, etiketler = `sap,tpd`. Ortam ve site her kartta ve filtrede görünür; tutarlı yazın.
2. **Log yollarını belirleyin.** Ajan `tail -F` gibi izler; döndürülen dosyayı ve yeni eşleşen dosyaları kendisi yakalar. SAP için tipik liste:
   `/usr/sap/TPD/D*/work/dev_w*,/usr/sap/TPD/D*/work/dev_disp,/usr/sap/TPD/D*/work/available.log,/usr/sap/TPD/J*/work/*.jvm,/usr/sap/TPD/J*/log/defaultTrace*.trc`.
   HANA için `/usr/sap/TPD/HDB*/<host>/trace/*.trc`. Watchover'ın parser'ı ABAP dev trace, HANA trace, NetWeaver ListFormatter, JVM GC, tp / R3trans / SUM, SM21 dışa aktarımı ve sapstartsrv `available.log` biçimlerini tanır; bilinmeyen biçim düz metin satırı olarak yine gelir.
3. **Erişimi doğrulayın.** Sunucudan `curl -s http://<alıcı>:8600/health` cevap vermeli. Vermezse güvenlik duvarı (bölüm 3) veya adres yanlıştır.
4. **Kaydedin ve kurun.** 3 sunucu için Yol B (filo anahtarı + `--site ANK-DC2 --tags "sap,tpd" --logs "…"`), tek sunucu için Yol A. Panelde satırların yeşile dönmesini bekleyin.
5. **İlk 15 dakikayı izleyin.** Operasyon sayfası: ortam ve sunucu filtresiyle sadece yeni sistemi seçin. Kartlar canlı metrik, olay yoğunluğu ve incident'ları gösterir. Hata haritası: sunucu × servis dağılımı.
6. **Gürültüyü ayarlayın.** İlk günlerde tekrar eden ama anlamsız şablonlar çıkar (ör. periyodik `RFC ping`). Bir incident'ı "gürültü" diye işaretlemek kural önerisi üretir (`noise_template`, `noise_type`); Watchover'a sor › Kurallar altında onaylayın. Onaylı kural bir sonraki analizden itibaren o şablonu düşürür ve karar kayıtlı kalır.
7. **Sahip ve bağımlılık girin.** Aynı sekmede `owner` (servis → ekip), `dependency` (`hana → tpd-app`) ve `recommendation` kuralları kök neden sıralamasını ve önerilen aksiyonu şekillendirir.
8. **Runbook bilgisini ekleyin.** Watchover'a sor › Bilgi tabanı: ekibin runbook notlarını, bilinen hataları ve eskalasyon listesini metin olarak yapıştırın (parçalanır, tekrarlar elenir). Sohbet bu bilgiyi kaynak göstererek kullanır.
9. **Playbook ve ITSM.** Playbook sayfasında yeni servisler için standart aksiyonları tanımlayın; ITSM sekmesi ticket eşlemesini gösterir. Aksiyonlar takip edilir, kapanış gerekçesi istenir.
10. **Kayıt altına alın.** Yeni sistem için: alıcı adresi, ortam / site adları, log listesi, hangi kuralların onaylandığı. Bu rehberin sonundaki kontrol listesini doldurun.

## 6. Güncelleme

- **Dashboard**: `watchover update ~/Downloads/watchover.zip` (zip) veya `watchover update` (git), ya da uygulama içinden Sistem › Güncelleme. Önce anlık görüntü alınır, kod değişir, bağımlılıklar kurulur, önceki sürümün önbellekleri (`__pycache__`, `.pytest_cache`, Streamlit disk önbelleği) temizlenir ve uygulama **sert yeniden başlatma** ile (`watchover restart --hard`: durdur, temizle, temiz başlat) açılır; `data/` (ayarlar, token'lar, bilgi tabanı, `.env`) korunur. `watchover` komutu da kendini yeniler.
- **Ajanlar**: kurulum komutunu aynı token / anahtarla yeniden çalıştırmak yeni `agent.py`'yi indirir ve servisi yeniden başlatır. Toplu: Ansible ile aynı satır.
- **Linux sunucu kurulumu**: `sudo bash scripts/install_linux.sh` yeniden çalıştırılır; systemd servisi yeniden başlar.

## 7. Sorun giderme

| Belirti | Sebep | Çözüm |
|---|---|---|
| Giriş yapılamıyor (yönetici parolası kabul edilmiyor, hesap kilitli, hesap yok) | Önceki sürümde ilk kaydolan hesap yönetici olduğu için yerleşik yönetici oluşmamış olabilir; 5 yanlış deneme 15 dakika kilitler | Terminalden: `watchover user list` (hesapları ve kilidi gösterir), `watchover user add siz@sirket.com --admin` (parolayı sorar), `watchover user promote siz@sirket.com`, `watchover user password siz@sirket.com`, `watchover user unlock siz@sirket.com` |
| Kurucu: `could not download agent.py` | Sunucu alıcıya ulaşamıyor | Adres / port / güvenlik duvarı; `curl http://<alıcı>:8600/health` |
| Kurucu: `the receiver did not accept this agent` | Token yanlış, iptal edilmiş veya süresi dolmuş | Panelde ↻ ile yeni token, komutu yeniden çalıştır |
| Kurucu: `self-enrolment failed` (401) | Filo anahtarı yenilenmiş veya kapalı | Paneldeki güncel komutu al |
| Kurucu: `self-enrolment failed` (409) | Aynı ad zaten kayıtlı | `--name` ile farklı ad ya da paneldeki eski kaydı sil / ↻ |
| Satır sarı "sessiz" | Ajan durmuş ya da ağ kesik | `systemctl status watchover-agent`, `journalctl -u watchover-agent` |
| Ajan log: `no file matches …` | Log yolu bu sunucuda yok | Yolu düzelt (`agent.env`), servisi yeniden başlat; olmayan yol hata değildir |
| Ajan log: `rejected by the receiver (HTTP 401)` | Token iptal edildi | Yeni token ile kurucuyu çalıştır |
| Dashboard: `Address already in use` | Alıcı portu başka süreçte | Portu değiştir (`LIVE_PORT`) ya da eski süreci kapat |
| Dashboard: `AttributeError … has no attribute` sayfada | Kod güncellendi ama süreç yeniden başlamadı | `watchover restart` |
| Windows'ta kart sayısı farklı | Farklı kod sürümü ya da farklı girdi | Yan paneldeki motor / senaryo / girdi damgalarını karşılaştır |

## 8. Güvenlik özeti

- Token'lar yalnız sha256 olarak saklanır, bir kez gösterilir; sunucuda `agent.env` 0600, spool dizini 0700.
- Filo anahtarı veri gönderemez; dağıtım sonrası yenileyin.
- Alıcı: gövde sınırı 16 MB, bağlantı zaman aşımı 30 sn, ilk ajandan sonra token zorunlu.
- Arayüz varsayılan olarak yalnız `127.0.0.1`; dışarı açmak için `data/.env` içinde `WATCHOVER_UI_ADDRESS=0.0.0.0` + TLS / SSO proxy.
- Kurumsal ağda alıcıyı LAN / VPN içinde tutun veya TLS reverse proxy arkasına alın.

## Kontrol listesi: yeni sistem

- [ ] Ad / ortam / site / etiket standardı yazıldı
- [ ] Log yolları belirlendi ve sunucuda okunabilir (`test -r`)
- [ ] `curl http://<alıcı>:8600/health` sunucudan cevap verdi
- [ ] Ajanlar kuruldu, panelde yeşil
- [ ] Operasyon sayfasında ortam / sunucu filtresi ile ilk 15 dk izlendi
- [ ] Gürültü kuralları onaylandı, sahip ve bağımlılıklar girildi
- [ ] Runbook notları bilgi tabanına eklendi
- [ ] Playbook aksiyonları tanımlandı
- [ ] Filo anahtarı yenilendi veya kapatıldı
