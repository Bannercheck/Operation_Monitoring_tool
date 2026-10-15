package parse

import (
	"strings"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// KvParser is the "kv" parser: port of src/watchover/parsers/kv_parser.py (P04 section 4). KV_RE is kvRE (detect.go);
// TS_RE / LEVEL_RE / LOGGER_RE are tsMatch / levelRE / loggerRE (text.go).
type KvParser struct{}

func (KvParser) Name() string       { return "kv" }
func (KvParser) Kind() string       { return "log" }
func (KvParser) LineOriented() bool { return true }

// Records yields the (line_no, record) pairs of text.
func (KvParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	for idx, ln := range py.SplitLines(text) {
		rec := kvRecord(ln)
		if rec == nil {
			continue
		}
		if !yield(Record{LineNo: idx + 1, Rec: rec}) {
			return nil
		}
	}
	return nil
}

// kvValue is the value rule of KvParser: a value starting with '"' loses its first and last code point and \" becomes ";
// otherwise trailing ',' and ';' are stripped.
func kvValue(v string) string {
	if strings.HasPrefix(v, `"`) {
		s := v[1:]
		if s == "" {
			return "" // v[1:-1] of a one-character string
		}
		_, n := utf8.DecodeLastRuneInString(s)
		return strings.ReplaceAll(s[:len(s)-n], `\"`, `"`)
	}
	return strings.TrimRight(v, ",;")
}

// kvRecord parses one line; nil when it is not a record.
func kvRecord(ln string) *py.Dict {
	rec := py.NewDict(8)
	rest := ln
	if ts, end, ok := tsMatch(rest); ok {
		rec.Set("timestamp", py.S(ts))
		rest = rest[end:]
	}
	if m := levelRE.FindStringSubmatchIndex(rest); m != nil {
		rec.Set("severity", py.S(rest[m[2]:m[3]]))
		rest = rest[m[1]:]
	}
	if m := loggerRE.FindStringSubmatchIndex(rest); m != nil && !strings.Contains(rest[m[2]:m[3]], "=") {
		rec.Set("service", py.S(rest[m[2]:m[3]]))
		rest = rest[m[1]:]
	}
	pairs := kvRE.FindAllStringSubmatch(rest, -1)
	if !(len(pairs) >= 2 || (len(pairs) > 0 && rec.Len() > 0)) {
		return nil
	}
	for _, p := range pairs {
		rec.Set(p[1], py.S(kvValue(p[2])))
	}
	if !rec.Has("message") && !rec.Has("msg") && rec.Has("type") && (rec.Has("clientId") || rec.Has("client_id")) {
		typ, _ := rec.Get("type")
		client := "None"
		if v, ok := rec.Get("clientId"); ok && v.S != "" {
			client = v.S
		} else if v, ok := rec.Get("client_id"); ok {
			client = v.S
		}
		rec.Set("message", py.S(typ.S+" client="+client))
	}
	return rec
}
