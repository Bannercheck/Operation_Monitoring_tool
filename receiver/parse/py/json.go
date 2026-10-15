package py

import (
	"errors"
	"math"
	"math/big"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

var (
	// ErrSyntax is json.JSONDecodeError (callers that catch it continue).
	ErrSyntax = errors.New("json: JSONDecodeError")
	// ErrRaise is an exception json.loads raises that is NOT a JSONDecodeError (int with more than 4300 digits ->
	// ValueError): Python's `except json.JSONDecodeError` lets it escape.
	ErrRaise = errors.New("json: ValueError (int exceeds 4300 digits)")
	// ErrDepth: nesting deeper than MaxDepth. Python raises RecursionError at a stack-dependent depth (~990 at top
	// level), so the exact outcome is not reproducible; callers forward such input to Python.
	ErrDepth = errors.New("json: nesting too deep (RecursionError in Python)")
	// ErrNaN: a NaN/Infinity float where the caller forbids it.
	ErrNaN = errors.New("json: NaN or Infinity value")
)

// MaxDepth is the deepest container nesting Loads accepts.
const MaxDepth = 900

type jdec struct {
	s     string
	i     int
	build bool
	depth int
}

func isJSONWS(c byte) bool { return c == ' ' || c == '\t' || c == '\n' || c == '\r' }

func (d *jdec) ws() {
	for d.i < len(d.s) && isJSONWS(d.s[d.i]) {
		d.i++
	}
}

// Loads is json.loads(s): insertion order kept, duplicate key -> first position last value, int vs float kept
// (big ints), NaN/Infinity/-Infinity accepted, 1e400 -> +Inf, lone surrogate escapes kept as WTF-8 bytes,
// only " \t\n\r" as outer whitespace, leading U+FEFF rejected.
func Loads(s string) (Value, error) {
	d := jdec{s: s, build: true}
	return d.top()
}

// Validate checks s like json.loads without building the value and returns the top-level kind.
func Validate(s string) (Kind, error) {
	d := jdec{s: s}
	v, err := d.top()
	return v.K, err
}

// Valid reports whether json.loads(s) succeeds.
func Valid(s string) bool {
	_, err := Validate(s)
	return err == nil
}

func (d *jdec) top() (Value, error) {
	if strings.HasPrefix(d.s, "\ufeff") {
		return Null, ErrSyntax
	}
	d.ws()
	v, err := d.value()
	if err != nil {
		return Null, err
	}
	d.ws()
	if d.i != len(d.s) {
		return Null, ErrSyntax
	}
	return v, nil
}

func (d *jdec) lit(word string) bool {
	if strings.HasPrefix(d.s[d.i:], word) {
		d.i += len(word)
		return true
	}
	return false
}

func (d *jdec) value() (Value, error) {
	if d.i >= len(d.s) {
		return Null, ErrSyntax
	}
	switch c := d.s[d.i]; c {
	case '"':
		s, err := d.str()
		if err != nil {
			return Null, err
		}
		return Value{K: KStr, S: s}, nil
	case '{':
		return d.object()
	case '[':
		return d.array()
	case 'n':
		if d.lit("null") {
			return Null, nil
		}
	case 't':
		if d.lit("true") {
			return Value{K: KBool, B: true}, nil
		}
	case 'f':
		if d.lit("false") {
			return Value{K: KBool}, nil
		}
	case 'N':
		if d.lit("NaN") {
			return Value{K: KFloat, F: math.NaN()}, nil
		}
	case 'I':
		if d.lit("Infinity") {
			return Value{K: KFloat, F: math.Inf(1)}, nil
		}
	case '-':
		if d.lit("-Infinity") {
			return Value{K: KFloat, F: math.Inf(-1)}, nil
		}
		return d.number()
	default:
		if c >= '0' && c <= '9' {
			return d.number()
		}
	}
	return Null, ErrSyntax
}

func isD(c byte) bool { return c >= '0' && c <= '9' }

// number mirrors CPython _match_number_unicode.
func (d *jdec) number() (Value, error) {
	s := d.s
	start := d.i
	i := d.i
	if s[i] == '-' {
		i++
		if i >= len(s) {
			return Null, ErrSyntax
		}
	}
	switch {
	case s[i] >= '1' && s[i] <= '9':
		i++
		for i < len(s) && isD(s[i]) {
			i++
		}
	case s[i] == '0':
		i++
	default:
		return Null, ErrSyntax
	}
	isFloat := false
	if i+1 < len(s) && s[i] == '.' && isD(s[i+1]) {
		isFloat = true
		i += 2
		for i < len(s) && isD(s[i]) {
			i++
		}
	}
	if i < len(s) && (s[i] == 'e' || s[i] == 'E') {
		eStart := i
		i++
		if i < len(s) && (s[i] == '-' || s[i] == '+') {
			i++
		}
		for i < len(s) && isD(s[i]) {
			i++
		}
		if isD(s[i-1]) {
			isFloat = true
		} else {
			i = eStart
		}
	}
	d.i = i
	num := s[start:i]
	if isFloat {
		if !d.build {
			return Value{K: KFloat}, nil
		}
		f, err := strconv.ParseFloat(num, 64)
		if err != nil {
			var ne *strconv.NumError
			if !(errors.As(err, &ne) && ne.Err == strconv.ErrRange) {
				return Null, ErrSyntax
			}
		}
		return Value{K: KFloat, F: f}, nil
	}
	digits := len(num)
	if num[0] == '-' {
		digits--
	}
	if digits > MaxIntDigits {
		return Null, ErrRaise
	}
	if !d.build {
		return Value{K: KInt}, nil
	}
	if n, err := strconv.ParseInt(num, 10, 64); err == nil {
		return Value{K: KInt, I: n}, nil
	}
	b, ok := new(big.Int).SetString(num, 10)
	if !ok {
		return Null, ErrSyntax
	}
	return BigInt(b), nil
}

func hexVal(s string) (rune, bool) {
	var r rune
	for i := 0; i < len(s); i++ {
		c := s[i]
		r <<= 4
		switch {
		case c >= '0' && c <= '9':
			r |= rune(c - '0')
		case c >= 'a' && c <= 'f':
			r |= rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			r |= rune(c-'A') + 10
		default:
			return 0, false
		}
	}
	return r, true
}

// appendWTF8 encodes r as UTF-8, and lone surrogates as their 3-byte (WTF-8) form, as Python keeps them in a str.
func appendWTF8(b []byte, r rune) []byte {
	if r >= 0xd800 && r <= 0xdfff {
		return append(b, 0xed, byte(0x80|(r>>6)&0x3f), byte(0x80|r&0x3f))
	}
	return utf8.AppendRune(b, r)
}

// str mirrors CPython scanstring_unicode (strict=True); d.i is at the opening quote.
func (d *jdec) str() (string, error) {
	s := d.s
	i := d.i + 1
	// Fast path: no escapes.
	j := i
	for j < len(s) {
		c := s[j]
		if c == '"' {
			d.i = j + 1
			return s[i:j], nil
		}
		if c == '\\' {
			break
		}
		if c < 0x20 {
			return "", ErrSyntax
		}
		j++
	}
	if j >= len(s) {
		return "", ErrSyntax
	}
	var buf []byte
	if d.build {
		buf = append(make([]byte, 0, j-i+16), s[i:j]...)
	}
	i = j
	for i < len(s) {
		c := s[i]
		switch {
		case c == '"':
			d.i = i + 1
			return string(buf), nil
		case c < 0x20:
			return "", ErrSyntax
		case c != '\\':
			if d.build {
				buf = append(buf, c)
			}
			i++
			continue
		}
		// escape
		i++
		if i >= len(s) {
			return "", ErrSyntax
		}
		e := s[i]
		i++
		var r rune
		switch e {
		case '"', '\\', '/':
			r = rune(e)
		case 'b':
			r = '\b'
		case 'f':
			r = '\f'
		case 'n':
			r = '\n'
		case 'r':
			r = '\r'
		case 't':
			r = '\t'
		case 'u':
			if i+4 > len(s) {
				return "", ErrSyntax
			}
			var ok bool
			r, ok = hexVal(s[i : i+4])
			if !ok {
				return "", ErrSyntax
			}
			i += 4
			// Surrogate pair (CPython: requires end + 6 < len, i.e. at least one char after the second escape).
			if r >= 0xd800 && r <= 0xdbff && i+6 < len(s) && s[i] == '\\' && s[i+1] == 'u' {
				r2, ok := hexVal(s[i+2 : i+6])
				if !ok {
					return "", ErrSyntax
				}
				if r2 >= 0xdc00 && r2 <= 0xdfff {
					r = 0x10000 + (r-0xd800)<<10 + (r2 - 0xdc00)
					i += 6
				}
			}
		default:
			return "", ErrSyntax
		}
		if d.build {
			buf = appendWTF8(buf, r)
		}
	}
	return "", ErrSyntax
}

func (d *jdec) object() (Value, error) {
	d.depth++
	if d.depth > MaxDepth {
		return Null, ErrDepth
	}
	defer func() { d.depth-- }()
	d.i++ // '{'
	var m *Dict
	if d.build {
		m = NewDict(4)
	}
	d.ws()
	if d.i < len(d.s) && d.s[d.i] == '}' {
		d.i++
		return Value{K: KMap, M: m}, nil
	}
	for {
		if d.i >= len(d.s) || d.s[d.i] != '"' {
			return Null, ErrSyntax
		}
		k, err := d.str()
		if err != nil {
			return Null, err
		}
		d.ws()
		if d.i >= len(d.s) || d.s[d.i] != ':' {
			return Null, ErrSyntax
		}
		d.i++
		d.ws()
		v, err := d.value()
		if err != nil {
			return Null, err
		}
		if d.build {
			m.Set(k, v)
		}
		d.ws()
		if d.i >= len(d.s) {
			return Null, ErrSyntax
		}
		switch d.s[d.i] {
		case '}':
			d.i++
			return Value{K: KMap, M: m}, nil
		case ',':
			d.i++
			d.ws()
		default:
			return Null, ErrSyntax
		}
	}
}

func (d *jdec) array() (Value, error) {
	d.depth++
	if d.depth > MaxDepth {
		return Null, ErrDepth
	}
	defer func() { d.depth-- }()
	d.i++ // '['
	var l []Value
	d.ws()
	if d.i < len(d.s) && d.s[d.i] == ']' {
		d.i++
		return Value{K: KList, L: l}, nil
	}
	for {
		v, err := d.value()
		if err != nil {
			return Null, err
		}
		if d.build {
			l = append(l, v)
		}
		d.ws()
		if d.i >= len(d.s) {
			return Null, ErrSyntax
		}
		switch d.s[d.i] {
		case ']':
			d.i++
			return Value{K: KList, L: l}, nil
		case ',':
			d.i++
			d.ws()
		default:
			return Null, ErrSyntax
		}
	}
}

// FloatRepr is the json.dumps / repr text of a float (P01 section 1.7): shortest round-trip digits, fixed notation
// for decimal exponents -4..15 with at least one fractional digit, else d.ddde±XX; NaN, Infinity, -Infinity.
func FloatRepr(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	e := strconv.FormatFloat(f, 'e', -1, 64)
	k := strings.LastIndexByte(e, 'e')
	exp, _ := strconv.Atoi(e[k+1:])
	if exp < -4 || exp >= 16 {
		return e // Go already writes the sign and at least two exponent digits
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if strings.IndexByte(s, '.') < 0 {
		s += ".0"
	}
	return s
}

// AppendString appends the JSON string literal of s as json.dumps(ensure_ascii=False) writes it: only '"', '\\' and
// code points below U+0020 are escaped (\n \r \t \b \f short forms, else \u00xx lowercase); everything else raw.
func AppendString(dst []byte, s string) []byte {
	dst = append(dst, '"')
	start := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 0x20 && c != '"' && c != '\\' {
			continue
		}
		dst = append(dst, s[start:i]...)
		switch c {
		case '"':
			dst = append(dst, '\\', '"')
		case '\\':
			dst = append(dst, '\\', '\\')
		case '\n':
			dst = append(dst, '\\', 'n')
		case '\r':
			dst = append(dst, '\\', 'r')
		case '\t':
			dst = append(dst, '\\', 't')
		case '\b':
			dst = append(dst, '\\', 'b')
		case '\f':
			dst = append(dst, '\\', 'f')
		default:
			const hx = "0123456789abcdef"
			dst = append(dst, '\\', 'u', '0', '0', hx[c>>4], hx[c&0xf])
		}
		start = i + 1
	}
	dst = append(dst, s[start:]...)
	return append(dst, '"')
}

// AppendValue appends json.dumps(v, ensure_ascii=False, default=str, sort_keys=sortKeys) of v. With allowNaN false a
// NaN/Infinity float returns ErrNaN (the bytes appended so far are then meaningless).
func AppendValue(dst []byte, v Value, sortKeys, allowNaN bool) ([]byte, error) {
	var err error
	switch v.K {
	case KNone:
		dst = append(dst, "null"...)
	case KBool:
		if v.B {
			dst = append(dst, "true"...)
		} else {
			dst = append(dst, "false"...)
		}
	case KInt:
		if v.Big != nil {
			dst = v.Big.Append(dst, 10)
		} else {
			dst = strconv.AppendInt(dst, v.I, 10)
		}
	case KFloat:
		if !allowNaN && (math.IsNaN(v.F) || math.IsInf(v.F, 0)) {
			return dst, ErrNaN
		}
		dst = append(dst, FloatRepr(v.F)...)
	case KStr:
		dst = AppendString(dst, v.S)
	case KList:
		dst = append(dst, '[')
		for i, e := range v.L {
			if i > 0 {
				dst = append(dst, ", "...)
			}
			if dst, err = AppendValue(dst, e, sortKeys, allowNaN); err != nil {
				return dst, err
			}
		}
		dst = append(dst, ']')
	case KMap:
		dst, err = AppendDict(dst, v.M, sortKeys, allowNaN)
	}
	return dst, err
}

// AppendDict appends a dict as json.dumps writes it.
func AppendDict(dst []byte, d *Dict, sortKeys, allowNaN bool) ([]byte, error) {
	dst = append(dst, '{')
	n := d.Len()
	order := make([]int, n)
	for i := range order {
		order[i] = i
	}
	if sortKeys {
		sort.SliceStable(order, func(a, b int) bool { return d.keys[order[a]] < d.keys[order[b]] })
	}
	var err error
	for j, i := range order {
		if j > 0 {
			dst = append(dst, ", "...)
		}
		dst = AppendString(dst, d.keys[i])
		dst = append(dst, ": "...)
		if dst, err = AppendValue(dst, d.vals[i], sortKeys, allowNaN); err != nil {
			return dst, err
		}
	}
	return append(dst, '}'), nil
}

// Dumps is json.dumps(v, ensure_ascii=False, default=str, sort_keys=sortKeys) with the default separators.
// NaN/Infinity are written as Python does (allow_nan=True), so the error is always nil.
func Dumps(v Value, sortKeys bool) (string, error) {
	b, err := AppendValue(nil, v, sortKeys, true)
	return string(b), err
}
