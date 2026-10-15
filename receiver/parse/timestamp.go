package parse

import (
	"regexp"
	"strings"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02 sections 3.3-3.6).

var (
	compactISORE = regexp.MustCompile(`^` + py.D + `{8}T` + py.D + `{6}(?:\.` + py.D + `+)?Z?$`) // step B (fullmatch)
	epochFracRE  = regexp.MustCompile(`^` + py.D + `{10}\.` + py.D + `+$`)                       // step C (fullmatch)
	pgOffsetRE   = regexp.MustCompile(` [+\-]` + py.D + `{2}$`)                                  // step D (search)
	commaMsRE    = regexp.MustCompile(py.D + `,` + py.D + `{3}$`)                                // step G (search)
	dayFirstRE   = regexp.MustCompile(`^` + py.D + `{2}[./]` + py.D + `{2}[./]` + py.D + `{4}`)  // step G (re.match)
	isoFracRE    = regexp.MustCompile(`^(.*?\.` + py.D + `{6})` + py.D + `+(.*)$`)               // _from_iso truncation
)

// runeAt returns the code point at code-point index i (or -1).
func runeAt(s string, i int) rune {
	for _, r := range s {
		if i == 0 {
			return r
		}
		i--
	}
	return -1
}

// parseTimestampStr is normalize._parse_timestamp_str(s) (P02 section 3.3); s is stripped and non-empty.
func (n *Normalizer) parseTimestampStr(s string) (tsResult, error) {
	ok := func(dt py.DateTime) (tsResult, error) { return tsResult{dt: aware(dt), ok: true}, nil }
	// A. digit strings: epochs and %Y%m%d%H%M%S
	if py.IsDigitStr(s) {
		switch py.Len(s) {
		case 13, 10, 16, 19:
			num, err := py.Atoi(s)
			if err != nil {
				return tsResult{}, pyRaise("ValueError: invalid literal for int(): %q", s)
			}
			var dt py.DateTime
			switch py.Len(s) {
			case 13:
				dt, err = fromTimestampRat(num, 1000)
			case 10:
				dt, err = fromTimestampInt(num)
			case 16:
				dt, err = fromTimestampRat(num, 1_000_000)
			default:
				dt, err = fromTimestampRat(num, 1_000_000_000)
			}
			if err != nil {
				return tsResult{}, err
			}
			return ok(dt)
		case 14:
			dt, good, _ := strptimeLayout(s, "%Y%m%d%H%M%S")
			if !good {
				return tsResult{}, pyRaise("ValueError: time data %q does not match format '%%Y%%m%%d%%H%%M%%S'", s)
			}
			return ok(dt)
		}
	}
	// B. compact ISO 20260922T091700[.fff][Z]: fraction and Z dropped
	if compactISORE.MatchString(s) {
		dt, good, _ := strptimeLayout(py.Head(s, 15), "%Y%m%dT%H%M%S")
		if !good {
			return tsResult{}, pyRaise("ValueError: time data %q does not match format '%%Y%%m%%dT%%H%%M%%S'", s)
		}
		return ok(dt)
	}
	// C. epoch seconds with a fraction
	if epochFracRE.MatchString(s) {
		f, err := py.ParseFloat(s)
		if err != nil {
			return tsResult{}, pyRaise("ValueError: could not convert string to float: %q", s)
		}
		dt, err := fromTimestampFloat(f)
		if err != nil {
			return tsResult{}, err
		}
		return ok(dt)
	}
	// D. PostgreSQL " +03" -> " +0300"
	if pgOffsetRE.MatchString(s) {
		s += "00"
	}
	var dt py.DateTime
	found := false
	// E. fromisoformat
	if py.Len(s) >= 19 && runeAt(s, 4) == '-' && runeAt(s, 7) == '-' {
		d, good, subsec := fromISOx(s)
		if good {
			if subsec {
				return tsResult{dt: d, ok: true, flag: tsSubSecOffset}, nil
			}
			dt, found = d, true
		}
	}
	// F. shape-cached strptime formats
	if !found {
		sh := shape(s)
		n.mu.Lock()
		fi, cached := n.formatCache[sh]
		n.mu.Unlock()
		used := -1
		subsec := false
		if cached {
			if d, good, sub := strptimeFast(s, fi); good {
				dt, found, used, subsec = d, true, fi, sub
			}
		}
		if !found {
			for i := range fastFormats {
				if d, good, sub := strptimeFast(s, i); good {
					dt, found, used, subsec = d, true, i, sub
					n.mu.Lock()
					n.formatCache[sh] = i
					n.mu.Unlock()
					break
				}
			}
		}
		if found && used == fmtSyslog {
			dt = withYear(dt, n.nowYear())
		}
		if subsec {
			return tsResult{dt: dt, ok: true, flag: tsSubSecOffset}, nil
		}
	}
	// G. dateutil
	if !found {
		s2 := s
		if commaMsRE.MatchString(s) {
			s2 = strings.Replace(s, ",", ".", 1)
		}
		r, err := dateutilParse(s2, n.Year, dayFirstRE.MatchString(s))
		if err != nil {
			return tsResult{}, err
		}
		if !r.ok {
			return tsResult{}, nil
		}
		r.dt = aware(r.dt)
		return r, nil
	}
	return ok(dt)
}

