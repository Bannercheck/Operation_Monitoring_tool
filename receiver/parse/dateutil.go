package parse

import (
	"math/big"
	"strings"
	"unicode"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02 section 3.7): dateutil.parser.parse fallback, ported from python-dateutil 2.9.0.post0
// (_timelex.split, parser._parse, _parse_numeric_token, _ymd.resolve_ymd, parserinfo.validate, _build_naive,
// _build_tzaware) for the fixed call parse(s, default=datetime(year, 1, 1), dayfirst=D) with fuzzy=False,
// yearfirst=False, no tzinfos, English parserinfo. Every exception that normalize.py catches (ValueError,
// OverflowError, TypeError, ParserError) becomes "no result". The host time zone is taken to be UTC
// (time.tzname == ('UTC', 'UTC')); results that would depend on it carry tsHostTZ.

// dateutilParse returns the parse result (ok == false is None). The datetime is naive unless the string carried an
// offset or a UTC name.
func dateutilParse(s string, year int, dayfirst bool) (r tsResult, err error) {
	defer func() {
		if e := recover(); e != nil {
			switch e.(type) {
			case duFail:
				r, err = tsResult{}, nil
			case duRaise:
				r, err = tsResult{}, pyRaise("decimal.InvalidOperation in dateutil (%q)", s)
			default:
				panic(e)
			}
		}
	}()
	p := &duParser{l: duSplit(s), dayfirst: dayfirst, curYear: int64(year)}
	p.ymd.ystridx, p.ymd.mstridx, p.ymd.dstridx = -1, -1, -1
	p.parse()
	return p.build(int64(year)), nil
}

// duFail is a ValueError / IndexError inside _parse (-> None) or a later ParserError / OverflowError.
type duFail struct{}

func fail() { panic(duFail{}) }

// ---- _timelex ----

func duIsWord(r rune) bool  { return unicode.IsLetter(r) }
func duIsNum(r rune) bool   { return py.IsDigitStr(string(r)) }
func duIsSpace(r rune) bool { return py.IsSpace(r) }

// duSplit is _timelex.split(s).
func duSplit(s string) []string {
	rs := []rune(strings.ReplaceAll(s, "\x00", "")) // NULs are skipped when read from the stream
	pos := 0
	eof := false
	var stack, out []string
	for {
		if len(stack) > 0 {
			out = append(out, stack[0])
			stack = stack[1:]
			continue
		}
		seenLetters := false
		var tok []rune
		state := byte(0) // 0 none, 'a', '0', 'A' (a.), 'N' (0.)
		have := false
		for !eof {
			if pos >= len(rs) {
				eof = true
				break
			}
			c := rs[pos]
			pos++
			if state == 0 {
				tok = []rune{c}
				have = true
				if duIsWord(c) {
					state = 'a'
				} else if duIsNum(c) {
					state = '0'
				} else if duIsSpace(c) {
					tok = []rune{' '}
					break
				} else {
					break
				}
			} else if state == 'a' {
				seenLetters = true
				if duIsWord(c) {
					tok = append(tok, c)
				} else if c == '.' {
					tok = append(tok, c)
					state = 'A'
				} else {
					pos--
					break
				}
			} else if state == '0' {
				if duIsNum(c) {
					tok = append(tok, c)
				} else if c == '.' || (c == ',' && len(tok) >= 2) {
					tok = append(tok, c)
					state = 'N'
				} else {
					pos--
					break
				}
			} else if state == 'A' {
				seenLetters = true
				if c == '.' || duIsWord(c) {
					tok = append(tok, c)
				} else if duIsNum(c) && tok[len(tok)-1] == '.' {
					tok = append(tok, c)
					state = 'N'
				} else {
					pos--
					break
				}
			} else { // 'N'
				if c == '.' || duIsNum(c) {
					tok = append(tok, c)
				} else if duIsWord(c) && tok[len(tok)-1] == '.' {
					tok = append(tok, c)
					state = 'A'
				} else {
					pos--
					break
				}
			}
		}
		if !have {
			return out
		}
		token := string(tok)
		if (state == 'A' || state == 'N') &&
			(seenLetters || strings.Count(token, ".") > 1 || tok[len(tok)-1] == '.' || tok[len(tok)-1] == ',') {
			parts := splitDecimal(token)
			token = parts[0]
			for _, t := range parts[1:] {
				if t != "" {
					stack = append(stack, t)
				}
			}
		}
		if state == 'N' && !strings.Contains(token, ".") {
			token = strings.ReplaceAll(token, ",", ".")
		}
		out = append(out, token)
	}
}

