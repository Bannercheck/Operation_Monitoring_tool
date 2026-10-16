package otlp

import (
	"encoding/hex"
	"encoding/json"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"watchover/receiver/parse"
	"watchover/receiver/parse/py"
)

// Conversion of decoded OTLP into what the dashboard already ingests:
//
//	logs     canonical observations (the parse stage's v=2 golden lines) -> LiveStore.ingest_parsed, like any parsed batch
//	metrics  host samples cpu / memory / disk / gpu in percent, as metric observations (LiveStore._accept's metric_of)
//	traces   span rows in TraceStore.add_spans' shape, with the RED rollups already summed here (spans are marked _counted)
//
// Everything that costs per item (decoding, attribute flattening, severity / environment mapping, CPU-time rates, latency
// buckets) happens here; the dashboard appends.

const maxMessage = 64 << 10

// Text renders a scalar value; maps and arrays become compact JSON, bytes hex.
func (v *Any) Text() string {
	switch v.Kind {
	case VStr:
		return v.S
	case VBool:
		if v.B {
			return "true"
		}
		return "false"
	case VInt:
		return strconv.FormatInt(v.I, 10)
	case VFloat:
		return strconv.FormatFloat(v.F, 'g', -1, 64)
	case VBytes:
		return hex.EncodeToString(v.Bytes)
	case VArr, VMap:
		b, _ := json.Marshal(v.plain())
		return string(b)
	}
	return ""
}

// plain is the value as Go data for encoding/json.
func (v *Any) plain() any {
	switch v.Kind {
	case VStr:
		return v.S
	case VBool:
		return v.B
	case VInt:
		return v.I
	case VFloat:
		if math.IsNaN(v.F) || math.IsInf(v.F, 0) {
			return nil
		}
		return v.F
	case VBytes:
		return hex.EncodeToString(v.Bytes)
	case VArr:
		out := make([]any, len(v.Arr))
		for i := range v.Arr {
			out[i] = v.Arr[i].plain()
		}
		return out
	case VMap:
		out := make(map[string]any, len(v.KV))
		for i := range v.KV {
			out[v.KV[i].Key] = v.KV[i].Val.plain()
		}
		return out
	}
	return nil
}

// pyValue is the attribute value for an observation's attributes (scalars as themselves, arrays as lists).
func (v *Any) pyValue() py.Value {
	switch v.Kind {
	case VStr:
		return py.S(v.S)
	case VBool:
		return py.B(v.B)
	case VInt:
		return py.Int(v.I)
	case VFloat:
		if math.IsNaN(v.F) || math.IsInf(v.F, 0) {
			return py.Null
		}
		return py.F(v.F)
	case VBytes:
		return py.S(hex.EncodeToString(v.Bytes))
	case VArr:
		out := make([]py.Value, 0, len(v.Arr))
		for i := range v.Arr {
			if v.Arr[i].Kind == VArr || v.Arr[i].Kind == VMap {
				out = append(out, py.S(v.Arr[i].Text()))
			} else {
				out = append(out, v.Arr[i].pyValue())
			}
		}
		return py.ListOf(out...)
	}
	return py.Null
}

// putAttrs flattens kvs into d: nested maps become dotted keys (http.request.header.x), at most 64 keys per record.
func putAttrs(d *py.Dict, prefix string, kvs []KV, depth int) {
	for i := range kvs {
		kv := &kvs[i]
		if d.Len() >= 64 || kv.Key == "" {
			continue
		}
		k := kv.Key
		if prefix != "" {
			k = prefix + "." + k
		}
		if kv.Val.Kind == VMap && depth < 3 {
			putAttrs(d, k, kv.Val.KV, depth+1)
			continue
		}
		if kv.Val.Kind == VNone {
			continue
		}
		d.Set(k, kv.Val.pyValue())
	}
}

