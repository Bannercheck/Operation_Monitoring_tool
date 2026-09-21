// UI strings, Turkish first. `t(key)` falls back to the key so a missing entry is visible, never a crash.
import { createContext, useContext } from "react";

export type Lang = "tr" | "en";
const S: Record<string, [string, string]> = {
  brand_tag: ["OPERASYON SİNYAL MASASI", "OPERATIONS SIGNAL DESK"],
  nav_ops: ["Operasyon", "Operations"], nav_anom: ["Anomaliler", "Anomalies"], nav_data: ["Veri setleri", "Datasets"], nav_actions: ["Aksiyonlar", "Actions"],
  nav_pb: ["Playbook", "Playbook"], nav_conn: ["Bağlantılar", "Connections"], nav_kb: ["Bilgi tabanı", "Knowledge"], nav_ntf: ["Bildirimler", "Alerts"],
  nav_users: ["Kullanıcılar", "Users"], nav_sys: ["Sistem", "System"], nav_map: ["Hata haritası", "Failure map"], nav_assist: ["Watchover'a sor", "Ask Watchover"], nav_llm: ["LLM", "LLM"], nav_itsm: ["ITSM", "ITSM"], nav_group_ops: ["İZLEME", "MONITORING"], nav_group_admin: ["YÖNETİM", "ADMIN"],
  signout: ["Çıkış", "Sign out"], collapse: ["Menüyü daralt", "Collapse"], expand: ["Menüyü aç", "Expand"],
  login_title: ["Watchover'a giriş", "Sign in to Watchover"], login_lead: ["Operasyon sinyal masası", "Operations signal desk"], email: ["E-posta", "E-mail"], password: ["Parola", "Password"],
  login_btn: ["Devam et", "Continue"], login_or: ["veya", "or"], login_with: ["{p} ile giriş", "Sign in with {p}"], mfa_title: ["Doğrulama kodu", "Verification code"],
  mfa_lead: ["E-postanıza gönderilen 6 haneli kodu girin ({m} dk geçerli).", "Enter the 6-digit code sent to your e-mail (valid {m} min)."], mfa_btn: ["Doğrula", "Verify"],
  login_failed: ["Giriş başarısız", "Sign-in failed"], back: ["Geri", "Back"], login_not_configured: ["Yapılandırılmadı: Sistem › Giriş sağlayıcıları", "Not configured: System › Sign-in providers"],
  sso_title: ["Giriş sağlayıcıları (SSO)", "Sign-in providers (SSO)"], sso_lead: ["Google, Microsoft, Apple ya da kurumun OIDC sağlayıcısıyla giriş. Sağlayıcı tarafında geri dönüş adresi olarak yanındaki adresi tanımlayın.", "Sign in with Google, Microsoft, Apple or the company's OIDC provider. Register the callback next to each provider on their side."],
  sso_enabled: ["etkin", "enabled"], sso_client_id: ["Client ID", "Client ID"], sso_client_secret: ["Client secret", "Client secret"], sso_tenant: ["Tenant", "Tenant"], sso_issuer: ["Issuer URL", "Issuer URL"], sso_team: ["Team ID", "Team ID"], sso_key_id: ["Key ID", "Key ID"], sso_p8: ["Private key (.p8)", "Private key (.p8)"],
  sso_self_register: ["Sağlayıcıdan gelen yeni hesaplar otomatik açılsın (operatör rolüyle)", "New identities from a provider get an account automatically (operator role)"], sso_domains: ["İzinli e-posta alanları (virgülle; boş = hepsi)", "Allowed e-mail domains (comma separated; empty = any)"],
  reg_title: ["Hesap oluştur", "Create an account"], reg_btn: ["Kayıt ol", "Register"], reg_have: ["Hesabım var, giriş yap", "I have an account, sign in"], reg_new: ["Hesabın yok mu? Kayıt ol", "No account? Register"],
  reg_sent: ["Doğrulama kodu e-postanıza gönderildi ({m} dk geçerli).", "A verification code was sent to your e-mail (valid {m} min)."], reg_verify: ["Doğrula ve giriş yap", "Verify and sign in"], reg_pw2: ["Parola (tekrar)", "Password (again)"], reg_mismatch: ["Parolalar eşleşmiyor", "Passwords do not match"],
  // map
  map_title: ["Hata haritası", "Failure map"], map_sub: ["servisler, bağımlılık hataları ve korelasyonlar; kök nedenler işaretli", "services, dependency errors and correlations; root causes flagged"], map_source: ["Kaynak", "Source"], map_live: ["Canlı akış", "Live feed"],
  map_incident: ["Incident", "Incident"], map_all_inc: ["Bütün incident'lar", "All incidents"], map_hosts_toggle: ["Sunucuları çiz", "Draw hosts"], map_empty: ["Bu kapsamda çizilecek servis yok.", "Nothing to draw in this scope."],
  map_k_services: ["servis", "services"], map_k_deps: ["bağımlılık kenarı", "dependency edges"], map_k_corr: ["korelasyon", "correlations"], map_k_root: ["kök neden", "root cause"], map_k_errors: ["ERROR+", "ERROR+"], map_k_affected: ["etkilenen", "affected"],
  // assist
  as_title: ["Watchover'a sor", "Ask Watchover"], as_sub: ["yüklü veri seti ve bilgi tabanı üzerinden, kaynak göstererek", "over the loaded dataset and the knowledge base, with sources"], as_input: ["Sorunuzu yazın…", "Type your question…"], as_send: ["Sor", "Ask"],
  as_hello: ["Merhaba. Yüklü veri seti, canlı akış ve bilgi tabanı üzerinden soru sorabilirsiniz; LLM yoksa deterministik özetle cevap veririm.", "Hello. Ask about the loaded dataset, the live feed and the knowledge base; without an LLM I answer from the deterministic summary."],
  as_ex1: ["En olası kök neden ne ve neden?", "What is the most likely root cause and why?"], as_ex2: ["Hangi servisler etkilendi, ne zaman başladı?", "Which services are affected and since when?"], as_ex3: ["İlk aksiyon ne olmalı?", "What should the first action be?"],
  as_via_llm: ["LLM ile · {m}", "via LLM · {m}"], as_via_ctx: ["deterministik özet (LLM yok)", "deterministic summary (no LLM)"], as_sources: ["Kaynaklar", "Sources"], as_context: ["Bağlam", "Context"], as_save: ["Bilgi tabanına kaydet", "Save to knowledge base"], as_clear: ["Sohbeti temizle", "Clear chat"],
  as_dataset: ["Veri seti", "Dataset"], as_thinking: ["Düşünüyor…", "Thinking…"], as_propose: ["LLM'den kural önerisi iste", "Ask the LLM for rule proposals"], as_proposed: ["{n} öneri kurallar sekmesine düştü", "{n} proposals landed in the rules tab"],
  // llm
  llm_title: ["LLM", "LLM"], llm_sub: ["isteğe bağlı; çekirdek ona bağlı değil", "optional; the core never depends on it"], llm_provider: ["Sağlayıcı", "Provider"], llm_base: ["Adres", "Base URL"], llm_key: ["API anahtarı", "API key"], llm_model: ["Model", "Model"], llm_embed: ["Gömme modeli", "Embedding model"],
  llm_test: ["Bağlantıyı test et", "Test connection"], llm_list: ["Modelleri listele", "List models"], llm_calls: ["çağrı", "calls"], llm_success: ["başarı", "success"], llm_ground: ["kaynağa dayalı", "grounded"], llm_tab_conn: ["Bağlantı", "Connection"], llm_tab_models: ["Ollama modelleri", "Ollama models"], llm_tab_quality: ["Kalite", "Quality"],
  llm_installed: ["kurulu", "installed"], llm_loaded: ["bellekte", "in memory"], llm_active: ["etkin", "active"], llm_use: ["Kullan", "Use"], llm_pull: ["İndir", "Pull"], llm_pull_custom: ["Başka bir model adı", "Another model name"], llm_recommended: ["Önerilen modeller", "Recommended models"], llm_none: ["Ulaşılabilir Ollama yok. Aynı makinede Ollama kuruluysa adresi bağlantı sekmesine yazın.", "No Ollama reachable. If Ollama runs on this machine, enter its address in the connection tab."],
  llm_benchmark: ["Yüklü veri setiyle kalite testi çalıştır", "Run the quality benchmark on the loaded dataset"], llm_have: ["kurulu", "installed"], llm_role_chat: ["sohbet", "chat"], llm_role_embed: ["gömme", "embedding"],
  // itsm
  itsm_title: ["ITSM", "ITSM"], itsm_sub: ["ticket'lar canlı kanıt ve incident'larla ilişkilendirilir", "tickets scored against live evidence and incidents"], itsm_system: ["Sistem", "System"], itsm_demo: ["Demo ticket'lar", "Demo tickets"], itsm_base: ["Adres", "Base URL"], itsm_query: ["Sorgu", "Query"], itsm_path: ["JSON yolu", "JSON path"], itsm_mapping: ["Alan eşlemesi (JSON)", "Field mapping (JSON)"],
  itsm_fetch: ["Ticket'ları çek", "Fetch tickets"], itsm_rel: ["ilgililik", "relevance"], itsm_related: ["ilişkili", "related"], itsm_why: ["neden", "why"], itsm_opened: ["açılış", "opened"], itsm_none: ["Ticket yok.", "No tickets."],
  // ops extras
  ops_files: ["Log dosyaları", "Log files"], ops_lines: ["satır", "lines"], ops_errors_only: ["yalnız hatalar", "errors only"], ops_report: ["SLO raporu", "SLO report"], rep_window: ["Pencere", "Window"], ops_scope: ["Kapsam", "Scope"],
  // datasets extras
  tab_search: ["Arama", "Search"], search_help: ["timeout service:payment-api -debug \"tam ifade\"  · alanlar: service, host, severity, env, file", "timeout service:payment-api -debug \"exact phrase\" · fields: service, host, severity, env, file"], search_regex: ["regex", "regex"], search_total: ["eşleşen", "matches"],
  ds_compare: ["Karşılaştır", "Compare"], cmp_metric: ["Ölçüt", "Metric"], cmp_shared: ["Ortak sinyaller", "Shared signals"], cmp_only_a: ["Yalnız A", "Only in A"], cmp_only_b: ["Yalnız B", "Only in B"],
  // notify channels
  ntf_channels_tab: ["Kanallar (SMTP / SMS)", "Channels (SMTP / SMS)"], smtp_host: ["SMTP sunucusu", "SMTP host"], smtp_port: ["Port", "Port"], smtp_security: ["Güvenlik", "Security"], smtp_user: ["Kullanıcı", "User"], smtp_password: ["Parola", "Password"], smtp_from: ["Gönderen adres", "From address"], smtp_from_name: ["Gönderen adı", "From name"],
  sms_preset: ["SMS sağlayıcı", "SMS provider"], sms_url: ["SMS API adresi", "SMS API URL"], sms_user: ["Kullanıcı", "User"], sms_password: ["Parola", "Password"], sms_token: ["Token", "Token"], sms_from: ["Başlık / gönderen", "Sender id"], ntf_test_send: ["Test gönder", "Send test"], ntf_test_target: ["e-posta ya da telefon", "e-mail or phone"],
  // system extras
  sys_settings: ["Ayarlar", "Settings"], set_self_monitor: ["Bu makineyi izle (yerleşik ajan)", "Monitor this machine (built-in agent)"], set_alerts_on: ["Uyarı kuralları çalışsın", "Alert rules run"], set_learn_min: ["Canlı öğrenme aralığı (dk, 0 = kapalı)", "Live learning interval (min, 0 = off)"],
  set_live_port: ["Alıcı portu", "Receiver port"], set_live_key: ["Alıcı API anahtarı", "Receiver API key"], set_public_host: ["Dış adres", "Public address"], set_workspace: ["Kurum / çalışma alanı adı", "Organisation / workspace name"], set_lang: ["Varsayılan dil", "Default language"],
  sys_update: ["Güncelleme", "Update"], sys_update_docker: ["Docker'da güncelleme sunucuda komutla yapılır; veri birimlerine dokunulmaz.", "In Docker the update is a command on the server; data volumes are untouched."], sys_restart: ["Uygulamayı yeniden başlat", "Restart the app"], sys_rollback: ["Bu sürüme dön", "Roll back to this"], sys_restarting: ["Yeniden başlatılıyor…", "Restarting…"],
  conn_discovered: ["Keşfedilen sunucular", "Discovered hosts"], inv_add_from: ["Envantere ekle", "Add to inventory"],
  sso_public_host: ["Dış adres (https://watchover.sirket.com)", "Public address (https://watchover.company.com)"], sso_mfa: ["Yönetici olmayanlara e-posta ile doğrulama kodu", "E-mail verification code for non-administrators"], sso_callback: ["Geri dönüş adresi", "Callback URL"], saved: ["Kaydedildi", "Saved"],
  pw_change_title: ["Yeni parola belirleyin", "Set a new password"], pw_change_lead: ["İlk girişte başlangıç parolası değiştirilir.", "The initial password must be changed at first sign-in."],
  pw_current: ["Mevcut parola", "Current password"], pw_new: ["Yeni parola", "New password"], save: ["Kaydet", "Save"], cancel: ["Vazgeç", "Cancel"], delete: ["Sil", "Delete"], add: ["Ekle", "Add"],
  close: ["Kapat", "Close"], refresh: ["Yenile", "Refresh"], search: ["Ara", "Search"], loading: ["Yükleniyor…", "Loading…"], none: ["Kayıt yok", "Nothing here yet"], error: ["Hata", "Error"],
  confirm: ["Emin misiniz?", "Are you sure?"], yes: ["Evet", "Yes"], all: ["Tümü", "All"], name: ["Ad", "Name"], status: ["Durum", "Status"], owner: ["Sorumlu", "Owner"], note: ["Not", "Note"],
  env: ["Ortam", "Env"], host: ["Sunucu", "Host"], service: ["Servis", "Service"], severity: ["Önem", "Severity"], count: ["Adet", "Count"], time: ["Zaman", "Time"], message: ["Mesaj", "Message"],
  // ops
  ops_title: ["Canlı operasyon", "Live operations"], ops_events15: ["olay / 15 dk", "events / 15 min"], ops_errors: ["ERROR+", "ERROR+"], ops_avail: ["erişilebilirlik", "availability"],
  ops_p95: ["p95 gecikme", "p95 latency"], ops_hosts: ["sunucu", "hosts"], ops_agents: ["ajan", "agents"], ops_per_min: ["Dakikalık olaylar (önem)", "Events per minute (severity)"],
  ops_top_services: ["En çok olay üreten servisler", "Busiest services"], ops_recent: ["Son olaylar", "Recent events"], ops_metrics: ["Altyapı metrikleri", "Infrastructure metrics"],
  ops_sim: ["Simülasyon", "Simulator"], ops_sim_help: ["Ajan yokken demo verisi üretir", "Feeds demo data when no agent is connected"], ops_no_data: ["Henüz canlı veri yok: bir ajan bağlayın ya da simülasyonu açın.", "No live data yet: connect an agent or switch the simulator on."],
  ops_analyze: ["Tamponu veri seti yap", "Snapshot buffer as dataset"], ops_anom_open: ["açık anomali", "open anomalies"], ops_breach: ["eşik aşımı", "threshold breach"],
  // anomalies
  an_title: ["Anomali takibi", "Anomaly tracking"], an_sub: ["kendi normalinden sapan ne varsa ve üzerinde atılan operasyonel adımlar", "what deviates from its own baseline and the steps taken on it"],
  an_open: ["açık", "open"], an_ack: ["üzerinde çalışılıyor", "in progress"], an_resolved_today: ["bugün çözülen", "resolved today"], an_scans: ["tarama", "scans"], an_scan_now: ["Şimdi tara", "Scan now"],
  an_st_active: ["Açık ve üzerinde çalışılan", "Open and in progress"], an_st_open: ["Açık", "Open"], an_st_ack: ["Üzerinde çalışılıyor", "In progress"], an_st_resolved: ["Çözüldü", "Resolved"], an_st_ignored: ["Yok sayıldı", "Ignored"], an_st_all: ["Tümü", "All"],
  an_kind_errors: ["hata sıçraması", "error spike"], an_kind_rate: ["log fırtınası", "log storm"], an_kind_silence: ["sessizlik", "went quiet"], an_kind_pattern: ["yeni örüntü", "new pattern"], an_kind_metric: ["metrik", "metric"],
  an_observed: ["gözlenen", "observed"], an_baseline: ["normal", "normal"], an_score: ["skor", "score"], an_hits: ["tekrar", "hits"], an_recovered: ["normale döndü", "recovered"], an_steps: ["Operasyonel adımlar", "Operational steps"],
  an_series: ["dakikalık geçmiş ve şimdi", "per-minute history and now"], an_btn_ack: ["Üstlen", "Take over"], an_btn_resolve: ["Çöz", "Resolve"], an_btn_ignore: ["Yok say", "Ignore"], an_btn_reopen: ["Yeniden aç", "Reopen"], an_btn_action: ["Aksiyon oluştur", "Create action"],
  an_none: ["Bu görünümde anomali yok. Takipçi bir sunucunun normalini söyleyebilmek için yaklaşık 30 dakika geçmişe ihtiyaç duyar.", "No anomaly in this view. The tracker needs about 30 minutes of history per host first."], an_linked: ["aksiyon #{id}", "action #{id}"],
  an_t_errors: ["{host}: 5 dk'da {v} ERROR+ (normal {b})", "{host}: {v} ERROR+ / 5 min (normal {b})"], an_t_rate: ["{host}: 5 dk'da {v} olay (normal {b})", "{host}: {v} events / 5 min (normal {b})"],
  an_t_metric: ["{host}: {metric} %{v} (son saatler %{b})", "{host}: {metric} {v}% (last hours {b}%)"], an_t_silence: ["{host}: {v} dk'dır sessiz", "{host}: silent for {v} min"], an_t_pattern: ["Yeni {sev} örüntü × {v}: {tpl}", "New {sev} pattern × {v}: {tpl}"],
  an_step_verify: ["Doğrula: sunucu / servis detayını aç, bilinen bir bakım ya da test olmadığından emin ol", "Verify: open the host / service detail, rule out a known maintenance or test"],
  an_step_scope: ["Kapsam: hangi servisler ve sunucular etkilendi, ne zamandan beri", "Scope: which services and hosts are affected, since when"], an_step_playbook: ["Playbook: örüntüyü ara; runbook varsa uygula", "Playbook: look the pattern up; follow the runbook if any"],
  an_step_assign: ["Sorumlu ata ve nöbet kanalına bildir", "Assign an owner and inform the on-call channel"], an_step_action: ["Öneri ve kanıtla bir aksiyon oluştur", "Create an action with the recommendation and evidence"],
  an_step_close: ["Kapat: kök neden ve yapılanları nota yaz", "Close: root cause and what was done in the note"], an_step_source: ["Kaynak: hangi gönderici şişiriyor; üreteni sınırla ya da düzelt", "Source: which sender floods; throttle or fix the emitter"],
  an_step_agent: ["Ajan: çalışıyor ve kayıtlı mı? (Bağlantılar › Ajanlar)", "Agent: running and enrolled? (Connections › Agents)"], an_step_host: ["Sunucu: ayakta mı? (ping / ssh / konsol); planlı kapanış mı?", "Host: is it up? (ping / ssh / console); planned shutdown?"],
  an_step_network: ["Ağ: sunucu alıcıya 8600 portundan ulaşabiliyor mu?", "Network: can the host reach the receiver on port 8600?"], an_step_top: ["En çok tüketenler: hangi süreç / sorgu yüklüyor", "Top consumers: which process / query drives it"],
  an_step_capacity: ["Kapasite: ölçekle, temizle ya da yükü taşı; tekrarlarsa takip işi aç", "Capacity: scale, clean up or move load; follow up if it recurs"],
  // datasets
  ds_title: ["Veri setleri", "Datasets"], ds_upload: ["Dosya yükle", "Upload files"], ds_drop: ["Log, ZIP, CSV, JSON ya da SAP arşivlerini buraya bırakın", "Drop log, ZIP, CSV, JSON or SAP archives here"], ds_combine: ["Birden çok dosyayı tek veri seti yap", "Combine several files into one dataset"],
  ds_demo: ["Demo veri setini yükle", "Load the demo dataset"], ds_jobs: ["Yükleme işleri", "Load jobs"], ds_loaded: ["Yüklü veri setleri", "Loaded datasets"], ds_events: ["olay", "events"], ds_signals: ["sinyal", "signals"], ds_incidents: ["incident", "incidents"],
  ds_open: ["Aç", "Open"], ds_elapsed: ["sn", "s"], ds_stage_parse: ["ayrıştırma", "parsing"], ds_stage_analyze: ["analiz", "analysis"], ds_stage_profile: ["profil", "profile"], ds_done: ["tamam", "done"], ds_failed: ["hata", "failed"],
  tab_overview: ["Özet", "Overview"], tab_incidents: ["Incident'lar", "Incidents"], tab_signals: ["Sinyaller", "Signals"], tab_noise: ["Gürültü", "Noise"], tab_actions: ["Aksiyonlar", "Actions"],
  funnel_raw: ["ham olay", "raw events"], funnel_signals: ["sinyal", "signals"], funnel_incidents: ["incident", "incidents"], funnel_reduction: ["azaltma", "reduction"], ds_files: ["Dosyalar", "Files"], ds_format: ["biçim", "format"], ds_records: ["kayıt", "records"],
  inc_root: ["Kök neden", "Root cause"], inc_why: ["Neden bu kök neden?", "Why this root cause?"], inc_narrative: ["Anlatı", "Narrative"], inc_factors: ["Skor bileşenleri", "Score factors"], inc_timeline: ["Zaman çizelgesi", "Timeline"],
  inc_evidence: ["Kanıt", "Evidence"], inc_recs: ["Öneriler", "Recommendations"], inc_postmortem: ["Postmortem (Markdown)", "Postmortem (Markdown)"], inc_affected: ["Etkilenen", "Affected"], inc_score: ["skor", "score"], inc_recovery: ["düzelme", "recovery"],
  noise_eliminated: ["elenen olay", "eliminated events"], noise_kept: ["tutulan", "kept"], noise_rules: ["Elenme nedenleri", "Elimination reasons"],
  sig_template: ["Şablon", "Template"], sig_burst: ["patlama", "burst"], sig_peak: ["tepe/dk", "peak/min"], sig_onset: ["başlangıç", "onset"],
  // actions
  act_title: ["Aksiyon takibi", "Action tracking"], act_new: ["Yeni aksiyon", "New action"], act_incident: ["Incident", "Incident"], act_priority: ["Öncelik", "Priority"], act_title_f: ["Başlık", "Title"], act_rec: ["Öneri", "Recommendation"],
  st_open: ["açık", "open"], st_in_progress: ["devam ediyor", "in progress"], st_done: ["tamam", "done"], st_suppressed: ["bastırıldı", "suppressed"],
  // playbook
  pb_title: ["Playbook · hata kütüphanesi", "Playbook · error library"], pb_sub: ["her örüntü, nerede ve ne zaman görüldüğü, nasıl düzeldiği, runbook notları", "every pattern, where and when it was seen, how it resolved, runbook notes"],
  pb_occ: ["görülme", "occurrences"], pb_datasets: ["veri seti", "datasets"], pb_resolution: ["Çözüm", "Resolution"], pb_runbook: ["Runbook", "Runbook"], pb_last: ["son", "last"],
  // connections
  conn_agents: ["Ajanlar", "Agents"], conn_sources: ["Kaynaklar", "Sources"], conn_inv: ["Envanter", "Inventory"], agent_enroll: ["Ajan kaydet", "Enroll agent"], agent_key: ["Kayıt anahtarı", "Enrolment key"], agent_key_rotate: ["Yenile", "Rotate"],
  agent_token_once: ["Token bir kez gösterilir; sunucuya kopyalayın.", "The token is shown once; copy it to the server."], agent_install: ["Kurulum komutu", "Install command"], agent_last: ["son görülme", "last seen"], agent_events: ["olay", "events"],
  agent_revoke: ["İptal et", "Revoke"], agent_reactivate: ["Etkinleştir", "Reactivate"], agent_rotate: ["Token yenile", "Rotate token"],
  src_add: ["Kaynak ekle", "Add source"], src_kind: ["Tür", "Kind"], src_url: ["Adres", "URL"], src_selector: ["Seçici / sorgu", "Selector / query"], src_interval: ["Aralık (sn)", "Interval (s)"], src_test: ["Test", "Test"], src_poll: ["Şimdi çek", "Poll now"],
  src_enabled: ["etkin", "enabled"], src_user: ["Kullanıcı", "User"], src_secret: ["Parola / token", "Password / token"], src_auth: ["Kimlik", "Auth"],
  inv_add: ["Sunucu ekle / güncelle", "Add / update host"], inv_hostname: ["Sunucu adı", "Hostname"], inv_dc: ["Veri merkezi", "Data center"], inv_crit: ["Kritiklik", "Criticality"], inv_export: ["CSV indir", "Download CSV"], inv_import: ["CSV yükle", "Upload CSV"],
  // knowledge
  kb_title: ["Bilgi tabanı", "Knowledge base"], kb_search: ["Bilgi tabanında ara", "Search the knowledge base"], kb_note_add: ["Not ekle", "Add note"], kb_rules: ["Kurallar", "Rules"], kb_propose: ["Kural öner", "Propose rule"],
  kb_approve: ["Onayla", "Approve"], kb_reject: ["Reddet", "Reject"], kb_kind: ["Tür", "Kind"], kb_key: ["Anahtar", "Key"], kb_value: ["Değer", "Value"], kb_title_f: ["Başlık", "Title"], kb_text: ["Metin", "Text"],
  // notify
  ntf_title: ["Bildirimler", "Alerts"], ntf_recipients: ["Alıcılar", "Recipients"], ntf_rules: ["Kurallar", "Rules"], ntf_log: ["Gönderim geçmişi", "Sent alerts"], ntf_phone: ["Telefon", "Phone"], ntf_groups: ["Gruplar", "Groups"],
  ntf_condition: ["Koşul", "Condition"], ntf_threshold: ["Eşik", "Threshold"], ntf_channels: ["Kanallar", "Channels"], ntf_targets: ["Hedefler", "Targets"], ntf_cooldown: ["Bekleme (dk)", "Cooldown (min)"], ntf_evaluate: ["Kuralları şimdi değerlendir", "Evaluate rules now"],
  // users
  us_title: ["Kullanıcılar ve roller", "Users and roles"], us_add: ["Hesap ekle", "Add account"], us_role: ["Rol", "Role"], us_roles: ["Roller ve yetkiler", "Roles and permissions"], us_pw_reset: ["Parola belirle", "Set password"],
  us_unlock: ["Kilidi aç", "Unlock"], us_disable: ["Devre dışı", "Disable"], us_enable: ["Etkinleştir", "Enable"], us_last: ["son giriş", "last sign-in"], us_events: ["Güvenlik günlüğü", "Security log"],
  // system
  sys_title: ["Sistem", "System"], sys_version: ["Sürüm", "Version"], sys_db: ["Veritabanı", "Database"], sys_uptime: ["çalışma süresi", "uptime"], sys_services: ["Servisler", "Services"], sys_snapshots: ["Sürüm anlık görüntüleri", "Version snapshots"],
  sys_snap_new: ["Anlık görüntü al", "Take snapshot"], sys_vacuum: ["Veritabanını sıkıştır", "Compact the database"], sys_tables: ["tablo", "tables"], sys_releases: ["Sürüm notları", "Release notes"],
};

export function tr(lang: Lang, key: string, kw: Record<string, any> = {}): string {
  const e = S[key];
  let s = e ? (lang === "tr" ? e[0] : e[1]) : key;
  for (const [k, v] of Object.entries(kw)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

export const LangCtx = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({ lang: "tr", setLang: () => {} });
export function useT() {
  const { lang, setLang } = useContext(LangCtx);
  return { t: (k: string, kw?: Record<string, any>) => tr(lang, k, kw), lang, setLang };
}
