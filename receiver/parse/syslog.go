package parse

import (
	"math/big"
	"strings"

	"watchover/receiver/parse/py"
)

// Port of src/watchover/parsers/syslog_parser.py (P04 section 1). SYSLOG_RE / SYSLOG_NOPROG_RE / RFC5424_RE come from
// detect.go (syslogMatch / syslogNoProgMatch apply the removed _NOT_LEVEL lookahead).

var (
	asaRE   = py.MustCompile(`^%(?P<vendor>ASA|FTD|PIX|FWSM)-(?P<sev>\d)-(?P<id>\d+)$`)
	sdRE    = py.MustCompile(`\[([\w@.\-]+)((?:\s+[\w.\-]+="(?:[^"\\]|\\.)*")*)\]`)
	sdKVRE  = py.MustCompile(`([\w.\-]+)="((?:[^"\\]|\\.)*)"`)
	panSev  = map[string]string{"critical": "CRITICAL", "high": "ERROR", "medium": "WARN", "low": "INFO", "informational": "INFO"}
	panType = map[string]string{"TRAFFIC": "INFO", "THREAT": "WARN", "SYSTEM": "INFO", "CONFIG": "INFO", "USERID": "INFO",
		"HIPMATCH": "INFO", "GLOBALPROTECT": "INFO"}

	r5424Pri   = rfc5424RE.SubexpIndex("pri")
	r5424TS    = rfc5424RE.SubexpIndex("ts")
	r5424Host  = rfc5424RE.SubexpIndex("host")
	r5424App   = rfc5424RE.SubexpIndex("app")
	r5424Pid   = rfc5424RE.SubexpIndex("pid")
	r5424MsgID = rfc5424RE.SubexpIndex("msgid")
	r5424SD    = rfc5424RE.SubexpIndex("sd")
	r5424Msg   = rfc5424RE.SubexpIndex("msg")

	bsdPri  = syslogRE.SubexpIndex("pri")
	bsdTS   = syslogRE.SubexpIndex("ts")
	bsdHost = syslogRE.SubexpIndex("host")
	bsdProg = syslogRE.SubexpIndex("prog")
	bsdPid  = syslogRE.SubexpIndex("pid")
	bsdMsg  = syslogRE.SubexpIndex("msg")

	npPri  = syslogNoProgRE.SubexpIndex("pri")
	npTS   = syslogNoProgRE.SubexpIndex("ts")
	npHost = syslogNoProgRE.SubexpIndex("host")
	npMsg  = syslogNoProgRE.SubexpIndex("msg")
)

const utf8BOM = "\U0000FEFF"

// subOK returns group g of a FindStringSubmatchIndex result and whether it participated (Python None vs "").
func subOK(s string, m []int, g int) (string, bool) {
	if m[2*g] < 0 {
		return "", false
	}
	return s[m[2*g]:m[2*g+1]], true
}

// sub is subOK without the participation flag (for Python tests where None and "" are both falsy).
func sub(s string, m []int, g int) string {
	v, _ := subOK(s, m, g)
	return v
}

var big8 = big.NewInt(8)

// priSeverity is str(int(pri) % 8); int() raises ValueError for more than 4300 digits.
func priSeverity(pri string) (string, error) {
	n, err := py.Atoi(pri)
	if err != nil {
		return "", pyRaise("ValueError: int(%q)", py.Head(pri, 40))
	}
	return new(big.Int).Mod(n, big8).String(), nil
}

// SyslogParser is the "syslog" parser (P04 section 1).
type SyslogParser struct{}

func (SyslogParser) Name() string       { return "syslog" }
func (SyslogParser) Kind() string       { return "log" }
func (SyslogParser) LineOriented() bool { return true }

// Records yields the (line_no, record) pairs of text.
func (SyslogParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	for idx, ln := range py.SplitLines(text) {
		if isBlank(ln) {
			continue
		}
		rec, err := syslogRecord(ln)
		if err != nil {
			return err
		}
		if rec == nil {
			continue
		}
		if !yield(Record{LineNo: idx + 1, Rec: rec}) {
			return nil
		}
	}
	return nil
}

