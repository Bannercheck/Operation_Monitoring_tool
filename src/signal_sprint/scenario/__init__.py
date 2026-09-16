"""Case-specific overrides. This is the ONLY place to touch on hackathon day.

Everything here is optional; empty values mean "use the generic defaults".
"""

from __future__ import annotations

# Column -> role mapping when the auto-mapper guesses wrong, e.g. {"timestamp": "event_ts", "message": "detail"}
MAPPING: dict[str, str] = {}

# Numeric severity scale of the dataset (S-A1: 1-5). Map raw value -> DEBUG/INFO/WARN/ERROR/CRITICAL.
# VERI_SOZLUGU.md: 1 = bilgi, 2 = uyarı, 3 = küçük, 4 = büyük, 5 = kritik
SEVERITY_MAP: dict[str, str] = {"5": "CRITICAL", "4": "ERROR", "3": "WARN", "2": "INFO", "1": "DEBUG"}

# Signals below this severity never start or join an incident unless they burst (background chatter guard)
MIN_SEVERITY: str = "ERROR"
# A WARN signal with at least this many alarms is a "sustained" (slow-burn) signal and may join an incident
SUSTAINED_MIN: int = 25
# A signal active over more than this share of the window with no burst is chronic background, not an event
CHRONIC_SPAN: float = 0.7

# Files that are reference tables, not event streams (regex on the file name, case-insensitive).
SIDE_TABLES: list[str] = [r"depend", r"bagimlilik", r"inventory", r"envanter", r"sozlugu", r"brifing", r"readme"]

# The same events delivered twice (alarms.json + alarms.csv): keep one copy per id, prefer this format.
DEDUP_KEY: str = "alarm_id"
DEDUP_PREFER: str = "json"

# Hard cap on incident cards ("as many as the on-call engineer can read"). Lower-scored groups go to the noise audit.
MAX_INCIDENTS: int = 15

# Correlation edge rules: shared entity always links; dependency table links; "both bursting" only if allowed.
LINK_BURST_ONLY: bool = False

# --- Alert-storm clustering (storm.py). CLUSTERING = "density" for S-A1, "fingerprint" for the generic engine,
# "auto" picks density when the dataset carries a service dependency table.
CLUSTERING: str = "auto"
BUCKET_MIN: int = 5          # time bucket size (minutes)
HOT_MIN: int = 4             # a service cell is hot at >= max(HOT_MIN, HOT_RATIO x its median bucket count)
HOT_RATIO: float = 3.0
HOST_HOT_MIN: int = 5        # a single host firing this many alarms in one bucket lights its service cell
PAD_MIN: int = 2             # minutes added around the hot span when collecting the incident's alarms
INFRA_TYPES: set[str] = {"network_down", "network_flap", "pkt_loss", "ntp_drift"}   # rack-level linking only for these
RACK_MIN_HOSTS: int = 3          # infra alarms on this many hosts of one rack = rack-level network root cause
MIN_CLUSTER_ALARMS: int = 15     # smaller dense spots are coincidences of background chatter -> LOW-n, not a card
MIN_CLUSTER_ERRORS: int = 3      # a card needs at least this many ERROR+ alarms
PRUNE_BACKGROUND: bool = True    # inside a cluster, (service, alarm_type) pairs that run at their normal rate go back to noise

# Causal prior per alarm type: how likely this type is a CAUSE rather than a symptom (0 = pure symptom).
# infrastructure > storage > database > external provider > resource > application symptom
CAUSE_RANK: dict[str, float] = {"network_down": 5, "pkt_loss": 4.5, "network_flap": 3.5, "disk_full": 5, "db_write_fail": 3.5, "oom_risk": 3.5,
                                "ext_unreach": 4.5, "ext_slow": 3.5, "db_conn_pool": 2.5, "conn_refused": 2.5, "batch_overlap": 3, "batch_slow": 2,
                                "gc_pressure": 2, "mem_high": 1.5, "cpu_high": 1.5, "thread_pool": 1.5, "queue_backlog": 1.5, "disk_warn": 1.5,
                                "timeout": 1, "http_5xx": 0.5, "latency_high": 0.5, "txn_fail": 0.5,
                                "cert_expiry": 0, "backup_warn": 0, "ntp_drift": 0, "log_rotate": 0}

# Extra (regex, replacement) masks applied before fingerprinting, e.g. [(r"order-\d+", "<order>")]
EXTRA_MASKS: list[tuple[str, str]] = []

# Words that mark a signal as an infrastructure dependency (root-cause hint)
EXTRA_DEPENDENCY_WORDS: list[str] = ["disk", "storage", "power", "switch", "link", "bgp", "san", "nfs", "tablespace", "veritabani", "baglanti havuzu",
                                     "arayuz", "koptu", "paket kaybi", "dis servis", "heap", "outofmemory"]

# Extra regexes that name the dependency a message talks about (Turkish S-A1 messages)
DEP_PATTERNS: list[str] = [r"([a-z][\w\-]+) servisine yapilan cagri", r"([a-z][\w\-]+) baglantisi reddedildi", r"dis servis ([a-z][\w\-]+)"]

