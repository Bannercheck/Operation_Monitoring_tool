package parse

import (
	"strconv"

	"watchover/receiver/parse/py"
)

// CanonLine is json.dumps(canon(o), sort_keys=True, ensure_ascii=False, default=str) of scripts/parse_golden.py
// (P01 section 2). A NaN/Infinity attribute returns ErrForward (the receiver forwards such batches raw).
func CanonLine(o Observation) ([]byte, error) {
	b, err := appendCanon(make([]byte, 0, 256+len(o.Raw)+len(o.Message)), o, false)
	if err != nil {
		return nil, forward("NaN/Infinity attribute")
	}
	return b, nil
}

// canonLineNaN is CanonLine with NaN/Infinity written as Python does (golden comparison only).
func canonLineNaN(o Observation) []byte {
	b, _ := appendCanon(nil, o, true)
	return b
}

func appendKey(b []byte, k string) []byte {
	b = py.AppendString(b, k)
	return append(b, ": "...)
}

// appendCanon writes the 14 keys in sorted order: attributes, environment, host, kind, line_no, message, origin,
// parser, parser_confidence, raw, service, severity, source, ts.
func appendCanon(b []byte, o Observation, allowNaN bool) ([]byte, error) {
	var err error
	b = append(b, '{')
	b = appendKey(b, "attributes")
	if b, err = py.AppendDict(b, o.Attributes, true, allowNaN); err != nil {
		return nil, err
	}
	str := func(k, v string) {
		b = append(b, ", "...)
		b = appendKey(b, k)
		b = py.AppendString(b, v)
	}
	str("environment", o.Environment)
	str("host", o.Host)
	str("kind", o.Kind)
	b = append(b, ", "...)
	b = appendKey(b, "line_no")
	b = strconv.AppendInt(b, int64(o.LineNo), 10)
	str("message", o.Message)
	str("origin", o.Origin)
	str("parser", o.Parser)
	b = append(b, ", "...)
	b = appendKey(b, "parser_confidence")
	b = append(b, py.FloatRepr(py.Round(o.ParserConfidence, 4))...)
	str("raw", o.Raw)
	str("service", o.Service)
	str("severity", o.Severity)
	str("source", o.Source)
	str("ts", o.TS.ISO())
	return append(b, '}'), nil
}

// appendReport writes one report entry with sorted keys (an empty Report, Format == "", is {}).
func appendReport(b []byte, r Report) []byte {
	if r.Format == "" {
		return append(b, "{}"...)
	}
	b = append(b, '{')
	b = appendKey(b, "confidence")
	b = append(b, py.FloatRepr(r.Confidence)...)
	if r.DedupDropped > 0 {
		b = append(b, ", "...)
		b = appendKey(b, "dedup_dropped")
		b = strconv.AppendInt(b, int64(r.DedupDropped), 10)
	}
	b = append(b, ", "...)
	b = appendKey(b, "family")
	b = py.AppendString(b, r.Family)
	b = append(b, ", "...)
	b = appendKey(b, "file")
	b = py.AppendString(b, r.File)
	b = append(b, ", "...)
	b = appendKey(b, "format")
	b = py.AppendString(b, r.Format)
	b = append(b, ", "...)
	b = appendKey(b, "keys")
	b = append(b, '[')
	for i, k := range r.Keys {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = py.AppendString(b, k)
	}
	b = append(b, "], "...)
	b = appendKey(b, "kind")
	b = py.AppendString(b, r.Kind)
	b = append(b, ", "...)
	b = appendKey(b, "profile")
	b = py.AppendString(b, r.Profile)
	b = append(b, ", "...)
	b = appendKey(b, "roles")
	b = append(b, '{')
	for i, name := range sortedRoleNames {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = appendKey(b, name)
		if k, ok := r.Roles.Get(roleIndex[name]); ok {
			b = py.AppendString(b, k)
		} else {
			b = append(b, "null"...)
		}
	}
	b = append(b, "}, "...)
	b = appendKey(b, "rows")
	b = strconv.AppendInt(b, int64(r.Rows), 10)
	return append(b, '}')
}

var sortedRoleNames = []string{"environment", "host", "message", "origin", "service", "severity", "timestamp"}

var roleIndex = func() map[string]int {
	m := map[string]int{}
	for i, n := range RoleNames {
		m[n] = i
	}
	return m
}()

// CanonReport is json.dumps({"report": reps}, sort_keys=True, ensure_ascii=False, default=str) without sha/table.
func CanonReport(reps []Report) []byte {
	b := append([]byte(nil), `{"report": [`...)
	for i, r := range reps {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = appendReport(b, r)
	}
	return append(b, "]}"...)
}
