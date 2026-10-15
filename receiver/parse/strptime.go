package parse

import (
	"regexp"
	"strings"
	"sync"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02 section 3.5): CPython _strptime emulation for the _FAST_FORMATS (C locale).

// fastFormats is normalize._FAST_FORMATS, tried in this order.
var fastFormats = [...]string{
	"%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
	"%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S",
	"%d/%b/%Y:%H:%M:%S %z", "%b %d %H:%M:%S", "%Y%m%d%H%M%S", "%b %d %Y %H:%M:%S", "%d %b %Y %H:%M:%S.%f", "%d %b %Y %H:%M:%S",
	"%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S %p", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z",
	"%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y.%m.%d %H:%M:%S",
}

// fmtSyslog is the index of "%b %d %H:%M:%S" (its result gets the current year).
const fmtSyslog = 11

// _strptime TimeRE directive regexes (C locale), \d / \s as Python Unicode classes; i in "fri" folds like Python re.I.
var strptimeDirectives = map[byte]string{
	'd': `(?P<d>3[0-1]|[1-2]` + py.D + `|0[1-9]|[1-9]| [1-9])`,
	'f': `(?P<f>[0-9]{1,6})`,
	'H': `(?P<H>2[0-3]|[0-1]` + py.D + `|` + py.D + `)`,
	'I': `(?P<I>1[0-2]|0[1-9]|[1-9])`,
	'm': `(?P<m>1[0-2]|0[1-9]|[1-9])`,
	'M': `(?P<M>[0-5]` + py.D + `|` + py.D + `)`,
	'S': `(?P<S>6[0-1]|[0-5]` + py.D + `|` + py.D + `)`,
	'Y': `(?P<Y>` + py.D + py.D + py.D + py.D + `)`,
	'z': `(?P<z>[+-]` + py.D + py.D + `:?[0-5]` + py.D + `(:?[0-5]` + py.D + `(\.` + py.D + `{1,6})?)?|(?-i:Z))`,
	'b': `(?P<b>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)`,
	'a': `(?P<a>mon|tue|wed|thu|fr` + py.I + `|sat|sun)`,
	'p': `(?P<p>am|pm)`,
}