// Resource attributes kept on every record: the ones the dashboard reads (technology evidence, Kubernetes placement).
var keepResource = []string{"service.namespace", "service.version", "service.instance.id", "k8s.namespace.name", "k8s.pod.name",
	"k8s.deployment.name", "k8s.node.name", "container.name", "container.image.name", "process.runtime.name", "process.runtime.version",
	"telemetry.sdk.language", "os.type", "cloud.provider", "cloud.region", "webengine.name"}

type res struct {
	service, host, env string
	keep               []KV
}

func resourceOf(kvs []KV) res {
	r := res{service: str(kvs, "service.name"), host: str(kvs, "host.name", "host.hostname", "k8s.node.name", "k8s.pod.name", "container.name")}
	if strings.HasPrefix(r.service, "unknown_service") {
		r.service = ""
	}
	if e := str(kvs, "deployment.environment.name", "deployment.environment"); e != "" {
		if n, err := parse.NormalizeEnvironment(py.S(e)); err == nil {
			r.env = n
		}
	}
	for _, k := range keepResource {
		if v, ok := find(kvs, k); ok && v.Kind != VNone {
			r.keep = append(r.keep, KV{Key: k, Val: *v})
		}
	}
	return r
}

func tsOf(ns uint64, now time.Time) py.DateTime {
	if ns == 0 || ns > uint64(math.MaxInt64) {
		return py.DateTime{T: now.UTC().Truncate(time.Microsecond)}
	}
	t := time.Unix(0, int64(ns)).UTC()
	if t.Year() < 2000 || t.After(now.Add(24*time.Hour)) {
		return py.DateTime{T: now.UTC().Truncate(time.Microsecond)}
	}
	return py.DateTime{T: t.Truncate(time.Microsecond)}
}

// severity: severity_number ranges first (TRACE / DEBUG -> DEBUG, INFO, WARN, ERROR, FATAL -> CRITICAL), then the text.
func severity(num int32, text string) string {
	switch {
	case num >= 21:
		return "CRITICAL"
	case num >= 17:
		return "ERROR"
	case num >= 13:
		return "WARN"
	case num >= 9:
		return "INFO"
	case num >= 1:
		return "DEBUG"
	}
	if text != "" {
		if s, err := parse.NormalizeSeverity(py.S(text)); err == nil && s != "" {
			return s
		}
	}
	return "INFO"
}

func idHex(b []byte, n int) string {
	if len(b) != n {
		return ""
	}
	for _, c := range b {
		if c != 0 {
			return hex.EncodeToString(b)
		}
	}
	return ""
}

func message(body *Any) string {
	var m string
	switch body.Kind {
	case VStr:
		m = body.S
	case VMap:
		for _, k := range []string{"message", "msg", "body", "log"} {
			if v, ok := find(body.KV, k); ok && v.Kind == VStr {
				m = v.S
				break
			}
		}
		if m == "" {
			m = body.Text()
		}
	default:
		m = body.Text()
	}
	if len(m) > maxMessage {
		m = m[:maxMessage]
	}
	return m
}

// Logs turns OTLP log records into canonical observations.
func Logs(rls []ResourceLogs, now time.Time) []parse.Observation {
	n := 0
	for i := range rls {
		for j := range rls[i].Scopes {
			n += len(rls[i].Scopes[j].Records)
		}
	}
	out := make([]parse.Observation, 0, n)
	for i := range rls {
		r := resourceOf(rls[i].Resource)
		for j := range rls[i].Scopes {
			sc := rls[i].Scopes[j].Scope
			for k := range rls[i].Scopes[j].Records {
				lr := &rls[i].Scopes[j].Records[k]
				a := py.NewDict(len(lr.Attrs) + len(r.keep) + 4)
				putAttrs(a, "", lr.Attrs, 0)
				putAttrs(a, "", r.keep, 0)
				if t := idHex(lr.TraceID, 16); t != "" {
					a.Set("trace_id", py.S(t))
				}
				if s := idHex(lr.SpanID, 8); s != "" {
					a.Set("span_id", py.S(s))
				}
				if sc.Name != "" {
					a.Set("otel.scope.name", py.S(sc.Name))
				}
				if lr.EventName != "" {
					a.Set("event.name", py.S(lr.EventName))
				}
				service := r.service
				if service == "" {
					service = str(lr.Attrs, "service.name")
				}
				origin := str(lr.Attrs, "code.namespace", "log.logger", "logger.name")
				if origin == "" && sc.Name != "" && !strings.Contains(sc.Name, "opentelemetry") {
					origin = sc.Name
				}
				msg := message(&lr.Body)
				ts := lr.TimeNs
				if ts == 0 {
					ts = lr.ObservedNs
				}
				out = append(out, parse.Observation{TS: tsOf(ts, now), Message: msg, Kind: "log", Severity: severity(lr.SevNum, lr.SevText),
					Service: service, Host: r.host, Environment: r.env, Origin: origin, Attributes: a, Source: "otlp", LineNo: len(out) + 1,
					Parser: "otlp", ParserConfidence: 1.0, Raw: msg})
			}
		}
	}
	return out
}

