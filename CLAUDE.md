# CLAUDE.md

## Proje

Signal Sprint — AO Hackathon 2026 Enterprise katılımı. Operasyonel gürültü → sinyal → gerekçeli incident → takip edilen aksiyon. Web uygulaması üzerinden canlı demo; çalışma zamanında LLM/API yok sayılır, çekirdek deterministiktir.

## Dizin Yapısı

- `app.py` — Streamlit arayüzü (Operasyon, Datasets, Playbook, ITSM, Connection Settings)
- `src/signal_sprint/` — motor (loader, format_detector, parsers/, normalize, analysis, live, itsm, connectors, llm, playbook, actions, cli); case'e özel kod **sadece** `src/signal_sprint/scenario/`
- `agent.py`, `mcp_server.py` — uzak ajan ve MCP sunucusu
- `tests/` — pytest; `samples/` — demo veri seti üretici
- `docs/` — plan, fazlar, mimari, AI_LOG
- `prompts/` — kullanılan kritik prompt'lar
- `demo/` — ekran görüntüleri ve video linki
- `AI_JURI.md`, `submission.json` — jüri özeti ve teslim künyesi

## Komutlar

```bash
# install: pip install -e ".[dev,mcp]"
# run:     streamlit run app.py          (make run)
# test:    python -m pytest -q           (make test)
# demo:    make demo                     (demo veri setini üret + çalıştır)
# mcp:     python mcp_server.py --http --port 8765
```

## Kod Kuralları

- Stack kullanıcının seçimi, değiştirme: Python 3.9+, Streamlit, pandas, python-dateutil, pytest.
- Motor `src/signal_sprint/` altında; senaryoya özel ayar yalnız `scenario/`.
- Her adım ayrı commit, `make test` yeşil; commit mesajı: kısa başlık + madde listesi.
- Branch: `claude/cool-tesla-dh9nw3`.

## Çalışma Kuralları

1. **Token tasarrufu.** Kodu her prompt'ta baştan sona tekrar okuma. Sadece
   değişecek dosyanın ilgili bölümünü oku, arama için Grep/Glob kullan,
   gereksiz açıklama ve tekrar üretme. Kısa ve net yanıt ver.
2. **AI araç kaydı.** Hackathon "AI kullanım becerisi"ni puanlıyor ve şahsi
   araçlar (Claude SAKA ve Codex dışındakiler) katılımcı sorumluluğunda
   serbest. Bu yüzden her geliştirme adımında hangi aracın (ve versiyonunun)
   hangi iş için kullanıldığı kayıt altına alınır. İki kayıt yeri:
   (a) README.md modül tablosu ("AI aracı" sütunu): dosya bazında;
   (b) `docs/AI_LOG.md`: **her geliştirme adımı için bir satır**, sadece üç
   sütun: araç + sürüm, tarih, yapılan düzeltme. Sohbet, yönlendirme veya
   iptal edilen denemeler yazılmaz. Her commit'ten önce satır eklenir.
   Bu oturumdaki model: claude-fable-5-1 (Cowork).
3. **Tek README, artımlı güncelleme.** Repoda tek bir `README.md` olur
   (docs/ altındaki plan, fazlar, mimari ve AI_LOG ile AI_JURI.md ayrı amaçlı yardımcı dosyalardır). Her
   dosya/modül için şu bilgiler tutulur: ne işe yarar, hangi AI
   aracı/versiyonu ile üretildi, ilgili kod nasıl çalışır. Her geliştirme
   sonrası README'nin sadece ilgili bölümü güncellenir; README asla baştan
   yazılmaz (Edit ile bölüm ekle/değiştir).

## Yapma

- `.env` dosyasını ASLA commit etme (`.env.example` kullan); `*.db` ve `data/live/` de commit edilmez.
- README'yi baştan yazma; sadece ilgili bölümü güncelle.
- AI_LOG tarihlerini ve kayıtlarını gerçekte olduğundan farklı yazma.

## Notlar

- Etkinlik: AI Hackathon TR 2026 "Signal Sprint". Senaryo ve veri seti
  etkinlik günü verilir, 3 saat kodlama, 17:30 hard stop, 7+3 dk sunum.
- Ürün: operasyonel gürültü -> sinyal -> gerekçeli incident -> aksiyon
  takibi. Web uygulaması üzerinden canlı demo. Çalışma zamanında LLM/API
  yok sayılır, çekirdek deterministiktir.
- Stack (kullanıcının seçimi, değiştirme): Python 3.11, Streamlit UI (`app.py`),
  pandas, stdlib csv/json, python-dateutil, pytest. Motor `src/signal_sprint/`,
  case'e özel kod sadece `src/signal_sprint/scenario/`. LLM opsiyonel, çekirdeğe bağlı değil.
- Branch: `claude/cool-tesla-dh9nw3`. Her adım ayrı commit, `make test` yeşil.
