package parse

import (
	"regexp"
	"strings"

	"watchover/receiver/parse/py"
)

// Port of src/watchover/parsers/access_parser.py (P04 section 2).

var (
	accClfRE = py.MustCompile(`^(?P<client>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>\d{3}|-) (?P<bytes>\S+)` +
		`(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?(?: (?P<extra>.*))?$`)
	// VHOST_RE ^(?P<vhost>[\w.\-]+)(?::\d+)? (?=\S+ \S+ \S+ \[) : the prefix match is unique, the lookahead is checked on
	// the remainder with accVhostAheadRE.
	accVhostRE      = py.MustCompile(`^([\w.\-]+)(?::\d+)? `)
	accVhostAheadRE = py.MustCompile(`^\S+ \S+ \S+ \[`)
	accAlbRE        = py.MustCompile(`^(?P<proto>https?|h2|ws|wss|tls|tcp) (?P<ts>\S+) (?P<elb>\S+) (?P<client>\S+) (?P<target>\S+) (?P<t_req>\S+) (?P<t_tgt>\S+) (?P<t_resp>\S+) ` +
		`(?P<status>\S+) (?P<tstatus>\S+) (?P<rbytes>\S+) (?P<sbytes>\S+) "(?P<request>[^"]*)" "(?P<agent>[^"]*)" \S+ \S+ (?P<tg>\S+) "[^"]*" "(?P<domain>[^"]*)"`)
	accEnvoyRE = py.MustCompile(`^\[(?P<ts>[^\]]+)\] "(?P<request>[A-Z]+ \S+ [^"]*)" (?P<status>\d{3}|-) (?P<flags>\S+) (?:\S+ )?(?P<rx>\d+|-) (?P<tx>\d+|-) ` +
		`(?P<dur>\d+|-) (?P<up>\d+|-) "(?P<xff>[^"]*)" "(?P<agent>[^"]*)" "(?P<reqid>[^"]*)" "(?P<authority>[^"]*)" "(?P<upstream>[^"]*)"`)
	accExtraKV = py.MustCompile(`(?P<k>[A-Za-z_][\w.\-]*)=(?P<v>"[^"]*"|\S+)`)
	accUUIDish = regexp.MustCompile(`^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$|^[0-9a-fA-F]{16,64}$`)
	accFloatRE = py.MustCompile(`^\d+\.\d+$`)
	accIntRE   = py.MustCompile(`^\d+$`)
)

// accGroups maps group names to indexes for one regex.
func accGroups(re *regexp.Regexp) map[string]int {
	m := map[string]int{}
	for i, n := range re.SubexpNames() {
		if n != "" {
			m[n] = i
		}
	}
	return m
}

var (
	clfG   = accGroups(accClfRE)
	albG   = accGroups(accAlbRE)
	envoyG = accGroups(accEnvoyRE)
)

// statusSeverity is access_parser.status_severity.
func statusSeverity(st string) string {
	switch {
	case strings.HasPrefix(st, "5"):
		return "ERROR"
	case strings.HasPrefix(st, "4"):
		return "WARN"
	}
	return "INFO"
}

// pyIntValue is int(s) for a \d+ match: an int (big when needed); ValueError (> 4300 digits) -> ErrPyRaise.
func pyIntValue(s string) (py.Value, error) {
	n, err := py.Atoi(s)
	if err != nil {
		return py.Value{}, pyRaise("ValueError: int(%q)", py.Head(s, 40))
	}
	return py.BigInt(n), nil
}

// parseExtra is access_parser.parse_extra(extra, rec) (P04 section 2.3).
func parseExtra(extra string, rec *py.Dict) error {
	for _, m := range accExtraKV.FindAllStringSubmatch(extra, -1) {
		rec.Set(py.Lower(m[1]), py.S(strings.Trim(m[2], `"`)))
	}
	var rest []string
	floats := 0
	for _, tok := range py.Split(accExtraKV.ReplaceAllLiteralString(extra, " ")) {
		t := strings.Trim(tok, `"`)
		switch {
		case accFloatRE.MatchString(t):
			f, err := py.ParseFloat(t)
			if err != nil {
				return pyRaise("ValueError: float(%q)", py.Head(t, 40))
			}
			k := "upstream_duration_s"
			if floats == 0 {
				k = "duration_s"
			}
			rec.Set(k, py.F(f))
			floats++
		case accIntRE.MatchString(t) && !rec.Has("duration_s") && !rec.Has("duration_us"):
			v, err := pyIntValue(t)
			if err != nil {
				return err
			}
			rec.Set("duration_us", v)
		case accUUIDish.MatchString(t) && !rec.Has("request_id"):
			rec.Set("request_id", py.S(t))
		case t != "-" && t != "":
			rest = append(rest, tok)
		}
	}
	if len(rest) > 0 {
		rec.Set("extra", py.S(strings.Join(rest, " ")))
	}
	return nil
}