# Dependency table columns (S-A1: kaynak_servis depends on hedef_servis; if hedef breaks, kaynak suffers)
DEP_COLUMNS: dict[str, list[str]] = {"source": ["kaynak_servis", "source_service", "source", "from", "consumer", "kaynak"],
                                     "target": ["hedef_servis", "target_service", "target", "to", "provider", "depends_on", "hedef"],
                                     "type": ["bagimlilik_tipi", "dependency_type", "type", "tip"],
                                     "criticality": ["kritiklik", "criticality", "critical"]}
INVENTORY_COLUMNS: dict[str, list[str]] = {"host": ["host", "hostname", "sunucu"], "service": ["servis", "service"],
                                           "dc": ["veri_merkezi", "datacenter", "dc"], "rack": ["kabin", "rack"],
                                           "env": ["ortam", "environment", "env"], "criticality": ["is_kritikligi", "business_criticality", "criticality"]}

# Correlation window in minutes and scoring weights; None keeps the defaults in analysis.py
WINDOW_MIN: int | None = 10
WEIGHTS: dict[str, float] | None = {"burst": 0.30, "severity": 0.20, "blast_radius": 0.20, "duration": 0.15, "criticality": 0.15}

# Recommendation templates: substring of root-cause template -> list of suggested actions
RECOMMENDATIONS: dict[str, list[str]] = {
    # S-A1 (Turkish alarm texts): key = substring of the root-cause template
    "disk kullanimi kritik": ["billing-db sunucularında /data alanını genişlet veya arşiv/log temizliği yap; tablespace'i yeniden genişlet",
                              "Yazma hataları durana kadar invoice-batch işini durdur, sonra yeniden başlat", "Disk %85 eşiğinde erken uyarı alarmı ekle"],
    "tablespace": ["Tablespace'i genişlet veya disk alanı aç", "Batch yazma yükünü geçici olarak durdur"],
    "dis servis": ["Ödeme sağlayıcısının (payment-provider-gw) durum sayfasını ve sözleşme SLA'sını kontrol et, sağlayıcı ile iletişime geç",
                   "payment-service için devre kesici / kuyruklama (retry with backoff) devreye al; müşteriye 'ödeme gecikmeli' mesajı",
                   "Alternatif sağlayıcı yolu varsa trafiği oraya al"],
    "paket kaybi": ["dc1/rack-A ToR switch / uplink'i kontrol et; port hatalarına ve link flap'e bak, gerekirse yedek uplink'e geç",
                    "Kabindeki kritik servisleri (dns-resolver, auth, api-gateway) diğer kabindeki eşlerine yönlendir",
                    "Ağ ekibine P1 ticket aç; DNS çözümleme sağlığını ilk doğrula"],
    "baglantisi koptu": ["Kabin/switch arayüzlerini kontrol et, yedek uplink'e geç", "Etkilenen sunucuların eşlerine trafik yönlendir"],
    "link durumu": ["Kabin switch portlarını ve kablolamayı kontrol et", "Flap yapan arayüzü geçici olarak devre dışı bırak"],
    "toplu is penceresi": ["Çakışan toplu işleri (reconciliation-batch / report-batch) durdur veya sırala; batch-scheduler pencerelerini ayır",
                           "subscriber-db bağlantı havuzunu geçici olarak artır, uzun süren sorguları sonlandır", "Batch pencereleri için çakışma korumasını zamanlayıcıya ekle"],
    "baglanti havuzu": ["Havuzu tüketen istemciyi bul (batch işleri), gerekirse havuz boyutunu geçici artır", "Uzun süren sorguları sonlandır"],
    "outofmemory": ["Servisi yeniden başlat, heap limitini artır, bellek sızıntısını araştır"],
    # generic (English datasets)
    "timeout": ["Check connection pool exhaustion on the dependency", "Verify network path / DNS to the target"],
    "latency": ["Inspect slow queries and locks", "Check CPU / IO saturation on the host"],
    "duration": ["Inspect slow queries and locks", "Check CPU / IO saturation on the host"],
    "slow": ["Inspect slow queries and locks", "Check CPU / IO saturation on the host"],
    "no space": ["Free disk on the host, rotate logs", "Add disk usage alert threshold at 85%"],
    "oom": ["Raise memory limit or fix the leak", "Add memory alert before OOM"],
    "5xx": ["Roll back last deploy if correlated", "Check upstream dependency health"],
    "certificate": ["Renew the certificate", "Automate certificate rotation"],
}

# Who owns the first action of a card, by root-cause service name substring (fallback: on-call)
OWNERS: dict[str, str] = {"db": "DBA ekibi", "provider": "Ödeme entegrasyon ekibi", "gw": "Entegrasyon ekibi", "batch": "Batch operasyon",
                          "dns": "Ağ ekibi", "network": "Ağ ekibi", "rack": "Ağ / veri merkezi ekibi"}
DEFAULT_OWNER: str = "Nöbetçi mühendis"
CRITICAL_LEVELS: set[str] = {"kritik", "yuksek", "critical", "high"}   # inventory is_kritikligi values that count as business-critical

# Service objectives shown on the live page. SLO = internal target, SLA = contractual.
SLO: dict = {"availability": 0.999, "p95_ms": 300}
SLA: dict = {"availability": 0.995}
# Infra metric thresholds (percent) that count as anomalies for ticket correlation and tile colouring
METRIC_THRESHOLDS: dict = {"cpu": 85, "gpu": 95, "memory": 90, "disk": 90}
