package otlp

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"testing"
	"time"

	"watchover/receiver/parse"
)

// pb is a tiny protobuf writer for the tests: messages are built field by field, exactly as an SDK encodes them.
type pb []byte

func (b pb) tag(f, wt int) pb { return binary.AppendUvarint(b, uint64(f<<3|wt)) }
func (b pb) raw(f int, v []byte) pb {
	b = b.tag(f, 2)
	b = binary.AppendUvarint(b, uint64(len(v)))
	return append(b, v...)
}
func (b pb) str(f int, s string) pb  { return b.raw(f, []byte(s)) }
func (b pb) msg(f int, m pb) pb      { return b.raw(f, m) }
func (b pb) vint(f int, v uint64) pb { return binary.AppendUvarint(b.tag(f, 0), v) }
func (b pb) f64(f int, v uint64) pb  { return binary.LittleEndian.AppendUint64(b.tag(f, 1), v) }

func kv(k string, v pb) pb { return pb{}.str(1, k).msg(2, v) }
func sv(s string) pb       { return pb{}.str(1, s) }
func iv(i int64) pb        { return pb{}.vint(3, uint64(i)) }
func dv(f float64) pb      { return pb{}.f64(4, math.Float64bits(f)) }

var (
	now     = time.Date(2026, 10, 16, 9, 0, 0, 0, time.UTC)
	t0      = uint64(now.Add(-time.Minute).UnixNano())
	traceID = []byte{0x5b, 0x8e, 0xff, 0xf7, 0x98, 0x03, 0x81, 0x03, 0xd2, 0x69, 0xb6, 0x33, 0x81, 0x3f, 0xc6, 0x0c}
	spanID  = []byte{0xee, 0xe1, 0x9b, 0x7e, 0xc3, 0xc1, 0xb1, 0x74}
)

func resourcePB() pb {
	return pb{}.msg(1, kv("service.name", sv("checkout"))).msg(1, kv("host.name", sv("prd-web-01"))).
		msg(1, kv("deployment.environment", sv("production"))).msg(1, kv("k8s.pod.name", sv("checkout-7f9"))).
		msg(1, kv("telemetry.sdk.language", sv("java"))).msg(1, kv("process.pid", iv(4242)))
}

func logsPB() []byte {
	rec := pb{}.f64(1, t0).vint(2, 17).str(3, "Error").msg(5, sv("payment failed for order 42")).
		msg(6, kv("http.status_code", iv(502))).msg(6, kv("retry", pb{}.vint(2, 1))).
		msg(6, kv("ctx", pb{}.msg(6, pb{}.msg(1, kv("tenant", sv("acme")))))).
		raw(9, traceID).raw(10, spanID)
	info := pb{}.f64(11, t0).str(3, "warning").msg(5, sv("slow call"))
	scope := pb{}.msg(1, pb{}.str(1, "com.shop.Checkout").str(2, "1.0")).msg(2, rec).msg(2, info)
	return pb{}.msg(1, pb{}.msg(1, resourcePB()).msg(2, scope))
}

const logsJSON = `{"resourceLogs":[{"resource":{"attributes":[
 {"key":"service.name","value":{"stringValue":"checkout"}},{"key":"host.name","value":{"stringValue":"prd-web-01"}},
 {"key":"deployment.environment","value":{"stringValue":"production"}},{"key":"k8s.pod.name","value":{"stringValue":"checkout-7f9"}},
 {"key":"telemetry.sdk.language","value":{"stringValue":"java"}},{"key":"process.pid","value":{"intValue":"4242"}}]},
 "scopeLogs":[{"scope":{"name":"com.shop.Checkout","version":"1.0"},"logRecords":[
  {"timeUnixNano":"%T0%","severityNumber":17,"severityText":"Error","body":{"stringValue":"payment failed for order 42"},
   "attributes":[{"key":"http.status_code","value":{"intValue":502}},{"key":"retry","value":{"boolValue":true}},
    {"key":"ctx","value":{"kvlistValue":{"values":[{"key":"tenant","value":{"stringValue":"acme"}}]}}}],
   "traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174"},
  {"observedTimeUnixNano":%T0%,"severityText":"warning","body":{"stringValue":"slow call"}}]}]}]}`

