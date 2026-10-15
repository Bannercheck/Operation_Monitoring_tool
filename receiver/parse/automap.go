package parse

import (
	"strings"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02 section 10).

// Mapping is an explicit {role: key} mapping (scenario.MAPPING or a learned profile's mapping), insertion ordered.
type Mapping struct{ m *py.Dict }

// NewMapping wraps a role -> key dict (values are str). nil -> empty mapping.
func NewMapping(d *py.Dict) *Mapping {
	if d == nil {
		d = py.NewDict(0)
	}
	return &Mapping{m: d}
}

// Dict returns the underlying role -> key dict.
func (mp *Mapping) Dict() *py.Dict {
	if mp == nil {
		return nil
	}
	return mp.m
}

// Len is len(mapping) (0 for nil).
func (mp *Mapping) Len() int {
	if mp == nil {
		return 0
	}
	return mp.m.Len()
}

// roleHints is normalize.ROLE_HINTS in role order (RoleTimestamp .. RoleMessage); hint order matters.
var roleHints = [NumRoles][]string{
	RoleTimestamp: {"timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time", "created_at", "logged_at", "t", "start_time",
		"__realtime_timestamp", "timeunixnano", "eventtime", "t.$date", "systemtime", "rt", "devtime", "starttime", "observedtimeunixnano", "timecreated",
		"startsat", "starts_at", "activeat", "active_at", "firedat", "opened_at", "created", "zaman", "tarih", "olusturma zamani",
		"olusturma_zamani", "olusturmazamani", "acilis zamani", "kayit zamani"},
	RoleSeverity: {"severity", "level", "loglevel", "log_level", "priority", "sev", "labels.severity", "log.level", "severitytext", "levelname", "s",
		"syslog.severity", "severity_label", "seviye", "oncelik", "onem", "kritiklik", "status"},
	RoleService: {"service", "app", "application", "logger", "module", "job", "program", "svc", "service_name", "service.name", "syslog_identifier",
		"_systemd_unit", "eventsource", "c", "logger_name", "appname", "app_name", "container_name", "kubernetes.container_name", "_service",
		"provider", "provider_name", "deviceproduct", "product", "alertname", "labels.job", "labels.service", "servis", "uygulama", "sistem", "kaynak sistem"},
	RoleEnvironment: {"environment", "env", "stage", "tier", "deployment", "labels.env", "labels.environment", "ortam", "cevre", "asama"},
	RoleOrigin: {"source", "origin", "error_source", "component", "subsystem", "category", "layer", "error_type", "exception", "labels.component",
		"kaynak", "bilesen", "hata kaynagi", "kategori", "katman"},
	RoleHost: {"host", "hostname", "node", "instance", "server", "pod", "container", "machine", "labels.instance", "_hostname", "host.name", "computer",
		"dvchost", "devname", "identhosthame", "identhostname", "kubernetes.pod_name", "sourcehost", "sunucu", "makine", "cihaz"},
	RoleMessage: {"message", "msg", "text", "description", "log", "summary", "title", "body", "line", "event", "alert", "short_message", "full_message",
		"body.stringvalue", "eventname", "errormessage", "name", "annotations.summary", "annotations.description", "ozet", "aciklama", "mesaj", "baslik", "konu"},
}

// normKeyTR is str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU").
var normKeyTR = strings.NewReplacer("ç", "c", "ğ", "g", "ı", "i", "ö", "o", "ş", "s", "ü", "u",
	"Ç", "C", "Ğ", "G", "İ", "I", "Ö", "O", "Ş", "S", "Ü", "U")

// normKey is normalize._norm_key(k): translate, lower, strip (P02 section 10.1).
func normKey(k string) string { return py.Strip(py.Lower(normKeyTR.Replace(k))) }

var suffixSeps = [...]string{"_", ".", "-", " "}

