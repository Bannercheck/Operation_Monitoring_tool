package parse

import (
	"math"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// SapParser is the "sap" parser: port of src/watchover/parsers/sap_parser.py (P05). One stateful line parser; all state
// lives in a sapState created per Records call (nothing survives between calls, like the Python generator locals).
type SapParser struct{}

func (SapParser) Name() string       { return "sap" }
func (SapParser) Kind() string       { return "log" }
func (SapParser) LineOriented() bool { return true }

const (
	sapDays = `(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)`
	sapMon  = `(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)`
	sapD    = py.D
	sapPS   = py.PS
	sapPNS  = py.PNS
	sapNW   = py.NW
	sapI    = py.I
)

// The 33 patterns of sap_parser.py with the verified RE2 rewrites of P05 section 2.1 (prefix match = leading ^,
// fullmatch = ^...$, lookarounds and Unicode \b rewritten as consumed boundaries; only groups are ever used).
var (
	sapDevTsRE     = regexp.MustCompile(`^([A-Z*])` + sapPS + `{1,3}` + sapDays + sapPS + `+(` + sapMon + `)` + sapPS + `+(` + sapD + `{1,2})` + sapPS + `+(` + sapD + `{2}):(` + sapD + `{2}):(` + sapD + `{2})(?::(` + sapD + `{3}))?` + sapPS + `+(` + sapD + `{4})` + sapPS + `*$`)
	sapDevLineRE   = regexp.MustCompile(`^([A-Z*])` + sapPS + `{1,3}(` + sapPNS + `.*)$`)
	sapDevLevelRE  = regexp.MustCompile(`^\*\*\*` + sapPS + `*(ERROR|WARNING|LOG)` + sapPS + `*(?:([A-Z0-9]{3}))?` + sapPS + `*=>` + sapPS + `*(.*)$`)
	sapThrRE       = regexp.MustCompile(`^\[Thr` + sapPS + `+(` + sapD + `+)\]` + sapPS + `*`)
	sapTrcHeadRE   = regexp.MustCompile(`^trc file:` + sapPS + `*"([^"]+)",` + sapPS + `*trc level:` + sapPS + `*(` + sapD + `+),` + sapPS + `*release:` + sapPS + `*"([^"]+)"`)
	sapDevMetaRE   = regexp.MustCompile(`(?i)^(s` + sapI + `d|sysno|p` + sapI + `d|node` + sapPS + `*name|hostname|prof` + sapI + `le|relno|patchno)` + sapPS + `+(.+)$`)
	sapHanaRE      = regexp.MustCompile(`^\[(` + sapD + `+)\]\{(-?` + sapD + `+)\}\[(-?` + sapD + `+)/(-?` + sapD + `+)\]` + sapPS + `+(` + sapD + `{4}-` + sapD + `{2}-` + sapD + `{2}` + sapPS + sapD + `{2}:` + sapD + `{2}:` + sapD + `{2}(?:\.` + sapD + `+)?)` + sapPS + `+([iwefd])` + sapPS + `+(` + sapPNS + `+)` + sapPS + `+(` + sapPNS + `+?)\((` + sapD + `+)\)` + sapPS + `*:` + sapPS + `*(.*)$`)
	sapNwjStartRE  = regexp.MustCompile(`^#(` + sapD + `\.` + sapD + `)[` + py.WSC + `\x08]?#(` + sapD + `{4})` + sapPS + `(` + sapD + `{2})` + sapPS + `(` + sapD + `{2})` + sapPS + `(` + sapD + `{2}):(` + sapD + `{2}):(` + sapD + `{2})(?::(` + sapD + `{3}))?#([+\-]` + sapD + `{4}|[^#]*)#([^#]*)#([^#]*)#`)
	sapLogHeaderRE = regexp.MustCompile(`^<!--([A-Z]+)\[(.*?)\]/-->` + sapPS + `*$`)
	sapAvailRE     = regexp.MustCompile(`^(Available|Unavailable)` + sapPS + `+(` + sapD + `{2})\.(` + sapD + `{2})\.(` + sapD + `{4}) (` + sapD + `{2}):(` + sapD + `{2}):(` + sapD + `{2})` + sapPS + `*-` + sapPS + `*(` + sapD + `{2})\.(` + sapD + `{2})\.(` + sapD + `{4}) (` + sapD + `{2}):(` + sapD + `{2}):(` + sapD + `{2})` + sapPS + `*$`)
	sapJvmPropHdRE = regexp.MustCompile(`^#` + sapPS + `*created at` + sapPS + `+` + sapDays + sapPS + `+(` + sapMon + `)` + sapPS + `+(` + sapD + `{1,2})` + sapPS + `+(` + sapD + `{2}):(` + sapD + `{2}):(` + sapD + `{2})` + sapPS + `+(` + sapD + `{4})` + sapPS + `*$`)
	sapJvmPropKvRE = regexp.MustCompile(`^([A-Za-z][\p{L}\p{N}_.]*)` + sapPS + `*=` + sapPS + `*(.*)$`)
	sapClassLineRE = regexp.MustCompile(`^[\p{L}\p{N}_$/.\-]+\.class` + sapPS + `*$`)
	sapSidPathRE   = regexp.MustCompile(`/usr/sap/([A-Z][A-Z0-9]{2})/([A-Z]+` + sapD + `{2})`)
	sapJulHeadRE   = regexp.MustCompile(`^(` + sapMon + `)` + sapPS + `(` + sapD + `{1,2}),` + sapPS + `(` + sapD + `{4})` + sapPS + `(` + sapD + `{1,2}):(` + sapD + `{2}):(` + sapD + `{2})` + sapPS + `(AM|PM)` + sapPS + `+(` + sapPNS + `+)(?:` + sapPS + `+(` + sapPNS + `+))?` + sapPS + `*$`)
	sapJulLineRE   = regexp.MustCompile(`^(SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST):` + sapPS + `?(.*)$`)
	sapGcUnifiedRE = regexp.MustCompile(`^\[(` + sapD + `{4}-` + sapD + `{2}-` + sapD + `{2}T` + sapD + `{2}:` + sapD + `{2}:` + sapD + `{2}(?:\.` + sapD + `+)?(?:[+\-]` + sapD + `{4}|Z)?)\]((?:\[[^\]]*\])*)` + sapPS + `*(.*)$`)
	sapGcTagRE     = regexp.MustCompile(`\[([^\]]*)\]`)
	sapGcClassicRE = regexp.MustCompile(`^(` + sapD + `{4}-` + sapD + `{2}-` + sapD + `{2}T` + sapD + `{2}:` + sapD + `{2}:` + sapD + `{2}(?:\.` + sapD + `+)?(?:[+\-]` + sapD + `{4}|Z)?):` + sapPS + `+[\p{Nd}.]+:` + sapPS + `+\[(.*)$`)
	sapGcPauseRE   = regexp.MustCompile(`(` + sapD + `+(?:\.` + sapD + `+)?)` + sapPS + `*ms(?:$|` + sapNW + `)`)
	sapGcSecsRE    = regexp.MustCompile(`(` + sapD + `+(?:\.` + sapD + `+)?)` + sapPS + `*secs`)
	sapTpRE        = regexp.MustCompile(`^(` + sapD + `)` + sapPS + `?([A-Z])?([A-Z]{3}` + sapD + `{3})[X ]?` + sapPS + `?(.*)$`)
	sapTpDateRE    = regexp.MustCompile(`(` + sapD + `{2})\.(` + sapD + `{2})\.(` + sapD + `{4})` + sapPS + `*-` + sapPS + `*(` + sapD + `{2}):(` + sapD + `{2}):(` + sapD + `{2})`)
	sapTpStampRE   = regexp.MustCompile(`(?:^|` + sapNW + `)(` + sapD + `{4})(` + sapD + `{2})(` + sapD + `{2})(` + sapD + `{2})(` + sapD + `{2})(` + sapD + `{2})(?:$|` + sapNW + `)`)
	sapAlogRE      = regexp.MustCompile(`^(START|STOP|ERR|WRN)` + sapPS + `+(` + sapPNS + `+)` + sapPS + `+(` + sapPNS + `+)?` + sapPS + `*(` + sapPNS + `+)` + sapPS + `+(` + sapD + `{4})` + sapPS + `+(` + sapD + `{14})` + sapPS + `+(` + sapPNS + `+)?` + sapPS + `*(` + sapPNS + `+)?`)
	sapSm21RE      = regexp.MustCompile(`^(` + sapD + `{2}:` + sapD + `{2}:` + sapD + `{2})` + sapPS + `+(DIA|UPD|UP2|BTC|ENQ|SPO|RD|MS|DP|GW|IC|` + py.W + `{2,3})` + sapPS + `+(` + sapD + `{3})?` + sapPS + `*(` + sapD + `{3})?` + sapPS + `*(` + sapPNS + `*)` + sapPS + `+(` + sapPNS + `*)` + sapPS + `+([A-Z][A-Z0-9])` + sapPS + `+(` + sapD + `)` + sapPS + `+(.*)$`)
	sapDateOnlyRE  = regexp.MustCompile(`(?:^|\P{Nd})(` + sapD + `{2})\.(` + sapD + `{2})\.(` + sapD + `{4})(?:$|\P{Nd})`)
	sapErrWordsRE  = regexp.MustCompile(`(?:^|` + sapNW + `)(?i:error|fa` + sapI + `led|fa` + sapI + `lure|abort|dump|except` + sapI + `on|rc` + sapPS + `*=` + sapPS + `*(?:8|12|16)|ex` + sapI + `t code` + sapPS + `*:` + sapPS + `*"?(?:8|12|16)|cannot|unable|not found|den` + sapI + `ed|t` + sapI + `meout|shortdump)(?:$|` + sapNW + `)`)
	sapWarnWordsRE = regexp.MustCompile(`(?:^|` + sapNW + `)(?i:warn` + sapI + `ng|retry|deprecated|slow|rc` + sapPS + `*=` + sapPS + `*4|ex` + sapI + `t code` + sapPS + `*:` + sapPS + `*"?4)(?:$|` + sapNW + `)`)
	sapTzRE        = regexp.MustCompile(`^[+\-]` + sapD + `{4}$`)
	sapNwjCategRE  = regexp.MustCompile(`^[A-Z]{2,3}(?:-[A-Z0-9]+)+$`)
	sapNwjMsgIDRE  = regexp.MustCompile(`^com\.sap\.[\p{L}\p{N}_.]+\.` + sapD + `{4,}$`)
	sapNwjUserRE   = regexp.MustCompile(`^[A-Za-z][\p{L}\p{N}_.\-]*$`)

	sapFamilyMarkers = []*regexp.Regexp{sapDevTsRE, sapTrcHeadRE, sapHanaRE, sapNwjStartRE, sapJulHeadRE, sapGcUnifiedRE, sapGcClassicRE, sapTpRE, sapAlogRE, sapSm21RE, sapAvailRE, sapClassLineRE}
	sapStrongMarkers = []*regexp.Regexp{sapTrcHeadRE, sapLogHeaderRE, sapJvmPropHdRE}
)

var (
	sapMonths      = map[string]int{"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
	sapHanaLevel   = map[string]string{"i": "INFO", "w": "WARN", "e": "ERROR", "f": "CRITICAL", "d": "DEBUG"}
	sapNwjLevel    = map[string]string{"error": "ERROR", "warning": "WARN", "info": "INFO", "debug": "DEBUG", "path": "DEBUG", "fatal": "CRITICAL", "plain": "INFO"}
	sapJulLevel    = map[string]string{"severe": "ERROR", "warning": "WARN", "info": "INFO", "config": "DEBUG", "fine": "DEBUG", "finer": "DEBUG", "finest": "DEBUG"}
	sapSm21Level   = map[string]string{"0": "INFO", "1": "INFO", "2": "INFO", "3": "WARN", "4": "WARN", "5": "ERROR", "6": "ERROR", "7": "ERROR", "8": "CRITICAL", "9": "CRITICAL"}
	sapGcLevel     = map[string]string{"warning": "WARN", "error": "ERROR", "debug": "DEBUG", "trace": "DEBUG"}
	sapDevLevel    = map[string]string{"ERROR": "ERROR", "WARNING": "WARN", "LOG": "INFO"}
	sapSvcSkip     = map[string]bool{"engine": true, "services": true, "service": true, "core": true, "impl": true, "server": true, "interfaces": true, "jee": true, "tc": true}
	sapDevPrefixes = []string{"M ", "A ", "B ", "C ", "N ", "S ", "X ", "Y ", "I ", "G ", "E ", "* ", "T "}
)

// sapSeparators is set("-=*_# ") of the separator-line test.
const sapSeparators = "-=*_# "

// sapG returns capture group k of a FindStringSubmatchIndex result ("" when it did not participate: Python `m[k] or ""`).
func sapG(s string, m []int, k int) string {
	if m[2*k] < 0 {
		return ""
	}
	return s[m[2*k]:m[2*k+1]]
}

// sapInt is int(str) for the regex-bounded, unsigned Nd digit runs of this file (any Unicode decimal digits).
func sapInt(s string) int {
	n := 0
	for _, r := range s {
		n = n*10 + py.DigitValue(r)
	}
	return n
}

// sapPad is f"{n:0{w}d}" for n >= 0.
func sapPad(n, w int) string {
	t := strconv.Itoa(n)
	for len(t) < w {
		t = "0" + t
	}
	return t
}

// iso is _iso(y, mo, d, h, mi, s, ms); ms == "" means absent (None and "" are both falsy). No range validation.
func iso(y, mo, d, h, mi, s int, ms string) string {
	out := sapPad(y, 4) + "-" + sapPad(mo, 2) + "-" + sapPad(d, 2) + "T" + sapPad(h, 2) + ":" + sapPad(mi, 2) + ":" + sapPad(s, 2)
	if ms != "" {
		out += "." + sapPad(sapInt(ms), 3)
	}
	return out
}

// sevFromWords is _sev_from_words(text, default): ERR_WORDS first, then WARN_WORDS.
func sevFromWords(text, def string) string {
	if sapErrWordsRE.MatchString(text) {
		return "ERROR"
	}
	if sapWarnWordsRE.MatchString(text) {
		return "WARN"
	}
	return def
}

// svcFromLogger is _svc_from_logger(logger) (pure; the Python lru_cache is unobservable).
func svcFromLogger(logger string) string {
	var parts []string
	for _, p := range strings.Split(logger, ".") {
		if p != "" {
			parts = append(parts, p)
		}
	}
	if len(parts) >= 3 && parts[0] == "com" && parts[1] == "sap" {
		for _, p := range parts[2:] {
			if !sapSvcSkip[py.Lower(p)] {
				return "sap-" + py.Lower(p)
			}
		}
		return "sap-" + py.Lower(parts[len(parts)-1])
	}
	if len(parts) >= 2 {
		return py.Lower(parts[1])
	}
	return py.Lower(logger)
}

// sapRSplitLast is s.rsplit(sep, 1)[-1].
func sapRSplitLast(s, sep string) string {
	if i := strings.LastIndex(s, sep); i >= 0 {
		return s[i+len(sep):]
	}
	return s
}

// sapNaive is datetime.fromisoformat on an _iso string without fraction: false when Python raises ValueError.
func sapNaive(y, mo, d, h, mi, s int) (time.Time, bool) {
	if y < 1 || y > 9999 || mo < 1 || mo > 12 || d < 1 || h > 23 || mi > 59 || s > 59 {
		return time.Time{}, false
	}
	t := time.Date(y, time.Month(mo), d, h, mi, s, 0, time.UTC)
	if t.Day() != d {
		return time.Time{}, false
	}
	return t, true
}

// sapRoundInt is round(x) for a float (ties to even) as a Python int; inf / nan raise like Python.
func sapRoundInt(x float64) (py.Value, error) {
	if math.IsInf(x, 0) {
		return py.Value{}, pyRaise("OverflowError: cannot convert float infinity to integer")
	}
	if math.IsNaN(x) {
		return py.Value{}, pyRaise("ValueError: cannot convert float NaN to integer")
	}
	r := math.RoundToEven(x)
	if math.Abs(r) < 1<<62 {
		return py.Int(int64(r)), nil
	}
	bi, _ := new(big.Float).SetFloat64(r).Int(nil)
	return py.BigInt(bi), nil
}

type sapJul struct {
	lineNo             int
	ts, logger, method string
}

type sapNwj struct {
	lineNo int
	rec    *py.Dict
	body   []string // rec["_body"]; the key is popped in _finish_nwj, so it never shows in the key order
	stale  bool     // nwj still points at an already finished record (see the single-line branch)
}

type sapState struct {
	curTs      string // "" = None (_iso never returns "")
	curDate    *[3]string
	meta       *py.Dict
	pendingJul *sapJul
	nwj        *sapNwj
	jvmprop    *py.Dict // the snapshot record without "_props"
	props      *py.Dict // jvmprop["_props"]
	classes    []string
}

func sapTS(ts string) py.Value {
	if ts == "" {
		return py.Null
	}
	return py.S(ts)
}

// metaInto is rec.update({k: v for k, v in meta.items() if k != "host"}).
func (st *sapState) metaInto(rec *py.Dict) {
	st.meta.Range(func(k string, v py.Value) bool {
		if k != "host" {
			rec.Set(k, v)
		}
		return true
	})
}

// metaStr is meta.get(k) as a str ("" when absent; meta values are always str).
func (st *sapState) metaStr(k string) string {
	if v, ok := st.meta.Get(k); ok {
		return v.S
	}
	return ""
}

// Records yields the (line_no, record) pairs of text.
func (SapParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	st := &sapState{meta: py.NewDict(8)}
	emit := func(lineNo int, rec *py.Dict) bool { return yield(Record{LineNo: lineNo, Rec: rec}) }
	for idx, ln := range py.SplitLines(text) {
		ok, err := st.line(idx+1, ln, emit)
		if err != nil {
			return err
		}
		if !ok {
			return nil
		}
	}
	if st.nwj != nil {
		n := st.nwj
		st.nwj = nil
		if n.stale {
			return pyRaise("KeyError: '_body'")
		}
		if !emit(n.lineNo, finishNWJ(n)) {
			return nil
		}
	}
	if st.jvmprop != nil {
		if !emit(1, st.finishJvmProps()) {
			return nil
		}
	}
	if len(st.classes) > 0 {
		emit(1, st.classListRecord())
	}
	return nil
}

// finishJvmProps builds the one record per property snapshot (P05 section 4.20.2).
func (st *sapState) finishJvmProps() *py.Dict {
	jp, pr := st.jvmprop, st.props
	get := func(k, def string) string {
		if v, ok := pr.Get(k); ok {
			return v.S
		}
		return def
	}
	home := get("home", "")
	if m := sapSidPathRE.FindStringSubmatchIndex(home); m != nil {
		sid := sapG(home, m, 1)
		jp.Set("service", py.S("sap-"+py.Lower(sid)))
		jp.Set("sap.sid", py.S(sid))
		jp.Set("sap.instance", py.S(sapG(home, m, 2)))
	}
	jp.Set("message", py.S(py.Strip(get("name", "JVM")+" "+get("version", "")+" (vm "+get("vmVersion", "")+", "+get("type", "")+", "+get("cpu", "")+") home="+home)))
	pr.Range(func(k string, v py.Value) bool {
		jp.Set("jvm."+k, v)
		return true
	})
	return jp
}

// classListRecord builds the class-list summary record (P05 section 4.20.3): stable sort by descending count.
func (st *sapState) classListRecord() *py.Dict {
	type pkgCount struct {
		k string
		n int
	}
	var pk []pkgCount
	pos := map[string]int{}
	for _, c := range st.classes {
		k := c
		if j := strings.LastIndex(c, "/"); j >= 0 {
			k = c[:j]
		}
		if p, ok := pos[k]; ok {
			pk[p].n++
		} else {
			pos[k] = len(pk)
			pk = append(pk, pkgCount{k, 1})
		}
	}
	sort.SliceStable(pk, func(a, b int) bool { return pk[a].n > pk[b].n })
	if len(pk) > 5 {
		pk = pk[:5]
	}
	parts := make([]string, len(pk))
	for j, e := range pk {
		parts[j] = e.k + " (" + strconv.Itoa(e.n) + ")"
	}
	rec := py.NewDict(6)
	rec.Set("timestamp", sapTS(st.curTs))
	rec.Set("severity", py.S("DEBUG"))
	rec.Set("service", py.S("sap-jvm"))
	rec.Set("message", py.S("class prefetch list: "+strconv.Itoa(len(st.classes))+" classes; "+strings.Join(parts, ", ")))
	rec.Set("sap.format", py.S("class-list"))
	rec.Set("jvm.class_count", py.Int(int64(len(st.classes))))
	return rec
}

// line runs the per-line cascade of SapParser.records (P05 sections 4.1-4.19, in that order); false = the consumer
// stopped (generator closed).
func (st *sapState) line(i int, ln string, emit func(int, *py.Dict) bool) (bool, error) {
	if st.nwj != nil && st.nwj.stale {
		return false, pyRaise("KeyError: '_body'")
	}
	s := py.RStrip(ln)
	if py.Strip(s) == "" {
		if st.nwj != nil {
			n := st.nwj
			st.nwj = nil
			return emit(n.lineNo, finishNWJ(n)), nil
		}
		return true, nil
	}
	c0, _ := utf8.DecodeRuneInString(s)
	if st.nwj != nil && c0 != '#' {
		st.nwj.body = append(st.nwj.body, s)
		return true, nil
	}
	if c0 == '#' {
		if m := sapNwjStartRE.FindStringSubmatchIndex(s); m != nil {
			flushed := st.nwj != nil
			if flushed {
				n := st.nwj
				st.nwj = nil
				if !emit(n.lineNo, finishNWJ(n)) {
					return false, nil
				}
			}
			ts := iso(sapInt(sapG(s, m, 2)), sapInt(sapG(s, m, 3)), sapInt(sapG(s, m, 4)), sapInt(sapG(s, m, 5)), sapInt(sapG(s, m, 6)), sapInt(sapG(s, m, 7)), sapG(s, m, 8))
			if tz := sapG(s, m, 9); sapTzRE.MatchString(tz) {
				ts += py.Head(tz, 3) + ":" + py.Slice(tz, 3, py.Len(tz))
			}
			rec := py.NewDict(12)
			rec.Set("timestamp", py.S(ts))
			sev := "INFO"
			if v, ok := sapNwjLevel[py.Lower(py.Strip(sapG(s, m, 10)))]; ok {
				sev = v
			}
			rec.Set("severity", py.S(sev))
			rec.Set("origin", py.S(py.Strip(sapG(s, m, 11))))
			rec.Set("sap.format", py.S("nw-java"))
			st.metaInto(rec)
			n := &sapNwj{lineNo: i, rec: rec, body: []string{s[m[1]:]}}
			if strings.HasSuffix(s, "#") && strings.Count(s, "#") >= 8 { // single-line record
				if flushed {
					// Python flushes the open record without clearing nwj, so nwj still holds the finished record whose
					// "_body" was popped: the next line (every line touches nwj) or the EOF flush raises KeyError.
					st.nwj = &sapNwj{stale: true}
				}
				return emit(i, finishNWJ(n)), nil
			}
			st.nwj = n
			return true, nil
		}
		if st.nwj != nil {
			st.nwj.body = append(st.nwj.body, s)
			return true, nil
		}
	}
	// ---- file headers that are not events ----
	if c0 == '<' {
		if m := sapLogHeaderRE.FindStringSubmatchIndex(s); m != nil {
			name, val := sapG(s, m, 1), sapG(s, m, 2)
			switch name {
			case "NAME":
				st.meta.Set("sap.log_name", py.S(val))
				if ms := sapSidPathRE.FindStringSubmatchIndex(val); ms != nil {
					st.meta.Set("sap.sid", py.S(sapG(val, ms, 1)))
					st.meta.Set("sap.instance", py.S(sapG(val, ms, 2)))
				}
			case "FORMATTER", "ENGINEVERSION":
				st.meta.Set("sap."+py.Lower(name), py.S(val))
			}
			return true, nil
		}
	}
	if c0 == '#' && strings.Contains(s, "created at") {
		if m := sapJvmPropHdRE.FindStringSubmatchIndex(s); m != nil {
			jp := py.NewDict(16)
			jp.Set("timestamp", py.S(iso(sapInt(sapG(s, m, 6)), sapMonths[sapG(s, m, 1)], sapInt(sapG(s, m, 2)), sapInt(sapG(s, m, 3)), sapInt(sapG(s, m, 4)), sapInt(sapG(s, m, 5)), "")))
			jp.Set("severity", py.S("INFO"))
			jp.Set("sap.format", py.S("jvm-properties"))
			st.jvmprop, st.props = jp, py.NewDict(16)
			return true, nil
		}
	}
	if st.jvmprop != nil {
		if m := sapJvmPropKvRE.FindStringSubmatchIndex(s); m != nil {
			st.props.Set(sapG(s, m, 1), py.S(py.Strip(sapG(s, m, 2))))
			return true, nil
		}
	}
	if strings.HasSuffix(s, ".class") && sapClassLineRE.MatchString(s) {
		st.classes = append(st.classes, py.Strip(s))
		return true, nil
	}
	// ---- availability log ----
	if c0 == 'A' || c0 == 'U' {
		if m := sapAvailRE.FindStringSubmatchIndex(s); m != nil {
			return st.availability(i, s, m, emit)
		}
	}
	// ---- HANA trace ----
	if m := sapHanaRE.FindStringSubmatchIndex(s); m != nil {
		comp := sapG(s, m, 7)
		rec := py.NewDict(9)
		rec.Set("timestamp", py.S(strings.ReplaceAll(sapG(s, m, 5), " ", "T")))
		lvl := "INFO"
		if v, ok := sapHanaLevel[sapG(s, m, 6)]; ok {
			lvl = v
		}
		rec.Set("severity", py.S(lvl))
		rec.Set("service", py.S("hana-"+py.Lower(comp)))
		rec.Set("origin", py.S(comp))
		rec.Set("message", py.S(py.Strip(sapG(s, m, 10))))
		rec.Set("sap.thread", py.S(sapG(s, m, 1)))
		rec.Set("sap.connection", py.S(sapG(s, m, 2)))
		rec.Set("sap.source", py.S(sapG(s, m, 8)+":"+sapG(s, m, 9)))
		rec.Set("sap.format", py.S("hana"))
		return emit(i, rec), nil
	}
	// ---- JVM GC ----
	if m := sapGcUnifiedRE.FindStringSubmatchIndex(s); m != nil {
		return st.gcUnified(i, s, m, emit)
	}
	if m := sapGcClassicRE.FindStringSubmatchIndex(s); m != nil {
		return st.gcClassic(i, s, m, emit)
	}
	// ---- java.util.logging two-line ----
	if m := sapJulHeadRE.FindStringSubmatchIndex(s); m != nil {
		h := sapInt(sapG(s, m, 4)) % 12
		if sapG(s, m, 7) == "PM" {
			h += 12
		}
		ts := iso(sapInt(sapG(s, m, 3)), sapMonths[sapG(s, m, 1)], sapInt(sapG(s, m, 2)), h, sapInt(sapG(s, m, 5)), sapInt(sapG(s, m, 6)), "")
		st.pendingJul = &sapJul{lineNo: i, ts: ts, logger: sapG(s, m, 8), method: sapG(s, m, 9)}
		st.curTs = ts
		return true, nil
	}
	if st.pendingJul != nil {
		if m := sapJulLineRE.FindStringSubmatchIndex(s); m != nil {
			pj := st.pendingJul
			st.pendingJul = nil
			lvl := "INFO"
			if v, ok := sapJulLevel[py.Lower(sapG(s, m, 1))]; ok {
				lvl = v
			}
			rec := py.NewDict(7)
			rec.Set("timestamp", py.S(pj.ts))
			rec.Set("severity", py.S(lvl))
			rec.Set("origin", py.S(pj.logger))
			rec.Set("service", py.S(svcFromLogger(pj.logger)))
			rec.Set("message", py.S(py.Strip(sapG(s, m, 2))))
			rec.Set("sap.method", py.S(pj.method))
			rec.Set("sap.format", py.S("java-util-logging"))
			return emit(pj.lineNo, rec), nil
		}
	}
	// ---- ABAP kernel developer trace ----
	if m := sapTrcHeadRE.FindStringSubmatchIndex(s); m != nil {
		st.meta.Set("sap.trc_file", py.S(sapG(s, m, 1)))
		st.meta.Set("sap.trc_level", py.S(sapG(s, m, 2)))
		st.meta.Set("sap.release", py.S(sapG(s, m, 3)))
		return true, nil
	}
	if m := sapDevTsRE.FindStringSubmatchIndex(s); m != nil {
		st.curTs = iso(sapInt(sapG(s, m, 8)), sapMonths[sapG(s, m, 2)], sapInt(sapG(s, m, 3)), sapInt(sapG(s, m, 4)), sapInt(sapG(s, m, 5)), sapInt(sapG(s, m, 6)), sapG(s, m, 7))
		return true, nil
	}
	if strings.Trim(py.Strip(s), sapSeparators) == "" { // set(s.strip()) <= set("-=*_# ")
		return true, nil
	}
	if m := sapDevLineRE.FindStringSubmatchIndex(s); m != nil {
		return st.devLine(i, sapG(s, m, 1), sapG(s, m, 2), emit)
	}
	// ---- SM21 system log export ----
	if m := sapSm21RE.FindStringSubmatchIndex(s); m != nil {
		ts := py.Null
		if st.curDate != nil {
			ts = py.S(st.curDate[2] + "-" + st.curDate[1] + "-" + st.curDate[0] + "T" + sapG(s, m, 1))
		}
		sev := "INFO"
		if v, ok := sapSm21Level[sapG(s, m, 8)]; ok {
			sev = v
		}
		rec := py.NewDict(11)
		rec.Set("timestamp", ts)
		rec.Set("severity", py.S(sev))
		rec.Set("service", py.S("sap-"+py.Lower(sapG(s, m, 2))))
		rec.Set("message", py.S(py.Strip(sapG(s, m, 9))))
		rec.Set("sap.wp_type", py.S(sapG(s, m, 2)))
		rec.Set("sap.wp_no", py.S(sapG(s, m, 3)))
		rec.Set("sap.client", py.S(sapG(s, m, 4)))
		rec.Set("sap.user", py.S(sapG(s, m, 5)))
		rec.Set("sap.tcode", py.S(sapG(s, m, 6)))
		rec.Set("sap.msg_code", py.S(sapG(s, m, 7)+sapG(s, m, 8)))
		rec.Set("sap.format", py.S("sm21"))
		return emit(i, rec), nil
	}
	// ---- transport ALOG / SLOG ----
	if m := sapAlogRE.FindStringSubmatchIndex(s); m != nil {
		stamp := sapG(s, m, 6)
		sl := func(a, b int) int { return sapInt(py.Slice(stamp, a, b)) }
		ts := iso(sl(0, 4), sl(4, 6), sl(6, 8), sl(8, 10), sl(10, 12), sl(12, 14), "")
		sev := "INFO"
		switch sapG(s, m, 1) {
		case "ERR":
			sev = "ERROR"
		case "WRN":
			sev = "WARN"
		}
		rec := py.NewDict(7)
		rec.Set("timestamp", py.S(ts))
		rec.Set("severity", py.S(sev))
		rec.Set("service", py.S("sap-"+py.Lower(sapG(s, m, 4))+"-transport"))
		rec.Set("message", py.S(py.Strip(s)))
		rec.Set("sap.step", py.S(sapG(s, m, 2)))
		rec.Set("sap.rc", py.S(sapG(s, m, 5)))
		rec.Set("sap.format", py.S("transport"))
		return emit(i, rec), nil
	}
	// ---- tp / R3trans / SUM ----
	if m := sapTpRE.FindStringSubmatchIndex(s); m != nil {
		body := sapG(s, m, 4)
		if md := sapTpDateRE.FindStringSubmatchIndex(body); md != nil {
			gi := func(k int) int { return sapInt(sapG(body, md, k)) }
			st.curTs = iso(gi(3), gi(2), gi(1), gi(4), gi(5), gi(6), "")
		} else if ms := sapTpStampRE.FindStringSubmatchIndex(body); ms != nil {
			gi := func(k int) int { return sapInt(sapG(body, ms, k)) }
			st.curTs = iso(gi(1), gi(2), gi(3), gi(4), gi(5), gi(6), "")
		}
		var sev string
		switch sapG(s, m, 2) {
		case "E":
			sev = "ERROR"
		case "W":
			sev = "WARN"
		default:
			sev = sevFromWords(body, "INFO")
		}
		code := sapG(s, m, 3)
		rec := py.NewDict(7)
		rec.Set("timestamp", sapTS(st.curTs))
		rec.Set("severity", py.S(sev))
		rec.Set("service", py.S("sap-"+py.Lower(py.Head(code, 3))))
		rec.Set("message", py.S(py.Strip(body)))
		rec.Set("sap.msg_code", py.S(code))
		rec.Set("sap.level", py.S(sapG(s, m, 1)))
		rec.Set("sap.format", py.S("tp"))
		return emit(i, rec), nil
	}
	// ---- date-only lines (SM21 exports, tp headers) ----
	if md := sapDateOnlyRE.FindStringSubmatchIndex(s); md != nil && py.Len(py.Strip(s)) <= 40 {
		st.curDate = &[3]string{sapG(s, md, 1), sapG(s, md, 2), sapG(s, md, 3)}
		return true, nil
	}
	// ---- anything else: keep as a message with the carried time ----
	rec := py.NewDict(5)
	rec.Set("timestamp", sapTS(st.curTs))
	rec.Set("severity", py.S(sevFromWords(s, "INFO")))
	rec.Set("message", py.S(py.Strip(s)))
	rec.Set("sap.format", py.S("other"))
	if sid := st.metaStr("sap.sid"); sid != "" {
		rec.Set("service", py.S("sap-"+py.Lower(sid)))
	}
	return emit(i, rec), nil
}

// availability is P05 section 4.8; an invalid date raises ValueError in datetime.fromisoformat (end is parsed first).
func (st *sapState) availability(i int, s string, m []int, emit func(int, *py.Dict) bool) (bool, error) {
	gi := func(k int) int { return sapInt(sapG(s, m, k)) }
	start := iso(gi(4), gi(3), gi(2), gi(5), gi(6), gi(7), "")
	end := iso(gi(10), gi(9), gi(8), gi(11), gi(12), gi(13), "")
	te, ok := sapNaive(gi(10), gi(9), gi(8), gi(11), gi(12), gi(13))
	if !ok {
		return false, pyRaise("ValueError: invalid isoformat string %q", end)
	}
	ts, ok := sapNaive(gi(4), gi(3), gi(2), gi(5), gi(6), gi(7))
	if !ok {
		return false, pyRaise("ValueError: invalid isoformat string %q", start)
	}
	mins := int64(math.RoundToEven(float64(te.Unix()-ts.Unix()) / 60))
	state := sapG(s, m, 1)
	sev := "INFO"
	if state == "Unavailable" {
		sev = "ERROR"
	}
	rec := py.NewDict(8)
	rec.Set("timestamp", py.S(start))
	rec.Set("severity", py.S(sev))
	rec.Set("service", py.S("sap-availability"))
	rec.Set("message", py.S(state+" for "+strconv.FormatInt(mins, 10)+" min ("+sapG(s, m, 5)+":"+sapG(s, m, 6)+":"+sapG(s, m, 7)+" - "+sapG(s, m, 8)+"."+sapG(s, m, 9)+"."+sapG(s, m, 10)+" "+sapG(s, m, 11)+":"+sapG(s, m, 12)+":"+sapG(s, m, 13)+")"))
	rec.Set("avail.state", py.S(py.Lower(state)))
	rec.Set("avail.end", py.S(end))
	rec.Set("avail.duration_min", py.Int(mins))
	rec.Set("sap.format", py.S("availability"))
	return emit(i, rec), nil
}

// gcUnified is P05 section 4.10.
func (st *sapState) gcUnified(i int, s string, m []int, emit func(int, *py.Dict) bool) (bool, error) {
	g2, g3 := sapG(s, m, 2), sapG(s, m, 3)
	var tags []string
	for _, t := range sapGcTagRE.FindAllStringSubmatch(g2, -1) {
		tags = append(tags, t[1])
	}
	level := "info"
	for _, t := range tags {
		if t == "info" || t == "warning" || t == "error" || t == "debug" || t == "trace" {
			level = t
			break
		}
	}
	sev := "INFO"
	if v, ok := sapGcLevel[level]; ok {
		sev = v
	}
	pm := sapGcPauseRE.FindStringSubmatchIndex(g3)
	pause, pf := "", 0.0
	if pm != nil {
		pause = sapG(g3, pm, 1)
		f, err := py.ParseFloat(pause)
		if err != nil {
			return false, pyRaise("ValueError: could not convert string to float: %q", pause)
		}
		pf = f
	}
	if pm != nil && pf >= 1000 {
		if pf >= 5000 {
			sev = "ERROR"
		} else {
			sev = "WARN"
		}
	} else if strings.Contains(g3, "Full GC") || strings.Contains(g3, "Pause Full") {
		sev = "WARN"
	}
	var keep []string
	for _, t := range tags {
		if t != level && !strings.HasSuffix(t, "s") {
			keep = append(keep, t)
		}
	}
	rec := py.NewDict(7)
	rec.Set("timestamp", py.S(sapG(s, m, 1)))
	rec.Set("severity", py.S(sev))
	rec.Set("service", py.S("jvm-gc"))
	rec.Set("message", py.S(py.Strip(g3)))
	rec.Set("sap.format", py.S("jvm-gc"))
	rec.Set("gc.tags", py.S(strings.Join(keep, ",")))
	if pm != nil {
		rec.Set("gc.pause_ms", py.S(pause))
	}
	return emit(i, rec), nil
}

// gcClassic is P05 section 4.11.
func (st *sapState) gcClassic(i int, s string, m []int, emit func(int, *py.Dict) bool) (bool, error) {
	g2 := sapG(s, m, 2)
	sev := "INFO"
	if strings.Contains(g2, "Full GC") {
		sev = "WARN"
	}
	pm := sapGcSecsRE.FindStringSubmatchIndex(g2)
	var pf float64
	if pm != nil {
		f, err := py.ParseFloat(sapG(g2, pm, 1))
		if err != nil {
			return false, pyRaise("ValueError: could not convert string to float: %q", sapG(g2, pm, 1))
		}
		pf = f
		if pf >= 1.0 {
			if pf >= 5.0 {
				sev = "ERROR"
			} else {
				sev = "WARN"
			}
		}
	}
	rec := py.NewDict(6)
	rec.Set("timestamp", py.S(sapG(s, m, 1)))
	rec.Set("severity", py.S(sev))
	rec.Set("service", py.S("jvm-gc"))
	rec.Set("message", py.S("["+py.Strip(g2)))
	rec.Set("sap.format", py.S("jvm-gc"))
	if pm != nil {
		v, err := sapRoundInt(pf * 1000)
		if err != nil {
			return false, err
		}
		rec.Set("gc.pause_ms", py.S(v.IntString()))
	}
	return emit(i, rec), nil
}

// devLine is the DEV_LINE branch of P05 section 4.14 (step 4).
func (st *sapState) devLine(i int, comp, body string, emit func(int, *py.Dict) bool) (bool, error) {
	if comp == "*" && !strings.HasPrefix(body, "***") { // "*  ACTIVE TRACE LEVEL 1" header decoration
		return true, nil
	}
	if mm := sapDevMetaRE.FindStringSubmatchIndex(body); mm != nil {
		key := strings.ReplaceAll(py.Lower(sapG(body, mm, 1)), " ", "")
		val := py.Strip(sapG(body, mm, 2))
		switch key {
		case "sid":
			st.meta.Set("sap.sid", py.S(val))
		case "pid":
			st.meta.Set("sap.pid", py.S(val))
		case "nodename", "hostname":
			st.meta.Set("host", py.S(val))
		case "profile":
			st.meta.SetDefault("host", py.S(sapRSplitLast(val, "_")))
			st.meta.Set("sap.instance", py.S(sapRSplitLast(val, "/")))
		}
		return true, nil
	}
	thr := ""
	if mt := sapThrRE.FindStringSubmatchIndex(body); mt != nil {
		thr, body = sapG(body, mt, 1), body[mt[1]:]
	}
	sev, code := "INFO", ""
	if ml := sapDevLevelRE.FindStringSubmatchIndex(body); ml != nil {
		kind := sapG(body, ml, 1)
		sev, code, body = sapDevLevel[kind], sapG(body, ml, 2), sapG(body, ml, 3)
		if kind == "LOG" {
			sev = sevFromWords(body, "INFO")
		}
	} else if strings.HasPrefix(body, "***") {
		sev = "ERROR"
	}
	if st.curTs == "" && body == "" {
		return true, nil
	}
	rec := py.NewDict(16)
	rec.Set("timestamp", sapTS(st.curTs))
	rec.Set("severity", py.S(sev))
	rec.Set("message", py.S(py.Strip(body)))
	rec.Set("sap.component", py.S(comp))
	rec.Set("sap.format", py.S("dev-trace"))
	if sid := st.metaStr("sap.sid"); sid != "" {
		rec.Set("service", py.S("sap-"+py.Lower(sid)))
	} else {
		tf := "kernel"
		if v, ok := st.meta.Get("sap.trc_file"); ok {
			tf = v.S
		}
		if j := strings.Index(tf, "."); j >= 0 {
			tf = tf[:j]
		}
		rec.Set("service", py.S("sap-"+py.Lower(tf)))
	}
	st.metaInto(rec)
	if h := st.metaStr("host"); h != "" {
		rec.Set("host", py.S(h))
	}
	if thr != "" {
		rec.Set("sap.thread", py.S(thr))
	}
	if code != "" {
		rec.Set("sap.msg_code", py.S(code))
	}
	return emit(i, rec), nil
}

// finishNWJ is SapParser._finish_nwj (P05 section 5).
func finishNWJ(n *sapNwj) *py.Dict {
	rec := n.rec
	body := strings.Join(n.body, "\n")
	raw := strings.Split(body, "#")
	fields := make([]string, len(raw))
	last, hasNonEmpty := "", false
	for j, f := range raw {
		fields[j] = py.Strip(f)
		if fields[j] != "" {
			last, hasNonEmpty = fields[j], true
		}
	}
	if hasNonEmpty {
		rec.Set("message", py.S(last))
	} else {
		rec.Set("message", py.S(py.Strip(body)))
	}
	lim := len(fields)
	if lim > 12 {
		lim = 12
	}
	for _, f := range fields[:lim] {
		if f == "" {
			continue
		}
		switch {
		case sapNwjCategRE.MatchString(f):
			rec.SetDefault("sap.category", py.S(f))
		case sapNwjMsgIDRE.MatchString(f):
			rec.SetDefault("sap.msg_id", py.S(f))
		case strings.Contains(f, "~"):
			rec.SetDefault("sap.application", py.S(f))
		case strings.HasPrefix(f, "com.sap.") && !rec.Has("sap.logger"):
			rec.Set("sap.logger", py.S(f))
		}
	}
	for idx := 1; idx < lim; idx++ { // user follows the logger field
		f := fields[idx]
		if lg, ok := rec.Get("sap.logger"); ok && strings.HasPrefix(f, "com.sap.") && f == lg.S && idx+1 < len(fields) {
			if sapNwjUserRE.MatchString(fields[idx+1]) {
				rec.Set("sap.user", py.S(fields[idx+1]))
			}
			break
		}
	}
	loc := ""
	if v, ok := rec.Get("origin"); ok {
		loc = v.S
	}
	if strings.HasPrefix(loc, "/") {
		rec.Set("service", py.S("sap-"+py.Lower(sapRSplitLast(py.StripChars(loc, "/"), "/"))))
	} else if loc != "" {
		rec.Set("service", py.S(svcFromLogger(loc)))
	}
	return rec
}

// sapScore is sap_parser.sap_score(lines) (P05 section 6); detect_format uses it at step 11.
func sapScore(lines []string) float64 {
	if len(lines) == 0 {
		return 0
	}
	head := lines
	if len(head) > 20 {
		head = head[:20]
	}
	for _, ln := range head {
		for _, r := range sapStrongMarkers {
			if r.MatchString(ln) {
				return 1.0
			}
		}
	}
	hits := 0.0
	for _, ln := range lines {
		hit := strings.HasPrefix(ln, "#") || sapJulLineRE.MatchString(ln)
		for _, r := range sapFamilyMarkers {
			if hit {
				break
			}
			hit = r.MatchString(ln)
		}
		if hit {
			hits++
			continue
		}
		if sapDevLineRE.MatchString(ln) {
			pre := strings.Contains(ln, "***")
			for _, p := range sapDevPrefixes {
				if pre {
					break
				}
				pre = strings.HasPrefix(ln, p)
			}
			if pre {
				hits += 0.6
			}
		}
	}
	return py.Round(hits/float64(len(lines)), 2)
}