// splitDecimal is re.split("([.,])", token).
func splitDecimal(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '.' || s[i] == ',' {
			out = append(out, s[start:i], s[i:i+1])
			start = i + 1
		}
	}
	return append(out, s[start:])
}

// ---- parserinfo (English) ----

var (
	duJump = map[string]bool{" ": true, ".": true, ",": true, ";": true, "-": true, "/": true, "'": true,
		"at": true, "on": true, "and": true, "ad": true, "m": true, "t": true, "of": true,
		"st": true, "nd": true, "rd": true, "th": true}
	duWeekdays = map[string]int64{"mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
		"thu": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
	duMonths = map[string]int64{"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
		"apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
		"sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
		"dec": 12, "december": 12}
	duHMS     = map[string]int64{"h": 0, "hour": 0, "hours": 0, "m": 1, "minute": 1, "minutes": 1, "s": 2, "second": 2, "seconds": 2}
	duAMPM    = map[string]int64{"am": 0, "a": 0, "pm": 1, "p": 1}
	duUTCLow  = map[string]bool{"utc": true, "gmt": true, "z": true}
	duUTCZone = map[string]bool{"UTC": true, "GMT": true, "Z": true, "z": true}
)

func duIsJump(s string) bool { return duJump[py.Lower(s)] }

func duLookup(m map[string]int64, s string) (int64, bool) {
	v, ok := m[py.Lower(s)]
	return v, ok
}

// ---- numbers ----

// duSat bounds Python ints: every value this large ends as an invalid datetime field or an overflowing offset.
const duSat = int64(1) << 40

// duInt is int(s) (ValueError -> fail), saturated.
func duInt(s string) int64 {
	n, err := py.Atoi(s)
	if err != nil {
		fail()
	}
	if !n.IsInt64() || n.Int64() > duSat {
		return duSat
	}
	return n.Int64()
}

// duFloatOK is float(token) succeeding (the main loop's numeric test).
func duFloatOK(s string) bool {
	_, err := py.ParseFloat(s)
	return err == nil
}

// duDecimal is _to_decimal(token): Decimal(token), non-finite -> ValueError.
func duDecimal(s string) *big.Rat {
	t := py.ToASCIIDigits(s)
	switch strings.ToLower(t) {
	case "inf", "infinity", "nan", "+inf", "-inf", "+infinity", "-infinity", "+nan", "-nan":
		fail()
	}
	for _, c := range t {
		if !(c >= '0' && c <= '9' || c == '.' || c == '+' || c == '-' || c == 'e' || c == 'E') {
			fail()
		}
	}
	r, ok := new(big.Rat).SetString(t)
	if !ok {
		fail()
	}
	return r
}

// ratInt is int(Decimal) (truncation toward zero), saturated.
func ratInt(r *big.Rat) int64 {
	q := new(big.Int).Quo(r.Num(), r.Denom())
	if !q.IsInt64() || q.Int64() > duSat {
		return duSat
	}
	if q.Int64() < -duSat {
		return -duSat
	}
	return q.Int64()
}

// decScale is the number of fraction digits of a numeric token (the Decimal exponent is -decScale).
func decScale(tok string) int {
	if i := strings.IndexByte(tok, '.'); i >= 0 {
		return py.Len(tok[i+1:])
	}
	return 0
}

// round28 rounds value (an exact multiple of 10**-scale) to 28 significant digits, half-even (Decimal._fix with the
// default context).
func round28(v *big.Rat, scale int) *big.Rat {
	pow := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(scale)), nil)
	coef := new(big.Int).Mul(v.Num(), pow)
	coef.Quo(coef, v.Denom()) // exact
	digits := len(new(big.Int).Abs(coef).String())
	if digits <= 28 {
		return v
	}
	d := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(digits-28)), nil)
	q, r := new(big.Int).QuoRem(coef, d, new(big.Int))
	twice := new(big.Int).Mul(new(big.Int).Abs(r), big.NewInt(2))
	if c := twice.Cmp(d); c > 0 || (c == 0 && q.Bit(0) == 1) {
		if coef.Sign() < 0 {
			q.Sub(q, big.NewInt(1))
		} else {
			q.Add(q, big.NewInt(1))
		}
	}
	q.Mul(q, d)
	return new(big.Rat).SetFrac(q, pow)
}

