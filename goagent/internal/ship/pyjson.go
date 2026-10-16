package ship

import (
	"strconv"
	"time"
	"unicode/utf8"
)

// kv / obj: an ordered JSON object written byte for byte like Python's json.dumps (", " and ": " separators, keys in
// insertion order), so a batch from wo-agent is the same text agent.py sends for the same event.
type kv struct {
	K string
	V any
}

type obj []kv

// r1 is a percentage rounded like Python's round(x, 1) and printed like its repr ("23.4", "100.0").
type r1 float64

func (o obj) bytes(ascii bool) []byte { return o.append(make([]byte, 0, 256), ascii) }

func (o obj) append(b []byte, ascii bool) []byte {
	b = append(b, '{')
	for i, e := range o {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = pyStr(b, e.K, ascii)
		b = append(b, ": "...)
		b = pyVal(b, e.V, ascii)
	}
	return append(b, '}')
}

func pyVal(b []byte, v any, ascii bool) []byte {
	switch x := v.(type) {
	case string:
		return pyStr(b, x, ascii)
	case int:
		return strconv.AppendInt(b, int64(x), 10)
	case int64:
		return strconv.AppendInt(b, x, 10)
	case r1:
		return strconv.AppendFloat(b, float64(x), 'f', 1, 64)
	case bool:
		return strconv.AppendBool(b, x)
	case []string:
		b = append(b, '[')
		for i, s := range x {
			if i > 0 {
				b = append(b, ", "...)
			}
			b = pyStr(b, s, ascii)
		}
		return append(b, ']')
	case obj:
		return x.append(b, ascii)
	case nil:
		return append(b, "null"...)
	}
	return append(b, "null"...)
}

const hexdig = "0123456789abcdef"

func appendU(b []byte, r rune) []byte {
	return append(b, '\\', 'u', hexdig[r>>12&0xf], hexdig[r>>8&0xf], hexdig[r>>4&0xf], hexdig[r&0xf])
}

// pyStr writes a JSON string like Python's json encoder: ensure_ascii=True escapes everything outside space..tilde as
// \uXXXX (lowercase hex, surrogate pairs above U+FFFF); ensure_ascii=False escapes only quotes, backslash and controls.
// Invalid UTF-8 (never seen by Python, which decoded with errors="replace") becomes U+FFFD.
func pyStr(b []byte, s string, ascii bool) []byte {
	b = append(b, '"')
	for i := 0; i < len(s); {
		c := s[i]
		if c >= 0x20 && c < 0x7f && c != '"' && c != '\\' {
			b = append(b, c)
			i++
			continue
		}
		if c < utf8.RuneSelf {
			switch c {
			case '"':
				b = append(b, '\\', '"')
			case '\\':
				b = append(b, '\\', '\\')
			case '\n':
				b = append(b, '\\', 'n')
			case '\r':
				b = append(b, '\\', 'r')
			case '\t':
				b = append(b, '\\', 't')
			case '\b':
				b = append(b, '\\', 'b')
			case '\f':
				b = append(b, '\\', 'f')
			default:
				if c == 0x7f && !ascii {
					b = append(b, c)
				} else {
					b = appendU(b, rune(c))
				}
			}
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			n := maximalSubpart(s[i:])
			i += n
			if ascii {
				b = appendU(b, 0xfffd)
			} else {
				b = append(b, "�"...)
			}
			continue
		}
		if ascii {
			if r > 0xffff {
				r -= 0x10000
				b = appendU(b, 0xd800+(r>>10&0x3ff))
				b = appendU(b, 0xdc00+(r&0x3ff))
			} else {
				b = appendU(b, r)
			}
		} else {
			b = append(b, s[i:i+size]...)
		}
		i += size
	}
	return append(b, '"')
}

// isoUTC is Python's datetime.now(timezone.utc).isoformat(): microseconds, omitted when zero, and "+00:00".
func isoUTC(t time.Time) string {
	t = t.UTC()
	b := make([]byte, 0, 32)
	b = t.AppendFormat(b, "2006-01-02T15:04:05")
	if us := t.Nanosecond() / 1000; us != 0 {
		b = append(b, '.')
		s := strconv.Itoa(us)
		for k := len(s); k < 6; k++ {
			b = append(b, '0')
		}
		b = append(b, s...)
	}
	return string(append(b, "+00:00"...))
}
