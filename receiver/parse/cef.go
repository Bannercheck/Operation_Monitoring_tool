package parse

import (
	"math/big"
	"strings"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// Port of src/watchover/parsers/cef_parser.py (P04 section 5): ArcSight CEF and QRadar LEEF.

var (
	cefHeadRE      = py.MustCompile(`^(?P<prefix>.*?)CEF:(?P<ver>\d+)\|`)
	leefHeadRE     = py.MustCompile(`^(?P<prefix>.*?)LEEF:(?P<ver>\d+(?:\.\d+)?)\|`)
	cefSyslogPfxRE = py.MustCompile(`^(?:<\d+>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\s+(?P<host>\S+)\s*$`)
	cefPfxTS       = cefSyslogPfxRE.SubexpIndex("ts")
	cefPfxHost     = cefSyslogPfxRE.SubexpIndex("host")
)

var (
	big9 = big.NewInt(9)
	big7 = big.NewInt(7)
	big4 = big.NewInt(4)
)

// cefSev is CEF_SEV(int(s)) for a string whose stripped form isdigit(); int() raises ValueError on non-decimal digits
// (superscripts) and on more than 4300 digits.
func cefSev(s string) (string, error) {
	n, err := py.Atoi(s)
	if err != nil {
		return "", pyRaise("ValueError: int(%q)", py.Head(s, 40))
	}
	switch {
	case n.Cmp(big9) >= 0:
		return "CRITICAL", nil
	case n.Cmp(big7) >= 0:
		return "ERROR", nil
	case n.Cmp(big4) >= 0:
		return "WARN", nil
	}
	return "INFO", nil
}

// splitHeader is _split_header(s, n): n pipe-separated fields ('\' escapes any next character), then the raw rest.
// Fewer than n+1 elements when fewer than n pipes were found.
func splitHeader(s string, n int) []string {
	var out []string
	var cur strings.Builder
	i := 0
	for i < len(s) && len(out) < n {
		c := s[i]
		if c == '\\' && i+1 < len(s) {
			_, w := utf8.DecodeRuneInString(s[i+1:])
			cur.WriteString(s[i+1 : i+1+w])
			i += 1 + w
			continue
		}
		if c == '|' {
			out = append(out, cur.String())
			cur.Reset()
			i++
			continue
		}
		_, w := utf8.DecodeRuneInString(s[i:])
		cur.WriteString(s[i : i+w])
		i += w
	}
	if len(out) == n {
		out = append(out, s[i:])
	} else {
		out = append(out, cur.String())
	}
	return out
}

// cefKVBoundary is the lookahead (?=\s+\w+=|$) at rune position j.
func cefKVBoundary(s []rune, j int) bool {
	if j == len(s) {
		return true
	}
	if !py.IsSpace(s[j]) {
		return false
	}
	q := j
	for q < len(s) && py.IsSpace(s[q]) {
		q++
	}
	r := q
	for r < len(s) && py.IsWord(s[r]) {
		r++
	}
	return r > q && r < len(s) && s[r] == '='
}

// cefKV is CEF_KV.findall(ext) for (\w+)=((?:\\.|[^\\=])*?)(?=\s+\w+=|$) (P04 section 5.1 scanner).
func cefKV(ext string) [][2]string {
	s := []rune(ext)
	var out [][2]string
	for k := 0; k < len(s); {
		e := k
		for e < len(s) && py.IsWord(s[e]) {
			e++
		}
		if e > k && e < len(s) && s[e] == '=' {
			j := e + 1
			ok := false
			for {
				if cefKVBoundary(s, j) {
					ok = true
					break
				}
				if j < len(s) && s[j] == '\\' && j+1 < len(s) && s[j+1] != '\n' {
					j += 2
					continue
				}
				if j < len(s) && s[j] != '\\' && s[j] != '=' {
					j++
					continue
				}
				break
			}
			if ok {
				out = append(out, [2]string{string(s[k:e]), string(s[e+1 : j])})
				if j == k { // cannot happen (a match is never empty); guard against an endless loop
					j++
				}
				k = j
				continue
			}
		}
		k++
	}
	return out
}

// CefParser is the "cef" parser (P04 section 5).
type CefParser struct{}

func (CefParser) Name() string       { return "cef" }
func (CefParser) Kind() string       { return "event" }
func (CefParser) LineOriented() bool { return true }

// Records yields the (line_no, record) pairs of text.
func (CefParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	for idx, ln := range py.SplitLines(text) {
		if isBlank(ln) {
			continue
		}
		rec, err := cefRecord(ln)
		if err != nil {
			return err
		}
		if rec == nil {
			continue
		}
		if !yield(Record{LineNo: idx + 1, Rec: rec}) {
			return nil
		}
	}
	return nil
}

func cefRecord(ln string) (*py.Dict, error) {
	rec := py.NewDict(12)
	pairs := py.NewDict(16)
	var prefix string
	if m := cefHeadRE.FindStringSubmatchIndex(ln); m != nil {
		prefix = ln[m[2]:m[3]]
		h := splitHeader(ln[m[1]:], 6)
		if len(h) != 7 {
			return nil, pyRaise("ValueError: not enough values to unpack (expected 7, got %d)", len(h))
		}
		vendor, product, version, sig, name, sev, ext := h[0], h[1], h[2], h[3], h[4], h[5], h[6]
		service := py.Strip(product)
		if service == "" {
			service = vendor
		}
		rec.Set("vendor", py.S(vendor))
		rec.Set("product", py.S(product))
		rec.Set("version", py.S(version))
		rec.Set("signature", py.S(sig))
		rec.Set("message", py.S(name))
		rec.Set("service", py.S(service))
		if s := py.Strip(sev); py.IsDigitStr(s) {
			v, err := cefSev(sev)
			if err != nil {
				return nil, err
			}
			rec.Set("severity", py.S(v))
		} else if s != "" {
			rec.Set("severity", py.S(s))
		}
		for _, kv := range cefKV(ext) {
			v := strings.ReplaceAll(kv[1], `\=`, "=")
			v = strings.ReplaceAll(v, `\n`, " ")
			pairs.Set(kv[0], py.S(py.Strip(v)))
		}
	} else if m := leefHeadRE.FindStringSubmatchIndex(ln); m != nil {
		prefix = ln[m[2]:m[3]]
		h := splitHeader(ln[m[1]:], 4)
		if len(h) != 5 {
			return nil, pyRaise("ValueError: not enough values to unpack (expected 5, got %d)", len(h))
		}
		vendor, product, version, name, rest := h[0], h[1], h[2], h[3], h[4]
		service := py.Strip(product)
		if service == "" {
			service = vendor
		}
		rec.Set("vendor", py.S(vendor))
		rec.Set("product", py.S(product))
		rec.Set("version", py.S(version))
		rec.Set("message", py.S(name))
		rec.Set("service", py.S(service))
		delim := "\t"
		if !strings.HasPrefix(rest, "|") && py.Len(rest) > 1 && rest[0] != '\t' && strings.Contains(py.Head(rest, 8), "|") && rest[0] != ' ' {
			delim, rest, _ = strings.Cut(rest, "|")
		} else if strings.HasPrefix(rest, "|") {
			rest = rest[1:]
		}
		for _, part := range strings.Split(rest, delim) {
			if k, v, ok := strings.Cut(part, "="); ok {
				pairs.Set(py.Strip(k), py.S(py.Strip(v)))
			}
		}
		if v, ok := pairs.Get("sev"); ok && py.IsDigitStr(v.S) {
			s, err := cefSev(v.S)
			if err != nil {
				return nil, err
			}
			rec.Set("severity", py.S(s))
		}
	} else {
		return nil, nil
	}
	if prefix != "" {
		pfx := py.Strip(prefix)
		if sp := cefSyslogPfxRE.FindStringSubmatchIndex(pfx); sp != nil {
			rec.SetDefault("host", py.S(sub(pfx, sp, cefPfxHost)))
			rec.SetDefault("syslog_ts", py.S(sub(pfx, sp, cefPfxTS)))
		}
	}
	// Python `a or b or ...` over str-or-None values: the first non-empty string.
	first := func(d *py.Dict, keys ...string) string {
		for _, k := range keys {
			if v, ok := d.Get(k); ok && v.S != "" {
				return v.S
			}
		}
		return ""
	}
	ts := first(pairs, "rt", "devTime", "end", "start")
	if ts == "" {
		ts = first(rec, "syslog_ts")
	}
	if ts != "" {
		rec.Set("timestamp", py.S(ts))
	}
	host := first(pairs, "dvchost", "identHostName", "dvc", "devname")
	if host == "" {
		host = first(rec, "host")
	}
	if host != "" {
		rec.Set("host", py.S(host))
	}
	if msg := first(pairs, "msg"); msg != "" {
		cur, _ := rec.Get("message")
		rec.Set("message", py.S(cur.S+": "+msg))
	}
	pairs.Range(func(k string, v py.Value) bool {
		if !rec.Has(k) {
			rec.Set(k, v)
		}
		return true
	})
	return rec, nil
}