// decMod1 is Decimal % 1: decimal.InvalidOperation (DivisionImpossible, not caught anywhere -> the ingest fails)
// when the integer quotient has more than 28 digits; the remainder is rounded to 28 significant digits.
func decMod1(r *big.Rat, scale int) *big.Rat {
	q := new(big.Int).Quo(r.Num(), r.Denom())
	if len(new(big.Int).Abs(q).String()) > 28 {
		panic(duRaise{})
	}
	return round28(new(big.Rat).Sub(r, new(big.Rat).SetInt(q)), scale)
}

// decMul60 is 60 * rem (rounded to 28 significant digits).
func decMul60(rem *big.Rat, scale int) *big.Rat {
	return round28(new(big.Rat).Mul(big.NewRat(60, 1), rem), scale)
}

// duRaise is an exception dateutil lets escape and normalize.py does not catch (decimal.InvalidOperation).
type duRaise struct{}

func ratCmp(r *big.Rat, v int64) int { return r.Cmp(new(big.Rat).SetInt64(v)) }

// ---- _ymd ----

type duYMD struct {
	vals                      []int64
	centurySpecified          bool
	dstridx, mstridx, ystridx int
}

func (y *duYMD) push(v int64, label byte) {
	y.vals = append(y.vals, v)
	switch label {
	case 'M':
		if y.mstridx >= 0 {
			fail()
		}
		y.mstridx = len(y.vals) - 1
	case 'D':
		if y.dstridx >= 0 {
			fail()
		}
		y.dstridx = len(y.vals) - 1
	case 'Y':
		if y.ystridx >= 0 {
			fail()
		}
		y.ystridx = len(y.vals) - 1
	}
}

// appendStr is _ymd.append(str_value, label).
func (y *duYMD) appendStr(s string, label byte) {
	if py.IsDigitStr(s) && py.Len(s) > 2 {
		y.centurySpecified = true
		if label != 0 && label != 'Y' {
			fail()
		}
		label = 'Y'
	}
	y.push(duInt(s), label)
}

// appendDec is _ymd.append(Decimal_value) (no label).
func (y *duYMD) appendDec(v *big.Rat) {
	var label byte
	if ratCmp(v, 100) > 0 {
		y.centurySpecified = true
		label = 'Y'
	}
	y.push(ratInt(v), label)
}

// appendMonth is _ymd.append(month_int, 'M').
func (y *duYMD) appendMonth(m int64) { y.push(m, 'M') }

func (y *duYMD) at(i int) int64 {
	if i < 0 {
		i += len(y.vals)
	}
	return y.vals[i]
}

func (y *duYMD) couldBeDay(v *big.Rat) bool {
	if y.dstridx >= 0 {
		return false
	}
	lo := ratCmp(v, 1) >= 0
	if y.mstridx < 0 {
		return lo && ratCmp(v, 31) <= 0
	}
	yr := int64(2000)
	if y.ystridx >= 0 {
		yr = y.vals[y.ystridx]
	}
	return lo && ratCmp(v, daysIn(yr, y.vals[y.mstridx])) <= 0
}