func lines(t *testing.T, obs []parse.Observation) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, o := range obs {
		b, err := parse.SpoolLine(o)
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		if err := json.Unmarshal(b, &m); err != nil {
			t.Fatalf("spool line is not JSON: %s", b)
		}
		out = append(out, m)
	}
	return out
}

func TestLogsProtobufAndJSONAgree(t *testing.T) {
	rpb, err := DecodeLogsPB(logsPB())
	if err != nil {
		t.Fatal(err)
	}
	js := []byte(strings.ReplaceAll(logsJSON, "%T0%", strconv.FormatUint(t0, 10)))
	rjs, err := DecodeLogsJSON(js)
	if err != nil {
		t.Fatal(err)
	}
	a, b := lines(t, Logs(rpb, now)), lines(t, Logs(rjs, now))
	ja, _ := json.Marshal(a)
	jb, _ := json.Marshal(b)
	if string(ja) != string(jb) {
		t.Fatalf("protobuf and JSON differ:\n%s\n%s", ja, jb)
	}
	if len(a) != 2 {
		t.Fatalf("want 2 records, got %d", len(a))
	}
	o := a[0]
	at := o["attributes"].(map[string]any)
	want := map[string]any{"severity": "ERROR", "service": "checkout", "host": "prd-web-01", "environment": "prod", "origin": "com.shop.Checkout",
		"message": "payment failed for order 42", "kind": "log", "parser": "otlp"}
	for k, v := range want {
		if o[k] != v {
			t.Errorf("%s = %v, want %v", k, o[k], v)
		}
	}
	if at["trace_id"] != hex.EncodeToString(traceID) || at["span_id"] != hex.EncodeToString(spanID) || at["http.status_code"] != float64(502) ||
		at["retry"] != true || at["ctx.tenant"] != "acme" || at["k8s.pod.name"] != "checkout-7f9" || at["otel.scope.name"] != "com.shop.Checkout" ||
		at["telemetry.sdk.language"] != "java" {
		t.Errorf("attributes %v", at)
	}
	if _, ok := at["process.pid"]; ok {
		t.Errorf("resource attributes outside the kept list must not ride on every record")
	}
	if a[1]["severity"] != "WARN" || a[1]["ts"] != a[0]["ts"] {
		t.Errorf("second record %v", a[1])
	}
}

func TestTracesRowsAndRollups(t *testing.T) {
	srv := pb{}.raw(1, traceID).raw(2, spanID).str(5, "GET /users/{id}").vint(6, 2).f64(7, t0).f64(8, t0+uint64(250*time.Millisecond)).
		msg(9, kv("http.request.method", sv("GET"))).msg(9, kv("http.route", sv("/users/{id}"))).msg(9, kv("http.response.status_code", iv(503))).
		msg(9, kv("enduser.id", sv("ayse@example.com"))).msg(15, pb{}.str(2, "upstream down").vint(3, 2))
	db := pb{}.raw(1, traceID).raw(2, []byte{1, 2, 3, 4, 5, 6, 7, 8}).raw(4, spanID).str(5, "SELECT orders").vint(6, 3).f64(7, t0).
		f64(8, t0+uint64(12*time.Millisecond)).msg(9, kv("db.system", sv("postgresql")))
	noTrace := pb{}.str(5, "orphan").vint(6, 1).f64(7, t0).f64(8, t0+1)
	req := pb{}.msg(1, pb{}.msg(1, resourcePB()).msg(2, pb{}.msg(1, pb{}.str(1, "io.opentelemetry.spring-webmvc-6.0")).msg(2, srv).msg(2, db).msg(2, noTrace)))
	rss, err := DecodeTracesPB(req)
	if err != nil {
		t.Fatal(err)
	}
	rows, aggs := Traces(rss, now)
	if len(rows) != 2 {
		t.Fatalf("want 2 rows (a span without a trace id is dropped), got %d", len(rows))
	}
	s := rows[0]
	if s.Operation != "GET /users/{id}" || s.Kind != "server" || s.Status != 503 || s.Error != 1 || s.DurationMS != 250 || s.Service != "checkout" ||
		s.Env != "prod" || s.Host != "prd-web-01" || s.User != "ayse@example.com" || s.Link != "explicit" || !s.Counted || s.Severity != "ERROR" ||
		s.Message != "GET /users/{id}: upstream down" || s.Attrs["otel.scope.name"] != "io.opentelemetry.spring-webmvc-6.0" ||
		s.Attrs["telemetry.sdk.language"] != "java" || *s.SpanID != hex.EncodeToString(spanID) || s.ParentID != nil {
		t.Fatalf("server span %+v", s)
	}
	if d := rows[1]; d.Kind != "db" || *d.ParentID != hex.EncodeToString(spanID) || d.Attrs["db.system"] != "postgresql" || d.Error != 0 {
		t.Fatalf("db span %+v", d)
	}
	if len(aggs) != 2 || aggs[0].N != 1 || aggs[0].Err != 1 || aggs[0].Hist[8] != 1 || aggs[1].Hist[bucket(12)] != 1 || bucket(12) != 4 {
		t.Fatalf("rollups %+v", aggs)
	}
	b, _ := json.Marshal(aggs[0])
	if string(b[:2]) != `["` {
		t.Fatalf("a rollup travels as a JSON array: %s", b)
	}
}

