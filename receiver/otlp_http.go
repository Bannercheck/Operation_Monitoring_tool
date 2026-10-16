package main

// OpenTelemetry intake (OTLP). An OpenTelemetry Collector or SDK points its exporter here and nothing else changes:
//
//	OTLP/HTTP  POST /v1/logs, /v1/traces, /v1/metrics   on :8600 and on WO_OTLP_HTTP (default :4318)
//	           application/x-protobuf or application/json, Content-Encoding gzip / deflate
//	OTLP/gRPC  opentelemetry.proto.collector.{logs,trace,metrics}.v1.*/Export   on WO_OTLP_GRPC (default :4317, cleartext h2)
//	           grpc-encoding gzip
//
// Authentication is the agents' token (Authorization: Bearer wo_… or X-API-Key; gRPC metadata "authorization"), so a
// collector is enrolled like an agent and its records carry that agent's environment and site. Each request is decoded
// and converted in Go (receiver/otlp) and lands in the spool as ready records, answered as soon as they are on disk:
//
//	/otlp/logs     v=2 "parsed": canonical observations -> LiveStore.ingest_parsed
//	/otlp/metrics  v=2 "parsed": cpu / memory / disk / gpu samples -> the metric store
//	/otlp/traces   v=2 "spans": {"agg": RED rollups} + span rows -> TraceStore
//
// The parse stage forwards these paths unchanged (it only parses /ingest). WO_OTLP_HTTP / WO_OTLP_GRPC = "off" closes
// the extra listeners (the /v1/* routes on :8600 stay).

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"

	"watchover/receiver/otlp"
	"watchover/receiver/parse"
)

const (
	otlpMaxDecoded = 64 << 20 // a decompressed request
	otlpChunk      = 5000     // items per spool record
)

type otlpStats struct{ requests, logs, points, spans, skipped, bad atomic.Int64 }

type otlpIn struct {
	s       *server
	metrics *otlp.Metrics
	st      otlpStats
}

func newOTLP(s *server) *otlpIn { return &otlpIn{s: s, metrics: otlp.NewMetrics()} }

func (o *otlpIn) health(h map[string]any) {
	h["otlp_requests"], h["otlp_log_records"], h["otlp_metric_points"] = o.st.requests.Load(), o.st.logs.Load(), o.st.points.Load()
	h["otlp_spans"], h["otlp_points_skipped"], h["otlp_bad_requests"] = o.st.spans.Load(), o.st.skipped.Load(), o.st.bad.Load()
}

// otlpErr is a refusal with its HTTP status (the gRPC code is derived from it).
type otlpErr struct {
	code int
	msg  string
}

func (e *otlpErr) Error() string { return e.msg }

func refuse(code int, format string, a ...any) *otlpErr {
	return &otlpErr{code, fmt.Sprintf(format, a...)}
}

// admit: token and backlog, exactly as /ingest. Returns the base spool header.
func (o *otlpIn) admit(r *http.Request) (map[string]any, *otlpErr) {
	ag, identity, code := o.s.auth.check(token(r))
	if code != 200 {
		if code == http.StatusServiceUnavailable {
			return nil, refuse(code, "receiver starting: retry")
		}
		o.s.stats.unauth.Add(1)
		return nil, refuse(code, "invalid or revoked token")
	}
	if o.s.sp.backlog.Load() > o.s.cfg.maxBacklog {
		o.s.stats.busy.Add(1)
		return nil, refuse(http.StatusServiceUnavailable, "backlog full: retry")
	}
	h := map[string]any{"ip": clientIP(r), "ts": time.Now().UTC().Format(time.RFC3339Nano), "identity": identity}
	if ag != nil {
		h["agent"], h["agent_id"], h["env"], h["site"] = ag.Name, ag.ID, ag.Env, ag.Site
	} else {
		name := r.Header.Get("X-Agent")
		if name == "" {
			name = "otel-" + clientIP(r)
		}
		if len(name) > 120 {
			name = name[:120]
		}
		h["agent"] = name
	}
	return h, nil
}