// resolve is _ymd.resolve_ymd(yearfirst=False, dayfirst); -1 = None.
func (y *duYMD) resolve(dayfirst bool) (int64, int64, int64) {
	n := len(y.vals)
	year, month, day := int64(-1), int64(-1), int64(-1)
	strids := map[byte]int{}
	if y.ystridx >= 0 {
		strids['y'] = y.ystridx
	}
	if y.mstridx >= 0 {
		strids['m'] = y.mstridx
	}
	if y.dstridx >= 0 {
		strids['d'] = y.dstridx
	}
	if (n == len(strids) && n > 0) || (n == 3 && len(strids) == 2) {
		if n == 3 && len(strids) == 2 {
			used := map[int]bool{}
			for _, v := range strids {
				used[v] = true
			}
			missing := -1
			for x := 0; x < 3; x++ {
				if !used[x] {
					missing = x
				}
			}
			for _, k := range []byte{'y', 'm', 'd'} {
				if _, ok := strids[k]; !ok {
					strids[k] = missing
				}
			}
		}
		if i, ok := strids['y']; ok {
			year = y.vals[i]
		}
		if i, ok := strids['m']; ok {
			month = y.vals[i]
		}
		if i, ok := strids['d']; ok {
			day = y.vals[i]
		}
		return year, month, day
	}
	ms := y.mstridx
	switch {
	case n > 3:
		fail()
	case n == 1 || (ms >= 0 && n == 2):
		var other int64
		if ms >= 0 {
			month = y.vals[ms]
			other = y.at(ms - 1)
		} else {
			other = y.vals[0]
		}
		if n > 1 || ms < 0 {
			if other > 31 {
				year = other
			} else {
				day = other
			}
		}
	case n == 2:
		a, b := y.vals[0], y.vals[1]
		switch {
		case a > 31:
			year, month = a, b
		case b > 31:
			month, year = a, b
		case dayfirst && b <= 12:
			day, month = a, b
		default:
			month, day = a, b
		}
	case n == 3:
		a, b, c := y.vals[0], y.vals[1], y.vals[2]
		switch ms {
		case 0:
			if b > 31 {
				month, year, day = a, b, c
			} else {
				month, day, year = a, b, c
			}
		case 1:
			if a > 31 {
				year, month, day = a, b, c
			} else {
				day, month, year = a, b, c
			}
		case 2:
			if b > 31 {
				day, year, month = a, b, c
			} else {
				year, day, month = a, b, c
			}
		default:
			if a > 31 || y.ystridx == 0 {
				if dayfirst && c <= 12 {
					year, day, month = a, b, c
				} else {
					year, month, day = a, b, c
				}
			} else if a > 12 || (dayfirst && b <= 12) {
				day, month, year = a, b, c
			} else {
				month, day, year = a, b, c
			}
		}
	}
	return year, month, day
}

// ---- parser._parse ----

type duParser struct {
	l        []string
	dayfirst bool
	curYear  int64
	ymd      duYMD

	year, month, day, weekday, hour, minute, second, usec, tzoffset, ampm                               int64
	hasYear, hasMonth, hasDay, hasWeekday, hasHour, hasMinute, hasSecond, hasUsec, hasTzoffset, hasAmpm bool
	tzname                                                                                              string
	hasTzname                                                                                           bool
	centurySpecified                                                                                    bool
}

func (p *duParser) tok(i int) string {
	if i < 0 || i >= len(p.l) {
		fail() // IndexError
	}
	return p.l[i]
}

func (p *duParser) setHour(v int64)   { p.hour, p.hasHour = v, true }
func (p *duParser) setMinute(v int64) { p.minute, p.hasMinute = v, true }

func (p *duParser) couldBeTzname(token string) bool {
	if !p.hasHour || p.hasTzname || p.hasTzoffset || py.Len(token) > 5 {
		return false
	}
	if duUTCZone[token] {
		return true
	}
	for _, c := range token {
		if c < 'A' || c > 'Z' {
			return false
		}
	}
	return true
}

