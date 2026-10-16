package otlp

import (
	"errors"
	"math"
)

// maxDepth bounds nested AnyValue arrays / maps (a hostile body cannot recurse the decoder into the ground).
const maxDepth = 32

// DecodeLogsPB reads an ExportLogsServiceRequest.
func DecodeLogsPB(b []byte) ([]ResourceLogs, error) {
	var out []ResourceLogs
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return nil, err
		}
		if f != 1 {
			if err := r.skip(wt); err != nil {
				return nil, err
			}
			continue
		}
		if err := want(wt, wtLen); err != nil {
			return nil, err
		}
		m, err := r.bytes()
		if err != nil {
			return nil, err
		}
		out = append(out, ResourceLogs{})
		if err := resourceLogs(m, &out[len(out)-1]); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func resourceLogs(b []byte, rl *ResourceLogs) error {
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return err
		}
		switch {
		case f == 1 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return err
			}
			if rl.Resource, err = resource(m); err != nil {
				return err
			}
		case (f == 2 || f == 1000) && wt == wtLen: // scope_logs; 1000 = instrumentation_library_logs (old senders)
			m, err := r.bytes()
			if err != nil {
				return err
			}
			rl.Scopes = append(rl.Scopes, ScopeLogs{})
			if err := scopeLogs(m, &rl.Scopes[len(rl.Scopes)-1]); err != nil {
				return err
			}
		default:
			if err := r.skip(wt); err != nil {
				return err
			}
		}
	}
	return nil
}

func scopeLogs(b []byte, sl *ScopeLogs) error {
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return err
		}
		switch {
		case f == 1 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return err
			}
			if sl.Scope, err = scope(m); err != nil {
				return err
			}
		case f == 2 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return err
			}
			sl.Records = append(sl.Records, LogRecord{})
			if err := logRecord(m, &sl.Records[len(sl.Records)-1]); err != nil {
				return err
			}
		default:
			if err := r.skip(wt); err != nil {
				return err
			}
		}
	}
	return nil
}

func logRecord(b []byte, lr *LogRecord) error {
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return err
		}
		switch {
		case f == 1 && wt == wtI64:
			lr.TimeNs, err = r.fixed64()
		case f == 11 && wt == wtI64:
			lr.ObservedNs, err = r.fixed64()
		case f == 2 && wt == wtVarint:
			var v uint64
			v, err = r.varint()
			lr.SevNum = int32(v)
		case f == 3 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			lr.SevText = string(m)
		case f == 5 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				lr.Body, err = anyValue(m, 0)
			}
		case f == 6 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				var kv KV
				if kv, err = keyValue(m, 0); err == nil {
					lr.Attrs = append(lr.Attrs, kv)
				}
			}
		case f == 9 && wt == wtLen:
			lr.TraceID, err = r.bytes()
		case f == 10 && wt == wtLen:
			lr.SpanID, err = r.bytes()
		case f == 12 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			lr.EventName = string(m)
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// DecodeTracesPB reads an ExportTraceServiceRequest.
func DecodeTracesPB(b []byte) ([]ResourceSpans, error) {
	var out []ResourceSpans
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return nil, err
		}
		if f != 1 || wt != wtLen {
			if err := r.skip(wt); err != nil {
				return nil, err
			}
			continue
		}
		m, err := r.bytes()
		if err != nil {
			return nil, err
		}
		rs, err := resourceSpans(m)
		if err != nil {
			return nil, err
		}
		out = append(out, rs)
	}
	return out, nil
}

func resourceSpans(b []byte) (ResourceSpans, error) {
	var rs ResourceSpans
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return rs, err
		}
		switch {
		case f == 1 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return rs, err
			}
			if rs.Resource, err = resource(m); err != nil {
				return rs, err
			}
		case (f == 2 || f == 1000) && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return rs, err
			}
			ss, err := scopeSpans(m)
			if err != nil {
				return rs, err
			}
			rs.Scopes = append(rs.Scopes, ss)
		default:
			if err := r.skip(wt); err != nil {
				return rs, err
			}
		}
	}
	return rs, nil
}

