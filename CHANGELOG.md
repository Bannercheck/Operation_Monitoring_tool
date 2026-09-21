# Watchover sürüm geçmişi

Numaralandırma: büyük sürüm **v1, v2** (ürün sahibinin kararı), küçük güncelleme **v1.1, v1.2** (her devreye alınan özellik adımı), düzeltme **v1.1.1**. Her sürüm `python -m watchover.release <major|minor|patch> "özet"` ile açılır; Sistem › Güncelleme sayfası bu dosyayı gösterir, anlık görüntüler sürüm numarasını taşır.

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
