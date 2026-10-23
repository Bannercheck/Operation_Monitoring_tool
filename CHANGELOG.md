# Watchover sürüm geçmişi

Numaralandırma: büyük sürüm **v1, v2** (ürün sahibinin kararı), küçük güncelleme **v1.1, v1.2** (her devreye alınan özellik adımı), düzeltme **v1.1.1**. Her sürüm `python -m watchover.release <major|minor|patch> "özet"` ile açılır; Sistem › Güncelleme sayfası bu dosyayı gösterir, anlık görüntüler sürüm numarasını taşır.

## v1.64.9 · 2026-10-23 · düzeltme

- Kapalı ağ paketi: yer ön kontrolü ve geçici dosya temizliği


## v1.64.8 · 2026-10-23 · düzeltme

- Kapalı ağ yükseltmesi: var olan birimler korunur


## v1.64.7 · 2026-10-23 · düzeltme

- Kurulum sunucunun kendi ajanını kurar


## v1.64.6 · 2026-10-23 · düzeltme

- keyd sağlık kontrolü Podman 4 uyumlu


## v1.64.5 · 2026-10-23 · düzeltme

- Edge yalnız konteyner ağının DNS'ini kullanır (RHEL 8 / Podman 4 502)


## v1.64.4 · 2026-10-22 · düzeltme

- PostgreSQL imajında gosu tamamen kaldırıldı


## v1.64.3 · 2026-10-22 · düzeltme

- Trivy bulguları: yamalı Compose, buildx, Ollama, PostgreSQL; pip kaldırıldı


## v1.64.2 · 2026-10-22 · düzeltme

- Sunucu kopyasında yalnız gerekenler


## v1.64.1 · 2026-10-22 · düzeltme

- Go 1.26.10, Alpine 3.24, Docker CLI 29.9.2; taban imajlar her derlemede tazelenir


## v1.64 · 2026-10-21 · güncelleme

- Tek dosyalık kurulum (internetli ve hepsi dahil)


## v1.63 · 2026-10-21 · güncelleme

- RHEL 8.6+, 9 ve 10 desteği


## v1.62.3 · 2026-10-21 · düzeltme

- Yükseltmede eski imajların temizliği


## v1.62.2 · 2026-10-21 · düzeltme

- Kaynak düzenleme


## v1.62.1 · 2026-10-21 · düzeltme

- SAP test sunucusu, S/4 tekrar düzeltmesi


## v1.62 · 2026-10-21 · güncelleme

- SAP S/4HANA / ABAP uygulama katmanı


## v1.61.1 · 2026-10-21 · düzeltme

- Güvenlik incelemesi 3: alıcı, ajan, kurulum


## v1.61 · 2026-10-20 · güncelleme

- SAP PO / PI entegrasyon izleme


## v1.60.2 · 2026-10-20 · düzeltme

- Güvenlik incelemesi 2: dışarıdan gelen veri


## v1.60.1 · 2026-10-20 · düzeltme

- Güvenlik incelemesi 1: giriş ve yetki


## v1.60 · 2026-10-20 · güncelleme

- Yeni uygulama sihirbazı


## v1.59.7 · 2026-10-20 · düzeltme

- Go 1.26.9, nginx 1.30, Alpine 3.22: bilinen açıklar kapandı


## v1.59.6 · 2026-10-20 · düzeltme

- Her sayfada Nasıl kullanılır; vite 6


## v1.59.5 · 2026-10-20 · düzeltme

- TLS 1.2 + 1.3, güçlü şifreler; edge tls 1.3


## v1.59.4 · 2026-10-20 · düzeltme

- Arayüz metinleri büyük harfle başlıyor


## v1.59.3 · 2026-10-20 · düzeltme

- Sayfa açıklamaları başlık yanındaki ⓘ düğmesinde


## v1.59.2 · 2026-10-20 · düzeltme

- edge lbtest: sunucuda LB provası


## v1.59.1 · 2026-10-19 · düzeltme

- Tema: koyu çelik mavisi + mercan, açık beyaz + açık mavi


