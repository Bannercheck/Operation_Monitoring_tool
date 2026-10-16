package act

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
)

// The host allowlist (§5.1–§5.4): /etc/watchover/actions.d holds local.json (local policy) and *.tmpl.json packs, root-owned
// and writable by nobody else. The dashboard can never push or change it; the actuator reports (id, sha256, risk, kind, os) of
// every entry at hello and refuses jobs for anything else.

// SemanticKeys are what the template sha256 binds to (templates.SEMANTIC_KEYS): titles and hints are excluded.
var SemanticKeys = []string{"id", "rev", "os", "kind", "risk", "reversibility", "rollback", "run_as", "argv", "dry_argv", "script", "builtin",
	"dry_run", "params", "pre", "timeout_s", "max_output_kb", "env", "limits", "output", "writes"}

var riskRank = map[string]int{"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}

// Template is one allowlist entry as loaded (Raw keeps the decoded JSON for the semantic hash and parameter specs).
type Template struct {
	ID, OS, Kind, Risk, Pack string
	Rev                      int
	SHA256                   string
	Raw                      map[string]any
	Params                   map[string]map[string]any
	Lint                     []string
}

// SemanticSHA is sha256(canonical(entry restricted to SemanticKeys)).
func SemanticSHA(t map[string]any) string {
	sem := map[string]any{}
	for _, k := range SemanticKeys {
		if v, ok := t[k]; ok {
			sem[k] = v
		}
	}
	return SHA256Hex(sem)
}

func (t *Template) Str(k string) string {
	s, _ := t.Raw[k].(string)
	return s
}

func (t *Template) Int(k string, def int) int {
	if n, ok := numOpt(t.Raw, k); ok {
		return int(n)
	}
	return def
}

func (t *Template) Bool(k string) bool {
	b, _ := t.Raw[k].(bool)
	return b
}

// Strings of a list field (argv, dry_argv, writes).
func (t *Template) Strings(k string) []string { return strList(t.Raw[k]) }

// Privacy is "structured" when the output names a parser, else "raw" (templates.privacy_of, for the parsers this actuator has).
func (t *Template) Privacy() string {
	if p := t.Str("privacy"); p == "raw" || p == "structured" {
		return p
	}
	if out, ok := t.Raw["output"].(map[string]any); ok {
		if p, _ := out["parser"].(string); p != "" {
			return "structured"
		}
	}
	return "raw"
}

// Parser named by output.parser.
func (t *Template) Parser() string {
	if out, ok := t.Raw["output"].(map[string]any); ok {
		p, _ := out["parser"].(string)
		return p
	}
	return ""
}

// Policy is local.json (§5.3): everything here narrows; nothing can widen what a template allows.
type Policy struct {
	Format          int               `json:"format"`
	Enabled         bool              `json:"enabled"`
	MaxRisk         string            `json:"max_risk"`
	Modes           []string          `json:"modes"`
	MaxActPerHour   int               `json:"max_act_per_hour"`
	MaxDiagPerHour  int               `json:"max_diag_per_hour"`
	ActConcurrency  int               `json:"act_concurrency"`
	DiagConcurrency int               `json:"diag_concurrency"`
	Hours           []HoursRange      `json:"hours"`
	Freeze          []FreezeRange     `json:"freeze"`
	Cosign          map[string]any    `json:"cosign"`
	Deny            []string          `json:"deny"`
	RequireKey      *bool             `json:"require_confirmed_key"`
	AllowHosts      []string          `json:"allow_hosts"`
	Raw             map[string]any    `json:"-"`
	Extra           map[string]string `json:"-"`
}

type HoursRange struct {
	Days string `json:"days"`
	From string `json:"from"`
	To   string `json:"to"`
	TZ   string `json:"tz"`
}

type FreezeRange struct {
	From    string `json:"from"`
	To      string `json:"to"`
	TZ      string `json:"tz"`
	MaxRisk string `json:"max_risk"`
}

// DefaultPolicy is what the installer writes: diagnose only (max_risk R0) until the host administrator raises it.
func DefaultPolicy() Policy {
	return Policy{Format: 1, Enabled: true, MaxRisk: "R0", Modes: []string{"diagnose", "dry_run", "execute", "verify", "rollback"},
		MaxActPerHour: 20, MaxDiagPerHour: 120, ActConcurrency: 1, DiagConcurrency: 2,
		Hours: []HoursRange{{Days: "mon-sun", From: "00:00", To: "24:00", TZ: "Europe/Istanbul"}}, Deny: []string{}}
}

// Allowlist is the loaded actions.d.
type Allowlist struct {
	Dir         string
	Policy      Policy
	Templates   map[string]*Template
	PermsOK     bool
	PermsDetail string
	Errors      []string
	PolicySHA   string
	FreezeSHA   string
	CosignKeys  int
}

var (
	shellRx = regexp.MustCompile(`(?i)^(sh|bash|dash|zsh|ksh|csh|tcsh|fish|ash|cmd|cmd\.exe|env|xargs|python[0-9]*(\.[0-9]+)?|python[0-9]*\.exe|perl|busybox|powershell|powershell\.exe|pwsh|pwsh\.exe|wscript\.exe|cscript\.exe|mshta\.exe|rundll32\.exe|node|ruby|php|lua)$`)
	idRx    = regexp.MustCompile(`^[a-z0-9_]+(\.[a-z0-9_]+)+$`)
)

// checkPerms: actions.d, every file in it and the scripts directory owned by uid 0 and not group- or other-writable.
// relaxed (simulator mode) only refuses other-writable files.
func checkPerms(paths []string, relaxed bool) (bool, string) {
	for _, p := range paths {
		fi, err := os.Lstat(p)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return false, p + ": " + err.Error()
		}
		st, ok := fi.Sys().(*syscall.Stat_t)
		mode := fi.Mode().Perm()
		if relaxed {
			if mode&0o002 != 0 {
				return false, p + " is writable by others"
			}
			continue
		}
		if !ok || st.Uid != 0 {
			return false, p + " is not owned by root"
		}
		if mode&0o022 != 0 {
			return false, p + " is group- or other-writable"
		}
		if fi.Mode()&os.ModeSymlink != 0 {
			return false, p + " is a symbolic link"
		}
	}
	return true, ""
}

