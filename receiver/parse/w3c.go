package parse

import (
	"strings"

	"watchover/receiver/parse/py"
)

// W3cParser is the "w3c" parser: port of src/watchover/parsers/w3c_parser.py (P04 section 3).
type W3cParser struct{}

func (W3cParser) Name() string       { return "w3c" }
func (W3cParser) Kind() string       { return "log" }
func (W3cParser) LineOriented() bool { return true }

// Records yields the (line_no, record) pairs of text.
func (W3cParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	var fields []string
	software := ""
	for idx, ln := range py.SplitLines(text) {
		if isBlank(ln) {
			continue
		}
		if strings.HasPrefix(ln, "#") {
			if strings.HasPrefix(ln, "#Fields:") {
				fields = py.Split(ln[len("#Fields:"):])
			} else if strings.HasPrefix(ln, "#Software:") {
				software = py.Strip(ln[len("#Software:"):])
			}
			continue
		}
		if len(fields) == 0 {
			continue
		}
		vals := strings.Split(ln, " ")
		rec := py.NewDict(len(fields) + 4)
		for i := 0; i < len(fields) && i < len(vals); i++ {
			if vals[i] != "-" {
				rec.Set(fields[i], py.S(vals[i]))
			}
		}
		get := func(k string) (string, bool) {
			v, ok := rec.Get(k)
			return v.S, ok
		}
		if rec.Has("date") && rec.Has("time") {
			d, _ := rec.Del("date")
			t, _ := rec.Del("time")
			rec.Set("timestamp", py.S(d.S+" "+t.S))
		} else if rec.Has("date-time") {
			v, _ := rec.Del("date-time")
			rec.Set("timestamp", v)
		}
		st, _ := get("sc-status")
		if st == "" {
			st, _ = get("status")
		}
		method, _ := get("cs-method")
		uri, ok := get("cs-uri-stem")
		if !ok {
			uri, _ = get("cs-uri")
		}
		var parts []string
		for _, x := range []string{method, uri, st} {
			if x != "" {
				parts = append(parts, x)
			}
		}
		msg := strings.Join(parts, " ")
		if msg == "" {
			msg = ln
		}
		rec.Set("message", py.S(msg))
		if st != "" {
			rec.Set("severity", py.S(statusSeverity(st)))
		}
		if rec.Has("s-ip") || rec.Has("s-computername") {
			host, _ := get("s-computername")
			if host == "" {
				ip, ok := get("s-ip")
				if !ok {
					return pyRaise("KeyError: 's-ip'")
				}
				host = ip
			}
			rec.Set("host", py.S(host))
		}
		service, _ := get("s-sitename")
		if service == "" {
			service = "w3c"
			if strings.Contains(software, "Internet Information Services") {
				service = "iis"
			}
		}
		rec.Set("service", py.S(service))
		if !yield(Record{LineNo: idx + 1, Rec: rec}) {
			return nil
		}
	}
	return nil
}