// withYear is dt.replace(year=y) (Feb 29 cannot reach here: strptime already rejects it for 1900).
func withYear(dt py.DateTime, y int) py.DateTime {
	t := dt.T
	return py.DateTime{T: t.AddDate(y-t.Year(), 0, 0), Naive: dt.Naive}
}

// fromISO is normalize._from_iso(s) (CPython 3.11 datetime.fromisoformat, P02 section 3.4).
func fromISO(s string) (py.DateTime, bool) {
	dt, ok, subsec := fromISOx(s)
	return dt, ok && !subsec
}

// fromISOx is _from_iso; subsec reports an offset with microseconds (dt then carries the offset rounded down to seconds).
func fromISOx(s string) (py.DateTime, bool, bool) {
	if strings.HasSuffix(s, "Z") || strings.HasSuffix(s, "z") {
		s = s[:len(s)-1] + "+00:00"
	}
	if strings.Contains(s, ".") {
		if m := isoFracRE.FindStringSubmatch(s); m != nil {
			s = m[1] + m[2]
		}
	}
	return cFromISOFormat(s)
}

// cFromISOFormat emulates CPython 3.11 _datetimemodule.c datetime_fromisoformat on the UTF-8 bytes of s
// (a NUL terminator is simulated: embedded NULs behave as in C).
func cFromISOFormat(s string) (py.DateTime, bool, bool) {
	if utf8.RuneCountInString(s) < 7 {
		return py.DateTime{}, false, false
	}
	b := []byte(s)
	at := func(i int) byte {
		if i < len(b) {
			return b[i]
		}
		return 0
	}
	sep := cFindSeparator(b, at)
	if sep < 0 {
		return py.DateTime{}, false, false
	}
	// parse_isoformat_date: only the YYYY-MM-DD / YYYYMMDD forms (week dates fail or never pass the caller's gate)
	p := 0
	year, okY := cDigits(at, &p, 4)
	if !okY {
		return py.DateTime{}, false, false
	}
	usesSep := at(p) == '-'
	if usesSep {
		p++
	}
	if at(p) == 'W' {
		y, m, d, ok := cISOWeekDate(at, p, sep, year, usesSep)
		if !ok {
			return py.DateTime{}, false, false
		}
		return cBuild(b, sep, y, m, d)
	}
	month, okM := cDigits(at, &p, 2)
	if !okM {
		return py.DateTime{}, false, false
	}
	if usesSep {
		c := at(p)
		p++
		if c != '-' {
			return py.DateTime{}, false, false
		}
	}
	day, okD := cDigits(at, &p, 2)
	if !okD {
		return py.DateTime{}, false, false
	}
	return cBuild(b, sep, year, month, day)
}