// syslogRecord parses one non-blank line; nil when no syslog regex matches.
func syslogRecord(ln string) (*py.Dict, error) {
	if m := rfc5424RE.FindStringSubmatchIndex(ln); m != nil {
		rec := py.NewDict(8)
		rec.Set("timestamp", py.S(sub(ln, m, r5424TS)))
		rec.Set("host", py.S(sub(ln, m, r5424Host)))
		app := sub(ln, m, r5424App)
		if app == "-" {
			app = ""
		}
		rec.Set("program", py.S(app))
		rec.Set("message", py.S(sub(ln, m, r5424Msg)))
		if pid := sub(ln, m, r5424Pid); pid != "" && pid != "-" {
			rec.Set("pid", py.S(pid))
		}
		if id := sub(ln, m, r5424MsgID); id != "" && id != "-" {
			rec.Set("msgid", py.S(id))
		}
		sev, err := priSeverity(sub(ln, m, r5424Pri))
		if err != nil {
			return nil, err
		}
		rec.Set("severity", py.S(sev))
		for _, b := range sdRE.FindAllStringSubmatch(sub(ln, m, r5424SD), -1) {
			for _, kv := range sdKVRE.FindAllStringSubmatch(b[2], -1) {
				rec.Set(b[1]+"."+kv[1], py.S(kv[2]))
			}
		}
		if v, _ := rec.Get("message"); strings.HasPrefix(v.S, utf8BOM) {
			rec.Set("message", py.S(v.S[len(utf8BOM):]))
		}
		return rec, nil
	}
	if m := syslogMatch(ln); m != nil {
		rec := py.NewDict(7)
		rec.Set("timestamp", py.S(sub(ln, m, bsdTS)))
		rec.Set("host", py.S(sub(ln, m, bsdHost)))
		prog := sub(ln, m, bsdProg)
		rec.Set("program", py.S(prog))
		rec.Set("message", py.S(sub(ln, m, bsdMsg)))
		if pid := sub(ln, m, bsdPid); pid != "" {
			rec.Set("pid", py.S(pid))
		}
		if pri := sub(ln, m, bsdPri); pri != "" {
			sev, err := priSeverity(pri)
			if err != nil {
				return nil, err
			}
			rec.Set("severity", py.S(sev))
		}
		if a := asaRE.FindStringSubmatch(prog); a != nil {
			rec.Set("program", py.S(strings.ToLower(a[1]))) // ASCII vendor names
			rec.Set("severity", py.S(a[2]))
			rec.Set("msgid", py.S(a[3]))
		}
		return rec, nil
	}
	if m := syslogNoProgMatch(ln); m != nil {
		rec := py.NewDict(8)
		rec.Set("timestamp", py.S(sub(ln, m, npTS)))
		rec.Set("host", py.S(sub(ln, m, npHost)))
		msg := sub(ln, m, npMsg)
		rec.Set("message", py.S(msg))
		if pri := sub(ln, m, npPri); pri != "" {
			sev, err := priSeverity(pri)
			if err != nil {
				return nil, err
			}
			rec.Set("severity", py.S(sev))
		}
		if pan := panRecord(msg); pan != nil {
			pan.Range(func(k string, v py.Value) bool {
				rec.Set(k, v)
				return true
			})
		}
		return rec, nil
	}
	return nil, nil
}

// panRecord is _pan(msg): the PAN-OS CSV payload fields, or nil.
func panRecord(msg string) *py.Dict {
	if !strings.HasPrefix(msg, "1,") || strings.Count(msg, ",") < 20 {
		return nil
	}
	f := strings.Split(msg, ",")
	typ, sb := py.Strip(f[3]), py.Strip(f[4])
	rec := py.NewDict(6)
	rec.Set("service", py.S("pan-os"))
	rec.Set("pan_type", py.S(typ))
	rec.Set("pan_subtype", py.S(sb))
	rec.Set("message", py.S(py.Strip(typ+" "+sb+" "+f[7]+" -> "+f[8])))
	if typ == "THREAT" {
		sev := "WARN"
		for _, x := range sliceStrs(f, 30, 40) {
			if s, ok := panSev[py.Lower(py.Strip(x))]; ok {
				sev = s
				break
			}
		}
		rec.Set("severity", py.S(sev))
		threat := ""
		for _, x := range sliceStrs(f, 30, 36) {
			if strings.HasPrefix(x, `"`) {
				threat = strings.Trim(x, `"`)
				break
			}
		}
		rec.Set("message", py.S(py.Strip(typ+" "+sb+" "+f[7]+" -> "+f[8]+" "+threat)))
	} else {
		sev, ok := panType[typ]
		if !ok {
			sev = "INFO"
		}
		rec.Set("severity", py.S(sev))
	}
	if len(f) > 1 && py.Strip(f[1]) != "" {
		rec.Set("timestamp", py.S(py.Strip(f[1])))
	}
	return rec
}

// sliceStrs is the Python list slice l[i:j] for 0 <= i <= j (clamped).
func sliceStrs(l []string, i, j int) []string {
	if j > len(l) {
		j = len(l)
	}
	if i > j {
		return nil
	}
	return l[i:j]
}
