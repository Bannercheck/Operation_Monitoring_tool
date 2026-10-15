// Package py implements the Python 3.11 string, number, JSON and datetime semantics the parse port relies on
// (docs/go-parse/01-pipeline.md section 1, 02-normalize.md section 0). It knows nothing about logs.
package py

import (
	"errors"
	"math"
	"math/big"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// WS is the Python 3.11 str.isspace() set (29 code points), which is also regex \s on str patterns.
const WS = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \U00000085\U000000a0\U00001680\U00002000\U00002001\U00002002\U00002003\U00002004\U00002005" +
	"\U00002006\U00002007\U00002008\U00002009\U0000200a\U00002028\U00002029\U0000202f\U0000205f\U00003000"

// ErrValue is Python's ValueError from int()/float() on a malformed string.
var ErrValue = errors.New("python ValueError")

// IsSpace reports str.isspace() for one code point.
func IsSpace(r rune) bool {
	switch {
	case r <= 0x20:
		return r == ' ' || (r >= '\t' && r <= '\r') || (r >= 0x1c && r <= 0x1f)
	case r < 0x85:
		return false
	}
	switch r {
	case 0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

// Strip is str.strip() without arguments.
func Strip(s string) string { return strings.TrimFunc(s, IsSpace) }

// LStrip is str.lstrip().
func LStrip(s string) string { return strings.TrimLeftFunc(s, IsSpace) }

// RStrip is str.rstrip().
func RStrip(s string) string { return strings.TrimRightFunc(s, IsSpace) }

// StripChars is str.strip(chars): removes leading and trailing code points contained in chars.
func StripChars(s, chars string) string {
	return strings.TrimFunc(s, func(r rune) bool { return strings.ContainsRune(chars, r) })
}

// Split is str.split() without arguments: runs of Python whitespace separate, empty parts are dropped.
func Split(s string) []string { return strings.FieldsFunc(s, IsSpace) }

func isLineBreak(r rune) bool {
	switch r {
	case '\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', 0x85, 0x2028, 0x2029:
		return true
	}
	return false
}

// SplitLines is str.splitlines() (P01 section 1.2).
func SplitLines(s string) []string { return SplitLinesN(s, -1) }

// SplitLinesN returns the first n elements of str.splitlines() (all when n < 0) without splitting the rest.
func SplitLinesN(s string, n int) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); {
		if n >= 0 && len(out) >= n {
			return out
		}
		c := s[i]
		if c < 0x80 {
			switch c {
			case '\n', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e':
				out = append(out, s[start:i])
				i++
				start = i
			case '\r':
				out = append(out, s[start:i])
				i++
				if i < len(s) && s[i] == '\n' {
					i++
				}
				start = i
			default:
				i++
			}
			continue
		}
		r, w := utf8.DecodeRuneInString(s[i:])
		if r == 0x85 || r == 0x2028 || r == 0x2029 {
			out = append(out, s[start:i])
			i += w
			start = i
			continue
		}
		i += w
	}
	if start < len(s) && (n < 0 || len(out) < n) {
		out = append(out, s[start:])
	}
	return out
}

// Lower is str.lower(): full Unicode lowercase (U+0130 -> "i̇") plus the Final_Sigma rule.
func Lower(s string) string {
	ascii := true
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			ascii = false
			break
		}
	}
	if ascii {
		return strings.ToLower(s)
	}
	rs := []rune(s)
	var b strings.Builder
	b.Grow(len(s))
	for i, r := range rs {
		switch r {
		case 0x130:
			b.WriteString("i\U00000307")
		case 0x3a3:
			if finalSigma(rs, i) {
				b.WriteRune(0x3c2)
			} else {
				b.WriteRune(0x3c3)
			}
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

func isCased(r rune) bool {
	return unicode.IsUpper(r) || unicode.IsLower(r) || unicode.IsTitle(r) ||
		unicode.Is(unicode.Other_Lowercase, r) || unicode.Is(unicode.Other_Uppercase, r)
}

func isCaseIgnorable(r rune) bool {
	switch r {
	case '\'', '.', ':', '^', '`', 0xa8, 0xad, 0xaf, 0xb4, 0xb7, 0xb8, 0x387, 0x55f, 0x5f4, 0x2018, 0x2019, 0x2024, 0x2027,
		0xfe13, 0xfe52, 0xfe55, 0xff07, 0xff0e, 0xff1a:
		return true
	}
	return unicode.In(r, unicode.Mn, unicode.Me, unicode.Cf, unicode.Lm, unicode.Sk)
}

// finalSigma mirrors CPython handle_capital_sigma.
func finalSigma(rs []rune, i int) bool {
	j := i - 1
	var c rune
	for ; j >= 0; j-- {
		c = rs[j]
		if !isCaseIgnorable(c) {
			break
		}
	}
	final := j >= 0 && isCased(c)
	if final && i+1 < len(rs) {
		j = i + 1
		for ; j < len(rs); j++ {
			c = rs[j]
			if !isCaseIgnorable(c) {
				break
			}
		}
		final = j == len(rs) || !isCased(c)
	}
	return final
}

// Upper is str.upper() (full mapping: "ß" -> "SS").
func Upper(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		if r >= 0x80 {
			if m, ok := upperSpecial[r]; ok {
				b.WriteString(m)
				continue
			}
		}
		b.WriteRune(unicode.ToUpper(r))
	}
	return b.String()
}

func isDigitRune(r rune) bool {
	if r >= '0' && r <= '9' {
		return true
	}
	if r < 0x80 {
		return false
	}
	if unicode.Is(unicode.Nd, r) {
		return true
	}
	for _, rg := range digitNotDecimal {
		if r >= rg[0] && r <= rg[1] {
			return true
		}
	}
	return false
}

// IsDigitStr is str.isdigit(): non-empty and every code point is Nd or a Numeric_Type=Digit code point.
func IsDigitStr(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !isDigitRune(r) {
			return false
		}
	}
	return true
}