// LoadAllowlist reads actions.d (and checks the scripts directory's ownership). It never fails as a whole: problems are
// reported through PermsOK / Errors and every job is refused while the permissions are wrong.
func LoadAllowlist(dir, scriptsDir string, relaxed bool, osName string) *Allowlist {
	a := &Allowlist{Dir: dir, Templates: map[string]*Template{}, Policy: DefaultPolicy()}
	a.Policy.Enabled = false
	check := []string{dir}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		check = append(check, filepath.Join(dir, e.Name()))
	}
	if scriptsDir != "" {
		check = append(check, scriptsDir)
		if se, err := os.ReadDir(scriptsDir); err == nil {
			for _, e := range se {
				check = append(check, filepath.Join(scriptsDir, e.Name()))
			}
		}
	}
	a.PermsOK, a.PermsDetail = checkPerms(check, relaxed)
	if raw, err := os.ReadFile(filepath.Join(dir, "local.json")); err == nil {
		var m map[string]any
		if err := Decode(raw, &m); err != nil {
			a.Errors = append(a.Errors, "local.json: "+err.Error())
		} else {
			p := DefaultPolicy()
			p.Enabled = false
			_ = json.Unmarshal(raw, &p)
			p.Raw = m
			if _, ok := m["enabled"]; !ok {
				p.Enabled = true
			}
			if riskRankOf(p.MaxRisk) < 0 {
				p.MaxRisk = "R0"
			}
			a.Policy = p
			a.PolicySHA = SHA256Hex(m)
			a.FreezeSHA = SHA256Hex(anyOr(m["freeze"], []any{}))
			if c, ok := m["cosign"].(map[string]any); ok {
				a.CosignKeys = len(strList(c["keys"]))
			}
		}
	} else {
		a.Errors = append(a.Errors, "local.json missing: actions disabled")
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.tmpl.json"))
	sort.Strings(files)
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			a.Errors = append(a.Errors, filepath.Base(f)+": "+err.Error())
			continue
		}
		var pack map[string]any
		if err := Decode(raw, &pack); err != nil {
			a.Errors = append(a.Errors, filepath.Base(f)+": "+err.Error())
			continue
		}
		if b, _ := pack["disabled"].(bool); b {
			continue
		}
		list, _ := pack["templates"].([]any)
		byID := map[string]map[string]any{}
		for _, x := range list {
			if m, ok := x.(map[string]any); ok {
				id, _ := m["id"].(string)
				byID[id] = m
			}
		}
		packName, _ := pack["pack"].(string)
		for _, x := range list {
			m, ok := x.(map[string]any)
			if !ok {
				continue
			}
			t := newTemplate(m, packName)
			t.Lint = lintTemplate(t, byID)
			if t.OS != osName {
				continue // another OS's template: reported by nobody, run by nobody
			}
			if len(t.Lint) > 0 {
				a.Errors = append(a.Errors, t.ID+": "+strings.Join(t.Lint, ", "))
				continue
			}
			if _, dup := a.Templates[t.ID]; dup {
				a.Errors = append(a.Errors, t.ID+": duplicate id")
				delete(a.Templates, t.ID)
				continue
			}
			a.Templates[t.ID] = t
		}
	}
	return a
}