func inflate(body []byte, enc string) ([]byte, *otlpErr) {
	var rd io.Reader
	switch strings.ToLower(strings.TrimSpace(enc)) {
	case "", "identity":
		return body, nil
	case "gzip", "x-gzip":
		zr, err := gzip.NewReader(bytes.NewReader(body))
		if err != nil {
			return nil, refuse(http.StatusBadRequest, "bad gzip body")
		}
		defer zr.Close()
		rd = zr
	case "deflate":
		rd = flate.NewReader(bytes.NewReader(body))
	default:
		return nil, refuse(http.StatusUnsupportedMediaType, "content encoding %q not supported (gzip, deflate)", enc)
	}
	out, err := io.ReadAll(io.LimitReader(rd, otlpMaxDecoded+1))
	if err != nil {
		return nil, refuse(http.StatusBadRequest, "bad compressed body")
	}
	if len(out) > otlpMaxDecoded {
		return nil, refuse(http.StatusRequestEntityTooLarge, "decompressed body larger than %d MB", otlpMaxDecoded>>20)
	}
	return out, nil
}

// spoolOne appends one ready record and waits until it is on disk.
func (o *otlpIn) spoolOne(base map[string]any, path, kind string, n int, body []byte) *otlpErr {
	h := make(map[string]any, len(base)+10)
	for k, v := range base {
		h[k] = v
	}
	h["v"], h["kind"], h["path"], h["n"], h["len"] = 2, kind, path, n, len(body)
	h["file"] = strings.ReplaceAll(strings.TrimPrefix(path, "/"), "/", "-")
	if kind == "parsed" {
		h["format"], h["family"], h["confidence"], h["hint_format"], h["parser_version"] = "otlp", "otlp", 1.0, "otlp", "otlp-1"
	}
	hb, _ := json.Marshal(h)
	rec := record{header: hb, body: body, done: make(chan error, 1)}
	select {
	case o.s.sp.in <- rec:
	case <-time.After(2 * time.Second):
		o.s.stats.busy.Add(1)
		return refuse(http.StatusServiceUnavailable, "receiver busy: retry")
	}
	if err := <-rec.done; err != nil {
		o.s.stats.rejected.Add(1)
		return refuse(http.StatusServiceUnavailable, "spool write failed: %v", err)
	}
	o.s.stats.batches.Add(1)
	o.s.stats.bytes.Add(int64(len(body)))
	return nil
}

func (o *otlpIn) spoolObs(base map[string]any, path string, obs []parse.Observation) *otlpErr {
	for i := 0; i < len(obs); i += otlpChunk {
		j := min(i+otlpChunk, len(obs))
		var b bytes.Buffer
		for k := i; k < j; k++ {
			obs[k].LineNo = k - i + 1
			line, err := parse.SpoolLine(obs[k])
			if err != nil {
				continue
			}
			b.Write(line)
			b.WriteByte('\n')
		}
		if b.Len() == 0 {
			continue
		}
		if err := o.spoolOne(base, path, "parsed", j-i, b.Bytes()); err != nil {
			return err
		}
	}
	return nil
}

