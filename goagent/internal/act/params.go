package act

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Parameter types of command templates (§5.5): the closed set of watchover.act.params, re-implemented here and parity-tested
// against the Python validators through tests/fixtures/act_params_parity.jsonl. There is no free-string type (I1).
// local=true is the actuator's extra pass: realpath + root re-check (symlink escape), must_exist, dir.

// ParamError carries the same codes as params.ParamError.
type ParamError struct {
	Code   string
	Detail string
}

func (e *ParamError) Error() string {
	if e.Detail != "" {
		return e.Code + ": " + e.Detail
	}
	return e.Code
}

func perr(code, detail string) error { return &ParamError{Code: code, Detail: detail} }

// ErrCode is the ParamError code of err ("" when it is not one).
func ErrCode(err error) string {
	if pe, ok := err.(*ParamError); ok {
		return pe.Code
	}
	return ""
}

var (
	rxIdent            = regexp.MustCompile(`^[A-Za-z0-9_.:@-]{1,64}$`)
	rxUnit             = regexp.MustCompile(`^[A-Za-z0-9@._-]{1,120}\.service$`)
	rxWinService       = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,80}$`)
	rxIISPool          = regexp.MustCompile(`^[A-Za-z0-9 ._-]{1,64}$`)
	rxSapSID           = regexp.MustCompile(`^[A-Z][A-Z0-9]{2}$`)
	rxSapNr            = regexp.MustCompile(`^[0-9]{2}$`)
	rxDuration         = regexp.MustCompile(`^([0-9]{1,9})([smhd])$`)
	rxSize             = regexp.MustCompile(`^([0-9]{1,12})([KMG])$`)
	rxEnumDyn          = regexp.MustCompile(`^[A-Za-z0-9_.@][A-Za-z0-9_.@-]{0,63}$`)
	rxIntText          = regexp.MustCompile(`^-?[0-9]{1,12}$`)
	rxPidStart         = regexp.MustCompile(`^[0-9A-Za-z:._-]{1,64}$`)
	rxURL              = regexp.MustCompile(`^(https?)://([A-Za-z0-9.-]{1,253})(:[0-9]{1,5})?(/[A-Za-z0-9._~/%-]*)?(\?[A-Za-z0-9._~=&%-]*)?$`)
	identFamily        = map[string]*regexp.Regexp{"ident": rxIdent, "unit": rxUnit, "win_service": rxWinService, "iis_pool": rxIISPool, "sap_sid": rxSapSID, "sap_nr": rxSapNr}
	durUnit            = map[string]int64{"s": 1, "m": 60, "h": 3600, "d": 86400}
	sizeUnit           = map[string]int64{"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
	knownTypes         = map[string]bool{"enum": true, "int": true, "duration": true, "size": true, "path": true, "ident": true, "unit": true, "win_service": true, "iis_pool": true, "sap_sid": true, "sap_nr": true, "pid": true, "url": true, "host": true}
	pidMax       int64 = 4194304
)

// ParamCtx is what the actuator knows when validating: its own host name, extra hosts local.json allows for url checks, and
// a function mapping a logical path to the real one (identity, or the sandbox root in simulator mode).
type ParamCtx struct {
	Host       string
	AllowHosts []string
	Real       func(string) string // logical -> real file system path (simulator: under the sandbox)
	Logical    func(string) string // real -> logical ("" when outside the sandbox)
}

func invisible(r rune) bool {
	return r <= 0x1f || r == 0x7f || (r >= 0x200b && r <= 0x200f) || (r >= 0x202a && r <= 0x202e) || (r >= 0x2060 && r <= 0x2069) || r == 0xfeff
}

func lookalike(r rune) bool {
	return (r >= 0x370 && r <= 0x3ff) || (r >= 0x400 && r <= 0x4ff) || r == 0x131 || r == 0x17f || r == 0x251 || r == 0x269 || r == 0x28b || r == 0x2113
}

