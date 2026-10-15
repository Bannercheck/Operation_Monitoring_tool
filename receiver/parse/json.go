package parse

import (
	"errors"
	"strings"

	"watchover/receiver/parse/py"
)

// Owner: WP5 (P04 section 8, P02 section 11.1).

// JsonParser is the "json" parser (parsers/json_parser.py).
type JsonParser struct{}

func (JsonParser) Name() string       { return "json" }
func (JsonParser) Kind() string       { return "event" }
func (JsonParser) LineOriented() bool { return false }

// Records yields the (line_no, record) pairs of text: json.loads(text), then the OTLP rows, the first list of dicts
// (find_records) or the document itself; line_no counts every row, dict or not.
func (JsonParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	obj, err := loadsErr(text)
	if err != nil {
		return err
	}
	var rows []py.Value
	if obj.K == py.KMap && obj.M.Has("resourceLogs") {
		if rows, err = otelRecords(obj.M); err != nil {
			return err
		}
	} else if found := findRecords(obj, 0); found != nil {
		rows = found
	} else if obj.K == py.KMap {
		rows = []py.Value{obj}
	}
	for i, r := range rows {
		if r.K != py.KMap {
			continue
		}
		if !yield(Record{LineNo: i + 1, Rec: enrich(Flatten(r.M, ""))}) {
			return nil
		}
	}
	return nil
}

// loadsErr is json.loads(s) with the error mapped to the pipeline errors: JSONDecodeError and ValueError escape
// (ErrPyRaise); the RecursionError depth is not reproducible (ErrForward).
func loadsErr(s string) (py.Value, error) {
	v, err := py.Loads(s)
	if err == nil {
		return v, nil
	}
	if errors.Is(err, py.ErrDepth) {
		return v, forward("json nesting: %v", err)
	}
	return v, pyRaise("json.loads: %v", err)
}

// findRecords is json_parser.find_records(obj, depth): the first non-empty list whose FIRST element is a dict,
// searched depth-first through dict values up to 4 dict levels; nil = None. Lists inside lists are not searched.
func findRecords(obj py.Value, depth int) []py.Value {
	switch obj.K {
	case py.KList:
		if len(obj.L) > 0 && obj.L[0].K == py.KMap {
			return obj.L
		}
	case py.KMap:
		if depth < 4 {
			for i := 0; i < obj.M.Len(); i++ {
				_, v := obj.M.At(i)
				if found := findRecords(v, depth+1); found != nil {
					return found
				}
			}
		}
	}
	return nil
}

// pyIter returns the items `for x in v` visits: list elements, the characters of a str, the keys of a dict;
// TypeError for anything else (None, numbers, bools).
func pyIter(v py.Value, what string) ([]py.Value, error) {
	switch v.K {
	case py.KList:
		return v.L, nil
	case py.KStr:
		out := make([]py.Value, 0, len(v.S))
		for _, r := range v.S {
			out = append(out, py.S(string(r)))
		}
		return out, nil
	case py.KMap:
		out := make([]py.Value, 0, v.M.Len())
		for _, k := range v.M.Keys() {
			out = append(out, py.S(k))
		}
		return out, nil
	}
	return nil, pyRaise("TypeError: %s object is not iterable (%s)", kindName(v), what)
}

func kindName(v py.Value) string {
	switch v.K {
	case py.KNone:
		return "'NoneType'"
	case py.KBool:
		return "'bool'"
	case py.KInt:
		return "'int'"
	case py.KFloat:
		return "'float'"
	case py.KStr:
		return "'str'"
	case py.KList:
		return "'list'"
	}
	return "'dict'"
}

// asDict is the precondition of `x.get(...)`: AttributeError unless x is a dict.
func asDict(v py.Value, what string) (*py.Dict, error) {
	if v.K != py.KMap {
		return nil, pyRaise("AttributeError: %s object has no attribute 'get' (%s)", kindName(v), what)
	}
	return v.M, nil
}

// dictGet is dict.get(k, def).
func dictGet(d *py.Dict, k string, def py.Value) py.Value {
	if v, ok := d.Get(k); ok {
		return v
	}
	return def
}

// pyOr is Python `a or b`.
func pyOr(a, b py.Value) py.Value {
	if a.Truthy() {
		return a
	}
	return b
}

// firstValue is next(iter(d.values()), def).
func firstValue(d *py.Dict, def py.Value) py.Value {
	if d.Len() == 0 {
		return def
	}
	_, v := d.At(0)
	return v
}

