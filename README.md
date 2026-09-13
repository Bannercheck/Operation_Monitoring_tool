# Signal Sprint

Operational noise in, ranked incidents with evidence out, actions tracked.

Built for AI Hackathon TR 2026 (Signal Sprint). Upload an unknown log /
event / alert dataset; the pipeline normalises it, collapses noise into
signals, correlates signals into incident candidates, explains every
decision with scored factors and evidence lines, and tracks follow-up
actions. No external API is required at runtime.

## Quick start

    pip install -e ".[dev]"
    streamlit run app.py            # sidebar: upload a dataset, or click "Load demo dataset"
    signal-sprint data.zip          # CLI: profile + incidents as text   (--json for machine output)
    signal-sprint data.zip --inspect   # per-file format, roles, columns: first 10 minutes with a new dataset
    make test

Python 3.9+ (3.9 ve 3.11 üzerinde test edildi), Streamlit + pandas + python-dateutil. No LLM, API key or network needed at runtime.
Performans: 300.000 satırlık JSONL ~15 sn (yükleme 6, analiz 7, profil 2).

## Mimari

    UNKNOWN DATASET -> loader (zip/gz/tar/dir, encoding fallback) -> format_detector -> parsers/* -> Observation
      -> analysis: fingerprint (masked template) -> Signal + burst -> correlation (time + shared entities)
      -> Incident: root cause, weighted score, factors, evidence, recommendations -> actions (SQLite) -> app.py

Generic core: `src/signal_sprint/`. Case-specific code on hackathon day: **only** `src/signal_sprint/scenario/`.

## Modüller

Kural: her dosya/modül için "ne işe yarar / hangi AI aracı / nasıl çalışır"
burada tutulur ve sadece ilgili satır güncellenir. Araç kısaltması:
**CF5.1** = claude-fable-5-1 (Claude Cowork).

| Yol | Ne işe yarar | AI aracı | Nasıl çalışır |
|-----|--------------|----------|---------------|
| `CLAUDE.md` | Claude için çalışma kuralları, her oturumda yüklenir | CF5.1 | Kullanıcı kuralları yazdı, dosyaya aktarıldı |
| `.streamlit/config.toml` | Streamlit sunucu ve tema ayarları | CF5.1 | `maxUploadSize = 1024` (MB): 1 GB'a kadar dosya; koyu tema (yeşil vurgu); kullanım istatistiği kapalı |
| `src/signal_sprint/i18n.py` | TR/EN metinler ve yerelleştirilmiş anlatı | CF5.1 | `t(key)` aktif dili session state'ten okur (varsayılan TR). Kök neden gerekçesi ve korelasyon bağları motorda **kod** olarak tutulur (`root_cause_codes`, `links[].ents/gap`), metin dile göre burada üretilir: `reason_text`, `link_text`, `narrative_text`, `factor_value`, `recommendation_text`. CLI/postmortem İngilizce |
| `pyproject.toml`, `Makefile` | Paket tanımı, `signal-sprint` CLI girişi, `make run / test / inspect DS=x` | CF5.1 | `pip install -e .` ile kurulur, `src/` layout |
| `app.py` | Streamlit dashboard (frontend), TR/EN | CF5.1 | Açılış: sadece yükleme alanı, demo butonu ve 4 adımlık ikonlu pipeline şeridi (metin yok). Sidebar: dil anahtarı (🇹🇷/🇬🇧, anında değişir), upload, demo yükle, huni. **Özet**: huni kartları, profil kartları (dosya/kayıt/servis/host/hata sınıfı/kapsam), incident pencereleri gölgeli aktivite grafiği, zamana göre seviye alan grafiği, en yoğun servisler (seviye renkli), kaynak türleri halkası, öne çıkan incident kartları, **canlı log akışı** (veri setini zaman sırasıyla oynatır: oynat/duraklat/başa al, hız, satır sayısı, seviye ve metin filtresi, konum kaydırıcısı; `st.fragment(run_every)` ile saniyede bir ilerler). **Sinyaller**: seviye/patlama/servis filtreleri, progress sütunlu tablo, "NEDEN BU SİNYAL?" paneli (gerekçe, şablon, gruplama, patlama, dakika grafiği, kanıt tablosu). **Incident'lar**: kök neden kartı, yayılım Gantt'ı, skor faktör grafiği, zaman çizgisi, korelasyon bağları, ham kanıt satırı, tek tıkla öneriden aksiyon, özel aksiyon formu, postmortem indir, LLM prompt popover. **Aksiyonlar**: öncelik renkli kanban (open / in_progress / done / suppressed) |
| `src/signal_sprint/models.py` | `Observation`, `Signal` (+`why()`), `Factor` (+`data`), `Incident` (+`root_cause_codes`), `Action` | CF5.1 | Dataclass'lar. Her satır önce Observation olur; `ref` = `dosya:satır` kanıt adresi, `parser_confidence` taşır |
| `src/signal_sprint/loader.py` | Universal loader | CF5.1 | Dosya / ZIP / GZ / TAR.GZ / klasör, recursive; utf-8-sig → utf-16 → latin-1 encoding fallback; ikili dosyaları atlar |
| `src/signal_sprint/format_detector.py` | Format tespiti | CF5.1 | İlk 200 satırla json / jsonl / csv-tsv / syslog / kv / text seçer, güven skoru döner; ayraç tespiti |
| `src/signal_sprint/normalize.py` | Zaman, severity, **schema auto-mapper** | CF5.1 | `parse_timestamp` hızlı yollar: epoch s/ms → `fromisoformat` → şekil imzasına göre önbelleklenen strptime formatı → son çare dateutil (300k satırda 31 sn → 6 sn). `normalize_severity` warn/err/P1/major/syslog 0-7 → DEBUG..CRITICAL. `auto_map`: açık mapping > isim ipucu > sonek > değer bazlı tahmin (zaman gibi görünen, seviye gibi görünen, en uzun metin) |
| `src/signal_sprint/parsers/` | `base.py` arayüz, `common.py` kayıt → Observation, json / jsonl / csv / kv / syslog / text parser'ları | CF5.1 | Her parser `(satır_no, dict)` üretir; `common` rolleri ilk 50 kaydın anahtar birleşiminden çözer, zamanı olmayan satıra önceki zamanı verir, dosya adına göre kind (alert/incident) atar |
| `src/signal_sprint/pipeline.py` | Ingest orkestrasyonu | CF5.1 | loader → detector → parser; `scenario.MAPPING` uygulanır; zamana göre sıralı Observation listesi + dosya raporu |
| `src/signal_sprint/profiler.py` | **Dataset profiler** | CF5.1 | pandas ile: dosya/kayıt sayısı, olası kaynak türleri, servis/host/hata sınıfı sayısı, zaman aralığı, dakikalık seri, dosyalar arası ilişki önerisi (id/host/service benzeri kolonlarda ≥ %50 değer örtüşmesi). `profile_text` 30 saniyelik özet |
| `src/signal_sprint/analysis.py` | Deterministik motor | CF5.1 | `template_of` uuid/ip/hex/ts/url/path/sayı maskelerini **tek birleşik regex** ile uygular (+`scenario.EXTRA_MASKS`); varlık çıkarımı sinyal başına 40 örnek satırda; fingerprint = sha1(template+severity+service). `score_burst`: peak vs tüm aralıktaki medyan. `correlate`: union-find, onset'ler pencere içinde VE (ortak varlık VEYA ikisi de burst). `pick_root_cause`: en erken onset (-3/dk), altyapı kelimesi (+1.5), fan-out, severity. `build_incident`: `WEIGHTS` burst .35 / severity .25 / blast_radius .25 / duration .15, faktör katkıları toplamı = skor, öneriler `scenario.RECOMMENDATIONS`'tan. `postmortem_md`, `llm_prompt` (opsiyonel, SAKA'ya yapıştırılır) |
| `src/signal_sprint/actions.py` | Aksiyon takibi (SQLite) | CF5.1 | title, priority P1-P4, status open/in_progress/done/suppressed, owner, recommendation, evidence |
| `src/signal_sprint/cli.py` | `signal-sprint <path> [--inspect] [--json]` | CF5.1 | `--inspect`: dosya başına format, roller, zaman aralığı, severity, kolon profili. Varsayılan: profil özeti + incident'lar |
| `src/signal_sprint/scenario/__init__.py` | **Hackathon günü dokunulacak tek yer** | CF5.1 | MAPPING, EXTRA_MASKS, EXTRA_DEPENDENCY_WORDS, WINDOW_MIN, WEIGHTS, RECOMMENDATIONS |
| `samples/make_demo.py` | Sentetik demo veri seti (`demo_mixed.zip`) | CF5.1 | 914 olay, 4 dosya (jsonl, text log, syslog, csv). Zincir: DB gecikmesi → payment timeout → checkout 500 → alarm; ayrıca worker-02 disk dolu; arka plan gürültüsü |
| `tests/test_smoke.py` | Paket + pipeline + Streamlit AppTest | CF5.1 | Demo yükle, huni 914, TR→EN dil geçişi |
| `tests/test_pipeline.py` | 9 test: tespit, parser'lar, auto-map, yardımcılar, ZIP + TAR.GZ, profil, burst, incident zinciri/kök neden/gerekçe, aksiyonlar | CF5.1 | `make test` |
| `docs/PLAN.md`, `docs/DECISIONS.md` | Hedef listesi ve tasarım kararları | CF5.1 | Sunumun "planlama" bölümüne kaynak |
| `docs/AI_LOG.md` | **Prompt başına AI kanıt kaydı**: tarih, araç + sürüm, prompt özeti, üretilen, commit | CF5.1 | Hackathon öncesi 15 satır dolu; etkinlik günü için şablon hazır. Sunumun "AI stratejisi" bölümünün kaynağı |

## Demo akışı (7 dk)

1. Sidebar → "Demo veri setini yükle". Özet: huni 914 → 11 → 8 → 2, grafikler (aktivite + incident pencereleri, zamana göre seviye, servisler, kaynaklar), canlı log akışını oynat: 14:31'de hata dalgası akarken görülür. Dil anahtarı ile EN'e geçip aynı ekranı göster.
2. Signals: 532 satırlık sağlıklı trafik tek satır (burst 0), timeout sinyali burst 1.0. "WHY THIS SIGNAL?" ile kanıt + gerekçe + güven.
3. Incidents → INC-1: kök neden postgres gecikmesi, 4 belirti timeline'da, faktör grafiği, kanıt satırı, öneriler.
4. Aksiyon oluştur (P1, owner), Actions sekmesinde in_progress → done. Postmortem indir. LLM prompt'unu göster.
5. Kapanış: plan vs gerçekleşen (docs/PLAN.md), AI kullanımı (bu tablo).

## Hackathon günü (14:20 → 17:30)

| Saat | İş |
|---|---|
| 14:20–14:30 | `signal-sprint data.zip --inspect`: dosyalar, formatlar, roller |
| 14:30–14:45 | Dashboard'a yükle, profil ve rol tahminini kontrol et |
| 14:45–15:10 | Gerekirse `scenario.MAPPING` / yeni parser |
| 15:10–15:50 | `scenario`: pencere, ağırlıklar, bağımlılık kelimeleri, maskeler |
| 15:50–16:25 | Öneriler, opsiyonel LLM zenginleştirme |
| 16:25–16:50 | Demo verisi ile aksiyon akışı provası |
| 16:50–17:10 | Edge case, testler |
| 17:10–17:25 | README, AI kullanım hikâyesi, sunum |
| 17:25 | Code freeze, push, GitHub doğrulama |
