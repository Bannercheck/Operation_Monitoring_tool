package otlp

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
)

// OTLP/JSON (the protobuf JSON mapping with OTLP's exceptions): lowerCamelCase keys, 64-bit integers as strings or
// numbers, enums as integers or names, trace / span ids as hex (not base64). The deprecated instrumentationLibrary*
// keys of old senders are read too.

// jint is an int64 / uint64 field: "123" or 123.
type jint struct {
	U   uint64
	I   int64
	set bool
}

func (n *jint) UnmarshalJSON(b []byte) error {
	b = bytes.Trim(b, `"`)
	if len(b) == 0 || string(b) == "null" {
		return nil
	}
	if u, err := strconv.ParseUint(string(b), 10, 64); err == nil {
		n.U, n.I, n.set = u, int64(u), true
		return nil
	}
	i, err := strconv.ParseInt(string(b), 10, 64)
	if err != nil {
		f, ferr := strconv.ParseFloat(string(b), 64) // 1.7e18 from a sender that wrote a float
		if ferr != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return errors.New("otlp: bad integer " + string(b))
		}
		i = int64(f)
	}
	n.U, n.I, n.set = uint64(i), i, true
	return nil
}

// jfloat is a double: 1.5, "1.5", "NaN", "Infinity".
type jfloat struct {
	F   float64
	set bool
}

func (n *jfloat) UnmarshalJSON(b []byte) error {
	b = bytes.Trim(b, `"`)
	if len(b) == 0 || string(b) == "null" {
		return nil
	}
	f, err := strconv.ParseFloat(string(b), 64)
	if err != nil {
		return errors.New("otlp: bad number " + string(b))
	}
	n.F, n.set = f, true
	return nil
}

// jenum is an enum: 2 or "SPAN_KIND_SERVER".
type jenum struct{ V int32 }

var enumNames = map[string]int32{
	"SPAN_KIND_UNSPECIFIED": 0, "SPAN_KIND_INTERNAL": 1, "SPAN_KIND_SERVER": 2, "SPAN_KIND_CLIENT": 3, "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5,
	"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2,
	"AGGREGATION_TEMPORALITY_UNSPECIFIED": 0, "AGGREGATION_TEMPORALITY_DELTA": 1, "AGGREGATION_TEMPORALITY_CUMULATIVE": 2,
}

func (e *jenum) UnmarshalJSON(b []byte) error {
	if len(b) > 0 && b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		if v, ok := enumNames[s]; ok {
			e.V = v
			return nil
		}
		if strings.HasPrefix(s, "SEVERITY_NUMBER_") {
			e.V = sevNumByName(strings.TrimPrefix(s, "SEVERITY_NUMBER_"))
			return nil
		}
		v, err := strconv.ParseInt(s, 10, 32)
		e.V = int32(v)
		return err
	}
	if string(b) == "null" {
		return nil
	}
	v, err := strconv.ParseInt(string(b), 10, 32)
	e.V = int32(v)
	return err
}

func sevNumByName(s string) int32 {
	base := map[string]int32{"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
	for name, n := range base {
		if strings.HasPrefix(s, name) {
			if d, err := strconv.Atoi(strings.TrimPrefix(s, name)); err == nil && d >= 2 && d <= 4 {
				return n + int32(d) - 1
			}
			return n
		}
	}
	return 0
}

// jid is a trace / span id: hex per OTLP/JSON; base64 (the plain protobuf JSON mapping) is accepted as a fallback.
type jid []byte

func (id *jid) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil || s == "" {
		return nil
	}
	if h, err := hex.DecodeString(s); err == nil && (len(h) == 16 || len(h) == 8) {
		*id = h
		return nil
	}
	if d, err := base64.StdEncoding.DecodeString(s); err == nil && (len(d) == 16 || len(d) == 8) {
		*id = d
		return nil
	}
	return nil // an id we cannot read is dropped, not the batch
}

type jAny struct {
	StringValue *string  `json:"stringValue"`
	BoolValue   *bool    `json:"boolValue"`
	IntValue    *jint    `json:"intValue"`
	DoubleValue *jfloat  `json:"doubleValue"`
	ArrayValue  *jValues `json:"arrayValue"`
	KvlistValue *jKVList `json:"kvlistValue"`
	BytesValue  *string  `json:"bytesValue"`
}

type jValues struct {
	Values []jAny `json:"values"`
}

type jKVList struct {
	Values []jKV `json:"values"`
}

type jKV struct {
	Key   string `json:"key"`
	Value jAny   `json:"value"`
}