// process decodes one export request of the given signal and spools the result. It returns the items accepted.
func (o *otlpIn) process(signal string, isJSON bool, body []byte, base map[string]any) (int, *otlpErr) {
	now := time.Now()
	bad := func(err error) (int, *otlpErr) {
		o.st.bad.Add(1)
		return 0, refuse(http.StatusBadRequest, "cannot decode OTLP %s: %v", signal, err)
	}
	switch signal {
	case "logs":
		var rls []otlp.ResourceLogs
		var err error
		if isJSON {
			rls, err = otlp.DecodeLogsJSON(body)
		} else {
			rls, err = otlp.DecodeLogsPB(body)
		}
		if err != nil {
			return bad(err)
		}
		obs := otlp.Logs(rls, now)
		if e := o.spoolObs(base, "/otlp/logs", obs); e != nil {
			return 0, e
		}
		o.st.logs.Add(int64(len(obs)))
		return len(obs), nil
	case "metrics":
		var rms []otlp.ResourceMetrics
		var err error
		if isJSON {
			rms, err = otlp.DecodeMetricsJSON(body)
		} else {
			rms, err = otlp.DecodeMetricsPB(body)
		}
		if err != nil {
			return bad(err)
		}
		obs, skipped := o.metrics.Convert(rms, now)
		o.st.skipped.Add(int64(skipped))
		if e := o.spoolObs(base, "/otlp/metrics", obs); e != nil {
			return 0, e
		}
		o.st.points.Add(int64(len(obs)))
		return len(obs), nil
	case "traces":
		var rss []otlp.ResourceSpans
		var err error
		if isJSON {
			rss, err = otlp.DecodeTracesJSON(body)
		} else {
			rss, err = otlp.DecodeTracesPB(body)
		}
		if err != nil {
			return bad(err)
		}
		rows, aggs := otlp.Traces(rss, now)
		if len(rows) == 0 {
			return 0, nil
		}
		for i := 0; i < len(rows); i += otlpChunk {
			j := min(i+otlpChunk, len(rows))
			var b bytes.Buffer
			enc := json.NewEncoder(&b)
			enc.SetEscapeHTML(false)
			if i == 0 { // the rollups of the whole request travel with the first chunk
				_ = enc.Encode(map[string]any{"agg": aggs})
			} else {
				_ = enc.Encode(map[string]any{"agg": []otlp.Agg{}})
			}
			for k := i; k < j; k++ {
				_ = enc.Encode(rows[k])
			}
			if e := o.spoolOne(base, "/otlp/traces", "spans", j-i, b.Bytes()); e != nil {
				return 0, e
			}
		}
		o.st.spans.Add(int64(len(rows)))
		return len(rows), nil
	}
	return 0, refuse(http.StatusNotFound, "unknown signal")
}

// ---------------------------------------------------------------- OTLP/HTTP