// nfkcUnstable reports runes that Unicode NFKC would change (the standard library has no normaliser): compatibility
// characters and combining marks. It is deliberately broader than NFKC — the actuator may refuse more, never less.
func nfkcUnstable(r rune) bool {
	if r < 0x80 {
		return false
	}
	switch {
	case r == 0xa0, r == 0xa8, r == 0xaa, r == 0xaf, r == 0xb2, r == 0xb3, r == 0xb4, r == 0xb5, r == 0xb8, r == 0xb9, r == 0xba,
		r == 0xbc, r == 0xbd, r == 0xbe:
		return true
	case r >= 0x132 && r <= 0x133, r >= 0x13f && r <= 0x140, r == 0x149, r == 0x17f, r >= 0x1c4 && r <= 0x1cc, r >= 0x1f1 && r <= 0x1f3:
		return true
	case r >= 0x2b0 && r <= 0x2ff, r >= 0x1d2c && r <= 0x1dbf: // modifier letters
		return true
	case r >= 0x2000 && r <= 0x200a, r == 0x2011, r == 0x2024, r == 0x2025, r == 0x2026, r == 0x202f, r >= 0x2033 && r <= 0x2037,
		r == 0x203c, r == 0x203e, r >= 0x2047 && r <= 0x2049, r == 0x2057, r == 0x205f:
		return true
	case r >= 0x2070 && r <= 0x209f, r >= 0x20a8 && r <= 0x20a8, r >= 0x2100 && r <= 0x214f, r >= 0x2150 && r <= 0x218f:
		return true
	case r >= 0x2460 && r <= 0x24ff, r >= 0x2a0c && r <= 0x2a0c, r >= 0x2c7c && r <= 0x2c7d, r >= 0x2e9f && r <= 0x2fdf:
		return true
	case r == 0x3000, r >= 0x3131 && r <= 0x318e, r >= 0x3192 && r <= 0x319f, r >= 0x3200 && r <= 0x33ff:
		return true
	case r >= 0xf900 && r <= 0xfaff, r >= 0xfb00 && r <= 0xfb4f, r >= 0xfb50 && r <= 0xfdff, r >= 0xfe10 && r <= 0xfe19,
		r >= 0xfe30 && r <= 0xfe6f, r >= 0xfe70 && r <= 0xfefc, r >= 0xff00 && r <= 0xffef:
		return true
	case r >= 0x1d400 && r <= 0x1d7ff, r >= 0x1f100 && r <= 0x1f1ff, r >= 0x1ee00 && r <= 0x1eeff, r >= 0x2f800 && r <= 0x2fa1f:
		return true
	}
	return unicode.Is(unicode.Mn, r) || unicode.Is(unicode.Mc, r) || unicode.Is(unicode.Me, r) || unicode.Is(unicode.Zs, r)
}

// pyStr is Python's str() of a decoded JSON scalar.
func pyStr(v any) (string, bool) {
	switch x := v.(type) {
	case string:
		return x, true
	case json.Number:
		return string(x), true
	case int:
		return strconv.Itoa(x), true
	case int64:
		return strconv.FormatInt(x, 10), true
	case float64:
		if x == float64(int64(x)) {
			return strconv.FormatInt(int64(x), 10) + ".0", true
		}
		return PyFloatRepr(x), true
	}
	return "", false
}

func text(v any) (string, error) {
	switch v.(type) {
	case bool, nil, map[string]any, []any:
		return "", perr("type", "a text value is required")
	}
	s, ok := pyStr(v)
	if !ok {
		return "", perr("type", "a text value is required")
	}
	for _, r := range s {
		if invisible(r) {
			return "", perr("control_char", "NUL, newline, control, zero-width or bidi character")
		}
	}
	for _, r := range s {
		if nfkcUnstable(r) || lookalike(r) {
			return "", perr("lookalike", "Unicode look-alike or compatibility character")
		}
	}
	return s, nil
}