// ---------------------------------------------------------------- metrics

// Metrics converts host metrics to percent samples. It keeps the previous system.cpu.time per host (a cumulative
// counter needs two readings for a rate), so one Metrics value lives for the receiver's lifetime.
type Metrics struct {
	mu   sync.Mutex
	prev map[string][3]float64 // host -> {busy seconds, total seconds, time ns}
}

func NewMetrics() *Metrics { return &Metrics{prev: map[string][3]float64{}} }

var pseudoFS = map[string]bool{"tmpfs": true, "devtmpfs": true, "overlay": true, "squashfs": true, "proc": true, "sysfs": true, "nsfs": true,
	"autofs": true, "devpts": true, "mqueue": true, "cgroup": true, "cgroup2": true, "tracefs": true, "debugfs": true, "fuse.lxcfs": true}

// directNames are metrics already in Watchover's own names (percent values), LiveStore METRIC_KEYS.
var directNames = map[string]string{"cpu": "cpu", "cpu_percent": "cpu", "cpu_pct": "cpu", "memory": "memory", "mem": "memory",
	"mem_percent": "memory", "memory_percent": "memory", "disk": "disk", "disk_percent": "disk", "disk_pct": "disk", "gpu": "gpu",
	"gpu_percent": "gpu", "gpu_util": "gpu", "gpu_utilization": "gpu"}

var gpuNames = map[string]bool{"gpu.utilization": true, "nvidia.gpu.utilization": true, "hw.gpu.utilization": true, "dcgm.gpu.utilization": true}

type sample struct {
	metric string
	ns     uint64
	value  float64
}

// Convert returns one metric observation per (host, metric, timestamp) and the number of points it did not use.
func (m *Metrics) Convert(rms []ResourceMetrics, now time.Time) ([]parse.Observation, int) {
	var out []parse.Observation
	skipped := 0
	for i := range rms {
		r := resourceOf(rms[i].Resource)
		host := r.host
		if host == "" {
			host = r.service
		}
		var samples []sample
		for j := range rms[i].Scopes {
			for k := range rms[i].Scopes[j].Metrics {
				mt := &rms[i].Scopes[j].Metrics[k]
				s, ok := m.metric(host, mt)
				if !ok {
					skipped += len(mt.Points)
					continue
				}
				samples = append(samples, s...)
			}
		}
		for _, s := range samples {
			if math.IsNaN(s.value) || math.IsInf(s.value, 0) {
				continue
			}
			v := math.Round(math.Max(0, math.Min(100, s.value))*10) / 10
			a := py.NewDict(3)
			a.Set("metric", py.S(s.metric))
			a.Set("value", py.F(v))
			a.Set("unit", py.S("%"))
			msg := s.metric + " " + strconv.FormatFloat(v, 'f', 1, 64) + "%"
			out = append(out, parse.Observation{TS: tsOf(s.ns, now), Message: msg, Kind: "metric", Severity: "INFO", Service: r.service, Host: r.host,
				Environment: r.env, Attributes: a, Source: "otlp-metrics", LineNo: len(out) + 1, Parser: "otlp", ParserConfidence: 1.0, Raw: msg})
		}
	}
	return out, skipped
}