## v1.59 · 2026-10-19 · güncelleme

- Yerel LLM hız ölçümüyle model seçimi; edge lbconf (F5/NetScaler/HAProxy)


## v1.58.5 · 2026-10-19 · düzeltme

- SSO: yeni hesap izleyici, genel posta adreslerine onay, Microsoft kiracı zorunlu


## v1.58.4 · 2026-10-19 · düzeltme

- İlk girişte parola değişimi mevcut parolayı tekrar sormuyor


## v1.58.3 · 2026-10-19 · düzeltme

- İlk girişte parola değişimi: yanlış parola çıkışa atmıyor, Türkçe kural mesajları


## v1.58.2 · 2026-10-19 · düzeltme

- RHEL: firewall reload konteyner ad çözümlemesini bozmuyor; keyd sağlık kontrolü


## v1.58.1 · 2026-10-19 · düzeltme

- Apple M4 VM'de RHEL 9: OpenSSL SVE2 koruması; .env açıklama temizliği, edge açılıyor


## v1.58 · 2026-10-16 · güncelleme

- Şirket ağından tek adres: edge public / csr / company / check


## v1.57.6 · 2026-10-16 · düzeltme

- RHEL 9 kurulumu: imaj derlemesi testte durmuyor; geri alma ctime toleranslı


## v1.57.5 · 2026-10-16 · düzeltme

- RHEL: sudo watchover komutu bulunuyor (/usr/bin bağlantısı)


## v1.57.4 · 2026-10-16 · düzeltme

- RHEL ön kontrolü kurulu RAM'i sayıyor (4096 MB VM = 4 GB)


## v1.57.3 · 2026-10-16 · düzeltme

- RHEL ön kontrolü: Red Hat CDN deponun kendi CA'sı ve abonelik sertifikasıyla denenir


## v1.57.2 · 2026-10-16 · düzeltme

- Yönetici parolası kaynak koddan kaldırıldı: kuruluma özel rastgele başlangıç parolası


## v1.57.1 · 2026-10-16 · düzeltme

- Güvenlik değerlendirmesi sonrası sıkılaştırma: giriş, MFA, oturum iptali, onaylı kayıt, CSP, react-router 7


## v1.57 · 2026-10-16 · güncelleme

- Simülatör kaldırıldı ve tam sıfırlama; nginx edge ve yük dengeleyici; uygulama bazlı entegrasyon haritası; elle log yerleri ve 1 yıllık arşiv; RHEL 9 tek komutla kurulum


## v1.56.1 · 2026-10-16 · düzeltme

- Mac'te yükseltme derlemesi: ajan güncelleme politikası içerik karşılaştırmasıyla yeniden okunuyor


## v1.56 · 2026-10-16 · güncelleme

- Demo ve gerçek veri kesin ayrı: simülasyon kapatılınca LLM hafızası dahil iz kalmaz, gerçek veri varken simülasyon açılmaz, öğrenme demo veriyi kullanmaz
- Ajanlar kendini günceller (imzalı manifest, otomatik geri dönüş); kurulum komutu sha256 ve anahtar sabitlemesiyle güvenli, edge ile HTTPS


## v1.55 · 2026-10-16 · güncelleme

- Uygulama içi güncelleme ve geri dönüş: Sistem › Güncelleme sekmesinde zip / çevrimdışı paket yükleme, adım adım güncelleme, otomatik geri dönüş, son 5 sürümden seçili sürüme dönüş
- Ayrıcalıklı işler yalnız updater konteynerinde; yeniden kimlik doğrulama, isteğe bağlı imza zorunluluğu


## v1.54 · 2026-10-16 · güncelleme

- Log ajanı Go'da: izlenen Linux sunucularda python3 gerekmez, tek curl ile wo-agent (log + uykudaki aksiyon ajanı)
- agent.py kurulu sunucularda aynı komutla kimliği koruyan geçiş; daha az CPU ve bellek


## v1.53 · 2026-10-16 · güncelleme