func (p *duParser) parse() {
	l := p.l
	n := len(l)
	for i := 0; i < n; i++ {
		v := l[i]
		if duFloatOK(v) {
			i = p.parseNumeric(i)
		} else if wd, ok := duLookup(duWeekdays, v); ok {
			p.weekday, p.hasWeekday = wd, true
		} else if mo, ok := duLookup(duMonths, v); ok {
			p.ymd.appendMonth(mo)
			if i+1 < n {
				if l[i+1] == "-" || l[i+1] == "/" {
					sep := l[i+1]
					p.ymd.appendStr(p.tok(i+2), 0)
					if i+3 < n && l[i+3] == sep {
						p.ymd.appendStr(p.tok(i+4), 0)
						i += 2
					}
					i += 2
				} else if i+4 < n && l[i+1] == " " && l[i+3] == " " && py.Lower(l[i+2]) == "of" {
					if py.IsDigitStr(l[i+4]) {
						yv := p.convertYear(duInt(l[i+4]), false)
						p.ymd.appendStr(itoa(yv), 'Y')
					}
					i += 4
				}
			}
		} else if ap, ok := duLookup(duAMPM, v); ok {
			// _ampm_valid (fuzzy=False): no hour or hour outside 0..12 raises
			if !p.hasHour || p.hour < 0 || p.hour > 12 {
				fail()
			}
			p.setHour(adjustAMPM(p.hour, ap))
			p.ampm, p.hasAmpm = ap, true
		} else if p.couldBeTzname(v) {
			p.tzname, p.hasTzname = v, true
			if v == "z" { // info.tzoffset: name in the lower-case UTC dict
				p.tzoffset, p.hasTzoffset = 0, true
			}
			if i+1 < n && (l[i+1] == "+" || l[i+1] == "-") {
				if l[i+1] == "+" {
					l[i+1] = "-"
				} else {
					l[i+1] = "+"
				}
				p.hasTzoffset = false
				p.tzoffset = 0
				if duUTCLow[py.Lower(p.tzname)] {
					p.tzname, p.hasTzname = "", false
				}
			}
		} else if p.hasHour && (v == "+" || v == "-") {
			signal := int64(-1)
			if v == "+" {
				signal = 1
			}
			next := p.tok(i + 1)
			lenLi := py.Len(next)
			var ho, mo int64
			if lenLi == 4 {
				ho = duInt(py.Slice(next, 0, 2))
				mo = duInt(py.Slice(next, 2, lenLi))
			} else if i+2 < n && l[i+2] == ":" {
				ho = duInt(next)
				mo = duInt(p.tok(i + 3))
				i += 2
			} else if lenLi <= 2 {
				ho = duInt(py.Slice(next, 0, 2))
				mo = 0
			} else {
				fail()
			}
			p.tzoffset, p.hasTzoffset = signal*(ho*3600+mo*60), true
			if i+5 < n && duIsJump(l[i+2]) && l[i+3] == "(" && l[i+5] == ")" && py.Len(l[i+4]) >= 3 {
				saved := p.hasTzoffset
				p.hasTzoffset = false // _could_be_tzname(..., None, token)
				can := p.couldBeTzname(l[i+4])
				p.hasTzoffset = saved
				if can {
					p.tzname, p.hasTzname = l[i+4], true
					i += 4
				}
			}
			i++
		} else if !duIsJump(v) {
			fail()
		}
	}
	y, m, d := p.ymd.resolve(p.dayfirst)
	p.centurySpecified = p.ymd.centurySpecified
	if y >= 0 {
		p.year, p.hasYear = y, true
	}
	if m >= 0 {
		p.month, p.hasMonth = m, true
	}
	if d >= 0 {
		p.day, p.hasDay = d, true
	}
	// parserinfo.validate
	if p.hasYear {
		p.year = p.convertYear(p.year, p.centurySpecified)
	}
	if (p.hasTzoffset && p.tzoffset == 0 && !p.hasTzname) || (p.hasTzname && (p.tzname == "Z" || p.tzname == "z")) {
		p.tzname, p.hasTzname = "UTC", true
		p.tzoffset, p.hasTzoffset = 0, true
	} else if !(p.hasTzoffset && p.tzoffset == 0) && p.hasTzname && duUTCLow[py.Lower(p.tzname)] {
		p.tzoffset, p.hasTzoffset = 0, true
	}
}

func itoa(v int64) string { return big.NewInt(v).String() }

func adjustAMPM(hour, ampm int64) int64 {
	if hour < 12 && ampm == 1 {
		hour += 12
	} else if hour == 12 && ampm == 0 {
		hour = 0
	}
	return hour
}

// convertYear is parserinfo.convertyear (two-digit years within [-50, 49] of the current year).
func (p *duParser) convertYear(year int64, centurySpecified bool) int64 {
	if year < 100 && !centurySpecified {
		year += p.curYear / 100 * 100
		if year >= p.curYear+50 {
			year -= 100
		} else if year < p.curYear-50 {
			year += 100
		}
	}
	return year
}

// parsems is _parsems: "I[.F]" -> (seconds, microseconds).
func parsems(v string) (int64, int64) {
	if !strings.Contains(v, ".") {
		return duInt(v), 0
	}
	parts := strings.Split(v, ".")
	if len(parts) != 2 {
		fail() // unpacking ValueError
	}
	f := []rune(parts[1])
	for len(f) < 6 {
		f = append(f, '0')
	}
	return duInt(parts[0]), duInt(string(f[:6]))
}

// parseMinSec is _parse_min_sec(Decimal) (scale: the token's fraction digits).
func parseMinSec(v *big.Rat, scale int) (int64, int64, bool) {
	minute := ratInt(v)
	rem := decMod1(v, scale)
	if rem.Sign() != 0 {
		return minute, ratInt(decMul60(rem, scale)), true
	}
	return minute, 0, false
}