// cISOWeekDate parses YYYY-Www[-D] (parse_isoformat_date's 'W' branch) into a calendar date.
func cISOWeekDate(at func(int) byte, p, length int, year int64, usesSep bool) (int64, int64, int64, bool) {
	p++ // 'W'
	week, ok := cDigits(at, &p, 2)
	if !ok {
		return 0, 0, 0, false
	}
	day := int64(1)
	if p < length {
		if usesSep {
			c := at(p)
			p++
			if c != '-' {
				return 0, 0, 0, false
			}
		}
		if day, ok = cDigits(at, &p, 1); !ok {
			return 0, 0, 0, false
		}
	}
	// iso_to_ymd
	if year < 1 || year > 9999 || week <= 0 || week >= 53 && !(week == 53 && isoHas53(year)) || day <= 0 || day >= 8 {
		return 0, 0, 0, false
	}
	jan4 := ordinal(year, 1, 4)
	firstMon := jan4 - (jan4+6)%7 // ordinal of the Monday of ISO week 1 (ordinal 1 = 0001-01-01, a Monday)
	o := firstMon + (week-1)*7 + day - 1
	y, m, d := fromOrdinal(o)
	if y < 1 || y > 9999 {
		return 0, 0, 0, false
	}
	return y, m, d, true
}

func isoHas53(year int64) bool {
	// a year has week 53 when Jan 1 is a Thursday, or a Wednesday in a leap year
	wd := (ordinal(year, 1, 1) + 6) % 7 // 0 = Monday
	return wd == 3 || (wd == 2 && isLeap(year))
}

// ordinal is date.toordinal().
func ordinal(y, m, d int64) int64 {
	yy := y - 1
	o := yy*365 + yy/4 - yy/100 + yy/400
	for i := int64(1); i < m; i++ {
		o += daysIn(y, i)
	}
	return o + d
}

func fromOrdinal(o int64) (int64, int64, int64) {
	t := py.NewNaive(1, 1, 1, 0, 0, 0, 0).T.AddDate(0, 0, int(o-1))
	return int64(t.Year()), int64(t.Month()), int64(t.Day())
}

// cFindSeparator is _find_isoformat_datetime_separator (byte positions).
func cFindSeparator(b []byte, at func(int) byte) int {
	n := len(b)
	if n == 7 {
		return 7
	}
	if at(4) == '-' {
		if at(5) == 'W' {
			if n < 8 {
				return -1
			}
			if n > 8 && at(8) == '-' {
				if n == 9 {
					return -1
				}
				if n > 10 && isASCIIDigit(at(10)) {
					return 8
				}
				return 10
			}
			return 8
		}
		return 10
	}
	if at(4) == 'W' {
		idx := 7
		for idx < n && isASCIIDigit(at(idx)) {
			idx++
		}
		if idx < 9 {
			return idx
		}
		if idx%2 == 0 {
			return 7
		}
		return 8
	}
	return 8
}

func isASCIIDigit(c byte) bool { return c >= '0' && c <= '9' }

// cDigits is parse_digits: exactly n ASCII digits at *p.
func cDigits(at func(int) byte, p *int, n int) (int64, bool) {
	var v int64
	for i := 0; i < n; i++ {
		c := at(*p)
		*p++
		if c < '0' || c > '9' {
			return 0, false
		}
		v = v*10 + int64(c-'0')
	}
	return v, true
}