func anyOr(v any, def any) any {
	if v == nil {
		return def
	}
	return v
}

func newTemplate(m map[string]any, pack string) *Template {
	t := &Template{Raw: m, Pack: pack, Params: map[string]map[string]any{}}
	t.ID, _ = m["id"].(string)
	t.OS, _ = m["os"].(string)
	t.Kind, _ = m["kind"].(string)
	t.Risk, _ = m["risk"].(string)
	if n, ok := numOpt(m, "rev"); ok {
		t.Rev = int(n)
	}
	if ps, ok := m["params"].(map[string]any); ok {
		for k, v := range ps {
			if spec, ok := v.(map[string]any); ok {
				t.Params[k] = spec
			}
		}
	}
	t.SHA256 = SemanticSHA(m)
	return t
}

func riskRankOf(r string) int {
	if n, ok := riskRank[r]; ok {
		return n
	}
	return -1
}

// lintTemplate is the actuator's own pass over the server lint (§5.4) for what matters on the host.
func lintTemplate(t *Template, pack map[string]map[string]any) []string {
	var out []string
	if !idRx.MatchString(t.ID) {
		out = append(out, "bad_id")
	}
	if riskRankOf(t.Risk) < 0 {
		out = append(out, "bad_risk")
	}
	if t.Kind != "diag" && t.Kind != "act" {
		out = append(out, "bad_kind")
	}
	bodies := 0
	for _, b := range []string{"argv", "script", "builtin"} {
		if _, ok := t.Raw[b]; ok {
			bodies++
		}
	}
	if bodies != 1 {
		out = append(out, "one_body")
	}
	for name, spec := range t.Params {
		ty, _ := spec["type"].(string)
		if !knownTypes[ty] || ty == "host" {
			out = append(out, "param_type:"+name)
		}
		if ty == "path" && len(strList(spec["roots"])) == 0 {
			out = append(out, "missing_roots:"+name)
		}
	}
	checkArgv := func(argv []string, where string) {
		if len(argv) == 0 {
			out = append(out, "bad_"+where)
			return
		}
		if !strings.HasPrefix(argv[0], "/") {
			out = append(out, where+"0_absolute")
		} else if shellRx.MatchString(filepath.Base(argv[0])) {
			out = append(out, "shell")
		}
		if len(placeholders(argv[0])) > 0 {
			out = append(out, "placeholder_argv0")
		}
		for _, el := range argv {
			for _, ph := range placeholders(el) {
				if _, ok := t.Params[ph]; !ok {
					out = append(out, "undeclared:"+ph)
				}
			}
			if strings.HasPrefix(strings.ToLower(el), "-command") || strings.EqualFold(el, "-encodedcommand") {
				out = append(out, "ps_command")
			}
		}
	}
	if _, ok := t.Raw["argv"]; ok {
		checkArgv(t.Strings("argv"), "argv")
	}
	if _, ok := t.Raw["dry_argv"]; ok {
		checkArgv(t.Strings("dry_argv"), "dry_argv")
	}
	if sc, ok := t.Raw["script"].(map[string]any); ok {
		p, _ := sc["path"].(string)
		sha, _ := sc["sha256"].(string)
		if !strings.HasPrefix(p, "/") && t.OS == "linux" {
			out = append(out, "script_absolute")
		}
		if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(sha) {
			out = append(out, "script_sha256")
		}
		if shellRx.MatchString(filepath.Base(p)) {
			out = append(out, "shell")
		}
	}
	if b, ok := t.Raw["builtin"].(string); ok {
		if _, known := builtinKinds[b]; !known {
			out = append(out, "unknown_builtin")
		}
	}
	if t.Kind == "diag" && (t.Risk != "R0" || len(t.Strings("writes")) > 0) {
		out = append(out, "diag_risk")
	}
	if t.Kind == "act" {
		rev := t.Str("reversibility")
		switch rev {
		case "transient", "data_preserving", "reversible", "irreversible":
		default:
			out = append(out, "reversibility")
		}
		if rev == "reversible" {
			rb := t.Str("rollback")
			if other, ok := pack[rb]; !ok || other["os"] != t.Raw["os"] {
				out = append(out, "rollback_pack")
			}
		}
	}
	if to := t.Int("timeout_s", 0); to <= 0 || to > 1800 {
		out = append(out, "timeout")
	}
	if t.Int("max_output_kb", 16) > 256 {
		out = append(out, "max_output")
	}
	for _, p := range preList(t) {
		if _, ok := predicateArity[p.name]; !ok {
			out = append(out, "unknown_predicate:"+p.name)
		}
	}
	return out
}

