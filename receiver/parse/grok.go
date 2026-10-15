package parse

import (
	"regexp"
	"strings"

	"watchover/receiver/parse/py"
)

// Port of src/watchover/grok.py on the parse path (P03 section 5): expand, the built-in LINES compiled once, detect.
// Tables (BASE, LINES) are in grok_lib.go, generated from Python. User patterns (<home>/grok.json) are not ported
// (P03 section 5.5): goldens use an empty home; the receiver must forward grok batches when grok.json exists.

// grokLineDef is one grok.LinePattern of LINES: roles in dict order (role -> captured field).
type grokLineDef struct {
	Name    string
	Pattern string
	Family  string
	Roles   [][2]string
}

// linePattern is grok.LinePattern with its compiled RE2 form.
type linePattern struct {
	Name    string
	Pattern string
	Family  string
	Roles   [][2]string     // role -> field, dict order
	roleVal map[string]bool // set(roles.values())
	rx      *regexp.Regexp
	names   []string // rx.SubexpNames()
}

// grokGoBase overrides BASE entries whose Python form RE2 cannot express; each is exact in every LINES context
// (P03 section 5.7.2): lookarounds of BASE10NUM / BASE16NUM / IPV4 / TIME are redundant there, Unicode \b of WORD /
// HOSTNAME / MONTH is implied by the neighbours, SYSLOGFACILITY's \b pair becomes "non-word, non-newline separator".
// The remaining \b (POSINT, NONNEGINT) sit between ASCII literals and stay RE2 \b (TranslateKeepB).
var grokGoBase = map[string]string{
	"BASE10NUM":      `(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+)))`,
	"BASE16NUM":      `(?:[+-]?(?:0x)?(?:[0-9A-Fa-f]+))`,
	"WORD":           `\w+`,
	"IPV4":           `(?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5]))`,
	"HOSTNAME":       `(?:[0-9A-Za-z][0-9A-Za-z-]{0,62})(?:\.(?:[0-9A-Za-z][0-9A-Za-z-]{0,62}))*\.?`,
	"MONTH":          `(?:[Jj]an(?:uary|uar)?|[Ff]eb(?:ruary|ruar)?|[Mm](?:a|ä)?r(?:ch|z)?|[Aa]pr(?:il)?|[Mm]a(?:y|i)?|[Jj]un(?:e|i)?|[Jj]ul(?:y|i)?|[Aa]ug(?:ust)?|[Ss]ep(?:tember)?|[Oo](?:c|k)?t(?:ober)?|[Nn]ov(?:ember)?|[Dd]e(?:c|z)(?:ember)?)`,
	"TIME":           `%{HOUR}:%{MINUTE}(?::%{SECOND})`,
	"SYSLOGFACILITY": `<(?P<facility>[0-9]+)[^\w\n](?P<priority>[0-9]+)>`,
}

// grokLineFix are line-level rewrites (P03 section 5.7.3): SSHD's trailing port \b is followed by free text.
var grokLineFix = map[string][2]string{
	"SSHD_AUTH": {`port %{POSINT:port}(?: ssh2)?`, `port (?P<port>[1-9][0-9]*)(?:[^\w]|$)`},
}

// grokSkip: DHCPD can never match in Python (WORD's \b between "DHCP" and a word character), so it is left out.
var grokSkip = map[string]bool{"DHCPD": true}

var grokSubRE = regexp.MustCompile(`%\{(\w+)(?::(\w+))?(?::(\w+))?\}`)

// grokExpand is grok.expand(pattern) with lib as BASE: %{X} -> (?:...), %{X:f} -> (?P<f>...), one left-to-right pass,
// recursive; "(?>" becomes "(?:" at every level. Panics on an unknown name or nesting deeper than 20 (built-ins only).
func grokExpand(p string, lib map[string]string, depth int) string {
	if depth > 20 {
		panic("grok: pattern nests too deep")
	}
	out := grokSubRE.ReplaceAllStringFunc(p, func(s string) string {
		m := grokSubRE.FindStringSubmatch(s)
		body, ok := lib[m[1]]
		if !ok {
			panic("grok: unknown pattern %{" + m[1] + "}")
		}
		inner := grokExpand(body, lib, depth+1)
		if m[2] != "" {
			return "(?P<" + m[2] + ">" + inner + ")"
		}
		return "(?:" + inner + ")"
	})
	return strings.ReplaceAll(out, "(?>", "(?:")
}

// grokGoSource returns the RE2 source of a built-in line pattern (overrides, line fix, expansion, class translation).
func grokGoSource(name, pat string) string {
	lib := make(map[string]string, len(grokBase))
	for k, v := range grokBase {
		lib[k] = v
	}
	for k, v := range grokGoBase {
		lib[k] = v
	}
	if f, ok := grokLineFix[name]; ok {
		if !strings.Contains(pat, f[0]) {
			panic("grok: line fix not applicable to " + name)
		}
		pat = strings.Replace(pat, f[0], f[1], 1)
	}
	return py.TranslateKeepB(grokExpand(pat, lib, 0))
}

// grokLines is all_patterns() for an empty home: the enabled built-ins in LINES order, compiled once (DHCPD left out).
var grokLines = func() []*linePattern {
	out := make([]*linePattern, 0, len(grokLineSrc))
	for _, d := range grokLineSrc {
		if grokSkip[d.Name] {
			continue
		}
		rx := regexp.MustCompile(grokGoSource(d.Name, d.Pattern))
		lp := &linePattern{Name: d.Name, Pattern: d.Pattern, Family: d.Family, Roles: d.Roles, rx: rx,
			names: rx.SubexpNames(), roleVal: map[string]bool{}}
		for _, r := range d.Roles {
			lp.roleVal[r[1]] = true
		}
		out = append(out, lp)
	}
	return out
}()

// grokDetect is grok.detect(lines, min_share=0.6) -> (pattern or nil, round(share, 2)): the pattern matching the
// largest share of the non-blank lines among lines[:120]; an earlier pattern keeps a tie; the 0.6 threshold is tested
// on the unrounded share.
func grokDetect(lines []string) (*linePattern, float64) {
	if len(lines) > 120 {
		lines = lines[:120]
	}
	sample := make([]string, 0, len(lines))
	for _, ln := range lines {
		if !isBlank(ln) {
			sample = append(sample, ln)
		}
	}
	if len(sample) == 0 {
		return nil, 0
	}
	var best *linePattern
	bestShare := 0.0
	for _, lp := range grokLines {
		hits := 0
		for _, ln := range sample {
			if lp.rx.MatchString(ln) {
				hits++
			}
		}
		if share := float64(hits) / float64(len(sample)); share > bestShare {
			best, bestShare = lp, share
		}
	}
	if best != nil && bestShare >= 0.6 {
		return best, py.Round(bestShare, 2)
	}
	return nil, 0
}
