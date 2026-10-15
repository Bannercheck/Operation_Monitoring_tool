package parse

import (
	"regexp"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02 sections 5-7).

// sevRank is models.SEV_RANK.
var sevRank = map[string]int{"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}

// severityMap is normalize.SEVERITY_MAP.
var severityMap = map[string]string{
	"trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "informational": "INFO", "notice": "INFO", "ok": "INFO",
	"warn": "WARN", "warning": "WARN", "error": "ERROR", "err": "ERROR", "critical": "CRITICAL", "crit": "CRITICAL",
	"fatal": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL", "severe": "CRITICAL",
	"high": "ERROR", "medium": "WARN", "low": "INFO", "p1": "CRITICAL", "p2": "ERROR", "p3": "WARN", "p4": "INFO",
	"firing": "ERROR", "resolved": "INFO", "major": "ERROR", "minor": "WARN",
	"path": "DEBUG", "config": "DEBUG", "fine": "DEBUG", "finer": "DEBUG", "finest": "DEBUG", "plain": "INFO",
	"e": "ERROR", "w": "WARN", "i": "INFO", "d": "DEBUG", "f": "CRITICAL",
	"inf": "INFO", "wrn": "WARN", "ftl": "CRITICAL", "dbg": "DEBUG", "vrb": "DEBUG",
	"panic": "CRITICAL", "information": "INFO", "verbose": "DEBUG", "audit_success": "INFO", "audit_failure": "WARN",
	"unknown": "INFO", "success": "INFO", "failed": "ERROR", "failure": "ERROR",
}

// scenarioSeverityMap is scenario.SEVERITY_MAP (S-A1 1-5 scale).
var scenarioSeverityMap = map[string]string{"5": "CRITICAL", "4": "ERROR", "3": "WARN", "2": "INFO", "1": "DEBUG"}

// ColumnSeverity is normalize.column_severity(v, use_scale) (memoised per Normalizer; 1, 1.0 and True share a key).
func (n *Normalizer) ColumnSeverity(v py.Value, useScale bool) (string, error) {
	var key sevKey
	switch v.K {
	case py.KStr, py.KInt, py.KFloat, py.KBool:
		key = sevKey{v.HashKey(), useScale}
	default:
		key = sevKey{py.S(v.Str()).HashKey(), useScale}
	}
	n.mu.Lock()
	hit, ok := n.sevCache[key]
	n.mu.Unlock()
	if ok {
		return hit, nil
	}
	s := ""
	if v.K != py.KNone {
		s = py.Lower(py.Strip(v.Str()))
	}
	var out string
	if m, ok := scenarioSeverityMap[s]; useScale && ok {
		out = m
	} else {
		var err error
		if out, err = NormalizeSeverity(v); err != nil {
			return "", err
		}
	}
	n.mu.Lock()
	if len(n.sevCache) < sevCacheMax {
		n.sevCache[key] = out
	}
	n.mu.Unlock()
	return out, nil
}

// NormalizeSeverity is normalize.normalize_severity(v).
func NormalizeSeverity(v py.Value) (string, error) {
	if v.K == py.KNone {
		return "INFO", nil
	}
	s := py.Lower(py.Strip(v.Str()))
	if m, ok := severityMap[s]; ok {
		return m, nil
	}
	if py.IsDigitStr(s) {
		n, err := py.Atoi(s)
		if err != nil {
			return "", pyRaise("ValueError: invalid literal for int(): %q", s)
		}
		if n.IsInt64() {
			switch n.Int64() {
			case 0, 1, 2:
				return "CRITICAL", nil
			case 3:
				return "ERROR", nil
			case 4:
				return "WARN", nil
			}
		}
		return "INFO", nil
	}
	if up := py.Upper(s); hasKey(sevRank, up) {
		return up, nil
	}
	return "INFO", nil
}

// levelWordAlts is LEVEL_WORD_RE's alternation with the optional suffixes expanded (longest first, same priority).
var levelWordAlts = []string{"TRACE", "DEBUG", "INFO", "NOTICE", "WARNING", "WARN", "ERROR", "ERR", "CRITICAL", "CRIT",
	"FATAL", "ALERT", "EMERGENCY", "EMERG", "SEVERE", "PANIC", "INF", "WRN", "FTL", "DBG", "VRB"}

// isLetterI is [A-Za-z] under Python re.I: ASCII letters plus İ ı ſ K.
func isLetterI(r rune) bool {
	return (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || r == 0x130 || r == 0x131 || r == 0x17f || r == 0x212a
}

// levelWordFind is LEVEL_WORD_RE.search(s)[1]: the leftmost level word not touching a letter (Python re.I).
func levelWordFind(s string) (string, bool) {
	prev := rune(-1)
	for i := 0; i < len(s); {
		r, w := utf8.DecodeRuneInString(s[i:])
		if prev < 0 || !isLetterI(prev) {
			for _, word := range levelWordAlts {
				k, ok := py.HasFoldPrefix(s[i:], word)
				if !ok {
					continue
				}
				if j := i + k; j < len(s) {
					if nr, _ := utf8.DecodeRuneInString(s[j:]); isLetterI(nr) {
						continue
					}
				}
				return s[i : i+k], true
			}
		}
		prev = r
		i += w
	}
	return "", false
}

// errorHintRE is ERROR_HINT_RE without its "\b[A-Za-z_.$]*(?:Exception|Error)\b: " alternative (see exceptionHint).
// Python \b around ASCII words becomes a consumed non-word rune or a string edge (boolean use only); every i of a
// re.I literal is [iIıİ].
var errorHintRE = regexp.MustCompile(`(?:(?i:ORA-` + py.D + `{5}|APP-FND-` + py.D + `{5}|FRM-` + py.D + `{5}|TNS-` + py.D + `{5}|` +
	`Traceback \(most recent call last\)|deadlock detected|Out of memory)` +
	`|(?:^|` + py.NW + `)(?:` +
	`(?i:segfault)(?:` + py.NW + `|$)|` +
	`(?i:pan` + py.I + `c:)|` +
	`(?i:kernel pan` + py.I + `c)|` +
	`(?i:Stack overflow)(?:` + py.NW + `|$)|` +
	`(?i:BACKUP fa` + py.I + `led)(?:` + py.NW + `|$)|` +
	`(?i:Log` + py.I + `n fa` + py.I + `led)(?:` + py.NW + `|$)|` +
	`(?i:Assert` + py.I + `on fa` + py.I + `lure)(?:` + py.NW + `|$)|` +
	`(?i:c` + py.I + `rcu` + py.I + `t breaker open)(?:` + py.NW + `|$)))`)

// warnHintRE is WARN_HINT_RE (every alternative has \b on both sides).
var warnHintRE = regexp.MustCompile(`(?:^|` + py.NW + `)(?i:t` + py.I + `med out|t` + py.I + `meout|retry|retry` + py.I + `ng|deferred|` +
	`den` + py.I + `ed|refused|slow query|unreachable|degraded|deprecated)(?:` + py.NW + `|$)`)

var exceptionWordRE = regexp.MustCompile(`(?i:Except` + py.I + `on|Error): `)

// isExcClass is [A-Za-z_.$] under Python re.I.
func isExcClass(r rune) bool { return isLetterI(r) || r == '_' || r == '.' || r == '$' }

// exceptionHint is the boolean search of `\b[A-Za-z_.$]*(?:Exception|Error)\b: ` (re.I, Unicode \b): some
// occurrence of "(Exception|Error): " at q has a word boundary somewhere in the class run that ends at q.
func exceptionHint(s string) bool {
	for start := 0; start < len(s); {
		loc := exceptionWordRE.FindStringIndex(s[start:])
		if loc == nil {
			return false
		}
		q := start + loc[0]
		// walk back over the class run ending at q, testing \b at every candidate start p in [a, q]
		p := q
		for {
			var prev rune = -1
			if p > 0 {
				prev, _ = utf8.DecodeLastRuneInString(s[:p])
			}
			cur, _ := utf8.DecodeRuneInString(s[p:])
			prevW := prev >= 0 && py.IsWord(prev)
			curW := p < len(s) && py.IsWord(cur)
			if prevW != curW {
				return true
			}
			if prev < 0 || !isExcClass(prev) {
				break
			}
			_, w := utf8.DecodeLastRuneInString(s[:p])
			p -= w
		}
		_, w := utf8.DecodeRuneInString(s[q:])
		start = q + w
	}
	return false
}

// SeverityFromText is normalize.severity_from_text(s).
func SeverityFromText(s string) string {
	if w, ok := levelWordFind(s); ok {
		sev, err := NormalizeSeverity(py.S(w))
		if err == nil {
			return sev
		}
	}
	if errorHintRE.MatchString(s) || exceptionHint(s) {
		return "ERROR"
	}
	if warnHintRE.MatchString(s) {
		return "WARN"
	}
	return "INFO"
}

func hasKey(m map[string]int, k string) bool {
	_, ok := m[k]
	return ok
}