func (p *duParser) hms(i int) (int64, bool) {
	if i < 0 || i >= len(p.l) {
		return 0, false
	}
	return duLookup(duHMS, p.l[i])
}

// findHMSIdx is _find_hms_idx(allow_jump=True); -1 = None.
func (p *duParser) findHMSIdx(idx int) int {
	n := len(p.l)
	if _, ok := p.hms(idx + 1); idx+1 < n && ok {
		return idx + 1
	}
	if _, ok := p.hms(idx + 2); idx+2 < n && p.l[idx+1] == " " && ok {
		return idx + 2
	}
	if _, ok := p.hms(idx - 1); idx > 0 && ok {
		return idx - 1
	}
	if _, ok := p.hms(idx - 2); 1 < idx && idx == n-1 && p.l[idx-1] == " " && ok {
		return idx - 2
	}
	return -1
}

// parseNumeric is _parse_numeric_token; returns the new index.
func (p *duParser) parseNumeric(idx int) int {
	l := p.l
	n := len(l)
	repr := l[idx]
	value := duDecimal(repr)
	lenLi := py.Len(repr)
	if _, hmsNext := p.hms(idx + 1); len(p.ymd.vals) == 3 && (lenLi == 2 || lenLi == 4) && !p.hasHour &&
		(idx+1 >= n || (l[idx+1] != ":" && !hmsNext)) {
		p.setHour(duInt(py.Slice(repr, 0, 2)))
		if lenLi == 4 {
			p.setMinute(duInt(py.Slice(repr, 2, lenLi)))
		}
	} else if lenLi == 6 || (lenLi > 6 && runeIndex(repr, '.') == 6) {
		if len(p.ymd.vals) == 0 && !strings.Contains(repr, ".") {
			p.ymd.appendStr(py.Slice(repr, 0, 2), 0)
			p.ymd.appendStr(py.Slice(repr, 2, 4), 0)
			p.ymd.appendStr(py.Slice(repr, 4, lenLi), 0)
		} else {
			p.setHour(duInt(py.Slice(repr, 0, 2)))
			p.setMinute(duInt(py.Slice(repr, 2, 4)))
			p.second, p.usec = parsems(py.Slice(repr, 4, lenLi))
			p.hasSecond, p.hasUsec = true, true
		}
	} else if lenLi == 8 || lenLi == 12 || lenLi == 14 {
		p.ymd.appendStr(py.Slice(repr, 0, 4), 'Y')
		p.ymd.appendStr(py.Slice(repr, 4, 6), 0)
		p.ymd.appendStr(py.Slice(repr, 6, 8), 0)
		if lenLi > 8 {
			p.setHour(duInt(py.Slice(repr, 8, 10)))
			p.setMinute(duInt(py.Slice(repr, 10, 12)))
			if lenLi > 12 {
				p.second, p.hasSecond = duInt(py.Slice(repr, 12, lenLi)), true
			}
		}
	} else if hi := p.findHMSIdx(idx); hi >= 0 {
		var hmsv int64
		if hi > idx {
			hmsv, _ = p.hms(hi)
			idx = hi
		} else {
			h, _ := p.hms(hi)
			hmsv = h + 1
		}
		// _assign_hms(res, value_repr, hms)
		val := duDecimal(repr)
		switch hmsv {
		case 0:
			p.setHour(ratInt(val))
			if rem := decMod1(val, decScale(repr)); rem.Sign() != 0 {
				p.setMinute(ratInt(decMul60(rem, decScale(repr))))
			}
		case 1:
			mi, se, hasSe := parseMinSec(val, decScale(repr))
			p.setMinute(mi)
			p.second, p.hasSecond = se, hasSe
		case 2:
			p.second, p.usec = parsems(repr)
			p.hasSecond, p.hasUsec = true, true
		}
	} else if idx+2 < n && l[idx+1] == ":" {
		p.setHour(ratInt(value))
		v2 := duDecimal(l[idx+2])
		mi, se, hasSe := parseMinSec(v2, decScale(l[idx+2]))
		p.setMinute(mi)
		p.second, p.hasSecond = se, hasSe
		if idx+4 < n && l[idx+3] == ":" {
			p.second, p.usec = parsems(l[idx+4])
			p.hasSecond, p.hasUsec = true, true
			idx += 2
		}
		idx += 2
	} else if idx+1 < n && (l[idx+1] == "-" || l[idx+1] == "/" || l[idx+1] == ".") {
		sep := l[idx+1]
		p.ymd.appendStr(repr, 0)
		if idx+2 < n && !duIsJump(l[idx+2]) {
			if py.IsDigitStr(l[idx+2]) {
				p.ymd.appendStr(l[idx+2], 0)
			} else if mo, ok := duLookup(duMonths, l[idx+2]); ok {
				p.ymd.appendMonth(mo)
			} else {
				fail()
			}
			if idx+3 < n && l[idx+3] == sep {
				t4 := p.tok(idx + 4)
				if mo, ok := duLookup(duMonths, t4); ok {
					p.ymd.appendMonth(mo)
				} else {
					p.ymd.appendStr(t4, 0)
				}
				idx += 2
			}
			idx++
		}
		idx++
	} else if idx+1 >= n || duIsJump(l[idx+1]) {
		ap, isAMPM := int64(0), false
		if idx+2 < n {
			ap, isAMPM = duLookup(duAMPM, l[idx+2])
		}
		if isAMPM {
			p.setHour(adjustAMPM(ratInt(value), ap)) // "12 am"
			idx++
		} else {
			p.ymd.appendDec(value) // year, month or day
		}
		idx++
	} else if ap, ok := duLookup(duAMPM, l[idx+1]); ok && value.Sign() >= 0 && ratCmp(value, 24) < 0 {
		p.setHour(adjustAMPM(ratInt(value), ap))
		idx++
	} else if p.ymd.couldBeDay(value) {
		p.ymd.appendDec(value)
	} else {
		fail()
	}
	return idx
}