type predCall struct {
	name string
	args []any
}

func preList(t *Template) []predCall {
	var out []predCall
	list, _ := t.Raw["pre"].([]any)
	for _, x := range list {
		m, ok := x.(map[string]any)
		if !ok {
			continue
		}
		name, _ := m["pred"].(string)
		args, _ := m["args"].([]any)
		out = append(out, predCall{name: name, args: args})
	}
	return out
}

// placeholders of an element: "{name}" not part of "{{" / "}}" (templates.PLACEHOLDER).
func placeholders(s string) []string {
	var out []string
	for i := 0; i < len(s); i++ {
		if s[i] != '{' || (i > 0 && s[i-1] == '{') {
			continue
		}
		j := i + 1
		for j < len(s) && (s[j] == '_' || (s[j] >= 'a' && s[j] <= 'z') || (s[j] >= 'A' && s[j] <= 'Z') || (j > i+1 && s[j] >= '0' && s[j] <= '9')) {
			j++
		}
		if j == i+1 || j >= len(s) || s[j] != '}' || (j+1 < len(s) && s[j+1] == '}') {
			continue
		}
		out = append(out, s[i+1:j])
	}
	return out
}

// Fill renders one element: placeholders replaced by the validated values, then "{{" / "}}" become literal braces.
func Fill(el string, vals map[string]any) (string, error) {
	var b strings.Builder
	for i := 0; i < len(el); {
		if el[i] == '{' && (i == 0 || el[i-1] != '{') {
			j := i + 1
			for j < len(el) && (el[j] == '_' || (el[j] >= 'a' && el[j] <= 'z') || (el[j] >= 'A' && el[j] <= 'Z') || (j > i+1 && el[j] >= '0' && el[j] <= '9')) {
				j++
			}
			if j > i+1 && j < len(el) && el[j] == '}' && !(j+1 < len(el) && el[j+1] == '}') {
				name := el[i+1 : j]
				v, ok := vals[name]
				if !ok {
					return "", perr("missing_param", name)
				}
				b.WriteString(valueString(v))
				i = j + 1
				continue
			}
		}
		b.WriteByte(el[i])
		i++
	}
	s := strings.ReplaceAll(strings.ReplaceAll(b.String(), "{{", "{"), "}}", "}")
	return s, CheckRendered(s)
}

