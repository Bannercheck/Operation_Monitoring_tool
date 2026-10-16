package main

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type wpb []byte

func (b wpb) raw(f int, v []byte) wpb {
	b = binary.AppendUvarint(b, uint64(f<<3|2))
	b = binary.AppendUvarint(b, uint64(len(v)))
	return append(b, v...)
}
func (b wpb) str(f int, s string) wpb { return b.raw(f, []byte(s)) }
func (b wpb) vint(f int, v uint64) wpb {
	return binary.AppendUvarint(binary.AppendUvarint(b, uint64(f<<3)), v)
}
func (b wpb) f64(f int, v uint64) wpb {
	return binary.LittleEndian.AppendUint64(binary.AppendUvarint(b, uint64(f<<3|1)), v)
}
func wkv(k, v string) wpb { return wpb{}.str(1, k).raw(2, wpb{}.str(1, v)) }

var tid = []byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}

func otlpResource() wpb {
	return wpb{}.raw(1, wkv("service.name", "billing")).raw(1, wkv("host.name", "prd-app-07"))
}

func otlpLogs() []byte {
	ts := uint64(time.Now().Add(-time.Second).UnixNano())
	rec := wpb{}.f64(1, ts).vint(2, 17).raw(5, wpb{}.str(1, "invoice 9 failed")).raw(9, tid)
	return wpb{}.raw(1, wpb{}.raw(1, otlpResource()).raw(2, wpb{}.raw(2, rec)))
}

func otlpTraces() []byte {
	ts := uint64(time.Now().Add(-time.Second).UnixNano())
	sp := wpb{}.raw(1, tid).raw(2, []byte{1, 1, 1, 1, 1, 1, 1, 1}).str(5, "POST /pay").vint(6, 2).f64(7, ts).f64(8, ts+uint64(40*time.Millisecond))
	return wpb{}.raw(1, wpb{}.raw(1, otlpResource()).raw(2, wpb{}.raw(2, sp)))
}

func otlpMetrics() []byte {
	ts := uint64(time.Now().Add(-time.Second).UnixNano())
	pt := wpb{}.f64(3, ts).f64(4, math.Float64bits(0.42)).raw(7, wkv("state", "used"))
	m := wpb{}.str(1, "system.memory.utilization").raw(5, wpb{}.raw(1, pt))
	return wpb{}.raw(1, wpb{}.raw(1, otlpResource()).raw(2, wpb{}.raw(2, m)))
}

func gz(b []byte) []byte {
	var o bytes.Buffer
	w := gzip.NewWriter(&o)
	_, _ = w.Write(b)
	_ = w.Close()
	return o.Bytes()
}

func otlpPost(t *testing.T, u, tok, ct, enc string, body []byte) (*http.Response, []byte) {
	t.Helper()
	req, _ := http.NewRequest("POST", u, bytes.NewReader(body))
	req.Header.Set("Content-Type", ct)
	if enc != "" {
		req.Header.Set("Content-Encoding", enc)
	}
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(r.Body)
	r.Body.Close()
	return r, b
}

func byPath(recs []map[string]any) map[string][]map[string]any {
	out := map[string][]map[string]any{}
	for _, r := range recs {
		p, _ := r["path"].(string)
		out[p] = append(out[p], r)
	}
	return out
}

func TestOTLPHTTP(t *testing.T) {
	st := &authState{Agents: map[string]agentRec{sha("wo_otel"): {ID: 3, Name: "otel-gw", Env: "prod", Site: "ist"}}}
	s, ts, stop := setup(t, st, nil)
	defer stop()
	pbct := "application/x-protobuf"
	if r, b := otlpPost(t, ts.URL+"/v1/logs", "wo_otel", pbct, "gzip", gz(otlpLogs())); r.StatusCode != 200 || r.Header.Get("Content-Type") != pbct || len(b) != 0 {
		t.Fatalf("logs: %d %q", r.StatusCode, b)
	}
	if r, b := otlpPost(t, ts.URL+"/v1/traces", "wo_otel", pbct, "", otlpTraces()); r.StatusCode != 200 {
		t.Fatalf("traces: %d %q", r.StatusCode, b)
	}
	if r, b := otlpPost(t, ts.URL+"/v1/metrics", "wo_otel", pbct, "", otlpMetrics()); r.StatusCode != 200 {
		t.Fatalf("metrics: %d %q", r.StatusCode, b)
	}
	js := `{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"web"}}]},"scopeLogs":[{"logRecords":[{"severityText":"WARN","body":{"stringValue":"slow"}}]}]}]}`
	if r, b := otlpPost(t, ts.URL+"/v1/logs", "wo_otel", "application/json", "", []byte(js)); r.StatusCode != 200 || string(b) != "{}" {
		t.Fatalf("json logs: %d %q", r.StatusCode, b)
	}
	if r, _ := otlpPost(t, ts.URL+"/v1/logs", "wo_bad", pbct, "", otlpLogs()); r.StatusCode != 401 {
		t.Fatalf("bad token: %d", r.StatusCode)
	}
	if r, _ := otlpPost(t, ts.URL+"/v1/logs", "wo_otel", "text/plain", "", otlpLogs()); r.StatusCode != 415 {
		t.Fatalf("text/plain: %d", r.StatusCode)
	}
	if r, _ := otlpPost(t, ts.URL+"/v1/logs", "wo_otel", pbct, "", []byte{0x0a, 0xff, 0xff, 0x0f}); r.StatusCode != 400 {
		t.Fatalf("garbage: %d", r.StatusCode)
	}
	if r, _ := otlpPost(t, ts.URL+"/v1/logs", "wo_otel", pbct, "br", otlpLogs()); r.StatusCode != 415 {
		t.Fatalf("brotli: %d", r.StatusCode)
	}
	time.Sleep(200 * time.Millisecond)
	recs := byPath(records(t, s.cfg.spool))
	if len(recs["/otlp/logs"]) != 2 || len(recs["/otlp/traces"]) != 1 || len(recs["/otlp/metrics"]) != 1 {
		t.Fatalf("spooled %v", recs)
	}
	lg := recs["/otlp/logs"][0]
	if lg["v"].(float64) != 2 || lg["kind"] != "parsed" || lg["agent"] != "otel-gw" || lg["agent_id"].(float64) != 3 || lg["env"] != "prod" || lg["format"] != "otlp" {
		t.Fatalf("log header %v", lg)
	}
	var o map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(lg["body"].(string))), &o); err != nil || o["severity"] != "ERROR" || o["host"] != "prd-app-07" || o["message"] != "invoice 9 failed" {
		t.Fatalf("log body %v %v", o, err)
	}
	tr := strings.Split(strings.TrimSpace(recs["/otlp/traces"][0]["body"].(string)), "\n")
	if len(tr) != 2 || !strings.HasPrefix(tr[0], `{"agg":[["`) || !strings.Contains(tr[1], `"operation":"POST /pay"`) || !strings.Contains(tr[1], `"_counted":true`) {
		t.Fatalf("trace body %q", tr)
	}
	if !strings.Contains(recs["/otlp/metrics"][0]["body"].(string), `"metric": "memory", "value": 42.0`) {
		t.Fatalf("metric body %q", recs["/otlp/metrics"][0]["body"])
	}
	h := s.health()
	if h["otlp_log_records"].(int64) != 2 || h["otlp_spans"].(int64) != 1 || h["otlp_metric_points"].(int64) != 1 || h["otlp_bad_requests"].(int64) != 1 {
		t.Fatalf("health %v", h)
	}
}