// AccessParser is the "access" parser (P04 section 2).
type AccessParser struct{}

func (AccessParser) Name() string       { return "access" }
func (AccessParser) Kind() string       { return "log" }
func (AccessParser) LineOriented() bool { return true }

// Records yields the (line_no, record) pairs of text.
func (AccessParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	for idx, ln := range py.SplitLines(text) {
		if isBlank(ln) {
			continue
		}
		rec, err := accessRecord(ln)
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

func accessRecord(ln string) (*py.Dict, error) {
	vhost := ""
	if v := accVhostRE.FindStringSubmatchIndex(ln); v != nil && accVhostAheadRE.MatchString(ln[v[1]:]) {
		vhost, ln = ln[v[2]:v[3]], ln[v[1]:]
	}
	if m := accClfRE.FindStringSubmatchIndex(ln); m != nil {
		g := func(n string) string { return sub(ln, m, clfG[n]) }
		st := g("status")
		service := vhost
		if service == "" {
			service = "http"
		}
		rec := py.NewDict(12)
		rec.Set("timestamp", py.S(g("ts")))
		rec.Set("client", py.S(g("client")))
		rec.Set("request", py.S(g("request")))
		rec.Set("status", py.S(st))
		rec.Set("bytes", py.S(g("bytes")))
		rec.Set("message", py.S(g("request")+" "+st))
		rec.Set("severity", py.S(statusSeverity(st)))
		rec.Set("service", py.S(service))
		if u := g("user"); u != "" && u != "-" {
			rec.Set("user", py.S(u))
		}
		if ref, ok := subOK(ln, m, clfG["referer"]); ok {
			rec.Set("referer", py.S(ref))
			rec.Set("agent", py.S(g("agent")))
		}
		if ex := g("extra"); ex != "" {
			if err := parseExtra(ex, rec); err != nil {
				return nil, err
			}
		}
		return rec, nil
	}
	if m := accAlbRE.FindStringSubmatchIndex(ln); m != nil {
		g := func(n string) string { return sub(ln, m, albG[n]) }
		status := g("status")
		st := status
		if status == "-" {
			st = g("tstatus")
		}
		sev := statusSeverity(st)
		if status == "-" || strings.HasPrefix(st, "5") {
			sev = "ERROR"
		}
		service := g("domain")
		if service == "" || service == "-" {
			service = g("elb")
		}
		host := ""
		if target := g("target"); target != "-" {
			host, _, _ = strings.Cut(target, ":")
		}
		client, _, _ := strings.Cut(g("client"), ":")
		rec := py.NewDict(11)
		rec.Set("timestamp", py.S(g("ts")))
		rec.Set("client", py.S(client))
		rec.Set("target", py.S(g("target")))
		rec.Set("request", py.S(g("request")))
		rec.Set("status", py.S(st))
		rec.Set("message", py.S(g("request")+" "+st))
		rec.Set("severity", py.S(sev))
		rec.Set("service", py.S(service))
		rec.Set("host", py.S(host))
		rec.Set("latency_s", py.S(g("t_tgt")))
		rec.Set("agent", py.S(g("agent")))
		return rec, nil
	}
	if m := accEnvoyRE.FindStringSubmatchIndex(ln); m != nil {
		g := func(n string) string { return sub(ln, m, envoyG[n]) }
		st := g("status")
		if st == "-" {
			st = "0"
		}
		sev := statusSeverity(st)
		if strings.HasPrefix(st, "5") || st == "0" {
			sev = "ERROR"
		}
		service := "envoy"
		if a := g("authority"); a != "" && a != "-" {
			service, _, _ = strings.Cut(a, ":")
		}
		rec := py.NewDict(13)
		rec.Set("timestamp", py.S(g("ts")))
		rec.Set("request", py.S(g("request")))
		rec.Set("status", py.S(st))
		rec.Set("message", py.S(g("request")+" "+st))
		rec.Set("severity", py.S(sev))
		rec.Set("service", py.S(service))
		rec.Set("envoy.flags", py.S(g("flags")))
		rec.Set("agent", py.S(g("agent")))
		rec.Set("ts_is_start", py.B(true))
		if d := g("dur"); d != "-" {
			v, err := pyIntValue(d)
			if err != nil {
				return nil, err
			}
			rec.Set("duration_ms", v)
		}
		if u := g("up"); u != "-" {
			v, err := pyIntValue(u)
			if err != nil {
				return nil, err
			}
			rec.Set("upstream_duration_ms", v)
		}
		if r := g("reqid"); r != "" && r != "-" {
			rec.Set("x-request-id", py.S(r))
		}
		if u := g("upstream"); u != "" && u != "-" {
			rec.Set("upstream_host", py.S(u))
		}
		return rec, nil
	}
	return nil, nil
}
