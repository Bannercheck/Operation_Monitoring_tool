# Signal Sprint

Operational noise in, ranked incidents with evidence out, actions tracked.

Built for AI Hackathon TR 2026 (Signal Sprint). Upload an unknown log /
event / alert dataset; the pipeline normalises it, collapses noise into
signals, correlates signals into incident candidates, explains every
decision with scored factors and evidence lines, and tracks follow-up
actions. No external API is required at runtime.

## Quick start

    make demo                                   # generates sample data and opens the dashboard on :8000
    python3 signal_sprint.py inspect data.zip   # what is in an unknown dataset?
    python3 signal_sprint.py analyze data.zip   # pipeline results as markdown (--json for machine output)
    python3 signal_sprint.py serve data.zip     # web dashboard; upload also works from the UI
    make test

Python 3.10+, standard library only. No API keys, no network.

## Modüller

Kural: her dosya/modül için "ne işe yarar / hangi AI aracı / nasıl çalışır"
burada tutulur ve sadece ilgili satır güncellenir. Araç kısaltması:
**CF5.1** = claude-fable-5-1 (Claude Cowork).

| Yol | Ne işe yarar | AI aracı | Nasıl çalışır |
|-----|--------------|----------|---------------|
| `CLAUDE.md` | Claude için çalışma kuralları, her oturumda yüklenir | CF5.1 | Kullanıcı kuralları yazdı, dosyaya aktarıldı |
| `Makefile` | `make test / demo / inspect DS=x / analyze DS=x` | CF5.1 | Kısayollar, tek dosyayı çağırır |
| `signal_sprint.py` | Ürünün tamamı, tek dosya, sadece stdlib | CF5.1 | Aşağıdaki bölümler aynı dosyanın numaralı başlıklarıdır |
| `signal_sprint.py` §1 model | `Observation`, `Signal`, `Factor`, `Incident` dataclass'ları | CF5.1 | Her satır önce Observation olur; analiz sadece bunu görür. `ref` = `dosya:satır` kanıt adresi |
| §2 loading + detect | Dosya/ZIP/GZ/klasör açma ve format tespiti | CF5.1 | `iter_files` recursive açar, ikili dosyaları atlar. `detect_format` ilk 200 satırla json / jsonl / csv-tsv / syslog / kv / plain seçer, güven skoru verir |
| §3 helpers | Zaman damgası, severity, rol eşleme | CF5.1 | `parse_timestamp` 15+ format + epoch s/ms, hep tz-aware UTC. `normalize_severity` warn/err/P1/syslog 0-7 → DEBUG..CRITICAL. `resolve_roles` isim ipuçlarıyla timestamp/severity/service/host/message sütunlarını seçer, açık mapping öncelikli |
| §4 parsers | 6 format için kayıt üreticileri ve `to_observations` | CF5.1 | Her parser `(satır_no, dict)` üretir. `to_observations` rolleri ilk 50 kaydın anahtar birleşiminden çözer, zamanı olmayan satıra önceki zamanı verir, rol dışı alanları `attributes`'a koyar |
| §5 noise reduction | Şablon çıkarma, fingerprint, Signal, burst | CF5.1 | `template_of` uuid/ip/hex/ts/url/path/sayıları maskeler. fingerprint = sha1(template+severity+service). `score_burst`: dakikalık peak vs tüm veri aralığındaki medyan (sessiz dakikalar 0 sayılır); sürekli trafik burst 0, ani patlama 1 |
| §6 correlation | Sinyalleri incident adayına gruplar, kök neden seçer | CF5.1 | Union-find: onset'ler 5 dk içinde VE (ortak varlık: servis/host/ip/id VEYA ikisi de burst). `pick_root_cause`: en erken onset ağır basar (-3/dk), altyapı kelimesi (+1.5), fan-out (+0.3/sinyal), severity (+0.3) |
| §7 scoring + explain | Skor, faktörler, anlatı, postmortem, Claude promptu | CF5.1 | `WEIGHTS` burst .35 / severity .25 / blast_radius .25 / duration .15, sprint günü burada ayarlanır. Her faktör değer + katkı taşır, toplam = skor. `postmortem_md` ve `claude_prompt` (API'siz, SAKA'ya yapıştırılır, kanıt ref'leri zorunlu) |
| §8 pipeline | `Analysis` sınıfı: uçtan uca akış ve `overview()` | CF5.1 | Tek sinyalli zayıf gruplar elenir (ERROR+ veya burst ≥ .5 değilse). Incident'lar skora göre sıralanır |
| §9 actions | SQLite aksiyon takibi | CF5.1 | `ActionStore`: open / in_progress / done, atama, not, zaman damgaları |
| §10 web | Gömülü dashboard, stdlib `http.server` | CF5.1 | Tek HTML+JS sayfası: Upload (sürükle-bırak), Overview (KPI + dakika grafiği), Signals, Incidents, Incident detail (timeline, faktör çubukları, tıklanabilir kanıt, aksiyon ekleme, postmortem export, Claude prompt kopyala), Actions kanban. API: `/api/overview, /signals, /incidents, /evidence/{ref}, /actions` |
| §11 cli | `inspect / analyze / serve` komutları | CF5.1 | `inspect` format, roller, zaman aralığı, severity dağılımı, sütun profili basar |
| `samples/make_demo.py` | Sentetik demo veri seti üretir (`demo_mixed.zip`) | CF5.1 | 914 olay, 4 dosya (jsonl, plain log, syslog, csv). Gömülü zincir: DB gecikmesi → payment timeout → checkout 500 → alarm; ayrıca worker-02 disk dolu; arka plan gürültüsü |
| `tests/test_signal_sprint.py` | 9 test: tespit, parser'lar, mapping, yardımcılar, ZIP, demo üzerinde azaltma/burst/incident zinciri/kök neden, postmortem, aksiyonlar | CF5.1 | `make test` |
| `docs/PLAN.md`, `docs/DECISIONS.md` | Hedef listesi ve tasarım kararları | CF5.1 | Sunumun "planlama" bölümüne kaynak |

## Demo akışı (7 dk)

1. Upload: `demo_mixed.zip` sürükle, ingest raporunda 4 dosya 4 format.
2. Overview: 914 olay → 11 sinyal → 2 incident, 83x azaltma, dakika grafiğinde 14:31 tepesi.
3. Signals: 532 satırlık sağlıklı trafik tek satır, burst 0; timeout sinyali burst 1.0.
4. Incident INC-1: kök neden postgres gecikmesi, timeline'da 4 belirti, faktör çubukları, kanıt satırına tıkla.
5. İki aksiyon ekle, birini Done yap, postmortem'i indir, Claude promptunu kopyala.
