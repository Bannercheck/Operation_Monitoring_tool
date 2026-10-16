// Package act is the host actuator of the Watchover action agent (docs/design/action-agent.md §5, §9.3): the role of the
// wo-agent binary that pulls signed jobs from the dashboard, checks them against the host's own root-owned allowlist and runs
// them as argv (never a shell). Standard library only.
package act

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Canonical JSON exactly as the server's watchover.act.types.canonical: UTF-8, sorted keys, separators (",", ":"),
// ensure_ascii=False and no floats (an integral float becomes an integer, any other float its Python repr as a string).
// Every signature and hash of the job channel is computed over these bytes, so both sides must agree byte for byte
// (parity test: tests/fixtures/act_canonical.jsonl).
func Canonical(v any) ([]byte, error) {
	norm, err := normalise(v)
	if err != nil {
		return nil, err
	}
	var b bytes.Buffer
	if err := writeCanon(&b, norm); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

// MustCanonical panics on values that cannot be canonical (NaN, channels): only used for values this package built itself.
func MustCanonical(v any) []byte {
	b, err := Canonical(v)
	if err != nil {
		panic(err)
	}
	return b
}

// SHA256Hex of the canonical form (types.sha256_hex of an object).
func SHA256Hex(v any) string {
	if s, ok := v.(string); ok {
		h := sha256.Sum256([]byte(s))
		return hex.EncodeToString(h[:])
	}
	if b, ok := v.([]byte); ok {
		h := sha256.Sum256(b)
		return hex.EncodeToString(h[:])
	}
	h := sha256.Sum256(MustCanonical(v))
	return hex.EncodeToString(h[:])
}

// Decode parses JSON keeping numbers exact (json.Number), as the canonical form needs.
func Decode(data []byte, out any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	return d.Decode(out)
}

// normalise turns any Go value (structs, typed maps, numbers) into the generic JSON tree with json.Number leaves.
func normalise(v any) (any, error) {
	switch v.(type) {
	case map[string]any, []any, string, json.Number, bool, nil:
		if ok := isGeneric(v); ok {
			return v, nil
		}
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	var out any
	if err := Decode(buf.Bytes(), &out); err != nil {
		return nil, err
	}
	return out, nil
}

func isGeneric(v any) bool {
	switch x := v.(type) {
	case map[string]any:
		for _, e := range x {
			if !isGeneric(e) {
				return false
			}
		}
		return true
	case []any:
		for _, e := range x {
			if !isGeneric(e) {
				return false
			}
		}
		return true
	case string, json.Number, bool, nil:
		return true
	}
	return false
}

var intRx = regexp.MustCompile(`^-?[0-9]+$`)

// canonNumber renders a JSON number the way Python's canonical() would after json.loads.
func canonNumber(n json.Number) (string, bool, error) {
	s := string(n)
	if intRx.MatchString(s) {
		s = strings.TrimLeft(strings.TrimPrefix(s, "-"), "0")
		if s == "" {
			return "0", false, nil
		}
		if strings.HasPrefix(string(n), "-") {
			return "-" + s, false, nil
		}
		return s, false, nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
		return "", false, fmt.Errorf("canonical JSON cannot carry %q", s)
	}
	if f == math.Trunc(f) {
		if f == 0 {
			return "0", false, nil
		}
		return strconv.FormatFloat(f, 'f', 0, 64), false, nil
	}
	return PyFloatRepr(f), true, nil
}

// PyFloatRepr is Python's repr(float): shortest round-trip digits, fixed notation for -4 < decpt <= 16, else d.ddde±XX.
func PyFloatRepr(f float64) string {
	neg := f < 0
	if neg {
		f = -f
	}
	e := strconv.FormatFloat(f, 'e', -1, 64) // d.dddde±XX
	mant, exp, _ := strings.Cut(e, "e")
	digits := strings.Replace(mant, ".", "", 1)
	x, _ := strconv.Atoi(exp)
	decpt := x + 1
	var out string
	if decpt > -4 && decpt <= 16 {
		switch {
		case decpt <= 0:
			out = "0." + strings.Repeat("0", -decpt) + digits
		case decpt >= len(digits):
			out = digits + strings.Repeat("0", decpt-len(digits)) + ".0"
		default:
			out = digits[:decpt] + "." + digits[decpt:]
		}
	} else {
		m := digits[:1]
		if len(digits) > 1 {
			m += "." + digits[1:]
		}
		sign := "+"
		if x < 0 {
			sign = "-"
			x = -x
		}
		out = fmt.Sprintf("%se%s%02d", m, sign, x)
	}
	if neg {
		return "-" + out
	}
	return out
}

func writeCanon(b *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		writeString(b, x)
	case json.Number:
		s, quoted, err := canonNumber(x)
		if err != nil {
			return err
		}
		if quoted {
			writeString(b, s)
		} else {
			b.WriteString(s)
		}
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanon(b, e); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys) // UTF-8 byte order = code point order = Python's sort of str keys
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			writeString(b, k)
			b.WriteByte(':')
			if err := writeCanon(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	default:
		return fmt.Errorf("canonical: unsupported %T", v)
	}
	return nil
}

// writeString escapes like Python's json with ensure_ascii=False: only ", \ and control characters below 0x20.
func writeString(b *bytes.Buffer, s string) {
	const hexd = "0123456789abcdef"
	b.WriteByte('"')
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			b.WriteString("�")
			i++
			continue
		}
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				b.WriteString(`\u00`)
				b.WriteByte(hexd[r>>4])
				b.WriteByte(hexd[r&0xf])
			} else {
				b.WriteString(s[i : i+size])
			}
		}
		i += size
	}
	b.WriteByte('"')
}