- Uygulama bazında izleme: Uygulamalar sayfası ve detayı, envanter ve OpenTelemetry'den keşif, uygulama kapsamlı eşikler, app: log filtresi, ekip görünürlüğü, MCP araçları


## v1.52 · 2026-10-16 · güncelleme

- Aksiyon ajanı 2. aşama (b): Go aksiyon ajanı (wo-agent), tek curl komutuyla log ajanı ve uyku halinde aksiyon ajanı kurulumu, runbook motoru, yerel model seçici
- Sunucularda Python gerekmeden aksiyon ajanı; düz HTTP kurulumda değiştiren adımlar kapalı


## v1.51 · 2026-10-16 · güncelleme

- OpenTelemetry (OTLP) alımı Go'da: OTLP/HTTP (8600, 4318) ve OTLP/gRPC (4317), bağımlılıksız protobuf ve JSON çözücü, loglar / sunucu metrikleri / span'lar Go'da dönüştürülüyor, RED özetleri Go'da
- Hatalı izlerde bekletilen span'lar da tutuluyor; ./watchover.sh otlp on|off, rhel-prepare --otlp, chart portları


## v1.50 · 2026-10-16 · güncelleme

- Aksiyon ajanı 2. aşama (a): anahtar servisi (watchover-keyd) ve anahtarlı denetim zinciri, kanıt kaynağı ve enjeksiyon karantinası, sunucu iş kanalı, runbook eşleştirici ve gölge kip
- Ekip rol şablonları act.* yetkilerini artık kendiliğinden vermiyor; elle işaretleniyor


## v1.49.2 · 2026-10-16 · düzeltme

- Giriş sayfası kısa pencerede yine kaydırılabiliyor: sayfa kaydırma kilidi yalnız uygulama kabuğu açıkken geçerli


## v1.49.1 · 2026-10-16 · düzeltme

- Safari'de kenar çubuğunun ve başlığın üstünün araç çubuğu altında kalması düzeltildi: kabuk kaymaz, yalnız içerik sütunu kayar


## v1.49 · 2026-10-15 · güncelleme

- Aksiyon ajanı temeli, iki yönlü MCP, dinamik öğrenme, RHEL ve OpenShift eşitliği


## v1.48.1 · 2026-10-15 · düzeltme

- Kaynaklar makineye göre kendiliğinden


## v1.48 · 2026-10-15 · güncelleme

- OpenShift'e hazırlık: sıcak / soğuk log, üretim kipi, boyut profili, eğitim takvimi


## v1.47.1 · 2026-10-15 · düzeltme

- Sunucu ayrıntısında canlı akış ve alarm metrikleri, geri düğmesi


## v1.47 · 2026-10-15 · güncelleme

- Sade menü ve panolar


## v1.46.1 · 2026-10-15 · düzeltme

- İz ayrıntısında istemci oturumu kartı


## v1.46 · 2026-10-15 · güncelleme

- Log gezgini, rol tabanlı ekipler, Go ayrıştırma, entegrasyon teknolojisi


## v1.45 · 2026-10-15 · güncelleme

- Ekranda tıklanan her öğe ayrıntısını açar: sunucu ayrıntı penceresi


## v1.44 · 2026-10-15 · güncelleme

- İstemci oturumları, sunucu bazında grafikler, sade keşfedilen sunucular


## v1.43 · 2026-10-15 · güncelleme

- Sunucu bazında eşik politikaları ve uyarı gönderimi (e-posta, SMS, webhook)


## v1.42.3 · 2026-10-15 · düzeltme

- Güncelleme sonrası eski ekran: index.html önbelleksiz, sürüm etiketi ve yenile bildirimi, update çalışan sürümü doğrular


## v1.42.2 · 2026-10-15 · düzeltme

- Gizlenen operasyon özellikleri aralığa göre geri geldi


## v1.42.1 · 2026-10-15 · düzeltme

- watchover.local: ağ değişince kendini yenileyen Bonjour duyurusu ve doctor komutu


## v1.42 · 2026-10-14 · güncelleme

