package py

import (
	"math"
	"math/big"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Kind is the Python type of a Value.
type Kind uint8

const (
	KNone Kind = iota
	KBool
	KInt
	KFloat
	KStr
	KList
	KMap
)

// Value is a record / attribute value with Python typing (int and float stay distinct, ints are unbounded).
type Value struct {
	K   Kind
	B   bool
	I   int64    // Kind==Int and Big==nil
	Big *big.Int // Kind==Int and the value is outside int64
	F   float64  // Kind==Float (NaN/±Inf allowed)
	S   string   // Kind==Str
	L   []Value  // Kind==List
	M   *Dict    // Kind==Map
}

// Null is Python None.
var Null = Value{}

// S returns a str value.
func S(s string) Value { return Value{K: KStr, S: s} }

// Int returns an int value.
func Int(i int64) Value { return Value{K: KInt, I: i} }

// BigInt returns an int value; it is normalised to the int64 form when it fits.
func BigInt(n *big.Int) Value {
	if n.IsInt64() {
		return Value{K: KInt, I: n.Int64()}
	}
	return Value{K: KInt, Big: new(big.Int).Set(n)}
}

// F returns a float value.
func F(f float64) Value { return Value{K: KFloat, F: f} }

// B returns a bool value.
func B(b bool) Value { return Value{K: KBool, B: b} }

// ListOf returns a list value.
func ListOf(vs ...Value) Value { return Value{K: KList, L: vs} }

// MapOf returns a dict value.
func MapOf(d *Dict) Value { return Value{K: KMap, M: d} }

// IntString is the decimal text of an int value.
func (v Value) IntString() string {
	if v.Big != nil {
		return v.Big.String()
	}
	return strconv.FormatInt(v.I, 10)
}

// Str is Python str(v).
func (v Value) Str() string {
	switch v.K {
	case KStr:
		return v.S
	case KNone:
		return "None"
	case KBool:
		if v.B {
			return "True"
		}
		return "False"
	case KInt:
		return v.IntString()
	case KFloat:
		return FloatStr(v.F)
	}
	return v.Repr()
}

// Repr is Python repr(v).
func (v Value) Repr() string {
	var b strings.Builder
	v.appendRepr(&b)
	return b.String()
}

func (v Value) appendRepr(b *strings.Builder) {
	switch v.K {
	case KStr:
		b.WriteString(StrRepr(v.S))
	case KList:
		b.WriteByte('[')
		for i, e := range v.L {
			if i > 0 {
				b.WriteString(", ")
			}
			e.appendRepr(b)
		}
		b.WriteByte(']')
	case KMap:
		b.WriteByte('{')
		if v.M != nil {
			for i, k := range v.M.keys {
				if i > 0 {
					b.WriteString(", ")
				}
				b.WriteString(StrRepr(k))
				b.WriteString(": ")
				v.M.vals[i].appendRepr(b)
			}
		}
		b.WriteByte('}')
	default:
		b.WriteString(v.Str())
	}
}

// FloatStr is Python str(float) / repr(float): like FloatRepr but nan, inf, -inf.
func FloatStr(f float64) string {
	switch {
	case math.IsNaN(f):
		return "nan"
	case math.IsInf(f, 1):
		return "inf"
	case math.IsInf(f, -1):
		return "-inf"
	}
	return FloatRepr(f)
}

// isPrintable approximates Python str.isprintable() for one code point.
func isPrintable(r rune) bool {
	if r == ' ' {
		return true
	}
	if r < 0x20 || r == 0x7f {
		return false
	}
	if unicode.In(r, unicode.Cc, unicode.Cf, unicode.Cs, unicode.Co, unicode.Zl, unicode.Zp, unicode.Zs) {
		return false
	}
	return unicode.In(r, unicode.L, unicode.M, unicode.N, unicode.P, unicode.S)
}

// StrRepr is Python repr(str).
func StrRepr(s string) string {
	q := byte('\'')
	if strings.IndexByte(s, '\'') >= 0 && strings.IndexByte(s, '"') < 0 {
		q = '"'
	}
	var b strings.Builder
	b.WriteByte(q)
	for i := 0; i < len(s); {
		r, w := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && w == 1 { // WTF-8 lone surrogate or invalid byte
			if i+2 < len(s) && s[i] == 0xed {
				cp := rune(s[i]&0x0f)<<12 | rune(s[i+1]&0x3f)<<6 | rune(s[i+2]&0x3f)
				b.WriteString(`\u` + hex4(cp))
				i += 3
				continue
			}
			b.WriteString(`\x` + hex2(rune(s[i])))
			i++
			continue
		}
		i += w
		switch {
		case r == rune(q) || r == '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r < 0x20 || r == 0x7f:
			b.WriteString(`\x` + hex2(r))
		case r < 0x7f:
			b.WriteRune(r)
		case isPrintable(r):
			b.WriteRune(r)
		case r <= 0xff:
			b.WriteString(`\x` + hex2(r))
		case r <= 0xffff:
			b.WriteString(`\u` + hex4(r))
		default:
			s8 := strconv.FormatInt(int64(r), 16)
			b.WriteString(`\U` + strings.Repeat("0", 8-len(s8)) + s8)
		}
	}
	b.WriteByte(q)
	return b.String()
}

func hex2(r rune) string {
	s := strconv.FormatInt(int64(r), 16)
	if len(s) < 2 {
		s = "0" + s
	}
	return s
}

func hex4(r rune) string {
	s := strconv.FormatInt(int64(r), 16)
	return strings.Repeat("0", 4-len(s)) + s
}

// Truthy is Python bool(v).
func (v Value) Truthy() bool {
	switch v.K {
	case KNone:
		return false
	case KBool:
		return v.B
	case KInt:
		return v.Big != nil || v.I != 0
	case KFloat:
		return v.F != 0 // NaN is truthy
	case KStr:
		return v.S != ""
	case KList:
		return len(v.L) > 0
	case KMap:
		return v.M != nil && v.M.Len() > 0
	}
	return false
}

// IsNoneOrEmpty is the `v in (None, "")` test.
func (v Value) IsNoneOrEmpty() bool { return v.K == KNone || (v.K == KStr && v.S == "") }

// HashKey returns a key that is equal for two values iff Python dict lookup treats them as the same key
// (1, 1.0 and True collide; "1" does not). Lists/dicts are keyed by their str() (callers key those by str(value)).
func (v Value) HashKey() string {
	switch v.K {
	case KNone:
		return "N"
	case KBool:
		if v.B {
			return "n:1"
		}
		return "n:0"
	case KInt:
		return "n:" + v.IntString()
	case KFloat:
		f := v.F
		if math.IsNaN(f) {
			return "f:nan"
		}
		if math.IsInf(f, 0) || f != math.Trunc(f) {
			return "f:" + FloatRepr(f)
		}
		if f == 0 {
			return "n:0"
		}
		if math.Abs(f) < 1<<62 {
			return "n:" + strconv.FormatInt(int64(f), 10)
		}
		bi, _ := new(big.Float).SetFloat64(f).Int(nil)
		return "n:" + bi.String()
	case KStr:
		return "s:" + v.S
	}
	return "s:" + v.Str()
}

// Dict is a Python dict with string keys: insertion ordered, Set on an existing key replaces in place (keeps the
// position), Del removes the key and keeps the order of the rest.
type Dict struct {
	keys []string
	vals []Value
	idx  map[string]int
}

// NewDict returns an empty dict with room for n keys.
func NewDict(n int) *Dict {
	return &Dict{keys: make([]string, 0, n), vals: make([]Value, 0, n)}
}

func (d *Dict) find(k string) int {
	if d == nil {
		return -1
	}
	if d.idx == nil {
		for i, x := range d.keys {
			if x == k {
				return i
			}
		}
		return -1
	}
	if i, ok := d.idx[k]; ok {
		return i
	}
	return -1
}

// reindex keeps the invariant "idx != nil iff len(keys) > dictIndexMin" (reads never mutate the dict).
func (d *Dict) reindex() {
	if len(d.keys) <= dictIndexMin {
		d.idx = nil
		return
	}
	d.idx = make(map[string]int, len(d.keys))
	for i, x := range d.keys {
		if _, dup := d.idx[x]; !dup {
			d.idx[x] = i
		}
	}
}

const dictIndexMin = 8

// Get is dict.get(k) plus presence.
func (d *Dict) Get(k string) (Value, bool) {
	if i := d.find(k); i >= 0 {
		return d.vals[i], true
	}
	return Null, false
}

// Has is `k in d`.
func (d *Dict) Has(k string) bool { return d.find(k) >= 0 }

// Set is d[k] = v.
func (d *Dict) Set(k string, v Value) {
	if i := d.find(k); i >= 0 {
		d.vals[i] = v
		return
	}
	d.keys = append(d.keys, k)
	d.vals = append(d.vals, v)
	if d.idx != nil {
		d.idx[k] = len(d.keys) - 1
	} else if len(d.keys) > dictIndexMin {
		d.reindex()
	}
}

// SetDefault is dict.setdefault(k, v).
func (d *Dict) SetDefault(k string, v Value) Value {
	if i := d.find(k); i >= 0 {
		return d.vals[i]
	}
	d.Set(k, v)
	return v
}

// Del is dict.pop(k): removes k and returns its value.
func (d *Dict) Del(k string) (Value, bool) {
	i := d.find(k)
	if i < 0 {
		return Null, false
	}
	v := d.vals[i]
	d.keys = append(d.keys[:i], d.keys[i+1:]...)
	d.vals = append(d.vals[:i], d.vals[i+1:]...)
	d.reindex()
	return v, true
}

// Keys returns the keys in insertion order (the slice must not be modified).
func (d *Dict) Keys() []string {
	if d == nil {
		return nil
	}
	return d.keys
}

// Len is len(d).
func (d *Dict) Len() int {
	if d == nil {
		return 0
	}
	return len(d.keys)
}

// Range calls f for every item in insertion order until f returns false.
func (d *Dict) Range(f func(k string, v Value) bool) {
	if d == nil {
		return
	}
	for i, k := range d.keys {
		if !f(k, d.vals[i]) {
			return
		}
	}
}

// At returns the i-th item in insertion order.
func (d *Dict) At(i int) (string, Value) { return d.keys[i], d.vals[i] }

// Clone is a shallow copy (dict(d)).
func (d *Dict) Clone() *Dict {
	if d == nil {
		return NewDict(0)
	}
	c := &Dict{keys: append([]string(nil), d.keys...), vals: append([]Value(nil), d.vals...)}
	c.reindex()
	return c
}