func numOpt(spec map[string]any, k string) (int64, bool) {
	v, ok := spec[k]
	if !ok || v == nil {
		return 0, false
	}
	switch x := v.(type) {
	case json.Number:
		n, err := x.Int64()
		if err != nil {
			f, ferr := x.Float64()
			if ferr != nil {
				return 0, false
			}
			return int64(f), true
		}
		return n, true
	case int:
		return int64(x), true
	case int64:
		return x, true
	case float64:
		return int64(x), true
	}
	return 0, false
}

func toInt(v any, spec map[string]any) (int64, error) {
	var n int64
	switch x := v.(type) {
	case bool:
		return 0, perr("type", "integer required")
	case json.Number:
		if !intRx.MatchString(string(x)) {
			return 0, perr("type", "integer required")
		}
		p, err := strconv.ParseInt(string(x), 10, 64)
		if err != nil {
			return 0, perr("bounds", string(x))
		}
		n = p
	case int:
		n = int64(x)
	case int64:
		n = x
	case string:
		t := strings.TrimSpace(x)
		if !rxIntText.MatchString(t) {
			return 0, perr("type", "integer required")
		}
		n, _ = strconv.ParseInt(t, 10, 64)
	default:
		return 0, perr("type", "integer required")
	}
	lo, hasLo := numOpt(spec, "min")
	hi, hasHi := numOpt(spec, "max")
	if (hasLo && n < lo) || (hasHi && n > hi) {
		return 0, perr("bounds", fmt.Sprintf("%d outside [%v, %v]", n, spec["min"], spec["max"]))
	}
	return n, nil
}

// pyNormpath is posixpath.normpath for an absolute path without "..": "//" at the start is kept, "///" becomes "/".
func pyNormpath(p string) string {
	if p == "" {
		return "."
	}
	initial := 0
	if strings.HasPrefix(p, "/") {
		initial = 1
		if strings.HasPrefix(p, "//") && !strings.HasPrefix(p, "///") {
			initial = 2
		}
	}
	var comps []string
	for _, c := range strings.Split(p, "/") {
		if c == "" || c == "." {
			continue
		}
		if c != ".." || (initial == 0 && len(comps) == 0) || (len(comps) > 0 && comps[len(comps)-1] == "..") {
			comps = append(comps, c)
		} else if len(comps) > 0 {
			comps = comps[:len(comps)-1]
		}
	}
	out := strings.Join(comps, "/")
	if initial > 0 {
		out = strings.Repeat("/", initial) + out
	}
	if out == "" {
		return "."
	}
	return out
}

func strList(v any) []string {
	var out []string
	switch x := v.(type) {
	case []any:
		for _, e := range x {
			if s, ok := pyStr(e); ok {
				out = append(out, s)
			}
		}
	case []string:
		out = append(out, x...)
	}
	return out
}

func underRoots(p string, roots []string) bool {
	for _, r := range roots {
		rn := pyNormpath(r)
		if p == rn || strings.HasPrefix(p, strings.TrimRight(rn, "/")+"/") || rn == "/" {
			return true
		}
	}
	return false
}

// Realpath is os.path.realpath (non-strict): symlinks of existing prefixes resolved, the missing tail kept as written.
func Realpath(p string) string {
	if !filepath.IsAbs(p) {
		if wd, err := os.Getwd(); err == nil {
			p = filepath.Join(wd, p)
		}
	}
	parts := strings.Split(filepath.Clean(p), "/")
	cur := "/"
	for i := 1; i < len(parts); i++ {
		part := parts[i]
		if part == "" {
			continue
		}
		next := filepath.Join(cur, part)
		fi, err := os.Lstat(next)
		if err != nil {
			return filepath.Join(append([]string{cur}, parts[i:]...)...)
		}
		if fi.Mode()&os.ModeSymlink == 0 {
			cur = next
			continue
		}
		r, err := filepath.EvalSymlinks(next)
		if err != nil { // a dangling link: resolve its target lexically once
			t, lerr := os.Readlink(next)
			if lerr != nil {
				return filepath.Join(append([]string{next}, parts[i+1:]...)...)
			}
			if !filepath.IsAbs(t) {
				t = filepath.Join(cur, t)
			}
			return filepath.Join(append([]string{filepath.Clean(t)}, parts[i+1:]...)...)
		}
		cur = r
	}
	return cur
}