// cBuild parses the time part after the separator (one UTF-8 character at byte sep) and validates the datetime.
func cBuild(b []byte, sep int, year, month, day int64) (py.DateTime, bool, bool) {
	var hour, minute, second, usec int64
	var offset *int64
	subsec := false
	if len(b) > sep {
		p := sep + 1
		if c := b[sep]; c&0x80 != 0 {
			switch c & 0xf0 {
			case 0xe0:
				p = sep + 3
			case 0xf0:
				p = sep + 4
			default:
				p = sep + 2
			}
		}
		if p > len(b) {
			p = len(b)
		}
		t := b[p:]
		rv, h, mi, se, us, off, tzus := cParseTime(t)
		if rv < 0 {
			return py.DateTime{}, false, false
		}
		hour, minute, second, usec = h, mi, se, us
		if rv == 1 {
			if off != 0 {
				total := off*1_000_000 + tzus
				if total <= -86400*1_000_000 || total >= 86400*1_000_000 {
					return py.DateTime{}, false, false // timezone(): offset must be strictly between -24h and 24h
				}
				if tzus != 0 {
					subsec = true
					o := floorDiv(total, 1_000_000)
					offset = &o
				} else {
					offset = &off
				}
			} else {
				z := int64(0)
				offset = &z
			}
		}
	}
	if !validDateTime(year, month, day, hour, minute, second, usec) {
		return py.DateTime{}, false, false
	}
	return mkDateTime(year, month, day, hour, minute, second, usec, offset), true, subsec
}

func floorDiv(a, b int64) int64 {
	q := a / b
	if (a%b != 0) && ((a < 0) != (b < 0)) {
		q--
	}
	return q
}

// cParseTime is parse_isoformat_time: rv < 0 error, 0 no offset, 1 with offset (seconds + signed microseconds).
func cParseTime(t []byte) (rv int, h, mi, s, us, off, tzus int64) {
	at := func(i int) byte {
		if i < len(t) {
			return t[i]
		}
		return 0
	}
	end := len(t)
	tz := 0
	for {
		c := at(tz)
		if c == 'Z' || c == '+' || c == '-' {
			break
		}
		tz++
		if tz >= end {
			break
		}
	}
	r := cParseHMSF(at, 0, tz, &h, &mi, &s, &us)
	if r < 0 {
		return r, 0, 0, 0, 0, 0, 0
	}
	if tz == end {
		if r == 1 {
			return -5, 0, 0, 0, 0, 0, 0
		}
		return 0, h, mi, s, us, 0, 0
	}
	if at(tz) == 'Z' {
		if at(tz+1) != 0 {
			return -5, 0, 0, 0, 0, 0, 0
		}
		return 1, h, mi, s, us, 0, 0
	}
	sign := int64(1)
	if at(tz) == '-' {
		sign = -1
	}
	var th, tm, ts, tu int64
	r = cParseHMSF(at, tz+1, end, &th, &tm, &ts, &tu)
	if r != 0 {
		return -5, 0, 0, 0, 0, 0, 0
	}
	return 1, h, mi, s, us, sign * (th*3600 + tm*60 + ts), sign * tu
}

// cParseHMSF is parse_hh_mm_ss_ff over [p, end) (reads past end like C, up to the NUL terminator).
func cParseHMSF(at func(int) byte, p, end int, hour, minute, second, usec *int64) int {
	*hour, *minute, *second, *usec = 0, 0, 0, 0
	vals := [3]*int64{hour, minute, second}
	hasSep := true
	for i := 0; i < 3; i++ {
		v, ok := cDigits(at, &p, 2)
		if !ok {
			return -3
		}
		*vals[i] = v
		c := at(p)
		p++
		if i == 0 {
			hasSep = c == ':'
		}
		if p >= end {
			if c != 0 {
				return 1
			}
			return 0
		} else if hasSep && c == ':' {
			continue
		} else if c == '.' || c == ',' {
			break
		} else if !hasSep {
			p--
		} else {
			return -4
		}
	}
	remains := end - p
	toParse := remains
	if remains >= 6 {
		toParse = 6
	}
	v, ok := cDigits(at, &p, toParse)
	if !ok {
		return -3
	}
	if toParse < 6 {
		v *= [5]int64{100000, 10000, 1000, 100, 10}[toParse-1]
	}
	*usec = v
	for isASCIIDigit(at(p)) {
		p++
	}
	if at(p) != 0 {
		return 1
	}
	return 0
}

// shape is normalize._shape(s): Unicode digits -> '9', then ASCII letters -> 'a' (P02 section 3.6).
func shape(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z'):
			b.WriteByte('a')
		case py.DigitValue(r) >= 0:
			b.WriteByte('9')
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}