func runeIndex(s string, r rune) int {
	i := 0
	for _, c := range s {
		if c == r {
			return i
		}
		i++
	}
	return -1
}

// build is parser.parse after _parse: len(res) check, _build_naive, _build_tzaware.
func (p *duParser) build(defYear int64) tsResult {
	if !(p.hasYear || p.hasMonth || p.hasDay || p.hasWeekday || p.hasHour || p.hasMinute || p.hasSecond ||
		p.hasUsec || p.hasTzname || p.hasTzoffset || p.hasAmpm) {
		fail() // "String does not contain a date"
	}
	y, mo, d := defYear, int64(1), int64(1)
	var h, mi, s, us int64
	if p.hasYear {
		y = p.year
	}
	if p.hasMonth {
		mo = p.month
	}
	if p.hasDay {
		d = p.day
	}
	if p.hasHour {
		h = p.hour
	}
	if p.hasMinute {
		mi = p.minute
	}
	if p.hasSecond {
		s = p.second
	}
	if p.hasUsec {
		us = p.usec
	}
	if !validDateTime(y, mo, d, h, mi, s, us) {
		fail()
	}
	if p.hasWeekday && (!p.hasDay || p.day == 0) {
		// relativedelta(weekday=wd): forward to the next such weekday (same day if it already is)
		o := ordinal(y, mo, d)
		wd := (o + 6) % 7 // 0 = Monday
		o += (7 - wd + p.weekday) % 7
		y, mo, d = fromOrdinal(o)
		if y > 9999 {
			fail() // OverflowError: date value out of range
		}
	}
	r := tsResult{ok: true}
	switch {
	case p.hasTzname && p.tzname == "UTC": // in time.tzname (UTC host): tzlocal() == UTC
		z := int64(0)
		r.dt = mkDateTime(y, mo, d, h, mi, s, us, &z)
	case p.hasTzoffset && p.tzoffset == 0:
		z := int64(0)
		r.dt = mkDateTime(y, mo, d, h, mi, s, us, &z)
	case p.hasTzoffset && p.tzoffset != 0:
		off := p.tzoffset
		// datetime.timedelta(seconds=off): |days| <= 999999999, else OverflowError
		if off >= 1_000_000_000*86400 || off < -999_999_999*86400 {
			fail()
		}
		if off <= -86400 || off >= 86400 {
			r.flag = tsBadOffset
			r.dt = mkDateTime(y, mo, d, h, mi, s, us, nil)
			r.dt.Naive = false
			return r
		}
		r.dt = mkDateTime(y, mo, d, h, mi, s, us, &off)
	default:
		r.dt = mkDateTime(y, mo, d, h, mi, s, us, nil) // naive (tz name not understood -> dropped)
	}
	if p.hasTzname && p.tzname != "UTC" {
		r.flag = tsHostTZ
	}
	return r
}