var (
	monthAbbr   = []string{"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}
	weekdayAbbr = []string{"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
)

type stLayout struct {
	re  *regexp.Regexp
	idx map[string]int // group name -> submatch index
}

var (
	stMu      sync.Mutex
	stLayouts = map[string]*stLayout{}
	stFast    [len(fastFormats)]*stLayout
)

func init() {
	for i, f := range fastFormats {
		stFast[i] = compileStrptime(f)
	}
}

// compileStrptime is TimeRE.compile(format) with re.IGNORECASE, used with re.match (prefix, then an end check).
func compileStrptime(format string) *stLayout {
	var b strings.Builder
	b.WriteString(`(?i)^`)
	rs := []rune(format)
	for i := 0; i < len(rs); i++ {
		r := rs[i]
		switch {
		case r == '%' && i+1 < len(rs):
			i++
			d, ok := strptimeDirectives[byte(rs[i])]
			if !ok || rs[i] >= 0x80 {
				panic("parse: unsupported strptime directive %" + string(rs[i]))
			}
			b.WriteString(d)
		case py.IsSpace(r):
			for i+1 < len(rs) && py.IsSpace(rs[i+1]) {
				i++
			}
			b.WriteString(py.PS + `+`)
		default:
			b.WriteString(regexp.QuoteMeta(string(r)))
		}
	}
	re := regexp.MustCompile(b.String())
	l := &stLayout{re: re, idx: map[string]int{}}
	for i, n := range re.SubexpNames() {
		if n != "" {
			l.idx[n] = i
		}
	}
	return l
}

// strptimeFast is datetime.strptime(s, fastFormats[f]): (datetime, true, subsecOffset) or (_, false, _) for ValueError.
func strptimeFast(s string, f int) (py.DateTime, bool, bool) { return stFast[f].parse(s) }

// strptime is datetime.strptime(s, fastFormats[f]) (offsets with microseconds count as failures here).
func strptime(s string, f int) (py.DateTime, bool) {
	dt, ok, sub := strptimeFast(s, f)
	return dt, ok && !sub
}

// strptimeLayout is datetime.strptime(s, layout) for any layout made of the supported directives.
func strptimeLayout(s, layout string) (py.DateTime, bool, bool) {
	stMu.Lock()
	l := stLayouts[layout]
	if l == nil {
		l = compileStrptime(layout)
		stLayouts[layout] = l
	}
	stMu.Unlock()
	return l.parse(s)
}

func (l *stLayout) parse(s string) (py.DateTime, bool, bool) {
	m := l.re.FindStringSubmatchIndex(s)
	if m == nil || m[1] != len(s) {
		return py.DateTime{}, false, false // no match / unconverted data remains
	}
	group := func(name string) (string, bool) {
		i, ok := l.idx[name]
		if !ok || m[2*i] < 0 {
			return "", false
		}
		return s[m[2*i]:m[2*i+1]], true
	}
	atoi := func(t string) int64 {
		n, err := py.Atoi(t)
		if err != nil || !n.IsInt64() {
			return -1
		}
		return n.Int64()
	}
	var year int64 = -1
	month, day := int64(1), int64(1)
	var hour, minute, second, frac int64
	var gmtoff, gmtfrac int64
	hasOff := false
	if g, ok := group("Y"); ok {
		year = atoi(g)
	}
	if g, ok := group("m"); ok {
		month = atoi(g)
	}
	if g, ok := group("b"); ok {
		i := indexOf(monthAbbr, py.Lower(g))
		if i < 0 {
			return py.DateTime{}, false, false
		}
		month = int64(i + 1)
	}
	if g, ok := group("d"); ok {
		day = atoi(g)
	}
	if g, ok := group("H"); ok {
		hour = atoi(g)
	}
	if g, ok := group("I"); ok {
		hour = atoi(g)
		p, _ := group("p")
		switch py.Lower(p) {
		case "", "am":
			if hour == 12 {
				hour = 0
			}
		case "pm":
			if hour != 12 {
				hour += 12
			}
		}
	}
	if g, ok := group("M"); ok {
		minute = atoi(g)
	}
	if g, ok := group("S"); ok {
		second = atoi(g)
	}
	if g, ok := group("f"); ok {
		frac = atoi(g + strings.Repeat("0", 6-len(g)))
	}
	if g, ok := group("a"); ok {
		if indexOf(weekdayAbbr, py.Lower(g)) < 0 {
			return py.DateTime{}, false, false
		}
	}
	if g, ok := group("z"); ok {
		hasOff = true
		if g != "Z" {
			z := []rune(g)
			if z[3] == ':' {
				z = append(z[:3:3], z[4:]...)
				if len(z) > 5 {
					if z[5] != ':' {
						return py.DateTime{}, false, false
					}
					z = append(z[:5:5], z[6:]...)
				}
			}
			hours := atoi(string(z[1:3]))
			minutes := atoi(string(z[3:5]))
			secs := int64(0)
			if len(z) > 5 {
				e := 7
				if e > len(z) {
					e = len(z)
				}
				secs = atoi(string(z[5:e]))
			}
			if hours < 0 || minutes < 0 || secs < 0 {
				return py.DateTime{}, false, false // int() ValueError (e.g. ":3")
			}
			gmtoff = hours*3600 + minutes*60 + secs
			rem := ""
			if len(z) > 8 {
				rem = string(z[8:])
			}
			gmtfrac = atoi(rem + strings.Repeat("0", 6-len([]rune(rem))))
			if z[0] == '-' {
				gmtoff, gmtfrac = -gmtoff, -gmtfrac
			}
		}
	}
	if year < 0 {
		year = 1900 // the Feb 29 leap-year fix only changes the julian computation; the year is reset to 1900
	}
	if !validDateTime(year, month, day, hour, minute, second, frac) {
		return py.DateTime{}, false, false
	}
	var off *int64
	subsec := false
	if hasOff {
		total := gmtoff*1_000_000 + gmtfrac
		if total <= -86400*1_000_000 || total >= 86400*1_000_000 {
			return py.DateTime{}, false, false // timezone(): strictly between -24h and 24h
		}
		o := floorDiv(total, 1_000_000)
		off = &o
		subsec = total%1_000_000 != 0
	}
	return mkDateTime(year, month, day, hour, minute, second, frac, off), true, subsec
}

func indexOf(xs []string, s string) int {
	for i, x := range xs {
		if x == s {
			return i
		}
	}
	return -1
}