func attrOf(p Point, k string) string { return str(p.Attrs, k) }

// byTime groups points by timestamp, in time order.
func byTime(pts []Point) ([]uint64, map[uint64][]Point) {
	g := map[uint64][]Point{}
	for _, p := range pts {
		g[p.TimeNs] = append(g[p.TimeNs], p)
	}
	keys := make([]uint64, 0, len(g))
	for k := range g {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	return keys, g
}

func pct(v float64) float64 {
	if v <= 1.0 {
		return v * 100
	}
	return v
}

func (m *Metrics) metric(host string, mt *Metric) ([]sample, bool) {
	name := strings.ToLower(mt.Name)
	if n, ok := directNames[name]; ok {
		var out []sample
		for _, p := range mt.Points {
			out = append(out, sample{n, p.TimeNs, p.Value})
		}
		return out, true
	}
	keys, g := byTime(mt.Points)
	var out []sample
	switch {
	case name == "system.cpu.utilization":
		for _, t := range keys {
			idle, n, sum := 0.0, 0, 0.0
			for _, p := range g[t] {
				st := attrOf(p, "state")
				sum += p.Value
				if st == "idle" || st == "wait" {
					idle += p.Value
				}
				if st == "idle" {
					n++
				}
			}
			if n > 0 {
				out = append(out, sample{"cpu", t, (1 - idle/float64(n)) * 100})
			} else if len(g[t]) > 0 {
				out = append(out, sample{"cpu", t, pct(sum / float64(len(g[t])))})
			}
		}
	case name == "system.cpu.time":
		for _, t := range keys {
			busy, total := 0.0, 0.0
			for _, p := range g[t] {
				total += p.Value
				if st := attrOf(p, "state"); st != "idle" && st != "wait" {
					busy += p.Value
				}
			}
			m.mu.Lock()
			prev, seen := m.prev[host]
			if len(m.prev) > 100000 {
				m.prev = map[string][3]float64{}
			}
			m.prev[host] = [3]float64{busy, total, float64(t)}
			m.mu.Unlock()
			if seen && float64(t) > prev[2] && total > prev[1] && busy >= prev[0] {
				out = append(out, sample{"cpu", t, (busy - prev[0]) / (total - prev[1]) * 100})
			}
		}
	case name == "system.memory.utilization":
		for _, t := range keys {
			used, any := 0.0, false
			for _, p := range g[t] {
				if st := attrOf(p, "state"); st == "used" || st == "" {
					used += p.Value
					any = true
				}
			}
			if any {
				out = append(out, sample{"memory", t, pct(used)})
			}
		}
	case name == "system.memory.usage":
		for _, t := range keys {
			used, total := 0.0, 0.0
			for _, p := range g[t] {
				switch attrOf(p, "state") {
				case "used":
					used += p.Value
					total += p.Value
				case "free", "buffered", "cached", "slab_reclaimable", "slab_unreclaimable":
					total += p.Value
				}
			}
			if total > 0 {
				out = append(out, sample{"memory", t, used / total * 100})
			}
		}
	case name == "system.filesystem.utilization" || name == "system.filesystem.usage":
		for _, t := range keys {
			type fsu struct{ used, free, ratio float64 }
			mounts := map[string]*fsu{}
			for _, p := range g[t] {
				if pseudoFS[attrOf(p, "type")] {
					continue
				}
				mp := attrOf(p, "mountpoint") + "|" + attrOf(p, "device")
				f := mounts[mp]
				if f == nil {
					f = &fsu{ratio: -1}
					mounts[mp] = f
				}
				if name == "system.filesystem.utilization" {
					f.ratio = math.Max(f.ratio, p.Value)
					continue
				}
				switch attrOf(p, "state") {
				case "used":
					f.used += p.Value
				case "free":
					f.free += p.Value
				}
			}
			best, ok := 0.0, false
			for _, f := range mounts {
				v := f.ratio
				if name == "system.filesystem.usage" {
					if f.used+f.free <= 0 {
						continue
					}
					v = f.used / (f.used + f.free)
				}
				if v >= 0 {
					best, ok = math.Max(best, v), true
				}
			}
			if ok {
				out = append(out, sample{"disk", t, best * 100})
			}
		}
	case gpuNames[name]:
		for _, t := range keys {
			best := 0.0
			for _, p := range g[t] {
				best = math.Max(best, pct(p.Value))
			}
			out = append(out, sample{"gpu", t, best})
		}
	default:
		return nil, false
	}
	return out, true
}

// ---------------------------------------------------------------- traces

// BOUNDS of TraceStore's latency histogram (traces.py): bucket i holds durations <= BOUNDS[i], the last one the rest.
var bounds = []float64{1, 2, 5, 10, 20, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 20000, 30000, 60000, 120000}

func bucket(ms float64) int { return sort.SearchFloat64s(bounds, ms) }

// SpanRow is one row for TraceStore.add_spans (start as ISO text; the dashboard turns it into a datetime).
type SpanRow struct {
	TraceID     string         `json:"trace_id"`
	SpanID      *string        `json:"span_id"`
	ParentID    *string        `json:"parent_id"`
	Start       string         `json:"start"`
	DurationMS  float64        `json:"duration_ms"`
	HasDuration bool           `json:"has_duration"`
	Service     string         `json:"service"`
	Operation   string         `json:"operation"`
	Kind        string         `json:"kind"`
	Status      int            `json:"status"`
	Error       int            `json:"error"`
	Env         string         `json:"env"`
	Host        string         `json:"host"`
	User        string         `json:"_user,omitempty"` // pseudonymised by the dashboard, never stored as is
	SessionID   string         `json:"session_id"`
	Link        string         `json:"link"`
	Confidence  float64        `json:"confidence"`
	Ref         string         `json:"ref"`
	Message     string         `json:"message"`
	Severity    string         `json:"severity"`
	Attrs       map[string]any `json:"attrs"`
	Counted     bool           `json:"_counted"`
}

// Agg is one RED rollup cell: minute, service, operation, kind, env -> count, errors, sum ms, max ms, histogram.
type Agg struct {
	Minute, Service, Operation, Kind, Env string
	N, Err                                int
	Sum, Max                              float64
	Hist                                  []int
}

func (a Agg) MarshalJSON() ([]byte, error) {
	return json.Marshal([]any{a.Minute, a.Service, a.Operation, a.Kind, a.Env, a.N, a.Err, math.Round(a.Sum*1000) / 1000, a.Max, a.Hist})
}

var spanKinds = map[int32]string{1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}

// span attributes copied to the row (technology evidence and the trace detail panel).
var spanKeep = []string{"db.system", "db.system.name", "messaging.system", "rpc.system", "rpc.service", "peer.service", "http.route",
	"server.address", "net.peer.name", "db.name", "db.namespace", "messaging.destination.name"}

func intAttr(kvs []KV, keys ...string) int {
	for _, k := range keys {
		if v, ok := find(kvs, k); ok {
			switch v.Kind {
			case VInt:
				return int(v.I)
			case VFloat:
				return int(v.F)
			case VStr:
				if n, err := strconv.Atoi(strings.TrimSpace(v.S)); err == nil {
					return n
				}
			}
		}
	}
	return 0
}

// Traces turns spans into rows and sums their RED rollups.
func Traces(rss []ResourceSpans, now time.Time) ([]SpanRow, []Agg) {
	var rows []SpanRow
	cells := map[[5]string]*Agg{}
	var order [][5]string
	for i := range rss {
		r := resourceOf(rss[i].Resource)
		lang := str(rss[i].Resource, "telemetry.sdk.language")
		for j := range rss[i].Scopes {
			sc := rss[i].Scopes[j].Scope
			for k := range rss[i].Scopes[j].Spans {
				sp := &rss[i].Scopes[j].Spans[k]
				tid := idHex(sp.TraceID, 16)
				if tid == "" {
					continue
				}
				dur := 0.0
				if sp.EndNs > sp.StartNs && sp.StartNs > 0 {
					dur = float64(sp.EndNs-sp.StartNs) / 1e6
				}
				if dur > 24*3600*1000 {
					dur = 0
				}
				kind := spanKinds[sp.Kind]
				if kind == "" {
					kind = "internal"
				}
				if kind == "client" && (str(sp.Attrs, "db.system", "db.system.name") != "") {
					kind = "db"
				}
				method := str(sp.Attrs, "http.request.method", "http.method")
				status := intAttr(sp.Attrs, "http.response.status_code", "http.status_code")
				op := sp.Name
				if route := str(sp.Attrs, "http.route"); route != "" && kind == "server" {
					op = strings.TrimSpace(method + " " + route)
				}
				if op == "" {
					op = "-"
				}
				if len(op) > 160 {
					op = op[:160]
				}
				errFlag := 0
				if sp.StatusCode == 2 || status >= 500 {
					errFlag = 1
				}
				svc := r.service
				if svc == "" {
					svc = str(sp.Attrs, "service.name")
				}
				if svc == "" {
					svc = r.host
				}
				if len(svc) > 120 {
					svc = svc[:120]
				}
				msg := sp.Name
				if sp.StatusMsg != "" {
					msg = sp.Name + ": " + sp.StatusMsg
				}
				if len(msg) > 300 {
					msg = msg[:300]
				}
				sev := "INFO"
				if errFlag == 1 {
					sev = "ERROR"
				}
				attrs := map[string]any{}
				if method != "" {
					attrs["http.method"] = method
				}
				if status != 0 {
					attrs["http.status"] = status
				}
				if sc.Name != "" {
					attrs["otel.scope.name"] = sc.Name
				}
				if lang != "" {
					attrs["telemetry.sdk.language"] = lang
				}
				for _, key := range spanKeep {
					if v, ok := find(sp.Attrs, key); ok && v.Kind != VNone {
						attrs[key] = v.Text()
					}
				}
				start := tsOf(sp.StartNs, now)
				row := SpanRow{TraceID: tid, Start: start.ISO(), DurationMS: math.Round(dur*1000) / 1000, HasDuration: true, Service: svc,
					Operation: op, Kind: kind, Status: status, Error: errFlag, Env: r.env, Host: r.host,
					User: str(sp.Attrs, "enduser.id", "user.id"), SessionID: str(sp.Attrs, "session.id"), Link: "explicit", Confidence: 1.0,
					Ref: "otlp:" + svc, Message: msg, Severity: sev, Attrs: attrs, Counted: true}
				if s := idHex(sp.SpanID, 8); s != "" {
					row.SpanID = &s
				}
				if p := idHex(sp.ParentID, 8); p != "" {
					row.ParentID = &p
				}
				if len(row.SessionID) > 128 {
					row.SessionID = row.SessionID[:128]
				}
				rows = append(rows, row)
				key := [5]string{start.T.UTC().Format("2006-01-02T15:04"), svc, op, kind, r.env}
				c := cells[key]
				if c == nil {
					c = &Agg{Minute: key[0], Service: key[1], Operation: key[2], Kind: key[3], Env: key[4], Hist: make([]int, len(bounds)+1)}
					cells[key] = c
					order = append(order, key)
				}
				c.N++
				c.Err += errFlag
				c.Sum += row.DurationMS
				c.Max = math.Max(c.Max, row.DurationMS)
				c.Hist[bucket(row.DurationMS)]++
			}
		}
	}
	aggs := make([]Agg, 0, len(order))
	for _, k := range order {
		aggs = append(aggs, *cells[k])
	}
	return rows, aggs
}
