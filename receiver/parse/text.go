package parse

import (
	"math"
	"strings"

	"watchover/receiver/parse/py"
)

// Port of src/watchover/parsers/text_parser.py (P03 sections 1-3). Every regex is written in Python syntax and passed
// through py.Translate (\d \w \s \S -> Python 3.11 Unicode classes); lookarounds and \b are rewritten by hand as
// documented next to each pattern (P03 sections 1.1-1.15, verified against Python with 0 differences).

const (
	txtMON  = `(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)`
	txtMONU = `(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)` // MON.upper()
	txtDOW  = `(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)`

	// TS_RE alternatives A1..A11 (everything except the two epoch alternatives, which carry a lookahead).
	tsAlts = `\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z| ?[+\-]\d{2}(?::?\d{2})?| UTC)?` +
		`|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?` +
		`|\d{2}[./-]\d{2}[./-]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?: ?[AP]M)?` +
		`|\d{1,2}-` + txtMONU + `-\d{4} \d{2}:\d{2}:\d{2}` +
		`|\d{1,2}-` + txtMON + `-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?` +
		`|\d{2} ` + txtMON + ` \d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?` +
		`|` + txtMON + ` \d{1,2} \d{4} \d{2}:\d{2}:\d{2}` +
		`|` + txtDOW + ` ` + txtMON + ` +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}` +
		`|` + txtDOW + `, \d{1,2} ` + txtMON + ` \d{4} \d{2}:\d{2}:\d{2}(?: [+\-]\d{4})?` +
		`|` + txtMON + ` \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)` +
		`|\d{8}T\d{6}(?:\.\d+)?Z?`

	reI = `[Iiıİ]` // the letter i under Python re.I (Go (?i) already folds s/ſ and k/K)
)

// Shared with WP4 (kv) and WP5 (json enrich): TS_RE, LEVEL_RE, LOGGER_RE (P03 section 1).
//
//   - tsRE: Python TS_RE with the epoch lookaheads `(?=\s)` replaced by consuming `\s+` in their own groups 2 and 3.
//     Python m[1] is whichever of groups 1..3 participated (use tsMatch); the match end equals Python m.end().
//   - levelRE: LEVEL_RE (re.I) with every i written as [Iiıİ]; group 1 keeps the original spelling.
//   - loggerRE: LOGGER_RE, class translation only.
var (
	tsRE     = py.MustCompile(`^\[?(?:(` + tsAlts + `)\]?:?\s*|(\d{10}(?:\.\d{1,6})?)\s+|(\d{13})\s+)`)
	levelRE  = py.MustCompile(`(?i)^\[?(TRACE|DEBUG|` + reI + `NFO|` + reI + `NFORMAT` + reI + `ON|NOT` + reI + `CE|NOTE|SYSTEM|WARN(?:` + reI + `NG)?|ERR(?:OR)?|CR` + reI + `T(?:` + reI + `CAL)?|FATAL|SEVERE|EMERG(?:ENCY)?|ALERT|PAN` + reI + `C|CONF` + reI + `G|F` + reI + `NE(?:R|ST)?|` + reI + `NF|WRN|FTL|DBG|VRB)\]?[:\s]+`)
	loggerRE = py.MustCompile(`^\[?([A-Za-z][\w.\-]{2,})\]?:\s+`)
)

var (
	appBracketRE    = py.MustCompile(`^\[([A-Za-z][\w.\-]{1,40})\]\s*`)
	threadBracketRE = py.MustCompile(`^\[[^\]]{1,60}\]\s*`)
	threadIDRE      = py.MustCompile(`^\d+\s+\[`) // ^\d+\s+(?=\[) : the consumed '[' is given back (end-1)
	codeRE          = py.MustCompile(`^[A-Z]{2,5}-\d{3,7}$`)
	kvTsRE          = py.MustCompile(`^(?:time|ts|timestamp|date|@timestamp)="?([^"\s]+)"?\s+`)
	criRE           = py.MustCompile(`^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (stdout|stderr) ([FP]) `)
	redisRE         = py.MustCompile(`^(\d+):([MCSX]) \d{2} [A-Z][a-z]{2} \d{4}`) // lookahead consumed: rest starts at end(g2)+1
	// CONT_RE with `(?::|\b)` after Exception|Error rewritten as `(?::|[^\w]|$)` (boolean use only).
	contRE     = py.MustCompile(`^(?:\s|\tat |at [\w$.]+\(|\.\.\. \d+ (?:more|common frames)|Caused by:|Traceback \(most|  File "|[\w$.]+(?:Exception|Error)(?::|[^\w]|$)|System\.[\w.]+:|ORA-\d{5}|APP-FND-\d{5}|\*\*\*|\+-{10,}|-{10,}$)`)
	embTsRE    = py.MustCompile(`(?:\*\*(?:Starts|Ends)\*\*|Current system time is |^\[)?(\d{2}-[A-Z]{3}-\d{4} \d{2}:\d{2}:\d{2}(?::\d{3})?)\]?`)
	appNameRE  = py.MustCompile(`^([a-z][a-z0-9]*(?:-[a-z0-9]+)+)[\s:]`) // (?=[\s:]) consumed; only group 1 is used
	pgRE       = py.MustCompile(`^\[\d+(?:-\d+)?\] (?:[\w.\-]+@[\w.\-]+ )?(LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|INFO|DEBUG\d?|DETAIL|STATEMENT|HINT|CONTEXT|QUERY):\s+`)
	sevNumRE   = py.MustCompile(`(?:^|[^\w])Severity: (\d{1,2})(?:[^\w]|$)`) // \bSeverity: (\d{1,2})\b, group 1 identical
	dynamicsRE = py.MustCompile(`(?i)^(?:M` + reI + `crosoft\.Dynam` + reI + `cs\.[\w.]+|Ax32Serv|NavServer)\s*[:\-]\s*`)
)