func valueString(v any) string {
	if m, ok := v.(map[string]any); ok {
		if p, ok := m["pid"]; ok {
			s, _ := pyStr(p)
			return s
		}
	}
	s, _ := pyStr(v)
	return s
}

// ValidateParams checks every declared parameter (defaults applied), no extra one, with the local pass (§9.3.4 #7).
func ValidateParams(t *Template, values map[string]any, ctx *ParamCtx) (map[string]any, error) {
	for k := range values {
		if _, ok := t.Params[k]; !ok {
			return nil, perr("extra_param", k)
		}
	}
	out := map[string]any{}
	names := make([]string, 0, len(t.Params))
	for k := range t.Params {
		names = append(names, k)
	}
	sort.Strings(names)
	for _, name := range names {
		spec := t.Params[name]
		v, ok := values[name]
		if !ok {
			def, hasDef := spec["default"]
			if !hasDef {
				return nil, perr("missing_param", name)
			}
			v = def
		}
		nv, err := Validate(spec, v, true, ctx)
		if err != nil {
			if pe, ok := err.(*ParamError); ok {
				pe.Detail = name + ": " + pe.Detail
			}
			return nil, err
		}
		out[name] = nv
	}
	return out, nil
}

// --------------------------------------------------------------- local policy: hours and freeze

var dayIdx = map[string]int{"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}

func dayIn(spec string, wd int) bool {
	for _, part := range strings.Split(strings.ToLower(spec), ",") {
		part = strings.TrimSpace(part)
		if a, b, ok := strings.Cut(part, "-"); ok {
			x, y := dayIdx[a], dayIdx[b]
			if x == 0 || y == 0 {
				continue
			}
			if x <= y && wd >= x && wd <= y || x > y && (wd >= x || wd <= y) {
				return true
			}
		} else if dayIdx[part] == wd {
			return true
		}
	}
	return false
}

func hm(s string) int {
	var h, m int
	if _, err := fmt.Sscanf(s, "%d:%d", &h, &m); err != nil {
		return -1
	}
	return h*60 + m
}

func loc(tz string) *time.Location {
	if tz == "" {
		return time.UTC
	}
	l, err := time.LoadLocation(tz)
	if err != nil {
		return nil
	}
	return l
}

// InHours: no ranges → always; an unreadable range never matches (fail closed).
func (p *Policy) InHours(now time.Time) bool {
	if len(p.Hours) == 0 {
		return true
	}
	for _, h := range p.Hours {
		l := loc(h.TZ)
		if l == nil {
			continue
		}
		t := now.In(l)
		wd := int(t.Weekday())
		if wd == 0 {
			wd = 7
		}
		if !dayIn(h.Days, wd) {
			continue
		}
		cur, from, to := t.Hour()*60+t.Minute(), hm(h.From), hm(h.To)
		if from < 0 || to < 0 {
			continue
		}
		if cur >= from && cur < to {
			return true
		}
	}
	return false
}

// FreezeMaxRisk is the max_risk of the local freeze range covering now ("" when none; "R0" for an unreadable range).
func (p *Policy) FreezeMaxRisk(now time.Time) string {
	best := ""
	for _, f := range p.Freeze {
		l := loc(f.TZ)
		if l == nil {
			return "R0"
		}
		day := now.In(l).Format("2006-01-02")
		if f.From == "" || f.To == "" || day < f.From || day > f.To {
			continue
		}
		r := f.MaxRisk
		if riskRankOf(r) < 0 {
			r = "R0"
		}
		if best == "" || riskRankOf(r) < riskRankOf(best) {
			best = r
		}
	}
	return best
}

// ModeAllowed: an empty list allows every mode.
func (p *Policy) ModeAllowed(mode string) bool {
	if len(p.Modes) == 0 {
		return true
	}
	for _, m := range p.Modes {
		if m == mode {
			return true
		}
	}
	return false
}

func (p *Policy) Denied(id string) bool {
	for _, d := range p.Deny {
		if d == id {
			return true
		}
	}
	return false
}

func (p *Policy) RequireConfirmedKey() bool { return p.RequireKey == nil || *p.RequireKey }
