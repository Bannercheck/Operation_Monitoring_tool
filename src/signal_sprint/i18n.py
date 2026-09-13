"""UI strings in Turkish and English. `t(key, **kw)` reads the active language from Streamlit session state."""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "tagline": "noise → signals → explained incidents → tracked actions",
        "upload": "Upload dataset (file / ZIP / TAR.GZ, up to 1 GB)",
        "load_demo": "Load demo dataset", "working": "Parsing, fingerprinting, correlating…",
        "raw_events": "raw events", "fingerprints": "fingerprints", "meaningful": "meaningful signals",
        "incidents": "incidents", "actions": "actions",
        "footer": "Deterministic engine · no LLM / API at runtime · every decision carries evidence",
        "landing_title": "Signal Sprint",
        "landing_body": "Logs, alerts, events, metrics, incidents, in any mix of **JSON / JSONL / CSV / TSV / syslog / key=value / text**, "
                        "as a file, ZIP or TAR.GZ. The engine normalises everything, collapses noise into signals, correlates them into "
                        "incident candidates with a root-cause hypothesis, and lets you track the resulting actions.",
        "step1": "Ingest", "step1d": "auto-detect format, map columns to roles",
        "step2": "Reduce", "step2d": "mask volatile values, fingerprint, burst detection",
        "step3": "Correlate", "step3d": "time window + shared entities → incidents, root cause",
        "step4": "Act", "step4d": "scored rationale, evidence, actions, postmortem",
        "landing_hint": "Use the sidebar to upload a dataset, or click **Load demo dataset**.",
        "tab_overview": "Overview", "tab_signals": "Signals", "tab_incidents": "Incidents", "tab_actions": "Actions",
        "activity": "Activity timeline", "activity_cap": "Shaded windows are incident candidates",
        "found": "What I found", "top_incidents": "Top incident candidates", "no_incidents": "No incident candidates: nothing bursts or correlates above the thresholds.",
        "files_rel": "Files and suggested relations", "signals_n": "signals", "origin": "origin",
        "min_sev": "Min severity", "only_burst": "Only bursting (≥ 0.5)", "only_meaningful": "Only meaningful", "services": "Services",
        "why_signal": "WHY THIS SIGNAL?", "signal": "Signal", "events": "events", "burst": "burst", "confidence": "confidence",
        "reason": "Reason", "reason_fp": "same normalized message fingerprint (volatile values masked)", "template": "Template",
        "grouping": "Grouping", "severity": "severity", "hosts": "hosts", "window": "window",
        "burst_detail": "peak {peak}/min vs baseline {base}/min → score {score}", "evidence": "Evidence", "evidence_first": "Evidence (first {n} of {total})",
        "incident": "Incident", "score": "score", "probable_origin": "Probable origin", "because": "because", "affected": "Affected",
        "propagation": "Propagation", "why_score": "Why this score", "narrative": "Narrative", "timeline": "Timeline", "links": "Correlation links",
        "open_raw": "Open a raw line", "recommended": "Recommended actions", "create_action": "Create action", "custom_action": "Custom action",
        "title": "Title", "priority": "Priority", "owner": "Owner", "create": "Create", "postmortem": "⬇ Postmortem (.md)", "llm_prompt": "LLM prompt",
        "llm_cap": "Optional enrichment: paste into Claude SAKA. The answer must cite evidence refs.", "actions_for": "Actions for {id}",
        "no_actions": "No actions yet. Create them from an incident's recommendations.", "unassigned": "unassigned",
        "status_open": "Open", "status_in_progress": "In progress", "status_done": "Done", "status_suppressed": "Suppressed",
        "root_cause": "root cause", "symptom": "symptom",
        "r_earliest": "earliest onset in the group", "r_lead": "starts {n} min after the first signal",
        "r_dep": "mentions an infrastructure dependency", "r_fan": "shares entities with {n} other signal(s)",
        "n_collapsed": "{events} raw events collapsed into {signals} signal(s) between {start} and {end}.",
        "n_origin": "Probable origin: {sig} \"{template}\" because: {reason}.", "n_symptoms": "Downstream symptoms: {items}.",
        "n_links": "Links: {items}.", "shared_entity": "shared entity {ents}, {gap}s apart", "both_burst": "both burst within {gap}s",
        "f_burst": "burst", "f_severity": "severity", "f_blast_radius": "blast radius", "f_duration": "duration",
        "fv_burst": "peak {peak}/min vs baseline {base}/min", "fv_severity": "{share} of {total} events are ERROR+",
        "fv_blast_radius": "{services} service(s), {hosts} host(s)", "fv_duration": "{minutes} min",
        "parser": "Parser", "mapping": "Column → role mapping", "auto": "(auto)", "apply": "Apply and re-run", "rows": "rows",
        "conf": "confidence", "mapping_hint": "Override only when the auto-mapper picked the wrong column. Applies to all files.",
        "sev_over_time": "Severity over time", "top_services": "Busiest services", "top_hosts": "Busiest hosts", "sources": "Source types",
        "errors_by_service": "Errors by service", "files_n": "files", "records_n": "records", "services_n": "services", "hosts_n": "hosts",
        "error_classes": "error classes", "span_min": "minutes covered",
        "live": "Live log stream", "live_cap": "Replays the dataset in time order. Play to follow the tail; filter by severity or text.",
        "play": "▶ Play", "pause": "⏸ Pause", "reset": "⟲ Reset", "speed": "Speed", "filter_text": "Filter text", "lines": "Lines",
        "position": "Position", "showing": "showing {n} of {total} lines up to {time}",
        "p_none": "No records parsed.", "p_found": "I found: {files} files, {records} records",
        "p_sources": "Probable sources: {items}", "p_entities": "Detected entities: {services} services, {hosts} hosts, {errors} error classes",
        "p_range": "Detected time range: {start} -> {end} ({minutes} min)", "p_relations": "Suggested relations: {items}",
    },
    "tr": {
        "tagline": "gürültü → sinyal → gerekçeli incident → takip edilen aksiyon",
        "upload": "Veri seti yükle (dosya / ZIP / TAR.GZ, 1 GB'a kadar)",
        "load_demo": "Demo veri setini yükle", "working": "Ayrıştırılıyor, gruplanıyor, ilişkilendiriliyor…",
        "raw_events": "ham olay", "fingerprints": "parmak izi", "meaningful": "anlamlı sinyal",
        "incidents": "incident", "actions": "aksiyon",
        "footer": "Deterministik motor · çalışma zamanında LLM / API yok · her karar kanıt taşır",
        "landing_title": "Signal Sprint",
        "landing_body": "Log, alarm, olay, metrik, incident; **JSON / JSONL / CSV / TSV / syslog / key=value / metin** karışık olabilir, "
                        "dosya, ZIP veya TAR.GZ olarak. Motor her şeyi normalize eder, gürültüyü sinyale indirger, sinyalleri kök neden "
                        "hipoteziyle incident adaylarına bağlar ve çıkan aksiyonların takibini sağlar.",
        "step1": "Al", "step1d": "formatı otomatik tanı, sütunları rollere eşle",
        "step2": "Azalt", "step2d": "değişken değerleri maskele, parmak izi, patlama tespiti",
        "step3": "İlişkilendir", "step3d": "zaman penceresi + ortak varlık → incident, kök neden",
        "step4": "Aksiyon", "step4d": "puanlı gerekçe, kanıt, aksiyon, postmortem",
        "landing_hint": "Sol menüden veri seti yükleyin ya da **Demo veri setini yükle** tıklayın.",
        "tab_overview": "Özet", "tab_signals": "Sinyaller", "tab_incidents": "Incident'lar", "tab_actions": "Aksiyonlar",
        "activity": "Aktivite zaman çizgisi", "activity_cap": "Gölgeli alanlar incident adayları",
        "found": "Ne buldum", "top_incidents": "Öne çıkan incident adayları", "no_incidents": "Incident adayı yok: eşiklerin üstünde patlayan ya da ilişkilenen sinyal bulunmadı.",
        "files_rel": "Dosyalar ve önerilen ilişkiler", "signals_n": "sinyal", "origin": "kaynak",
        "min_sev": "Min. seviye", "only_burst": "Sadece patlayanlar (≥ 0.5)", "only_meaningful": "Sadece anlamlılar", "services": "Servisler",
        "why_signal": "NEDEN BU SİNYAL?", "signal": "Sinyal", "events": "olay", "burst": "patlama", "confidence": "güven",
        "reason": "Gerekçe", "reason_fp": "aynı normalize mesaj parmak izi (değişken değerler maskelendi)", "template": "Şablon",
        "grouping": "Gruplama", "severity": "seviye", "hosts": "hostlar", "window": "pencere",
        "burst_detail": "tepe {peak}/dk, taban {base}/dk → skor {score}", "evidence": "Kanıt", "evidence_first": "Kanıt ({total} satırdan ilk {n})",
        "incident": "Incident", "score": "skor", "probable_origin": "Olası kaynak", "because": "çünkü", "affected": "Etkilenen",
        "propagation": "Yayılım", "why_score": "Bu skor neden", "narrative": "Anlatı", "timeline": "Zaman çizgisi", "links": "Korelasyon bağları",
        "open_raw": "Ham satırı aç", "recommended": "Önerilen aksiyonlar", "create_action": "Aksiyon oluştur", "custom_action": "Özel aksiyon",
        "title": "Başlık", "priority": "Öncelik", "owner": "Sorumlu", "create": "Oluştur", "postmortem": "⬇ Postmortem (.md)", "llm_prompt": "LLM promptu",
        "llm_cap": "Opsiyonel zenginleştirme: Claude SAKA'ya yapıştırın. Cevap kanıt referanslarını göstermek zorunda.", "actions_for": "{id} aksiyonları",
        "no_actions": "Henüz aksiyon yok. Bir incident'ın önerilerinden oluşturun.", "unassigned": "atanmamış",
        "status_open": "Açık", "status_in_progress": "Devam ediyor", "status_done": "Tamamlandı", "status_suppressed": "Bastırıldı",
        "root_cause": "kök neden", "symptom": "belirti",
        "r_earliest": "gruptaki en erken başlangıç", "r_lead": "ilk sinyalden {n} dk sonra başlıyor",
        "r_dep": "bir altyapı bağımlılığından bahsediyor", "r_fan": "{n} başka sinyalle ortak varlık paylaşıyor",
        "n_collapsed": "{events} ham olay, {start} ile {end} arasında {signals} sinyale indirgendi.",
        "n_origin": "Olası kaynak: {sig} \"{template}\", çünkü: {reason}.", "n_symptoms": "Aşağı akış belirtileri: {items}.",
        "n_links": "Bağlar: {items}.", "shared_entity": "ortak varlık {ents}, {gap} sn arayla", "both_burst": "ikisi de {gap} sn içinde patladı",
        "f_burst": "patlama", "f_severity": "seviye", "f_blast_radius": "etki alanı", "f_duration": "süre",
        "fv_burst": "tepe {peak}/dk, taban {base}/dk", "fv_severity": "{total} olayın {share}'i ERROR+",
        "fv_blast_radius": "{services} servis, {hosts} host", "fv_duration": "{minutes} dk",
        "parser": "Parser", "mapping": "Sütun → rol eşlemesi", "auto": "(otomatik)", "apply": "Uygula ve yeniden çalıştır", "rows": "satır",
        "conf": "güven", "mapping_hint": "Sadece otomatik eşleme yanlış sütunu seçtiyse değiştirin. Tüm dosyalara uygulanır.",
        "sev_over_time": "Zamana göre seviye", "top_services": "En yoğun servisler", "top_hosts": "En yoğun hostlar", "sources": "Kaynak türleri",
        "errors_by_service": "Servise göre hatalar", "files_n": "dosya", "records_n": "kayıt", "services_n": "servis", "hosts_n": "host",
        "error_classes": "hata sınıfı", "span_min": "dakika kapsam",
        "live": "Canlı log akışı", "live_cap": "Veri setini zaman sırasıyla oynatır. Oynat ile kuyruğu izleyin; seviye veya metinle filtreleyin.",
        "play": "▶ Oynat", "pause": "⏸ Duraklat", "reset": "⟲ Başa al", "speed": "Hız", "filter_text": "Metin filtresi", "lines": "Satır",
        "position": "Konum", "showing": "{time} anına kadar {total} satırdan {n} tanesi",
        "p_none": "Hiç kayıt ayrıştırılamadı.", "p_found": "Buldum: {files} dosya, {records} kayıt",
        "p_sources": "Olası kaynaklar: {items}", "p_entities": "Tespit edilen varlıklar: {services} servis, {hosts} host, {errors} hata sınıfı",
        "p_range": "Zaman aralığı: {start} -> {end} ({minutes} dk)", "p_relations": "Önerilen ilişkiler: {items}",
    },
}