var pgLevel = map[string]string{"LOG": "INFO", "DETAIL": "DETAIL", "STATEMENT": "STATEMENT", "HINT": "HINT", "CONTEXT": "CONTEXT", "QUERY": "STATEMENT"}

var redisMark = map[byte]string{'#': "WARN", '*': "INFO", '-': "INFO", '.': "DEBUG"}

// tsMatch is TS_RE.match(s): (m[1], m.end(), matched).
func tsMatch(s string) (string, int, bool) {
	m := tsRE.FindStringSubmatchIndex(s)
	if m == nil {
		return "", 0, false
	}
	for g := 1; g <= 3; g++ {
		if m[2*g] >= 0 {
			return s[m[2*g]:m[2*g+1]], m[1], true
		}
	}
	return "", 0, false // unreachable: one of the three groups always participates
}

// pyIntDigits is int() of a short non-empty run of \p{Nd} digits (Unicode digit values).
func pyIntDigits(s string) int {
	n := 0
	for _, r := range s {
		n = n*10 + py.DigitValue(r)
	}
	return n
}

// parseHead is text_parser.parse_head(ln) -> (fields, rest) (P03 section 2). Values are str, except "_partial" (bool).
func parseHead(ln string) (*py.Dict, string) {
	rec := py.NewDict(6)
	rest := ln
	if m := criRE.FindStringSubmatchIndex(rest); m != nil {
		rec.Set("timestamp", py.S(rest[m[2]:m[3]]))
		rec.Set("stream", py.S(rest[m[4]:m[5]]))
		rec.Set("_partial", py.B(rest[m[6]:m[7]] == "P"))
		rest = rest[m[1]:]
		if ts, end, ok := tsMatch(rest); ok {
			rec.Set("timestamp", py.S(ts))
			rest = rest[end:]
		}
	} else if m := redisRE.FindStringSubmatchIndex(rest); m != nil {
		rec.Set("pid", py.S(rest[m[2]:m[3]]))
		rec.Set("role", py.S(rest[m[4]:m[5]]))
		rest = rest[m[5]+1:]
		if ts, end, ok := tsMatch(rest); ok {
			rec.Set("timestamp", py.S(ts))
			rest = rest[end:]
		}
		if len(rest) >= 2 && rest[1] == ' ' {
			if sev, ok := redisMark[rest[0]]; ok {
				rec.Set("severity", py.S(sev))
				rest = rest[2:]
			}
		}
	} else if ts, end, ok := tsMatch(rest); ok {
		rec.Set("timestamp", py.S(ts))
		rest = rest[end:]
	} else if m := kvTsRE.FindStringSubmatchIndex(rest); m != nil {
		rec.Set("timestamp", py.S(rest[m[2]:m[3]]))
	}
	if m := threadIDRE.FindStringIndex(rest); m != nil {
		rest = rest[m[1]-1:]
	}
	if m := pgRE.FindStringSubmatchIndex(rest); m != nil {
		lvl := rest[m[2]:m[3]] // .upper() is a no-op: the group is case-sensitive ASCII
		sev, ok := pgLevel[lvl]
		if !ok {
			sev = lvl
		}
		rec.Set("severity", py.S(sev))
		rest = rest[m[1]:]
		pid, _, _ := strings.Cut(ln, "]")
		rec.Set("pid", py.S(strings.TrimLeft(pid, "[")))
	} else if m := levelRE.FindStringSubmatchIndex(rest); m != nil {
		rec.Set("severity", py.S(rest[m[2]:m[3]]))
		rest = rest[m[1]:]
		for it := 0; it < 3; it++ {
			b := appBracketRE.FindStringSubmatchIndex(rest)
			if b != nil && codeRE.MatchString(rest[b[2]:b[3]]) {
				rec.Set("code", py.S(rest[b[2]:b[3]]))
				rest = rest[b[1]:]
			} else if b != nil && !rec.Has("service") {
				rec.Set("service", py.S(rest[b[2]:b[3]]))
				rest = rest[b[1]:]
			} else if t := threadBracketRE.FindStringIndex(rest); t != nil {
				rest = rest[t[1]:]
			} else {
				break
			}
		}
	}
	if !rec.Has("severity") {
		if m := sevNumRE.FindStringSubmatchIndex(rest); m != nil {
			n := pyIntDigits(rest[m[2]:m[3]])
			sev := "INFO"
			if n >= 19 {
				sev = "CRITICAL"
			} else if n >= 11 {
				sev = "ERROR"
			}
			rec.Set("severity", py.S(sev))
		}
	}
	if !rec.Has("timestamp") {
		pre := py.Head(rest, 120)
		if m := embTsRE.FindStringSubmatchIndex(pre); m != nil {
			ts := pre[m[2]:m[3]]
			if strings.Count(ts, ":") == 3 {
				ts = py.Head(ts, 19)
			}
			rec.Set("timestamp", py.S(ts))
			if strings.HasPrefix(rest, "[") {
				rest = py.Strip(rest[m[1]:])
			}
		}
	}
	if !rec.Has("service") {
		if m := dynamicsRE.FindStringIndex(rest); m != nil {
			rec.Set("service", py.S(strings.Trim(rest[:m[1]], " :-")))
			rest = rest[m[1]:]
		} else if m := loggerRE.FindStringSubmatchIndex(rest); m != nil && !strings.HasSuffix(rest[m[2]:m[3]], ".go") {
			rec.Set("service", py.S(rest[m[2]:m[3]]))
			rest = rest[m[1]:]
		} else if m := appNameRE.FindStringSubmatchIndex(rest); m != nil {
			rec.Set("service", py.S(rest[m[2]:m[3]]))
		} else if m := appBracketRE.FindStringSubmatchIndex(rest); m != nil {
			rec.Set("service", py.S(rest[m[2]:m[3]]))
			rest = rest[m[1]:]
		}
	}
	return rec, py.Strip(rest)
}