func (o *otlpIn) serveHTTP(w http.ResponseWriter, r *http.Request) {
	signal := strings.TrimPrefix(r.URL.Path, "/v1/")
	if signal != "logs" && signal != "traces" && signal != "metrics" {
		o.s.json(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	ct, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	isJSON := ct == "application/json"
	fail := func(e *otlpErr) {
		if e.code == http.StatusServiceUnavailable || e.code == http.StatusTooManyRequests {
			w.Header().Set("Retry-After", "5")
		}
		if isJSON {
			o.s.json(w, e.code, map[string]any{"code": grpcCode(e.code), "message": e.msg})
			return
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(e.code)
		_, _ = w.Write(statusPB(grpcCode(e.code), e.msg))
	}
	if r.Method != http.MethodPost {
		fail(refuse(http.StatusMethodNotAllowed, "POST only"))
		return
	}
	if !isJSON && ct != "application/x-protobuf" && ct != "application/protobuf" {
		fail(refuse(http.StatusUnsupportedMediaType, "content type %q: use application/x-protobuf or application/json", ct))
		return
	}
	o.st.requests.Add(1)
	base, e := o.admit(r)
	if e != nil {
		fail(e)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, o.s.cfg.maxBody+1))
	if err != nil {
		fail(refuse(http.StatusBadRequest, "read failed"))
		return
	}
	if int64(len(body)) > o.s.cfg.maxBody {
		fail(refuse(http.StatusRequestEntityTooLarge, "body larger than %d MB: send smaller batches", o.s.cfg.maxBody>>20))
		return
	}
	if body, e = inflate(body, r.Header.Get("Content-Encoding")); e != nil {
		fail(e)
		return
	}
	if _, e := o.process(signal, isJSON, body, base); e != nil {
		fail(e)
		return
	}
	if isJSON { // Export*ServiceResponse without partial_success
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("{}"))
		return
	}
	w.Header().Set("Content-Type", "application/x-protobuf")
	w.WriteHeader(http.StatusOK)
}

// statusPB is a google.rpc.Status{code=1, message=2} for an OTLP/HTTP protobuf error body.
func statusPB(code int, msg string) []byte {
	b := []byte{0x08}
	b = binary.AppendUvarint(b, uint64(code))
	b = append(b, 0x12)
	b = binary.AppendUvarint(b, uint64(len(msg)))
	return append(b, msg...)
}

// grpcCode maps an HTTP status to the gRPC status code (OTLP: UNAVAILABLE and RESOURCE_EXHAUSTED are retried).
func grpcCode(httpCode int) int {
	switch httpCode {
	case http.StatusBadRequest, http.StatusRequestEntityTooLarge:
		return 3 // INVALID_ARGUMENT
	case http.StatusUnauthorized:
		return 16 // UNAUTHENTICATED
	case http.StatusForbidden:
		return 7 // PERMISSION_DENIED
	case http.StatusNotFound, http.StatusMethodNotAllowed:
		return 12 // UNIMPLEMENTED
	case http.StatusUnsupportedMediaType:
		return 3
	case http.StatusTooManyRequests:
		return 8 // RESOURCE_EXHAUSTED
	case http.StatusServiceUnavailable:
		return 14 // UNAVAILABLE
	}
	return 13 // INTERNAL
}

// ---------------------------------------------------------------- OTLP/gRPC (unary Export over cleartext HTTP/2)

var grpcSignal = map[string]string{
	"/opentelemetry.proto.collector.logs.v1.LogsService/Export":       "logs",
	"/opentelemetry.proto.collector.trace.v1.TraceService/Export":     "traces",
	"/opentelemetry.proto.collector.metrics.v1.MetricsService/Export": "metrics",
}

func (o *otlpIn) serveGRPC(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Trailer", "Grpc-Status, Grpc-Message")
	w.Header().Set("Content-Type", "application/grpc")
	w.Header().Set("Grpc-Accept-Encoding", "gzip")
	end := func(code int, msg string) {
		w.Header().Set("Grpc-Status", fmt.Sprint(code))
		if msg != "" {
			w.Header().Set("Grpc-Message", url.PathEscape(msg))
		}
	}
	signal, ok := grpcSignal[r.URL.Path]
	if !ok || r.Method != http.MethodPost || !strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
		w.WriteHeader(http.StatusOK)
		end(12, "unknown service or method")
		return
	}
	o.st.requests.Add(1)
	fail := func(e *otlpErr) {
		w.WriteHeader(http.StatusOK)
		end(grpcCode(e.code), e.msg)
	}
	base, e := o.admit(r)
	if e != nil {
		fail(e)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, o.s.cfg.maxBody+5))
	if err != nil || len(raw) < 5 {
		fail(refuse(http.StatusBadRequest, "short gRPC frame"))
		return
	}
	n := binary.BigEndian.Uint32(raw[1:5])
	if int64(n) > o.s.cfg.maxBody || int(n) != len(raw)-5 {
		fail(refuse(http.StatusBadRequest, "gRPC frame length %d does not match the body", n))
		return
	}
	msg := raw[5:]
	if raw[0] == 1 {
		if msg, e = inflate(msg, r.Header.Get("Grpc-Encoding")); e != nil {
			fail(e)
			return
		}
	}
	if _, e := o.process(signal, false, msg, base); e != nil {
		fail(e)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte{0, 0, 0, 0, 0}) // an empty Export*ServiceResponse
	end(0, "")
}

// listen starts the dedicated OTLP listeners (cleartext HTTP/2 enabled on both).
func (o *otlpIn) listen(httpAddr, grpcAddr string) []*http.Server {
	var out []*http.Server
	start := func(addr, what string, h http.Handler, http1 bool) {
		if addr == "" || addr == "off" || addr == "0" {
			return
		}
		var p http.Protocols
		p.SetUnencryptedHTTP2(true)
		p.SetHTTP1(http1)
		srv := &http.Server{Addr: addr, Handler: h, Protocols: &p, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 60 * time.Second,
			WriteTimeout: 30 * time.Second, IdleTimeout: 300 * time.Second, MaxHeaderBytes: 64 << 10}
		out = append(out, srv)
		go func() {
			log.Printf("OTLP/%s on %s", what, addr)
			if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("OTLP/%s listener %s: %v (disabled)", what, addr, err)
			}
		}()
	}
	hm := http.NewServeMux()
	hm.HandleFunc("/v1/", o.serveHTTP)
	start(httpAddr, "HTTP", hm, true)
	start(grpcAddr, "gRPC", http.HandlerFunc(o.serveGRPC), false)
	return out
}