func (a *jAny) conv(depth int) Any {
	if depth > maxDepth {
		return Any{}
	}
	switch {
	case a.StringValue != nil:
		return Any{Kind: VStr, S: *a.StringValue}
	case a.BoolValue != nil:
		return Any{Kind: VBool, B: *a.BoolValue}
	case a.IntValue != nil:
		return Any{Kind: VInt, I: a.IntValue.I}
	case a.DoubleValue != nil:
		return Any{Kind: VFloat, F: a.DoubleValue.F}
	case a.ArrayValue != nil:
		v := Any{Kind: VArr, Arr: make([]Any, 0, len(a.ArrayValue.Values))}
		for i := range a.ArrayValue.Values {
			v.Arr = append(v.Arr, a.ArrayValue.Values[i].conv(depth+1))
		}
		return v
	case a.KvlistValue != nil:
		return Any{Kind: VMap, KV: convKVs(a.KvlistValue.Values, depth+1)}
	case a.BytesValue != nil:
		d, _ := base64.StdEncoding.DecodeString(*a.BytesValue)
		return Any{Kind: VBytes, Bytes: d}
	}
	return Any{}
}

func convKVs(in []jKV, depth int) []KV {
	if len(in) == 0 {
		return nil
	}
	out := make([]KV, 0, len(in))
	for i := range in {
		out = append(out, KV{Key: in[i].Key, Val: in[i].Value.conv(depth)})
	}
	return out
}

type jResource struct {
	Attributes []jKV `json:"attributes"`
}

type jScope struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

// ---------------------------------------------------------------- logs

type jLogsReq struct {
	ResourceLogs []struct {
		Resource  jResource    `json:"resource"`
		ScopeLogs []jScopeLogs `json:"scopeLogs"`
		Old       []jScopeLogs `json:"instrumentationLibraryLogs"`
	} `json:"resourceLogs"`
}

type jScopeLogs struct {
	Scope      jScope `json:"scope"`
	OldScope   jScope `json:"instrumentationLibrary"`
	LogRecords []struct {
		TimeUnixNano         jint   `json:"timeUnixNano"`
		ObservedTimeUnixNano jint   `json:"observedTimeUnixNano"`
		SeverityNumber       jenum  `json:"severityNumber"`
		SeverityText         string `json:"severityText"`
		Body                 jAny   `json:"body"`
		Attributes           []jKV  `json:"attributes"`
		TraceID              jid    `json:"traceId"`
		SpanID               jid    `json:"spanId"`
		EventName            string `json:"eventName"`
	} `json:"logRecords"`
}

// DecodeLogsJSON reads an ExportLogsServiceRequest in OTLP/JSON.
func DecodeLogsJSON(b []byte) ([]ResourceLogs, error) {
	var req jLogsReq
	if err := json.Unmarshal(b, &req); err != nil {
		return nil, err
	}
	out := make([]ResourceLogs, 0, len(req.ResourceLogs))
	for _, rl := range req.ResourceLogs {
		r := ResourceLogs{Resource: convKVs(rl.Resource.Attributes, 0)}
		for _, sl := range append(rl.ScopeLogs, rl.Old...) {
			sc := Scope{Name: sl.Scope.Name, Version: sl.Scope.Version}
			if sc.Name == "" {
				sc = Scope{Name: sl.OldScope.Name, Version: sl.OldScope.Version}
			}
			s := ScopeLogs{Scope: sc, Records: make([]LogRecord, 0, len(sl.LogRecords))}
			for i := range sl.LogRecords {
				lr := &sl.LogRecords[i]
				s.Records = append(s.Records, LogRecord{TimeNs: lr.TimeUnixNano.U, ObservedNs: lr.ObservedTimeUnixNano.U, SevNum: lr.SeverityNumber.V,
					SevText: lr.SeverityText, Body: lr.Body.conv(0), Attrs: convKVs(lr.Attributes, 0), TraceID: lr.TraceID, SpanID: lr.SpanID,
					EventName: lr.EventName})
			}
			r.Scopes = append(r.Scopes, s)
		}
		out = append(out, r)
	}
	return out, nil
}

// ---------------------------------------------------------------- traces

type jTracesReq struct {
	ResourceSpans []struct {
		Resource   jResource     `json:"resource"`
		ScopeSpans []jScopeSpans `json:"scopeSpans"`
		Old        []jScopeSpans `json:"instrumentationLibrarySpans"`
	} `json:"resourceSpans"`
}

