package parse

import (
	"errors"
	"regexp"
	"strings"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// format_detector regexes (P01 section 8.1, verified Go forms). Captures keep the Python group names.
const pyWSClass = py.WSC

var (
	syslogTS = `(?P<ts>[A-Z][a-z]{2}` + py.PS + `+` + py.D + `{1,2}(?:` + py.PS + py.D + `{4})?` + py.PS + py.D + `{2}:` + py.D + `{2}:` + py.D + `{2}` +
		`|` + py.D + `{4}-` + py.D + `{2}-` + py.D + `{2}T[\p{Nd}:.+\-Z]+)`

	// R1 SYSLOG_RE without the _NOT_LEVEL lookahead: a match also needs notLevel(line[hostStart:]) (see syslogMatch).
	syslogRE = regexp.MustCompile(`^(?:<(?P<pri>` + py.D + `+)>)?` + syslogTS + py.PS + `+(?P<host>` + py.PNS + `+)` + py.PS +
		`+(?P<prog>[^:\[` + pyWSClass + `]+)(?:\[(?P<pid>` + py.D + `+)\])?:` + py.PS + `*(?P<msg>.*)$`)
	// R2 SYSLOG_NOPROG_RE without the lookahead.
	syslogNoProgRE = regexp.MustCompile(`^(?:<(?P<pri>` + py.D + `+)>)?` + syslogTS + py.PS + `+(?P<host>[A-Za-z][\p{L}\p{N}_.\-]*)` +
		py.PS + `+(?P<msg>.*)$`)
	// R3 RFC5424_RE.
	rfc5424RE = regexp.MustCompile(`^<(?P<pri>` + py.D + `+)>1 (?P<ts>` + py.PNS + `+) (?P<host>` + py.PNS + `+) (?P<app>` + py.PNS +
		`+) (?P<pid>` + py.PNS + `+) (?P<msgid>` + py.PNS + `+) (?P<sd>-|(?:\[.*?\])+) ?(?P<msg>.*)$`)
	// R4 CEF_RE.
	cefRE = regexp.MustCompile(`^(?:.*?` + py.PS + `)?(?:CEF:` + py.D + `+\||LEEF:` + py.D + `+(?:\.` + py.D + `+)?\|)`)
	// R5 CLF_RE.
	clfRE = regexp.MustCompile(`^` + py.PNS + `+ ` + py.PNS + `+ ` + py.PNS + `+ \[` + py.D + `{2}/[A-Z][a-z]{2}/` + py.D + `{4}:` + py.D +
		`{2}:` + py.D + `{2}:` + py.D + `{2} [+\-]` + py.D + `{4}\] "`)
	// R6 ALB_RE.
	albRE = regexp.MustCompile(`^(?:https?|h2|ws|wss|tls|tcp) ` + py.D + `{4}-` + py.D + `{2}-` + py.D + `{2}T` + py.D + `{2}:` + py.D +
		`{2}:` + py.D + `{2}\.` + py.D + `+Z ` + py.PNS + `+ `)
	// R7 ENVOY_RE.
	envoyRE = regexp.MustCompile(`^\[` + py.D + `{4}-` + py.D + `{2}-` + py.D + `{2}T[\p{Nd}:.]+Z?\] "[A-Z]+ ` + py.PNS + `+ [^"]*" (?:` +
		py.D + `{3}|-) ` + py.PNS + `+ `)
	// R8 WINEVT_RE ^\s*<Event\b[^>]*xmlns= : \b after "Event" = next char is not a word char (and cannot be '>').
	winevtRE = regexp.MustCompile(`^` + py.PS + `*<Event[^\p{L}\p{N}_>][^>]*xmlns=`)
	// R9 W3C_RE.
	w3cRE = regexp.MustCompile(`^#Fields:` + py.PS)
	// R10 CRI_LINE_RE.
	criLineRE = regexp.MustCompile(`^` + py.D + `{4}-` + py.D + `{2}-` + py.D + `{2}T` + py.D + `{2}:` + py.D + `{2}:` + py.D + `{2}(?:\.` +
		py.D + `+)?Z (?:stdout|stderr) [FP] `)
	// R11 KV_RE (findall).
	kvRE = regexp.MustCompile(`([\p{L}\p{N}_.\-@]+)=("(?:[^"\\]|\\.)*"|` + py.PNS + `+)`)

	syslogHostIdx       = syslogRE.SubexpIndex("host")
	syslogNoProgHostIdx = syslogNoProgRE.SubexpIndex("host")
)

// levelWords is the alternation of _NOT_LEVEL, in order.
var levelWords = []string{"TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERR", "ERROR", "CRIT", "CRITICAL",
	"FATAL", "SEVERE", "INF", "WRN", "FTL", "DBG", "VERBOSE"}

// notLevel is the removed lookahead (?!(?i:TRACE|...|VERBOSE)\b) evaluated at the host start: false when s starts
// with one of the words (Python re.I folding) followed by a Unicode word boundary.
func notLevel(s string) bool {
	for _, w := range levelWords {
		if n, ok := py.HasFoldPrefix(s, w); ok {
			if n == len(s) {
				return false
			}
			r, _ := utf8.DecodeRuneInString(s[n:])
			if !py.IsWord(r) {
				return false
			}
		}
	}
	return true
}

// syslogMatch is SYSLOG_RE.match(ln): the submatch indexes or nil.
func syslogMatch(ln string) []int {
	m := syslogRE.FindStringSubmatchIndex(ln)
	if m == nil || !notLevel(ln[m[2*syslogHostIdx]:]) {
		return nil
	}
	return m
}

// syslogNoProgMatch is SYSLOG_NOPROG_RE.match(ln): the submatch indexes or nil.
func syslogNoProgMatch(ln string) []int {
	m := syslogNoProgRE.FindStringSubmatchIndex(ln)
	if m == nil || !notLevel(ln[m[2*syslogNoProgHostIdx]:]) {
		return nil
	}
	return m
}

// errCSV is csv.Error inside the _csv reader (field larger than field_size_limit).
var errCSV = errors.New("csv.Error: field larger than field limit (131072)")

const csvFieldLimit = 131072

// csvReadLines runs CPython 3.11's _csv reader (excel dialect, given delimiter, strict off) over a list of lines
// without line terminators and returns the records (P01 section 8.2).
func csvReadLines(lines []string, delim rune) ([][]string, error) {
	const (
		startRecord = iota
		startField
		inField
		inQuoted
		quoteInQuoted
	)
	var recs [][]string
	var fields []string
	var field []rune
	state := startRecord
	add := func(c rune) error {
		if len(field) >= csvFieldLimit {
			return errCSV
		}
		field = append(field, c)
		return nil
	}
	save := func() {
		fields = append(fields, string(field))
		field = field[:0]
	}
	const eol = rune(-2)
	process := func(c rune) error {
		switch state {
		case startRecord:
			if c == eol {
				return nil // empty line: the record is []
			}
			state = startField
			fallthrough
		case startField:
			switch {
			case c == eol:
				save()
				state = startRecord
			case c == '"':
				state = inQuoted
			case c == delim:
				save()
			default:
				if err := add(c); err != nil {
					return err
				}
				state = inField
			}
		case inField:
			switch c {
			case eol:
				save()
				state = startRecord
			case delim:
				save()
				state = startField
			default:
				return add(c)
			}
		case inQuoted:
			switch c {
			case eol:
			case '"':
				state = quoteInQuoted
			default:
				return add(c)
			}
		case quoteInQuoted:
			switch c {
			case '"':
				if err := add('"'); err != nil {
					return err
				}
				state = inQuoted
			case delim:
				save()
				state = startField
			case eol:
				save()
				state = startRecord
			default:
				if err := add(c); err != nil {
					return err
				}
				state = inField
			}
		}
		return nil
	}
	for i := 0; i < len(lines); {
		// One reader iteration: lines until the state is back at START_RECORD.
		fields = nil
		field = field[:0]
		for {
			if i >= len(lines) {
				// End of input inside a record (unclosed quote): strict=False saves the field.
				if len(field) != 0 || state == inQuoted {
					save()
					recs = append(recs, fields)
				}
				return recs, nil
			}
			for _, c := range lines[i] {
				if err := process(c); err != nil {
					return recs, err
				}
			}
			i++
			if err := process(eol); err != nil {
				return recs, err
			}
			if state == startRecord {
				break
			}
		}
		if fields == nil {
			fields = []string{}
		}
		recs = append(recs, fields)
	}
	return recs, nil
}

// DetectDelimiter is format_detector.detect_delimiter(lines): the first of TAB , ; | whose csv.reader field counts over
// lines[:30] are all >= 2 with at most two distinct values; 0 when none. A csv.Error skips that delimiter.
func DetectDelimiter(lines []string) (delim rune, err error) {
	if len(lines) > 30 {
		lines = lines[:30]
	}
	for _, d := range []rune{'\t', ',', ';', '|'} {
		recs, err := csvReadLines(lines, d)
		if err != nil {
			continue
		}
		if len(recs) == 0 {
			continue
		}
		distinct := map[int]bool{}
		minN := -1
		for _, r := range recs {
			if minN < 0 || len(r) < minN {
				minN = len(r)
			}
			distinct[len(r)] = true
		}
		if minN >= 2 && len(distinct) <= 2 {
			return d, nil
		}
	}
	return 0, nil
}

// jsonKind runs json.loads(s) as a validity check: (top-level kind, nil), ErrSyntax-wrapping error for a
// JSONDecodeError, or ErrPyRaise / ErrForward for exceptions Python does not catch here.
func jsonKind(s string) (py.Kind, error) {
	k, err := py.Validate(s)
	switch {
	case err == nil:
		return k, nil
	case errors.Is(err, py.ErrSyntax):
		return 0, err
	case errors.Is(err, py.ErrRaise):
		return 0, pyRaise("ValueError in json.loads: %v", err)
	default:
		return 0, forward("json.loads: %v", err)
	}
}

// DetectFormat is format_detector.detect_format(text) -> (format, confidence) (P01 section 8.3).
func DetectFormat(text string, n *Normalizer) (format string, conf float64, err error) {
	var lines []string
	for _, ln := range py.SplitLinesN(py.Head(text, 400_000), 200) {
		if py.Strip(ln) != "" {
			lines = append(lines, ln)
		}
	}
	if len(lines) == 0 {
		return "text", 0.0, nil
	}
	total := float64(len(lines))
	first := py.LStrip(lines[0])
	if strings.HasPrefix(first, "[") || (strings.HasPrefix(first, "{") && (len(lines) == 1 || !strings.HasSuffix(py.RStrip(first), "}"))) {
		if _, err := jsonKind(text); err == nil {
			return "json", 0.95, nil
		} else if !errors.Is(err, py.ErrSyntax) {
			return "", 0, err
		}
	}
	ok := 0
	for _, ln := range lines {
		k, err := jsonKind(ln)
		if err == nil {
			if k == py.KMap {
				ok++
			}
		} else if !errors.Is(err, py.ErrSyntax) {
			return "", 0, err
		}
	}
	if float64(ok)/total > 0.8 {
		return "jsonl", py.Round(float64(ok)/total, 2), nil
	}
	for i, ln := range lines {
		if i >= 10 {
			break
		}
		if w3cRE.MatchString(ln) {
			return "w3c", 0.95, nil
		}
	}
	count := func(pred func(string) bool) int {
		c := 0
		for _, ln := range lines {
			if pred(ln) {
				c++
			}
		}
		return c
	}
	if float64(count(winevtRE.MatchString))/total > 0.6 {
		return "winevt", 0.95, nil
	}
	if c := count(cefRE.MatchString); float64(c)/total > 0.6 {
		return "cef", py.Round(float64(c)/total, 2), nil
	}
	if c := count(func(ln string) bool { return clfRE.MatchString(ln) || albRE.MatchString(ln) || envoyRE.MatchString(ln) }); float64(c)/total > 0.6 {
		return "access", py.Round(float64(c)/total, 2), nil
	}
	if float64(count(criLineRE.MatchString))/total > 0.6 {
		return "text", 0.9, nil
	}
	hits := count(func(ln string) bool { return syslogMatch(ln) != nil || rfc5424RE.MatchString(ln) })
	if float64(hits)/total > 0.6 {
		return "syslog", py.Round(float64(hits)/total, 2), nil
	}
	hits2 := count(func(ln string) bool { return syslogNoProgMatch(ln) != nil })
	if float64(hits+hits2)/total > 0.6 {
		return "syslog", py.Round(float64(hits+hits2)/total, 2), nil
	}
	if sap := sapScore(lines); sap >= 0.5 {
		return "sap", sap, nil
	}
	if lp, share := grokDetect(lines); lp != nil && share >= 0.6 {
		return "grok", share, nil
	}
	if d, _ := DetectDelimiter(lines); d != 0 && len(lines) >= 2 {
		recs, _ := csvReadLines(lines[:1], d)
		if len(recs) > 0 && len(recs[0]) > 0 {
			header := recs[0]
			alpha := 0
			for _, h := range header {
				h = strings.ReplaceAll(strings.ReplaceAll(py.Strip(h), "_", ""), " ", "")
				if py.IsAlphaStr(h) {
					alpha++
				}
			}
			if float64(alpha)/float64(len(header)) > 0.6 {
				return "csv", 0.9, nil
			}
		}
	}
	if c := count(func(ln string) bool { return len(kvRE.FindAllStringIndex(ln, 2)) >= 2 }); float64(c)/total > 0.6 {
		return "kv", py.Round(float64(c)/total, 2), nil
	}
	return "text", 0.5, nil
}