func scopeSpans(b []byte) (ScopeSpans, error) {
	var ss ScopeSpans
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return ss, err
		}
		switch {
		case f == 1 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return ss, err
			}
			if ss.Scope, err = scope(m); err != nil {
				return ss, err
			}
		case f == 2 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return ss, err
			}
			sp, err := span(m)
			if err != nil {
				return ss, err
			}
			ss.Spans = append(ss.Spans, sp)
		default:
			if err := r.skip(wt); err != nil {
				return ss, err
			}
		}
	}
	return ss, nil
}

func span(b []byte) (Span, error) {
	var sp Span
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return sp, err
		}
		switch {
		case f == 1 && wt == wtLen:
			sp.TraceID, err = r.bytes()
		case f == 2 && wt == wtLen:
			sp.SpanID, err = r.bytes()
		case f == 4 && wt == wtLen:
			sp.ParentID, err = r.bytes()
		case f == 5 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			sp.Name = string(m)
		case f == 6 && wt == wtVarint:
			var v uint64
			v, err = r.varint()
			sp.Kind = int32(v)
		case f == 7 && wt == wtI64:
			sp.StartNs, err = r.fixed64()
		case f == 8 && wt == wtI64:
			sp.EndNs, err = r.fixed64()
		case f == 9 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				var kv KV
				if kv, err = keyValue(m, 0); err == nil {
					sp.Attrs = append(sp.Attrs, kv)
				}
			}
		case f == 15 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				sp.StatusCode, sp.StatusMsg, err = status(m)
			}
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return sp, err
		}
	}
	return sp, nil
}

func status(b []byte) (int32, string, error) {
	var code int32
	var msg string
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return 0, "", err
		}
		switch {
		case f == 2 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			msg = string(m)
		case f == 3 && wt == wtVarint:
			var v uint64
			v, err = r.varint()
			code = int32(v)
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return 0, "", err
		}
	}
	return code, msg, nil
}

// DecodeMetricsPB reads an ExportMetricsServiceRequest.
func DecodeMetricsPB(b []byte) ([]ResourceMetrics, error) {
	var out []ResourceMetrics
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return nil, err
		}
		if f != 1 || wt != wtLen {
			if err := r.skip(wt); err != nil {
				return nil, err
			}
			continue
		}
		m, err := r.bytes()
		if err != nil {
			return nil, err
		}
		rm, err := resourceMetrics(m)
		if err != nil {
			return nil, err
		}
		out = append(out, rm)
	}
	return out, nil
}

func resourceMetrics(b []byte) (ResourceMetrics, error) {
	var rm ResourceMetrics
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return rm, err
		}
		switch {
		case f == 1 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return rm, err
			}
			if rm.Resource, err = resource(m); err != nil {
				return rm, err
			}
		case (f == 2 || f == 1000) && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return rm, err
			}
			sm, err := scopeMetrics(m)
			if err != nil {
				return rm, err
			}
			rm.Scopes = append(rm.Scopes, sm)
		default:
			if err := r.skip(wt); err != nil {
				return rm, err
			}
		}
	}
	return rm, nil
}

func scopeMetrics(b []byte) (ScopeMetrics, error) {
	var sm ScopeMetrics
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return sm, err
		}
		switch {
		case f == 1 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return sm, err
			}
			if sm.Scope, err = scope(m); err != nil {
				return sm, err
			}
		case f == 2 && wt == wtLen:
			m, err := r.bytes()
			if err != nil {
				return sm, err
			}
			mt, err := metric(m)
			if err != nil {
				return sm, err
			}
			sm.Metrics = append(sm.Metrics, mt)
		default:
			if err := r.skip(wt); err != nil {
				return sm, err
			}
		}
	}
	return sm, nil
}

func metric(b []byte) (Metric, error) {
	var mt Metric
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return mt, err
		}
		switch {
		case f == 1 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			mt.Name = string(m)
		case f == 3 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			mt.Unit = string(m)
		case (f == 5 || f == 7) && wt == wtLen: // gauge / sum: data_points = 1 (NumberDataPoint), sum.is_monotonic = 3
			var m []byte
			if m, err = r.bytes(); err == nil {
				mt.Shape = MGauge
				if f == 7 {
					mt.Shape = MSum
				}
				err = numberData(m, &mt)
			}
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return mt, err
		}
	}
	return mt, nil
}