- Operasyon ekranı yeniden tasarım ve zaman aralığına göre log süzme


## v1.41 · 2026-10-14 · güncelleme

- 200+ sunucu: pool/sunucu görünümü, tekilleştirilmiş loglar, dakikalık özetler, 100-300 bin satır/dk yük testi


## v1.40 · 2026-10-14 · güncelleme

- İş akışları (huni, takılan/hatalı kayıtlar) ve Hatalar sayfası


## v1.39 · 2026-10-14 · güncelleme

- Ekipler: sunucu bazlı görünürlük


## v1.38 · 2026-10-14 · güncelleme

- Anomali öğrenme modu


## v1.37 · 2026-10-14 · güncelleme

- Servis haritası, ağ akışları, sunucu bazında entegrasyonlar


## v1.36 · 2026-10-14 · güncelleme

- Kullanıcı deneyimi: web ve mobil


## v1.35 · 2026-10-14 · güncelleme

- Go alıcısı


## v1.34 · 2026-10-14 · güncelleme

- İz ve APM çekirdeği


## v1.33 · 2026-10-14 · güncelleme

- SigNoz düzeninde menü


## v1.32.2 · 2026-10-14 · düzeltme

- Bonjour duyurusu kalıcı (LaunchAgent), start/update yeniden duyurur


## v1.32.1 · 2026-10-14 · düzeltme

- watchover.sh upgrade: tek komutla sürüm geçişi


## v1.32 · 2026-10-14 · güncelleme

- Token sayacı ve RHEL 8.8 üretim hazırlığı


## v1.31.1 · 2026-09-22 · düzeltme

- Çevrimdışı öğrenme: llm prepare ve HF offline


## v1.31 · 2026-09-22 · güncelleme

- İnternetsiz kurulum: bundle/unbundle ve çevrimdışı mod


## v1.30 · 2026-09-22 · güncelleme

- Yük testi 2.000 olay/sn ve alım optimizasyonu: tespit önbelleği, tek pencere okuması


## v1.29 · 2026-09-22 · güncelleme

- Faz 1 ölçek: canlı pencere veritabanında, kapasite paneli, saklama ayarları, yedek rotasyonu


## v1.28.1 · 2026-09-22 · düzeltme