// IsDecimalStr is str.isdecimal(): non-empty and every code point is Nd.
func IsDecimalStr(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9') && (r < 0x80 || !unicode.Is(unicode.Nd, r)) {
			return false
		}
	}
	return true
}

// IsAlphaStr is str.isalpha(): non-empty and every code point is a letter (L*).
func IsAlphaStr(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !unicode.IsLetter(r) {
			return false
		}
	}
	return true
}

// DigitValue is the decimal value of an Nd code point, or -1.
func DigitValue(r rune) int {
	if r >= '0' && r <= '9' {
		return int(r - '0')
	}
	if r < 0x80 {
		return -1
	}
	// Nd digits come in contiguous runs of whole 0..9 sequences; the table's ranges are maximal runs (stride 1).
	for _, rg := range unicode.Nd.R16 {
		if r >= rune(rg.Lo) && r <= rune(rg.Hi) {
			return int(r-rune(rg.Lo)) % 10
		}
	}
	for _, rg := range unicode.Nd.R32 {
		if r >= rune(rg.Lo) && r <= rune(rg.Hi) {
			return int(r-rune(rg.Lo)) % 10
		}
	}
	return -1
}

// ToASCIIDigits replaces every Nd digit by its ASCII digit (other code points unchanged).
func ToASCIIDigits(s string) string {
	ascii := true
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			ascii = false
			break
		}
	}
	if ascii {
		return s
	}
	var b strings.Builder
	for _, r := range s {
		if v := DigitValue(r); v >= 0 {
			b.WriteByte(byte('0' + v))
		} else {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// MaxIntDigits is CPython's default int max str digits (sys.int_info.default_max_str_digits).
const MaxIntDigits = 4300

// Atoi is int(str) for base 10: Python whitespace strip, optional sign, Nd digits, single '_' between digits.
// Errors wrap ErrValue (Python ValueError), including more than 4300 digits.
func Atoi(s string) (*big.Int, error) {
	t := Strip(s)
	neg := false
	if t != "" && (t[0] == '+' || t[0] == '-') {
		neg = t[0] == '-'
		t = t[1:]
	}
	if t == "" {
		return nil, ErrValue
	}
	var b strings.Builder
	prevDigit := false
	for _, r := range t {
		if r == '_' {
			if !prevDigit {
				return nil, ErrValue
			}
			prevDigit = false
			continue
		}
		v := DigitValue(r)
		if v < 0 {
			return nil, ErrValue
		}
		b.WriteByte(byte('0' + v))
		prevDigit = true
	}
	if !prevDigit {
		return nil, ErrValue
	}
	ds := b.String()
	if len(ds) > MaxIntDigits {
		return nil, ErrValue
	}
	n, ok := new(big.Int).SetString(ds, 10)
	if !ok {
		return nil, ErrValue
	}
	if neg {
		n.Neg(n)
	}
	return n, nil
}

// ParseFloat is float(str): Python strip, sign, Nd digits, '_' only between digits, inf/infinity/nan in any case.
func ParseFloat(s string) (float64, error) {
	t := Strip(s)
	if t == "" {
		return 0, ErrValue
	}
	body := t
	sign := 1.0
	if body[0] == '+' || body[0] == '-' {
		if body[0] == '-' {
			sign = -1
		}
		body = body[1:]
	}
	switch strings.ToLower(body) {
	case "inf", "infinity":
		return math.Inf(int(sign)), nil
	case "nan":
		return math.NaN(), nil
	}
	// Normalise digits and validate the grammar: digits [. digits] [e [sign] digits], with '_' between digits.
	var b strings.Builder
	b.Grow(len(body))
	prev := rune(0) // last code point written (after digit normalisation), 'd' for a digit
	seenDigit, seenDot, seenExp, expDigit := false, false, false, false
	for _, r := range body {
		if r == '_' {
			if prev != 'd' {
				return 0, ErrValue
			}
			prev = '_'
			continue
		}
		if prev == '_' {
			if DigitValue(r) < 0 {
				return 0, ErrValue
			}
		}
		if v := DigitValue(r); v >= 0 {
			b.WriteByte(byte('0' + v))
			prev = 'd'
			if seenExp {
				expDigit = true
			} else {
				seenDigit = true
			}
			continue
		}
		switch {
		case r == '.' && !seenDot && !seenExp:
			seenDot = true
		case (r == 'e' || r == 'E') && !seenExp && seenDigit:
			seenExp = true
		case (r == '+' || r == '-') && (prev == 'e' || prev == 'E'):
		default:
			return 0, ErrValue
		}
		b.WriteRune(r)
		prev = r
	}
	if prev == '_' || !seenDigit || (seenExp && !expDigit) {
		return 0, ErrValue
	}
	f, err := strconv.ParseFloat(b.String(), 64)
	if err != nil {
		var ne *strconv.NumError
		if errors.As(err, &ne) && ne.Err == strconv.ErrRange {
			return sign * f, nil
		}
		return 0, ErrValue
	}
	return sign * f, nil
}

// Round is Python round(x, n) for floats: correctly rounded from the exact binary value (ties to even on exact ties).
func Round(x float64, n int) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	r, err := strconv.ParseFloat(strconv.FormatFloat(x, 'f', n, 64), 64)
	if err != nil {
		return x
	}
	if r == 0 && math.Signbit(x) {
		return math.Copysign(0, -1)
	}
	return r
}

// Len is len(str) in code points.
func Len(s string) int { return utf8.RuneCountInString(s) }

// Head is s[:n] in code points (n >= 0), without scanning past the cut.
func Head(s string, n int) string {
	if n <= 0 {
		return ""
	}
	if len(s) <= n {
		return s
	}
	i := 0
	for c := 0; c < n; c++ {
		if i >= len(s) {
			return s
		}
		if s[i] < 0x80 {
			i++
			continue
		}
		_, w := utf8.DecodeRuneInString(s[i:])
		i += w
	}
	return s[:i]
}

// Slice is s[i:j] in code points with Python negative-index and clamping semantics.
func Slice(s string, i, j int) string {
	n := -1
	count := func() int {
		if n < 0 {
			n = utf8.RuneCountInString(s)
		}
		return n
	}
	if i < 0 {
		i += count()
		if i < 0 {
			i = 0
		}
	}
	if j < 0 {
		j += count()
		if j < 0 {
			j = 0
		}
	}
	if j <= i {
		return ""
	}
	start, end := -1, len(s)
	c := 0
	for bi := range s {
		if c == i {
			start = bi
		}
		if c == j {
			end = bi
			break
		}
		c++
	}
	if start < 0 {
		return ""
	}
	return s[start:end]
}

// IsWord is Python regex \w on str patterns: str.isalnum() or '_', i.e. [\p{L}\p{N}_].
func IsWord(r rune) bool {
	if r < 0x80 {
		return r == '_' || (r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')
	}
	return unicode.IsLetter(r) || unicode.IsNumber(r)
}

// pyFixes are CPython 3.11 sre_compile._equivalences: code points that match each other under re.I besides lower().
var pyFixes = [][]rune{
	{0x69, 0x131}, {0x73, 0x17f}, {0xb5, 0x3bc}, {0x345, 0x3b9, 0x1fbe}, {0x390, 0x1fd3}, {0x3b0, 0x1fe3},
	{0x3b2, 0x3d0}, {0x3b5, 0x3f5}, {0x3b8, 0x3d1}, {0x3ba, 0x3f0}, {0x3c0, 0x3d6}, {0x3c1, 0x3f1},
	{0x3c2, 0x3c3}, {0x3c6, 0x3d5}, {0x1e61, 0x1e9b}, {0xfb05, 0xfb06},
}

func foldClass(r rune) rune {
	if r < utf8.RuneSelf { // ASCII fast path: lower() stays ASCII and the only ASCII members of pyFixes (i, s) head their sets
		if 'A' <= r && r <= 'Z' {
			r += 'a' - 'A'
		}
		return r
	}
	l := unicode.ToLower(r) // simple lowercase, as sre_lower_unicode
	for _, fx := range pyFixes {
		for _, x := range fx {
			if x == l {
				return fx[0]
			}
		}
	}
	return l
}

// FoldEqual reports whether r1 and r2 match each other under Python re.IGNORECASE (str pattern):
// i/I/ı/İ, s/S/ſ, k/K/K (Kelvin) and the other sre equivalences.
func FoldEqual(r1, r2 rune) bool {
	if r1 == r2 {
		return true
	}
	return foldClass(r1) == foldClass(r2)
}

// HasFoldPrefix reports whether s starts with word under Python re.I matching (rune by rune) and returns the number
// of bytes of s consumed.
func HasFoldPrefix(s, word string) (n int, ok bool) {
	i := 0
	for _, w := range word {
		if i >= len(s) {
			return 0, false
		}
		r, sz := utf8.DecodeRuneInString(s[i:])
		if !FoldEqual(r, w) {
			return 0, false
		}
		i += sz
	}
	return i, true
}
