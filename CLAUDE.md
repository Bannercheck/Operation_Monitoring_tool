# Çalışma Kuralları (Claude için)

Bu dosya her oturumda geçerli. Kurallar eklendikçe güncellenir.

1. **Token tasarrufu.** Kodu her prompt'ta baştan sona tekrar okuma. Sadece
   değişecek dosyanın ilgili bölümünü oku, arama için Grep/Glob kullan,
   gereksiz açıklama ve tekrar üretme. Kısa ve net yanıt ver.
2. **AI araç kaydı.** Hackathon "AI kullanım becerisi"ni puanlıyor ve şahsi
   araçlar (Claude SAKA ve Codex dışındakiler) katılımcı sorumluluğunda
   serbest. Bu yüzden her geliştirme adımında hangi aracın (ve versiyonunun)
   hangi iş için kullanıldığı kayıt altına alınır. Kayıt yeri: README.md
   içindeki modül tablosu ("AI aracı" sütunu). Bu oturumdaki model:
   claude-fable-5-1 (Cowork).
3. **Tek README, artımlı güncelleme.** Repoda tek bir `README.md` olur. Her
   dosya/modül için şu bilgiler tutulur: ne işe yarar, hangi AI
   aracı/versiyonu ile üretildi, ilgili kod nasıl çalışır. Her geliştirme
   sonrası README'nin sadece ilgili bölümü güncellenir; README asla baştan
   yazılmaz (Edit ile bölüm ekle/değiştir).

## Proje bağlamı (kısa)

- Etkinlik: AI Hackathon TR 2026 "Signal Sprint". Senaryo ve veri seti
  etkinlik günü verilir, 3 saat kodlama, 17:30 hard stop, 7+3 dk sunum.
- Ürün: operasyonel gürültü -> sinyal -> gerekçeli incident -> aksiyon
  takibi. Web uygulaması üzerinden canlı demo. Çalışma zamanında LLM/API
  yok sayılır, çekirdek deterministiktir.
- Stack: backend Python 3.11 + FastAPI, frontend React + Vite, test pytest.
- Branch: `claude/cool-tesla-dh9nw3`. Her adım ayrı commit, `make test` yeşil.