func numberData(b []byte, mt *Metric) error {
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return err
		}
		switch {
		case f == 1 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				var p Point
				if p, err = numberPoint(m); err == nil {
					mt.Points = append(mt.Points, p)
				}
			}
		case f == 3 && wt == wtVarint && mt.Shape == MSum:
			var v uint64
			v, err = r.varint()
			mt.Monotonic = v != 0
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func numberPoint(b []byte) (Point, error) {
	var p Point
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return p, err
		}
		switch {
		case f == 7 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				var kv KV
				if kv, err = keyValue(m, 0); err == nil {
					p.Attrs = append(p.Attrs, kv)
				}
			}
		case f == 3 && wt == wtI64:
			p.TimeNs, err = r.fixed64()
		case f == 4 && wt == wtI64: // as_double
			var v uint64
			v, err = r.fixed64()
			p.Value = math.Float64frombits(v)
		case f == 6 && wt == wtI64: // as_int (sfixed64)
			var v uint64
			v, err = r.fixed64()
			p.Value = float64(int64(v))
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return p, err
		}
	}
	return p, nil
}

// ---------------------------------------------------------------- common/v1, resource/v1

func resource(b []byte) ([]KV, error) {
	var kvs []KV
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return nil, err
		}
		if f == 1 && wt == wtLen {
			m, err := r.bytes()
			if err != nil {
				return nil, err
			}
			kv, err := keyValue(m, 0)
			if err != nil {
				return nil, err
			}
			kvs = append(kvs, kv)
			continue
		}
		if err := r.skip(wt); err != nil {
			return nil, err
		}
	}
	return kvs, nil
}

func scope(b []byte) (Scope, error) {
	var s Scope
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return s, err
		}
		switch {
		case f == 1 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			s.Name = string(m)
		case f == 2 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			s.Version = string(m)
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return s, err
		}
	}
	return s, nil
}

func keyValue(b []byte, depth int) (KV, error) {
	var kv KV
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return kv, err
		}
		switch {
		case f == 1 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			kv.Key = string(m)
		case f == 2 && wt == wtLen:
			var m []byte
			if m, err = r.bytes(); err == nil {
				kv.Val, err = anyValue(m, depth)
			}
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return kv, err
		}
	}
	return kv, nil
}

func anyValue(b []byte, depth int) (Any, error) {
	if depth > maxDepth {
		return Any{}, errors.New("otlp: value nested too deep")
	}
	var v Any
	r := reader{b: b}
	for !r.done() {
		f, wt, err := r.tag()
		if err != nil {
			return v, err
		}
		switch {
		case f == 1 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			v = Any{Kind: VStr, S: string(m)}
		case f == 2 && wt == wtVarint:
			var x uint64
			x, err = r.varint()
			v = Any{Kind: VBool, B: x != 0}
		case f == 3 && wt == wtVarint:
			var x uint64
			x, err = r.varint()
			v = Any{Kind: VInt, I: int64(x)}
		case f == 4 && wt == wtI64:
			var x uint64
			x, err = r.fixed64()
			v = Any{Kind: VFloat, F: math.Float64frombits(x)}
		case (f == 5 || f == 6) && wt == wtLen: // ArrayValue{values=1 AnyValue} / KeyValueList{values=1 KeyValue}
			var m []byte
			if m, err = r.bytes(); err != nil {
				break
			}
			if f == 5 {
				v = Any{Kind: VArr}
			} else {
				v = Any{Kind: VMap}
			}
			in := reader{b: m}
			for !in.done() && err == nil {
				var g, gwt int
				if g, gwt, err = in.tag(); err != nil {
					break
				}
				if g != 1 || gwt != wtLen {
					err = in.skip(gwt)
					continue
				}
				var e []byte
				if e, err = in.bytes(); err != nil {
					break
				}
				if f == 5 {
					var a Any
					if a, err = anyValue(e, depth+1); err == nil {
						v.Arr = append(v.Arr, a)
					}
				} else {
					var kv KV
					if kv, err = keyValue(e, depth+1); err == nil {
						v.KV = append(v.KV, kv)
					}
				}
			}
		case f == 7 && wt == wtLen:
			var m []byte
			m, err = r.bytes()
			v = Any{Kind: VBytes, Bytes: m}
		default:
			err = r.skip(wt)
		}
		if err != nil {
			return v, err
		}
	}
	return v, nil
}