// otelAttrs is json_parser._otel_attrs(items), written into out (Python builds a new dict and merges it with
// `**`; setting in place gives the same order and values). Keys must be str: any other hashable key is a Python
// dict key py.Dict cannot hold (ErrForward); an unhashable one raises TypeError.
func otelAttrs(items py.Value, out *py.Dict) error {
	if !items.Truthy() {
		return nil
	}
	list, err := pyIter(items, "_otel_attrs items")
	if err != nil {
		return err
	}
	for _, a := range list {
		ad, err := asDict(a, "_otel_attrs item")
		if err != nil {
			return err
		}
		val := py.Null
		if v, ok := ad.Get("value"); !ok {
			val = py.Null // {}: next(iter({}.values()), None)
		} else if v.K == py.KMap {
			val = firstValue(v.M, py.Null)
		} else {
			val = v
		}
		key := dictGet(ad, "key", py.S(""))
		switch key.K {
		case py.KStr:
			out.Set(key.S, val)
		case py.KList, py.KMap:
			return pyRaise("TypeError: unhashable type: %s", kindName(key))
		default:
			return forward("OTLP attribute key of type %s", kindName(key))
		}
	}
	return nil
}

// otelRecords is json_parser.otel_records(obj) (P04 section 8.2).
func otelRecords(obj *py.Dict) ([]py.Value, error) {
	var rows []py.Value
	rls, err := pyIter(dictGet(obj, "resourceLogs", py.ListOf()), "resourceLogs")
	if err != nil {
		return nil, err
	}
	for _, rlv := range rls {
		rl, err := asDict(rlv, "resourceLogs item")
		if err != nil {
			return nil, err
		}
		res := py.NewDict(0)
		if rv, ok := rl.Get("resource"); ok {
			resource, err := asDict(rv, "resource")
			if err != nil {
				return nil, err
			}
			if err := otelAttrs(dictGet(resource, "attributes", py.Null), res); err != nil {
				return nil, err
			}
		}
		scopes, err := pyIter(pyOr(dictGet(rl, "scopeLogs", py.ListOf()), dictGet(rl, "instrumentationLibraryLogs", py.ListOf())), "scopeLogs")
		if err != nil {
			return nil, err
		}
		for _, slv := range scopes {
			sl, err := asDict(slv, "scopeLogs item")
			if err != nil {
				return nil, err
			}
			lrs, err := pyIter(dictGet(sl, "logRecords", py.ListOf()), "logRecords")
			if err != nil {
				return nil, err
			}
			for _, lrv := range lrs {
				lr, err := asDict(lrv, "logRecord")
				if err != nil {
					return nil, err
				}
				row := res.Clone()
				row.Set("timestamp", pyOr(dictGet(lr, "timeUnixNano", py.Null), dictGet(lr, "observedTimeUnixNano", py.Null)))
				row.Set("severity", pyOr(dictGet(lr, "severityText", py.Null), dictGet(lr, "severityNumber", py.Null)))
				if body, ok := lr.Get("body"); !ok {
					row.Set("message", py.S("")) // body {} -> next(iter(()), "")
				} else if body.K == py.KMap {
					row.Set("message", firstValue(body.M, py.S("")))
				} else {
					row.Set("message", body)
				}
				if err := otelAttrs(dictGet(lr, "attributes", py.Null), row); err != nil {
					return nil, err
				}
				rows = append(rows, py.MapOf(row))
			}
		}
	}
	return rows, nil
}

var (
	enrichSkip = []string{"severity", "level", "log.level", "severityText", "PRIORITY", "s"}
	enrichText = []string{"log", "message", "msg", "short_message"}
)

// enrich is json_parser.enrich(rec) (P02 section 11.1): lift the level (and app) out of a text field that carries
// its own "<ts> LEVEL app message" head. Mutates and returns rec.
func enrich(rec *py.Dict) *py.Dict {
	for _, k := range enrichSkip {
		if rec.Has(k) {
			return rec
		}
	}
	txt := ""
	for _, k := range enrichText {
		if v, ok := rec.Get(k); ok && v.K == py.KStr {
			txt = v.S
			break
		}
	}
	if txt == "" {
		return rec
	}
	head, rest := parseHead(strings.TrimRight(txt, "\n"))
	sev, ok := head.Get("severity")
	if !ok || !sev.Truthy() {
		return rec
	}
	rec.Set("severity", sev)
	if svc, ok := head.Get("service"); ok && svc.Truthy() {
		has := false
		for _, k := range roleHints[RoleService] {
			if rec.Has(k) {
				has = true
				break
			}
		}
		if !has {
			rec.Set("service", svc)
		}
	}
	if rest != "" {
		rec.Set("message", py.S(rest))
	}
	return rec
}
