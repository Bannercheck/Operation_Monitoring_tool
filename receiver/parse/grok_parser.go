package parse

import (
	"watchover/receiver/parse/py"
)

// GrokParser is the "grok" parser (P03 section 6, parsers/grok_parser.py).
type GrokParser struct{}

func (GrokParser) Name() string       { return "grok" }
func (GrokParser) Kind() string       { return "log" }
func (GrokParser) LineOriented() bool { return true }

// statusSev is STATUS_SEV: the first character of the status string decides.
func statusSev(st string) string {
	switch {
	case len(st) > 0 && st[0] == '5':
		return "ERROR"
	case len(st) > 0 && st[0] == '4':
		return "WARN"
	}
	return "INFO"
}

// Records yields the (line_no, record) pairs of text (GrokParser.records). The pattern is re-detected on this text
// (lines = text.splitlines(), so the sample is the non-blank lines among the first 120 physical lines).
func (GrokParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	lines := py.SplitLines(text)
	lp, _ := grokDetect(lines)
	if lp == nil {
		return nil
	}
	var prev *py.Dict
	prevNo := 0
	for idx, ln := range lines {
		if isBlank(ln) {
			continue
		}
		var rec *py.Dict
		m := lp.rx.FindStringSubmatchIndex(ln)
		if m == nil {
			if prev != nil && (contMatch(ln) || startsWithSpace(ln)) {
				msg := ""
				if v, ok := prev.Get("message"); ok {
					msg = v.Str()
				}
				prev.Set("message", py.S(py.Strip(msg+"\n"+py.Strip(ln))))
				prev.Set("_lines", py.Int(linesOf(prev)+1))
				continue
			}
			rec = py.NewDict(4)
			rec.Set("message", py.S(py.Strip(ln)))
			rec.Set("_unmatched", py.B(true))
		} else {
			// caught = {k: v for k, v in m.groupdict().items() if v is not None}, group order
			caught := py.NewDict(len(lp.names))
			for g, name := range lp.names {
				if g == 0 || name == "" || m[2*g] < 0 {
					continue
				}
				caught.Set(name, py.S(ln[m[2*g]:m[2*g+1]]))
			}
			rec = py.NewDict(caught.Len() + 3)
			for _, rf := range lp.Roles {
				if v, ok := caught.Get(rf[1]); ok {
					rec.Set(rf[0], v)
				}
			}
			if st, ok := rec.Get("status"); ok && !rec.Has("severity") {
				rec.Del("status")
				rec.Set("severity", py.S(statusSev(st.Str())))
			} else if ok {
				rec.Del("status")
			}
			def := py.S(py.Strip(ln))
			if v, ok := caught.Get("message"); ok && v.Truthy() {
				def = v
			}
			rec.SetDefault("message", def)
			caught.Range(func(k string, v py.Value) bool {
				if !rec.Has(k) && !lp.roleVal[k] {
					rec.Set(k, v)
				}
				return true
			})
			if v, ok := caught.Get("pid"); ok && !rec.Has("pid") {
				rec.Set("pid", v)
			}
		}
		if lp.Family != "" && !truthy(rec, "service") {
			rec.Set("service", py.S(lp.Family))
		}
		rec.Set("_grok", py.S(lp.Name))
		if prev != nil && !yield(Record{LineNo: prevNo, Rec: prev}) {
			return nil
		}
		prev, prevNo = rec, idx+1
	}
	if prev != nil {
		yield(Record{LineNo: prevNo, Rec: prev})
	}
	return nil
}

// startsWithSpace is ln[:1].isspace().
func startsWithSpace(ln string) bool {
	for _, r := range ln {
		return py.IsSpace(r)
	}
	return false
}