type jScopeSpans struct {
	Scope    jScope `json:"scope"`
	OldScope jScope `json:"instrumentationLibrary"`
	Spans    []struct {
		TraceID           jid    `json:"traceId"`
		SpanID            jid    `json:"spanId"`
		ParentSpanID      jid    `json:"parentSpanId"`
		Name              string `json:"name"`
		Kind              jenum  `json:"kind"`
		StartTimeUnixNano jint   `json:"startTimeUnixNano"`
		EndTimeUnixNano   jint   `json:"endTimeUnixNano"`
		Attributes        []jKV  `json:"attributes"`
		Status            struct {
			Code    jenum  `json:"code"`
			Message string `json:"message"`
		} `json:"status"`
	} `json:"spans"`
}

// DecodeTracesJSON reads an ExportTraceServiceRequest in OTLP/JSON.
func DecodeTracesJSON(b []byte) ([]ResourceSpans, error) {
	var req jTracesReq
	if err := json.Unmarshal(b, &req); err != nil {
		return nil, err
	}
	out := make([]ResourceSpans, 0, len(req.ResourceSpans))
	for _, rs := range req.ResourceSpans {
		r := ResourceSpans{Resource: convKVs(rs.Resource.Attributes, 0)}
		for _, ss := range append(rs.ScopeSpans, rs.Old...) {
			sc := Scope{Name: ss.Scope.Name, Version: ss.Scope.Version}
			if sc.Name == "" {
				sc = Scope{Name: ss.OldScope.Name, Version: ss.OldScope.Version}
			}
			s := ScopeSpans{Scope: sc, Spans: make([]Span, 0, len(ss.Spans))}
			for i := range ss.Spans {
				sp := &ss.Spans[i]
				s.Spans = append(s.Spans, Span{TraceID: sp.TraceID, SpanID: sp.SpanID, ParentID: sp.ParentSpanID, Name: sp.Name, Kind: sp.Kind.V,
					StartNs: sp.StartTimeUnixNano.U, EndNs: sp.EndTimeUnixNano.U, Attrs: convKVs(sp.Attributes, 0), StatusCode: sp.Status.Code.V,
					StatusMsg: sp.Status.Message})
			}
			r.Scopes = append(r.Scopes, s)
		}
		out = append(out, r)
	}
	return out, nil
}

// ---------------------------------------------------------------- metrics

type jPoints struct {
	DataPoints []struct {
		Attributes   []jKV  `json:"attributes"`
		TimeUnixNano jint   `json:"timeUnixNano"`
		AsDouble     jfloat `json:"asDouble"`
		AsInt        jint   `json:"asInt"`
	} `json:"dataPoints"`
	IsMonotonic bool `json:"isMonotonic"`
}

type jMetricsReq struct {
	ResourceMetrics []struct {
		Resource     jResource       `json:"resource"`
		ScopeMetrics []jScopeMetrics `json:"scopeMetrics"`
		Old          []jScopeMetrics `json:"instrumentationLibraryMetrics"`
	} `json:"resourceMetrics"`
}

type jScopeMetrics struct {
	Scope   jScope `json:"scope"`
	Metrics []struct {
		Name  string   `json:"name"`
		Unit  string   `json:"unit"`
		Gauge *jPoints `json:"gauge"`
		Sum   *jPoints `json:"sum"`
	} `json:"metrics"`
}

// DecodeMetricsJSON reads an ExportMetricsServiceRequest in OTLP/JSON.
func DecodeMetricsJSON(b []byte) ([]ResourceMetrics, error) {
	var req jMetricsReq
	if err := json.Unmarshal(b, &req); err != nil {
		return nil, err
	}
	out := make([]ResourceMetrics, 0, len(req.ResourceMetrics))
	for _, rm := range req.ResourceMetrics {
		r := ResourceMetrics{Resource: convKVs(rm.Resource.Attributes, 0)}
		for _, sm := range append(rm.ScopeMetrics, rm.Old...) {
			s := ScopeMetrics{Scope: Scope{Name: sm.Scope.Name, Version: sm.Scope.Version}}
			for _, m := range sm.Metrics {
				mt := Metric{Name: m.Name, Unit: m.Unit}
				pts := m.Gauge
				if pts != nil {
					mt.Shape = MGauge
				} else if m.Sum != nil {
					pts, mt.Shape, mt.Monotonic = m.Sum, MSum, m.Sum.IsMonotonic
				}
				if pts != nil {
					for _, dp := range pts.DataPoints {
						v := dp.AsDouble.F
						if !dp.AsDouble.set && dp.AsInt.set {
							v = float64(dp.AsInt.I)
						}
						mt.Points = append(mt.Points, Point{Attrs: convKVs(dp.Attributes, 0), TimeNs: dp.TimeUnixNano.U, Value: v})
					}
				}
				s.Metrics = append(s.Metrics, mt)
			}
			r.Scopes = append(r.Scopes, s)
		}
		out = append(out, r)
	}
	return out, nil
}