// contMatch is CONT_RE.match(ln) as a boolean (P03 section 1.10).
func contMatch(ln string) bool { return contRE.MatchString(ln) }

// isBlank is `not ln.strip()`.
func isBlank(ln string) bool { return py.Strip(ln) == "" }

// TextParser is the "text" parser (P03 section 3).
type TextParser struct{}

func (TextParser) Name() string       { return "text" }
func (TextParser) Kind() string       { return "log" }
func (TextParser) LineOriented() bool { return true }

// Records yields the (line_no, record) pairs of text (TextParser.records).
func (TextParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	lines := py.SplitLines(text)
	type head struct {
		rec  *py.Dict
		rest string
	}
	// parse_head is pure: the sample pass keeps its results for the first 300 physical lines.
	nHead := min(len(lines), 300)
	heads := make([]head, nHead)
	sample := make([]string, 0, nHead)
	withTS := 0
	for i := 0; i < nHead; i++ {
		ln := lines[i]
		if isBlank(ln) {
			continue
		}
		sample = append(sample, ln)
		r, rest := parseHead(ln)
		heads[i] = head{r, rest}
		if r.Has("timestamp") {
			withTS++
		}
	}
	fold := float64(withTS) >= math.Max(1, 0.3*float64(len(sample)))
	family := detectFamily(sample, fc)

	var prev *py.Dict
	prevNo := 0
	for idx, ln := range lines {
		if isBlank(ln) {
			continue
		}
		var rec *py.Dict
		var rest string
		if idx < nHead {
			rec, rest = heads[idx].rec, heads[idx].rest
			heads[idx].rec = nil
		} else {
			rec, rest = parseHead(ln)
		}
		if prev != nil {
			// (a) PostgreSQL detail lines belong to the record above.
			if sv, ok := rec.Get("severity"); ok && sv.K == py.KStr {
				switch sv.S {
				case "DETAIL", "STATEMENT", "HINT", "CONTEXT":
					msg, _ := prev.Get("message")
					prev.Set("message", py.S(msg.S+"\n"+sv.S+": "+rest))
					prev.Set("_lines", py.Int(linesOf(prev)+1))
					continue
				}
			}
			// (b) an untimestamped line folds into the record above.
			if !rec.Has("timestamp") && (fold || contMatch(ln) || truthy(prev, "_partial")) {
				msg, _ := prev.Get("message")
				if !truthy(prev, "_partial") {
					prev.Set("message", py.S(py.Strip(msg.S+"\n"+py.Strip(ln))))
				} else {
					prev.Set("message", py.S(msg.S+py.Strip(ln)))
				}
				prev.Set("_lines", py.Int(linesOf(prev)+1))
				if p, ok := rec.Get("_partial"); ok {
					prev.Set("_partial", p)
				} else {
					prev.Set("_partial", py.B(false))
				}
				continue
			}
		}
		// (c) a new record.
		rec.Set("message", py.S(rest))
		if family != "" && !truthy(rec, "service") {
			rec.Set("service", py.S(family))
		}
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

// truthy is bool(d.get(k)).
func truthy(d *py.Dict, k string) bool {
	v, ok := d.Get(k)
	return ok && v.Truthy()
}

// linesOf is d.get("_lines", 1) for the int values this package stores there.
func linesOf(d *py.Dict) int64 {
	if v, ok := d.Get("_lines"); ok && v.K == py.KInt {
		return v.I
	}
	return 1
}