RECOMMENDATIONS_TR = {
    "Check connection pool exhaustion on the dependency": "Bağımlılıkta bağlantı havuzu tükenmesini kontrol et",
    "Verify network path / DNS to the target": "Hedefe giden ağ yolunu / DNS'i doğrula",
    "Inspect slow queries and locks": "Yavaş sorguları ve kilitleri incele",
    "Check CPU / IO saturation on the host": "Host'ta CPU / IO doygunluğunu kontrol et",
    "Free disk on the host, rotate logs": "Host'ta disk alanı aç, logları döndür",
    "Add disk usage alert threshold at 85%": "%85'te disk kullanım alarmı ekle",
    "Raise memory limit or fix the leak": "Bellek limitini artır ya da sızıntıyı düzelt",
    "Add memory alert before OOM": "OOM öncesi bellek alarmı ekle",
    "Roll back last deploy if correlated": "İlişkiliyse son deploy'u geri al",
    "Check upstream dependency health": "Üst bağımlılığın sağlığını kontrol et",
    "Renew the certificate": "Sertifikayı yenile",
    "Automate certificate rotation": "Sertifika rotasyonunu otomatikleştir",
    "Investigate the root-cause signal's evidence lines": "Kök neden sinyalinin kanıt satırlarını incele",
    "Confirm blast radius with service owners": "Etki alanını servis sahipleriyle doğrula",
}


