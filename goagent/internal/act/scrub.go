package act

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"
)

// Field-aware scrubbing on the host (§9.3.5, C34, R12): the standard-library twin of watchover.privacy.scrub_strict and
// scrub_fields, parity-tested over tests/fixtures/privacy_parity.jsonl. Numbers, enums, identifiers and paths keep their
// value (a 16-digit size is never a card number); text fields and raw output go through ScrubStrict. The dashboard scrubs
// again the same way, so a gap here is never the last line of defence. Two documented differences: Go has no Unicode NFKC
// table (compatFold covers full-width forms, compatibility spaces, ligatures, super- and subscript digits) and RE2 has no
// look-around (the phone, digit-run and TCKN rules are scanned by hand with the same boundaries).

var (
	secretWords = `pass(?:word|wd)?|pwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?key|keystore[_-]?pass\w*|truststore[_-]?pass\w*`
	strictRules = []struct {
		rx   *regexp.Regexp
		repl string
	}{
		{regexp.MustCompile(`(?s)-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?(?:-----END [A-Z0-9 ]*-----|\z)`), "[PEM]"},
		{regexp.MustCompile(`(\b[A-Za-z][\w+.-]*://)[^/\s:@]+:[^@\s/]+@`), "${1}[KIMLIK]@"},
		{regexp.MustCompile(`(?i)(jdbc:oracle:\w+:|sqlplus\s+)[^/\s@]+/[^@\s]+@`), "${1}[KIMLIK]@"},
		{regexp.MustCompile(`(?i)([A-Z0-9_.-]*(?:` + secretWords + `)\s*[=:]\s*)\S+`), "${1}[GIZLI]"},
		{regexp.MustCompile(`(?m)((?:\s|^)(?:-p|(?i:--pass(?:word)?|--token|--secret|--api-key))(?:\s+|=))\S+`), "${1}[GIZLI]"},
		{regexp.MustCompile(`\b(?:AKIA|ASIA)[A-Z0-9]{16}\b`), "[GIZLI]"},
		{regexp.MustCompile(`\beyJ[\w-]+\.[\w-]+\.[\w-]+`), "[GIZLI]"},
		{regexp.MustCompile(`(?i)((?:jsessionid|sessionid|sid|session|cookie|set-cookie)[\w-]*\s*[=:]\s*)[^;\s]{12,}`), "${1}[GIZLI]"},
	}
	// privacy.SECRET with a hand-made leading boundary (Go's \b is ASCII-only and "şifre" starts with a non-ASCII letter)
	rxSecret  = regexp.MustCompile(`(?i)(^|[^\p{L}\p{N}_])(password|passwd|pwd|parola|sifre|şifre|token|secret|api[_-]?key|authorization)(\s*[=:]\s*|"\s*:\s*")(Bearer\s+)?[^\s&",;]+`)
	rxBearer  = regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9._\-~+/]{12,}=*`)
	rxEmail   = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
	rxIBAN    = regexp.MustCompile(`\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b`)
	rxPhone   = regexp.MustCompile(`^(?:\+?90[\s\-]?)?\(?0?5\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}`)
	rxEntropy = regexp.MustCompile(`[A-Za-z0-9+/=_-]{20,}`)
	rxSecKey  = regexp.MustCompile(`(?i)(` + secretWords + `|credential|cookie|authorization)`)
	rxNumText = regexp.MustCompile(`^-?[0-9]+(?:[.,][0-9]+)?$`)
	rxIdText  = regexp.MustCompile(`^[A-Za-z0-9_.:@-]{1,64}\n?$`)
	rxHomeSeg = regexp.MustCompile(`(?i)^(/home/|/Users/|C:\\Users\\)([^/\\]+)`)
)

func isWord(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r) }

func isZWBidi(r rune) bool {
	return (r >= 0x200b && r <= 0x200f) || (r >= 0x202a && r <= 0x202e) || (r >= 0x2060 && r <= 0x2069) || r == 0xfeff
}

var compatMap = map[rune]string{0xa0: " ", 0x3000: " ", 0x202f: " ", 0x205f: " ", 0xfb00: "ff", 0xfb01: "fi", 0xfb02: "fl", 0xfb03: "ffi",
	0xfb04: "ffl", 0xfb05: "st", 0xfb06: "st", 0xb9: "1", 0xb2: "2", 0xb3: "3", 0x2070: "0", 0x2024: ".", 0x2025: "..", 0x2026: "...",
	0x2113: "l", 0xaa: "a", 0xba: "o", 0x2122: "TM"}

// compatFold is the subset of NFKC the scrubber needs, then invisible and bidi controls removed (privacy.fold, confusables off).
func compatFold(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 0xff01 && r <= 0xff5e:
			b.WriteRune(r - 0xfee0)
		case r >= 0x2000 && r <= 0x200a:
			b.WriteByte(' ')
		case r >= 0x2074 && r <= 0x2079:
			b.WriteRune('4' + (r - 0x2074))
		case r >= 0x2080 && r <= 0x2089:
			b.WriteRune('0' + (r - 0x2080))
		case isZWBidi(r):
		default:
			if m, ok := compatMap[r]; ok {
				b.WriteString(m)
			} else {
				b.WriteRune(r)
			}
		}
	}
	return b.String()
}

func prevRune(s string, i int) (rune, bool) {
	if i <= 0 {
		return 0, false
	}
	r, _ := utf8.DecodeLastRuneInString(s[:i])
	return r, true
}

func nextRune(s string, i int) (rune, bool) {
	if i >= len(s) {
		return 0, false
	}
	r, _ := utf8.DecodeRuneInString(s[i:])
	return r, true
}

func tcknOK(d string) bool {
	if len(d) != 11 || d[0] == '0' {
		return false
	}
	n := make([]int, 11)
	for i := range d {
		if d[i] < '0' || d[i] > '9' {
			return false
		}
		n[i] = int(d[i] - '0')
	}
	odd := n[0] + n[2] + n[4] + n[6] + n[8]
	even := n[1] + n[3] + n[5] + n[7]
	return ((odd*7-even)%10+10)%10 == n[9] && (n[0]+n[1]+n[2]+n[3]+n[4]+n[5]+n[6]+n[7]+n[8]+n[9])%10 == n[10]
}

func luhnOK(d string) bool {
	s, alt := 0, false
	for i := len(d) - 1; i >= 0; i-- {
		x := int(d[i] - '0')
		if alt {
			if x > 4 {
				x = x*2 - 9
			} else {
				x *= 2
			}
		}
		s += x
		alt = !alt
	}
	return s%10 == 0
}

// phoneSub: (?<![\w]) PHONE (?![\w]) → [TEL].
func phoneSub(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		if p, ok := prevRune(s, i); !ok || !isWord(p) {
			if m := rxPhone.FindStringIndex(s[i:]); m != nil && m[1] > 0 {
				if n, ok := nextRune(s, i+m[1]); !ok || !isWord(n) {
					b.WriteString("[TEL]")
					i += m[1]
					continue
				}
			}
		}
		_, size := utf8.DecodeRuneInString(s[i:])
		b.WriteString(s[i : i+size])
		i += size
	}
	return b.String()
}

func isDigit(c byte) bool { return c >= '0' && c <= '9' }

// digitsSub: (?<![\w])\d[\d \-]{9,22}\d(?![\w]) → [TCKN] / [KART] by checksum, the longest such run first (greedy).
func digitsSub(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		p, hasPrev := prevRune(s, i)
		if isDigit(s[i]) && (!hasPrev || !isWord(p)) {
			j := i
			for j < len(s) && j-i < 24 && (isDigit(s[j]) || s[j] == ' ' || s[j] == '-') {
				j++
			}
			matched := false
			for l := j - i; l >= 11; l-- {
				end := i + l
				if !isDigit(s[end-1]) {
					continue
				}
				if n, ok := nextRune(s, end); ok && isWord(n) {
					continue
				}
				raw := s[i:end]
				d := strings.Map(func(r rune) rune {
					if r >= '0' && r <= '9' {
						return r
					}
					return -1
				}, raw)
				switch {
				case len(d) == 11 && tcknOK(d):
					b.WriteString("[TCKN]")
				case len(d) >= 13 && len(d) <= 19 && luhnOK(d):
					b.WriteString("[KART]")
				default:
					b.WriteString(raw)
				}
				i = end
				matched = true
				break
			}
			if matched {
				continue
			}
		}
		_, size := utf8.DecodeRuneInString(s[i:])
		b.WriteString(s[i : i+size])
		i += size
	}
	return b.String()
}

// tcknSub: (?<!\d)(\d{11})(?!\d) with a valid checksum → [TCKN].
func tcknSub(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		if isDigit(s[i]) {
			j := i
			for j < len(s) && isDigit(s[j]) {
				j++
			}
			if j-i == 11 && tcknOK(s[i:j]) {
				b.WriteString("[TCKN]")
			} else {
				b.WriteString(s[i:j])
			}
			i = j
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

// Scrub is privacy.scrub (the base patterns).
func Scrub(t string) string {
	if t == "" {
		return t
	}
	t = rxSecret.ReplaceAllString(t, "${1}${2}${3}***")
	t = rxBearer.ReplaceAllString(t, "Bearer ***")
	t = rxEmail.ReplaceAllString(t, "[E-POSTA]")
	t = rxIBAN.ReplaceAllString(t, "[IBAN]")
	t = phoneSub(t)
	t = digitsSub(t)
	return tcknSub(t)
}

func shannon(s string) float64 {
	counts := map[rune]int{}
	n := 0
	for _, r := range s {
		counts[r]++
		n++
	}
	h := 0.0
	for _, c := range counts {
		p := float64(c) / float64(n)
		h -= p * math.Log2(p)
	}
	return h
}

func highEntropy(s string) bool {
	var lo, up, dg bool
	for _, r := range s {
		lo = lo || unicode.IsLower(r)
		up = up || unicode.IsUpper(r)
		dg = dg || unicode.IsDigit(r)
	}
	return lo && up && dg && shannon(s) >= 4.0
}

// ScrubStrict is privacy.scrub_strict: host credentials, PEM blocks, cloud keys, JWTs, sessions, high-entropy tokens.
func ScrubStrict(text string) string {
	if text == "" {
		return text
	}
	t := compatFold(text)
	for _, r := range strictRules {
		t = r.rx.ReplaceAllString(t, r.repl)
	}
	t = Scrub(t)
	return rxEntropy.ReplaceAllStringFunc(t, func(m string) string {
		if highEntropy(m) {
			return "[GIZLI]"
		}
		return m
	})
}

// Pseudonymiser holds the host's own HMAC key (state/pseudo.key): the same person stays the same token on this host.
type Pseudonymiser struct {
	mu  sync.Mutex
	key []byte
	dir string
}

func (p *Pseudonymiser) load() []byte {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.key != nil {
		return p.key
	}
	path := filepath.Join(p.dir, "pseudo.key")
	if b, err := os.ReadFile(path); err == nil && len(strings.TrimSpace(string(b))) > 0 {
		p.key = []byte(strings.TrimSpace(string(b)))
		return p.key
	}
	buf := make([]byte, 32)
	_, _ = rand.Read(buf)
	k := []byte(hex.EncodeToString(buf))
	if p.dir != "" {
		_ = os.WriteFile(path, k, 0o600)
	}
	p.key = k
	return k
}

// Of is privacy.pseudonym: "u_" + 16 hex characters of HMAC-SHA256 over the lower-cased value.
func (p *Pseudonymiser) Of(value string) string {
	v := strings.ToLower(strings.TrimSpace(value))
	if v == "" || v == "-" || v == "anonymous" || v == "null" || v == "none" {
		return ""
	}
	m := hmac.New(sha256.New, p.load())
	m.Write([]byte(v))
	return "u_" + hex.EncodeToString(m.Sum(nil))[:16]
}

// ScrubFields is privacy.scrub_fields: structured output scrubbed field by field by declared type (number, enum, ident,
// path, text); undeclared strings are scrubbed as text; a string under a secret-like key is masked whatever it holds.
func ScrubFields(fields any, schema map[string]any, ps *Pseudonymiser) any {
	var one func(key *string, v any, typ any) any
	one = func(key *string, v any, typ any) any {
		switch x := v.(type) {
		case nil, bool, json.Number, int, int64, float64, uint64:
			return v
		case map[string]any:
			sub, _ := typ.(map[string]any)
			out := make(map[string]any, len(x))
			for k, e := range x {
				kk := k
				var t any
				if sub != nil {
					t = sub[k]
				}
				out[k] = one(&kk, e, t)
			}
			return out
		case []any:
			out := make([]any, len(x))
			for i, e := range x {
				out[i] = one(key, e, typ)
			}
			return out
		}
		s, ok := pyStr(v)
		if !ok {
			return v
		}
		ty, _ := typ.(string)
		if key != nil && rxSecKey.MatchString(*key) && ty != "number" && ty != "enum" {
			if s == "" {
				return s
			}
			return "[GIZLI]"
		}
		switch ty {
		case "number":
			if rxNumText.MatchString(strings.TrimSpace(s)) {
				return s
			}
		case "enum":
			if rxIdText.MatchString(s) {
				return s
			}
			return ScrubStrict(s)
		case "ident":
			if rxIdText.MatchString(s) {
				return s
			}
		case "path":
			if !strings.ContainsAny(s, "\n\x00") {
				return rxHomeSeg.ReplaceAllStringFunc(s, func(m string) string {
					g := rxHomeSeg.FindStringSubmatch(m)
					return g[1] + ps.Of(g[2])
				})
			}
		}
		return ScrubStrict(s)
	}
	if schema == nil {
		schema = map[string]any{}
	}
	return one(nil, fields, schema)
}

// parserSchema is the field type table of the dashboard's parsers (templates.PARSERS) for the parsers this actuator emits;
// the fields of "rows" share the parser's flat schema.
var parserSchema = map[string]map[string]string{
	"kv":               {"use_pct": "number", "avail_gb": "number", "size_gb": "number", "mount": "path", "fs": "ident"},
	"du_top":           {"path": "path", "bytes": "number"},
	"files_largest":    {"path": "path", "bytes": "number", "mtime": "number"},
	"lsof_deleted":     {"deleted_open_bytes": "number", "files": "number", "pid": "number", "comm": "ident", "path": "path", "bytes": "number"},
	"systemctl_show":   {"ActiveState": "enum", "SubState": "enum", "MainPID": "number", "NRestarts": "number"},
	"jcmd_heap":        {"used_kb": "number", "capacity_kb": "number", "metaspace_kb": "number"},
	"compress_result":  {"list_id": "ident", "files": "number", "bytes": "number", "bytes_before": "number", "bytes_after": "number", "fallback": "enum"},
	"proc_top":         {"pid": "number", "start": "ident", "comm": "ident", "user": "ident", "cpu": "number", "mem_mb": "number"},
	"proc_find":        {"pid": "number", "start": "ident", "exe": "path", "unit": "ident", "user": "ident"},
	"journal_vacuum":   {"freed_bytes": "number"},
	"logrotate_result": {"rotated": "number", "errors": "number"},
	"http_status":      {"status": "number", "ms": "number"},
	"port_state":       {"port": "number", "open": "enum"},
}

// SchemaFor builds the nested schema ScrubFields wants for a parser (rows share the flat field types).
func SchemaFor(parser string) map[string]any {
	flat, ok := parserSchema[parser]
	if !ok {
		return nil
	}
	out := map[string]any{}
	row := map[string]any{}
	for k, v := range flat {
		out[k] = v
		row[k] = v
	}
	out["rows"] = row
	return out
}