func pathParam(v any, spec map[string]any, local bool, ctx *ParamCtx) (string, error) {
	s, err := text(v)
	if err != nil {
		return "", err
	}
	if n := utf8.RuneCountInString(s); n == 0 || n > 255 {
		return "", perr("length", "1..255 characters")
	}
	if strings.HasPrefix(s, "-") {
		return "", perr("leading_dash", "")
	}
	if strings.Contains(s, "~") {
		return "", perr("tilde", "")
	}
	if strings.ContainsAny(s, "*?[]{}") {
		return "", perr("glob", "* ? [ ] { } are not allowed")
	}
	roots := strList(spec["roots"])
	win := len(roots) > 0
	for _, r := range roots {
		if !(len(r) >= 3 && ((r[0] >= 'A' && r[0] <= 'Z') || (r[0] >= 'a' && r[0] <= 'z')) && r[1] == ':' && r[2] == '\\') {
			win = false
		}
	}
	for _, part := range strings.FieldsFunc(s, func(r rune) bool { return r == '/' || r == '\\' }) {
		if part == ".." {
			return "", perr("dotdot", "")
		}
	}
	if win {
		return "", perr("os_mismatch", "Windows paths are checked by the Windows actuator")
	}
	if !strings.HasPrefix(s, "/") || strings.Contains(s, "\\") {
		return "", perr("not_absolute", "")
	}
	norm := pyNormpath(s)
	if len(roots) == 0 {
		return "", perr("no_roots", "the template declares no roots")
	}
	if !underRoots(norm, roots) {
		return "", perr("outside_roots", "allowed: "+strings.Join(roots, ", "))
	}
	if local {
		realFS := norm
		if ctx != nil && ctx.Real != nil {
			realFS = ctx.Real(norm)
		}
		resolved := Realpath(realFS)
		logical := resolved
		if ctx != nil && ctx.Logical != nil {
			logical = ctx.Logical(resolved)
		}
		if logical == "" || !underRoots(logical, roots) {
			return "", perr("symlink_escape", norm+" resolves outside the roots")
		}
		fi, statErr := os.Stat(resolved)
		if b, _ := spec["must_exist"].(bool); b && statErr != nil {
			return "", perr("missing", norm)
		}
		if b, _ := spec["dir"].(bool); b && statErr == nil && !fi.IsDir() {
			return "", perr("not_dir", norm)
		}
	}
	return norm, nil
}

func fullMatch(pat, s string) (bool, error) {
	rx, err := regexp.Compile(`^(?:` + pat + `)$`)
	if err != nil {
		return false, err
	}
	return rx.MatchString(s), nil
}