// AutoMap is normalize.auto_map(keys, sample, mapping). It is a Normalizer method because the value phase calls
// parse_timestamp (shared caches) and can raise.
func (n *Normalizer) AutoMap(keys []string, sample []*py.Dict, mapping *Mapping) (Roles, error) {
	var out Roles
	// lowered = {_norm_key(k): k}: ordered, last original key wins, first position kept
	var lowKeys []string
	lowVal := map[string]string{}
	for _, k := range keys {
		lk := normKey(k)
		if _, ok := lowVal[lk]; !ok {
			lowKeys = append(lowKeys, lk)
		}
		lowVal[lk] = k
	}
	inKeys := map[string]bool{}
	for _, k := range keys {
		inKeys[k] = true
	}
	get := func(r *py.Dict, k string) py.Value {
		v, _ := r.Get(k)
		return v
	}
	filled := func(k string) int {
		c := 0
		for _, r := range sample {
			if !get(r, k).IsNoneOrEmpty() {
				c++
			}
		}
		return c
	}
	for role := 0; role < NumRoles; role++ {
		if mapping.Len() > 0 {
			if v, ok := mapping.m.Get(RoleNames[role]); ok && v.K == py.KStr && inKeys[v.S] {
				out.Set(role, v.S)
				continue
			}
		}
		hints := roleHints[role]
		var cands []string
		for _, h := range hints {
			if k, ok := lowVal[h]; ok {
				cands = append(cands, k)
			}
		}
		hit, has := "", false
		if len(cands) > 0 {
			hit, has = cands[0], true
		}
		if len(cands) > 1 && len(sample) > 0 {
			best := -1
			for _, k := range cands { // first maximum wins (ties -> earliest hint)
				if f := filled(k); f > best {
					best, hit = f, k
				}
			}
		}
		if !has {
		suffix:
			for _, lk := range lowKeys {
				for _, h := range hints {
					for _, sep := range suffixSeps {
						if strings.HasSuffix(lk, sep+h) {
							hit, has = lowVal[lk], true
							break suffix
						}
					}
				}
			}
		}
		if has {
			out.Set(role, hit)
		}
	}
	if len(sample) == 0 {
		return out, nil
	}
	taken := map[string]bool{}
	for i := 0; i < NumRoles; i++ {
		if k, ok := out.Key(i); ok {
			taken[k] = true
		}
	}
	// share(k, pred): fraction of the non-empty values of k that satisfy pred
	share := func(k string, pred func(py.Value) (bool, error)) (float64, error) {
		total, hits := 0, 0
		for _, r := range sample {
			v := get(r, k)
			if v.IsNoneOrEmpty() {
				continue
			}
			total++
			ok, err := pred(v)
			if err != nil {
				return 0, err
			}
			if ok {
				hits++
			}
		}
		if total == 0 {
			return 0, nil
		}
		return float64(hits) / float64(total), nil
	}
	// best key by share among keys not taken (first maximum); assigned when share > 0.8 and the key is truthy
	pick := func(role int, pred func(py.Value) (bool, error)) error {
		if _, ok := out.Get(role); ok {
			return nil
		}
		best, bestShare, found := "", 0.0, false
		for _, k := range keys {
			if taken[k] {
				continue
			}
			sh, err := share(k, pred)
			if err != nil {
				return err
			}
			if !found || sh > bestShare {
				best, bestShare, found = k, sh, true
			}
		}
		if !found || best == "" {
			return nil
		}
		sh, err := share(best, pred)
		if err != nil {
			return err
		}
		if sh > 0.8 {
			out.Set(role, best)
			taken[best] = true
		}
		return nil
	}
	tsPred := func(v py.Value) (bool, error) {
		r, err := n.parseTS(v)
		if err != nil {
			return false, err
		}
		return r.ok && py.Len(v.Str()) >= 8, nil
	}
	sevPred := func(v py.Value) (bool, error) {
		_, ok := severityMap[py.Lower(v.Str())]
		return ok, nil
	}
	envPred := func(v py.Value) (bool, error) {
		_, ok := envMap[py.Lower(py.Strip(v.Str()))]
		return ok, nil
	}
	if err := pick(RoleTimestamp, tsPred); err != nil {
		return Roles{}, err
	}
	if err := pick(RoleSeverity, sevPred); err != nil {
		return Roles{}, err
	}
	if err := pick(RoleEnvironment, envPred); err != nil {
		return Roles{}, err
	}
	if _, ok := out.Get(RoleMessage); !ok {
		numeric := func(v py.Value) (bool, error) {
			_, err := py.ParseFloat(v.Str())
			return err == nil, nil
		}
		best, bestLen, found := "", -1, false
		for _, k := range keys {
			if taken[k] {
				continue
			}
			sh, _ := share(k, numeric)
			if sh >= 0.5 {
				continue
			}
			total := 0
			for _, r := range sample {
				if v, ok := r.Get(k); ok {
					total += py.Len(v.Str())
				}
			}
			if !found || total > bestLen {
				best, bestLen, found = k, total, true
			}
		}
		if found {
			out.Set(RoleMessage, best)
		}
	}
	return out, nil
}