func cpuTime(ts uint64, idle, user, sys float64) pb {
	pt := func(state string, v float64) pb {
		return pb{}.msg(7, kv("cpu", sv("cpu0"))).msg(7, kv("state", sv(state))).f64(3, ts).f64(4, math.Float64bits(v))
	}
	sum := pb{}.msg(1, pt("idle", idle)).msg(1, pt("user", user)).msg(1, pt("system", sys)).vint(2, 2).vint(3, 1)
	return pb{}.str(1, "system.cpu.time").str(3, "s").msg(7, sum)
}

func gaugeStates(name string, ts uint64, attrs [][2]string, vals []float64, sum bool) pb {
	var data pb
	for i, v := range vals {
		p := pb{}.f64(3, ts).f64(4, math.Float64bits(v))
		for _, a := range attrs[i*2 : i*2+2] {
			if a[0] != "" {
				p = p.msg(7, kv(a[0], sv(a[1])))
			}
		}
		data = data.msg(1, p)
	}
	f := 5
	if sum {
		f = 7
	}
	return pb{}.str(1, name).msg(f, data)
}

func metricsReq(ms ...pb) []byte {
	sc := pb{}
	for _, m := range ms {
		sc = sc.msg(2, m)
	}
	return pb{}.msg(1, pb{}.msg(1, resourcePB()).msg(2, sc))
}

func TestHostMetrics(t *testing.T) {
	m := NewMetrics()
	r1, err := DecodeMetricsPB(metricsReq(cpuTime(t0, 80, 15, 5)))
	if err != nil {
		t.Fatal(err)
	}
	if obs, _ := m.Convert(r1, now); len(obs) != 0 {
		t.Fatalf("one cpu.time reading has no rate yet: %d samples", len(obs))
	}
	t1 := t0 + uint64(10*time.Second)
	mem := gaugeStates("system.memory.usage", t1, [][2]string{{"state", "used"}, {}, {"state", "free"}, {}, {"state", "cached"}, {}}, []float64{4e9, 2e9, 2e9}, true)
	fs := gaugeStatesFS(t1)
	other := gaugeStates("process.runtime.go.goroutines", t1, [][2]string{{}, {}}, []float64{12}, false)
	direct := gaugeStates("gpu", t1, [][2]string{{}, {}}, []float64{41.26}, false)
	r2, err := DecodeMetricsPB(metricsReq(cpuTime(t1, 88, 20, 12), mem, fs, other, direct))
	if err != nil {
		t.Fatal(err)
	}
	obs, skipped := m.Convert(r2, now)
	got := map[string]float64{}
	for _, o := range lines(t, obs) {
		a := o["attributes"].(map[string]any)
		got[a["metric"].(string)] = a["value"].(float64)
		if o["kind"] != "metric" || o["host"] != "prd-web-01" {
			t.Fatalf("metric observation %v", o)
		}
	}
	want := map[string]float64{"cpu": 60, "memory": 50, "disk": 30, "gpu": 41.3}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %v, want %v (all: %v)", k, got[k], v, got)
		}
	}
	if skipped != 1 {
		t.Errorf("skipped = %d, want 1 (goroutines)", skipped)
	}
}