// Validate one parameter value against its spec; the normalised value or a *ParamError.
func Validate(spec map[string]any, value any, local bool, ctx *ParamCtx) (any, error) {
	t, _ := spec["type"].(string)
	if !knownTypes[t] {
		return nil, perr("unknown_type", fmt.Sprintf("%q (there is no free-string type)", t))
	}
	switch t {
	case "enum":
		s, err := text(value)
		if err != nil {
			return nil, err
		}
		if vals, ok := spec["values"]; ok && vals != nil {
			for _, x := range strList(vals) {
				if x == s {
					return s, nil
				}
			}
			return nil, perr("not_in_enum", s)
		}
		if !rxEnumDyn.MatchString(s) {
			return nil, perr("charset", s)
		}
		return s, nil
	case "int":
		n, err := toInt(value, spec)
		if err != nil {
			return nil, err
		}
		return json.Number(strconv.FormatInt(n, 10)), nil
	case "duration", "size":
		s, err := text(value)
		if err != nil {
			return nil, err
		}
		rx, units := rxDuration, durUnit
		if t == "size" {
			rx, units = rxSize, sizeUnit
		}
		m := rx.FindStringSubmatch(s)
		if m == nil {
			if t == "duration" {
				return nil, perr("format", "like 30m / 7d")
			}
			return nil, perr("format", "like 500M / 2G")
		}
		k, _ := strconv.ParseInt(m[1], 10, 64)
		n := k * units[m[2]]
		lo, hasLo := numOpt(spec, "min")
		hi, hasHi := numOpt(spec, "max")
		if (hasLo && n < lo) || (hasHi && n > hi) {
			return nil, perr("bounds", fmt.Sprintf("%d outside [%v, %v]", n, spec["min"], spec["max"]))
		}
		return s, nil
	case "path":
		return pathParam(value, spec, local, ctx)
	case "ident", "unit", "win_service", "iis_pool", "sap_sid", "sap_nr":
		s, err := text(value)
		if err != nil {
			return nil, err
		}
		if strings.HasPrefix(s, "-") {
			return nil, perr("leading_dash", "")
		}
		if !identFamily[t].MatchString(s) {
			short := s
			if utf8.RuneCountInString(short) > 40 {
				short = string([]rune(short)[:40])
			}
			return nil, perr("charset", t+": "+short)
		}
		if pat, _ := spec["pattern"].(string); pat != "" {
			ok, err := fullMatch(pat, s)
			if err != nil || !ok {
				return nil, perr("pattern", "does not match "+pat)
			}
		}
		return s, nil
	case "pid":
		m, ok := value.(map[string]any)
		if !ok {
			return nil, perr("pid_pair", "a pid travels with its process start time: {pid, start}")
		}
		pv, hasPid := m["pid"]
		sv, hasStart := m["start"]
		if !hasPid || !hasStart {
			return nil, perr("pid_pair", "a pid travels with its process start time: {pid, start}")
		}
		pid, err := toInt(pv, map[string]any{"min": json.Number("2"), "max": json.Number(strconv.FormatInt(pidMax, 10))})
		if err != nil {
			return nil, err
		}
		var start any
		switch x := sv.(type) {
		case json.Number:
			if !intRx.MatchString(string(x)) {
				return nil, perr("pid_start", "process start time required")
			}
			start = x
		case int, int64:
			start = x
		case string:
			if !rxPidStart.MatchString(x) {
				return nil, perr("pid_start", "process start time required")
			}
			start = x
		default:
			return nil, perr("pid_start", "process start time required")
		}
		return map[string]any{"pid": json.Number(strconv.FormatInt(pid, 10)), "start": start}, nil
	case "url":
		s, err := text(value)
		if err != nil {
			return nil, err
		}
		m := rxURL.FindStringSubmatch(s)
		if m == nil {
			return nil, perr("format", "http(s)://host[:port]/path")
		}
		host := strings.ToLower(m[2])
		allowed := map[string]bool{"localhost": true, "127.0.0.1": true}
		if ctx != nil {
			if ctx.Host != "" {
				allowed[strings.ToLower(ctx.Host)] = true
			}
			for _, h := range ctx.AllowHosts {
				allowed[strings.ToLower(h)] = true
			}
		}
		if !allowed[host] {
			return nil, perr("url_host", host+" is not the case host, its health_url or an allowlisted host")
		}
		return s, nil
	case "host":
		return nil, perr("host_scope", "the host is the actuator itself; never a job parameter")
	}
	return nil, perr("unknown_type", t)
}

// CheckRendered refuses a rendered argv element with NUL or a line break (§5.5).
func CheckRendered(el string) error {
	if strings.ContainsAny(el, "\x00\n\r") {
		return perr("control_char", "rendered element contains NUL or newline")
	}
	return nil
}