func grpcCall(t *testing.T, c *http.Client, u, tok string, msg []byte, compress bool) (string, string) {
	t.Helper()
	flag := byte(0)
	if compress {
		msg, flag = gz(msg), 1
	}
	frame := append([]byte{flag, 0, 0, 0, 0}, msg...)
	binary.BigEndian.PutUint32(frame[1:5], uint32(len(msg)))
	req, _ := http.NewRequest("POST", u, bytes.NewReader(frame))
	req.Header.Set("Content-Type", "application/grpc")
	req.Header.Set("TE", "trailers")
	if compress {
		req.Header.Set("Grpc-Encoding", "gzip")
	}
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	r, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.ReadAll(r.Body)
	r.Body.Close()
	if r.ProtoMajor != 2 {
		t.Fatalf("gRPC needs HTTP/2, got %s", r.Proto)
	}
	return r.Trailer.Get("Grpc-Status"), r.Trailer.Get("Grpc-Message")
}

func TestOTLPGRPC(t *testing.T) {
	st := &authState{Agents: map[string]agentRec{sha("wo_otel"): {ID: 3, Name: "otel-gw"}}}
	s, _, stop := setup(t, st, nil)
	defer stop()
	gs := httptest.NewUnstartedServer(http.HandlerFunc(s.otlp.serveGRPC))
	var p http.Protocols
	p.SetUnencryptedHTTP2(true)
	gs.Config.Protocols = &p
	gs.Start()
	defer gs.Close()
	c := &http.Client{Transport: &http.Transport{Protocols: &p}}
	base := gs.URL + "/opentelemetry.proto.collector."
	if code, msg := grpcCall(t, c, base+"logs.v1.LogsService/Export", "wo_otel", otlpLogs(), true); code != "0" {
		t.Fatalf("logs: %s %s", code, msg)
	}
	if code, _ := grpcCall(t, c, base+"trace.v1.TraceService/Export", "wo_otel", otlpTraces(), false); code != "0" {
		t.Fatalf("traces: %s", code)
	}
	if code, _ := grpcCall(t, c, base+"metrics.v1.MetricsService/Export", "wo_otel", otlpMetrics(), false); code != "0" {
		t.Fatalf("metrics: %s", code)
	}
	if code, _ := grpcCall(t, c, base+"logs.v1.LogsService/Export", "wo_nope", otlpLogs(), false); code != "16" {
		t.Fatalf("bad token: grpc-status %s, want 16 UNAUTHENTICATED", code)
	}
	if code, _ := grpcCall(t, c, base+"logs.v1.LogsService/Other", "wo_otel", otlpLogs(), false); code != "12" {
		t.Fatalf("unknown method: grpc-status %s, want 12 UNIMPLEMENTED", code)
	}
	if code, _ := grpcCall(t, c, base+"logs.v1.LogsService/Export", "wo_otel", []byte{0x0a, 0xff, 0xff, 0x0f}, false); code != "3" {
		t.Fatalf("garbage: grpc-status %s, want 3 INVALID_ARGUMENT", code)
	}
	time.Sleep(200 * time.Millisecond)
	recs := byPath(records(t, s.cfg.spool))
	if len(recs["/otlp/logs"]) != 1 || len(recs["/otlp/traces"]) != 1 || len(recs["/otlp/metrics"]) != 1 {
		t.Fatalf("spooled %v", recs)
	}
}
