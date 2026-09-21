# Watchover sürüm geçmişi

Numaralandırma: büyük sürüm **v1, v2** (ürün sahibinin kararı), küçük güncelleme **v1.1, v1.2** (her devreye alınan özellik adımı), düzeltme **v1.1.1**. Her sürüm `python -m watchover.release <major|minor|patch> "özet"` ile açılır; Sistem › Güncelleme sayfası bu dosyayı gösterir, anlık görüntüler sürüm numarasını taşır.

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