def current_lang() -> str:
    try:
        import streamlit as st
        return st.session_state.get("lang", "tr")
    except Exception:
        return "en"


def t(key: str, lang: str | None = None, **kw) -> str:
    lang = lang or current_lang()
    s = STRINGS.get(lang, STRINGS["en"]).get(key) or STRINGS["en"].get(key, key)
    return s.format(**kw) if kw else s


def reason_text(codes: list, lang: str | None = None) -> str:
    out = []
    for c in codes:
        if isinstance(c, tuple):
            out.append(t(c[0], lang, n=c[1]))
        else:
            out.append(t(c, lang))
    return "; ".join(out)


def link_text(e: dict, lang: str | None = None) -> str:
    if e.get("ents"):
        return t("shared_entity", lang, ents=", ".join(e["ents"]), gap=e["gap"])
    return t("both_burst", lang, gap=e["gap"])


def narrative_text(inc, signals: dict, lang: str | None = None) -> str:
    root = signals[inc.root_cause_signal]
    members = [signals[s] for s in inc.signal_ids]
    parts = [t("n_collapsed", lang, events=sum(s.count for s in members), signals=len(members),
               start=f"{inc.started_at:%H:%M:%S}", end=f"{inc.ended_at:%H:%M:%S}"),
             t("n_origin", lang, sig=root.id, template=root.template, reason=reason_text(inc.root_cause_codes, lang))]
    symptoms = [s for s in members if s is not root]
    if symptoms:
        parts.append(t("n_symptoms", lang, items="; ".join(f"{s.id} \"{s.template[:60]}\" x{s.count}" for s in symptoms[:4])))
    if inc.links:
        parts.append(t("n_links", lang, items="; ".join(f"{e['a']}-{e['b']} ({link_text(e, lang)})" for e in inc.links[:4])))
    return " ".join(parts)


def factor_value(f, lang: str | None = None) -> str:
    return t("fv_" + f.name, lang, **f.data) if f.data else f.value


def recommendation_text(rec: str, lang: str | None = None) -> str:
    return RECOMMENDATIONS_TR.get(rec, rec) if (lang or current_lang()) == "tr" else rec