- Kaynak sınırları makineye göre otomatik (tüm CPU'lar), 10 CPU hatası giderildi


## v1.28 · 2026-09-22 · güncelleme

- Gözetimsiz öğrenme: trainer servisi, kalite kapısı, watchover-ops otomatik devreye alma


## v1.27.1 · 2026-09-22 · düzeltme

- LLM komutları mevcut bağlantıya dokunmaz; ilk yüklemede belge dili


## v1.27 · 2026-09-22 · güncelleme

- Kendi modelin: hafızadan eğitim verisi, CPU LoRA betiği ve kalite kapısı (watchover-ops)


## v1.26 · 2026-09-22 · güncelleme

- CPU'da yerel LLM: Ollama compose profili ve watchover.sh llm komutu


## v1.25 · 2026-09-22 · güncelleme

- Güvenlik sertleştirmesi ve envanter zafiyet taraması


## v1.24 · 2026-09-22 · güncelleme

- Kurumsal hafıza: sürekli öğrenen bilgi tabanı, hash/bge-m3 gömme, hafıza sayfası; menü sadeleştirme (Bağlantılar sekmeleri, Parser → LLM)


## v1.23 · 2026-09-22 · güncelleme

- Parser v2 adım 4: Drain şablon madenciliği ve öğrenen sütun eşlemesi


## v1.22 · 2026-09-22 · güncelleme

- Sunucu eşik uyarıları (CPU / bellek / disk / GPU) zil, anomaliler ve bildirim kurallarında


## v1.21 · 2026-09-22 · güncelleme

- Watchover'a sor sağ alt sohbet balonuna taşındı


## v1.20.2 · 2026-09-22 · düzeltme

- Menü dizisindeki derleme hatası düzeltildi


## v1.20.1 · 2026-09-22 · düzeltme

- Menüdeki eski Bildirimler öğesi kaldırıldı


## v1.20 · 2026-09-22 · güncelleme

- Sade menü (Bildirimler ve README Sistem sekmesi), açılır kapanır Sistem kartları, bildirim zili, korpus .log dosyaları depoda


## v1.19.1 · 2026-09-22 · düzeltme

- Yazı ölçeği küçültüldü (gövde 13 px, başlıklar, KPI, tablo, incident listesi)


## v1.19 · 2026-09-22 · güncelleme

- LLM incelemesi: incident kartında LLM yorumu, veri setinde LLM incelemesi sekmesi (tüm set / log satırları)


## v1.18 · 2026-09-22 · güncelleme

- Parser v2 adım 3: grok kütüphanesi (27 hazır paket, kullanıcı desenleri, test, ayrıştırılamayan satırlar)


## v1.17.1 · 2026-09-22 · düzeltme

- Envanter sayfası geri, incident kartı dayanıklılığı, rozet büyük harf, harita kök neden kartı


## v1.17 · 2026-09-22 · güncelleme

- Parser v2 adım 2: yeni biçimler (erişim, CEF/LEEF, Windows, IIS, OTel), çok satırlı olaylar, ERP aileleri (Oracle EBS, Dynamics, SAP); kapsama 44.6 → 80.8


## v1.16.1 · 2026-09-22 · düzeltme

- Parser kapsaması kartı tam genişlik


## v1.16 · 2026-09-22 · güncelleme

- Parser v2 adım 1: biçim korpusu (38 log ailesi), kapsama ve gürültü oranı raporu, gerileme testi, Sistem kartı


## v1.15.2 · 2026-09-22 · düzeltme

- Sistem: veritabanı tablo listesi süzgeçli ve daraltılabilir


## v1.15.1 · 2026-09-22 · düzeltme

- Kayıt ol: görüntüleyici rolüyle hemen giriş, eski ayar göçü; metrik kutuları simetrik


## v1.15 · 2026-09-22 · güncelleme

- Sanal ad (watchover.local, Bonjour), Kayıt ol düğmesi geri, Apple girişi kaldırıldı


## v1.14 · 2026-09-21 · güncelleme

- Logo: kenar çubuğu, giriş kartı, README sayfası, favicon


## v1.13.3 · 2026-09-21 · düzeltme

- edge: IP ile bağlanınca TLS internal error (default_sni)


## v1.13.2 · 2026-09-21 · düzeltme

- edge: HTTP/3 kapalı (yalnız TCP 443 yayınlanıyor)


## v1.13.1 · 2026-09-21 · düzeltme

- edge açıkken düz 8501 yayınlanmaz (Mac'te launcher portuyla çakışma)


## v1.13 · 2026-09-21 · güncelleme

- Şirket ağına açılış: edge profili (Caddy, TLS), watchover.sh edge on|off|cert, ajan --ca, rehber bölüm 8


## v1.12 · 2026-09-21 · güncelleme

- Veri kaynakları sayfası geri geldi (Elasticsearch / Loki / Splunk / Graylog / HTTP, katalog ve test), veri seti çekme türleri HTTP / MCP / Canlı / Kayıtlı kaynak, yumuşak açık tema


## v1.11 · 2026-09-21 · güncelleme

- Operasyon ayrıntı panelleri, geri getirilen Streamlit özellikleri (incident kartı, örnek setler, uzak çekme, ısı haritası, belge yükleme, bakım, README), koyu / sistem / açık tema


## v1.10.1 · 2026-09-21 · düzeltme

- Ollama: kurulu modeller (:latest) öneri listesinde 'kurulu' görünür, biten indirme çubukları kalkar


## v1.10 · 2026-09-21 · güncelleme

- Faz 3: Hata haritası, Watchover'a sor, LLM ve Ollama, ITSM, log arama ve karşılaştırma, log dosyaları, SLO raporu, SMTP / SMS kanalları, sistem ayarları ve geri dönüş, e-posta doğrulamalı kayıt React'e taşındı
- Docker: dashboard konteyneri artık React arayüzü + API (8501) ve canlı alıcı (8600); Streamlit legacy profili (8502)


## v1.9.1 · 2026-09-21 · düzeltme

- Giriş sayfasında Google / Microsoft / Apple düğmeleri her zaman; Apple ile giriş; Sistem › Giriş sağlayıcıları kartı; sağlayıcıdan gelen hesaplar otomatik açılır


## v1.9 · 2026-09-21 · güncelleme

- Yeni React arayüzü (faz 2): giriş + MFA + SSO, Operasyon, Anomaliler, Veri setleri ve incident detayı, Aksiyonlar, Playbook, Bilgi tabanı, Bağlantılar, Bildirimler, Kullanıcılar ve roller, Sistem; telefon ve tablet düzeni; ./watchover.sh api on ile :8000
- API: OIDC / SSO akışı (Google, Microsoft, OIDC; PKCE), derlenmiş arayüzün sunumu; Dockerfile node derleme aşaması


## v1.8 · 2026-09-21 · güncelleme

- HTTP API (FastAPI): motorun tamamı JSON uçları olarak, giriş + e-posta MFA, RBAC, arka planda veri seti yükleme, OpenAPI /api/docs; Streamlit'ten React'e geçişin 1. fazı
- Docker: api profili (./watchover.sh api on, :8000); pip install -e .[api] ile yerel çalıştırma


## v1.7.1 · 2026-09-21 · düzeltme

- watchover.sh macOS'ta (Docker Desktop) sunucu adresini doğru basar


## v1.7 · 2026-09-21 · güncelleme

- Anomali takibi: her sunucu kendi normaline göre (hata sıçraması, log fırtınası, sessizlik, yeni örüntü, metrik), operasyonel adım listesiyle takip, aksiyon bağı, bildirim kuralı
- Operasyon sayfasında Anomali takibi bölümü; act.anomaly yetkisi


## v1.6 · 2026-09-21 · güncelleme

- Kurumsal Docker kurulumu tek komutla: ./watchover.sh install (imaj sunucuda kaynak koddan derlenir, GitHub gerekmez, .env rastgele sırlarla üretilir)
- watchover.sh: update, start / stop / status / logs, mcp on|off, backup / restore, user, migrate, save / load (kapalı ağ), shell
- Kurulum rehberi baştan yazıldı: sunucu gereksinimleri, arayüze erişim, sunucu izleme ve log çekme, TLS, kapalı ağ, yedek


## v1.5.1 · 2026-09-21 · düzeltme

- MCP sunucusu Docker'da: MCP_API_KEY koruması, /health, ./datasets klasörü, sağlık denetimi; rehber bölüm 7 (istemci ayarı)


## v1.5 · 2026-09-21 · güncelleme

- PostgreSQL ürün veritabanı: kullanıcılar, roller, bilgi tabanı, aksiyonlar, playbook, ajanlar, kaynaklar, bildirimler ve rollup'lar tek veritabanında (ortak db katmanı)
- SQLite'tan taşıma: python -m watchover.migrate ve PostgreSQL'e ilk bağlanışta otomatik kopyalama
- Docker: compose PostgreSQL 16'yı zorunlu servis olarak kurar; anlık görüntü / geri dönüş PostgreSQL'de JSON döküm; kurulum rehberi yenilendi
- Gerçek PostgreSQL ile bulunan lehçe hataları düzeltildi; testler PostgreSQL üzerinde de koşar


## v1.4 · 2026-09-21 · güncelleme

- Docker dağıtımı: tek imaj (dashboard + alıcı + ajan), compose ile kurulum ve güncelleme, kaynak sınırları, isteğe bağlı PostgreSQL ve MCP profilleri
- GitHub Actions imajı ghcr.io/bannercheck/watchover olarak yayınlar (main ve sürüm etiketleri)
- Uygulama Docker farkındalığı: Sistem › Güncelleme compose komutları, imaj etiketi çipi; Docker kurulum rehberi


## v1.3.2 · 2026-09-21 · düzeltme

- SAP NetWeaver Java izleri 4 kat hızlı ayrıştırılır; biçim algılama dosyanın tamamını bölmez; ilerleme çubuğu her dosya başında ilerler


## v1.3.1 · 2026-09-21 · düzeltme

- Yükleme ilerlemesi: yüzde, aşama, dosya adı ve kalan süre tahmini (sayfa çubuğu ve kenar çubuğu)


## v1.3 · 2026-09-21 · güncelleme

- Donma giderme: canlı tampon sorguları pencereli ve tik başına memoize, Operasyon paneli aynı sorguyu bir kez hesaplar; damgalar ve git bilgisi önbellekte
- ZIP / TAR / çoklu yüklemede dosyalar 8+ çekirdekte paralel ayrıştırılır (sonuçlar birebir aynı); az çekirdekte kapalı, WATCHOVER_PARALLEL anahtarı


## v1.2.1 · 2026-09-21 · düzeltme

- Büyük dosyalarda yükleme hızı: ayrıştırma ve analiz 2 kat hızlandı (700 bin satır 22 s → 10 s), sonuçlar birebir aynı


## v1.2 · 2026-09-21 · güncelleme

- Veri seti yükleme arka planda: ayrıştırma ve analiz ayrı iş parçacığında sürer, yükleme sırasında sayfalar arasında gezilebilir
- İlerleme Veri setleri sayfasında ve kenar çubuğu DURUM kutusunda; bitince veri seti kaydedilir ve bildirim gelir


## v1.1 · 2026-09-21 · güncelleme

- Veri seti yükleme: aynı adlı veya boş SAP dosyalarında sonsuz yükleme döngüsü giderildi
- Çoklu dosya yükleme: dosyalar birlikte tek veri seti (varsayılan) ya da ayrı veri setleri olarak
- Boş dosyalar atlanır, yükleme hatası ekranda gösterilir


## v1.0 · 2026-09-20 · büyük sürüm

- Ürün sürümü: giriş kapısı (cam giriş kartı, Google / Microsoft / Apple / OIDC, Beni hatırla, e-posta MFA, kayıtta e-posta doğrulaması), yerleşik yönetici ve zorunlu parola değişimi
- Kullanıcılar sayfası: hesaplar, roller ve yetkiler matrisi, hesap ekleme; terminalden `watchover user` komutları
- Sistem sayfası: durum, bakım, güncelleme ve sürüm geçmişi (anlık görüntü / geri dönüş), giriş sağlayıcıları, e-posta ve SMS uyarıları (Türkiye SMS altyapıları), güvenlik günlüğü
- Sırlar şifreli (Fernet), parolalar scrypt; dağıtım sonrası önbellek temizliği ve sert yeniden başlatma; modeller ve veriler korunur
- Kurulu makinenin kendi kendini izlemesi (yerleşik ajan); depo `Bannercheck/Operation_Monitoring_tool`, dal `main`
- Liquid glass arayüz: cam kenar çubuğu, çip sekmeler, telefon / tablet uyumu

## v0.3 · 2026-09-18 · güncelleme

- Kaynaklar sayfası (Elasticsearch, OpenSearch, Loki, Splunk, Graylog, HTTP), çoklu sunucu kapsamı, SLO raporu dışa aktarma (haftalık / aylık), canlı öğrenme ve eğitim verisi, Envanter (CMDB-lite), simülasyon anahtarı, etkin model seçici, ajan otomatik log keşfi ve Log dosyaları kartı

## v0.2 · 2026-09-15 · güncelleme

- Canlı alıcı ve ajanlar (token, filo kayıt anahtarı, kurucu betikleri), launcher ve kurulum rehberi, Watchover'a sor (bilgi tabanı), LLM sayfası, ITSM bağlantıları, hata haritası, playbook

## v0.1 · 2026-09-13 · güncelleme

- Hackathon çekirdeği: veri seti yükleme, biçim algılama, normalizasyon, sinyal ve incident analizi, deterministik motor, Streamlit arayüzü, testler