// gaugeStatesFS: "/" ext4 30 used / 70 free, "/run" tmpfs 99 used / 1 free (pseudo file systems never count).
func gaugeStatesFS(ts uint64) pb {
	pt := func(mp, typ, state string, v float64) pb {
		return pb{}.msg(7, kv("mountpoint", sv(mp))).msg(7, kv("type", sv(typ))).msg(7, kv("state", sv(state))).f64(3, ts).f64(4, math.Float64bits(v))
	}
	data := pb{}.msg(1, pt("/", "ext4", "used", 30)).msg(1, pt("/", "ext4", "free", 70)).msg(1, pt("/run", "tmpfs", "used", 99)).msg(1, pt("/run", "tmpfs", "free", 1))
	return pb{}.str(1, "system.filesystem.usage").msg(7, data)
}

func TestMalformedInputIsAnErrorNotAPanic(t *testing.T) {
	good := logsPB()
	for i := 1; i < len(good); i += 3 {
		_, _ = DecodeLogsPB(good[:i])
	}
	bad := [][]byte{{0x0a, 0xff, 0xff, 0xff, 0xff, 0x0f}, {0x0b}, {0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01}}
	for _, b := range bad {
		if _, err := DecodeLogsPB(b); err == nil {
			t.Errorf("% x decoded without error", b)
		}
		_, _ = DecodeTracesPB(b)
		_, _ = DecodeMetricsPB(b)
	}
	deep := sv("x")
	for i := 0; i < maxDepth+5; i++ {
		deep = pb{}.msg(5, pb{}.msg(1, deep))
	}
	if _, err := DecodeLogsPB(pb{}.msg(1, pb{}.msg(2, pb{}.msg(2, pb{}.msg(5, deep))))); err == nil {
		t.Error("a value nested deeper than maxDepth must be refused")
	}
	if _, err := DecodeLogsJSON([]byte(`{"resourceLogs":[{"scopeLogs":[{"logRecords":[{"timeUnixNano":"x1"}]}]}]}`)); err == nil {
		t.Error("a bad integer in JSON must be refused")
	}
}

func FuzzDecodeLogsPB(f *testing.F) {
	f.Add(logsPB())
	f.Add([]byte{})
	f.Fuzz(func(t *testing.T, b []byte) {
		if rls, err := DecodeLogsPB(b); err == nil {
			for _, o := range Logs(rls, now) {
				_, _ = parse.SpoolLine(o)
			}
		}
	})
}

func BenchmarkLogsPB(b *testing.B) {
	rec := pb{}.f64(1, t0).vint(2, 9).msg(5, sv("GET /api/orders/42 200 12ms")).msg(6, kv("http.status_code", iv(200))).
		msg(6, kv("http.route", sv("/api/orders/{id}"))).raw(9, traceID).raw(10, spanID)
	sc := pb{}.msg(1, pb{}.str(1, "com.shop.Http"))
	for i := 0; i < 1000; i++ { // one resource, 1000 records: what a collector batch looks like
		sc = sc.msg(2, rec)
	}
	req := []byte(pb{}.msg(1, pb{}.msg(1, resourcePB()).msg(2, sc)))
	b.SetBytes(int64(len(req)))
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		rls, err := DecodeLogsPB(req)
		if err != nil {
			b.Fatal(err)
		}
		for _, o := range Logs(rls, now) {
			if _, err := parse.SpoolLine(o); err != nil {
				b.Fatal(err)
			}
		}
	}
}
