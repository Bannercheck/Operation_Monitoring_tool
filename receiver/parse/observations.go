package parse

import (
	"regexp"
	"strings"

	"watchover/receiver/parse/py"
)

// headN is common.HEAD: records used to resolve the schema.
const headN = 50

// levelWordSearch is the boolean form of normalize.LEVEL_WORD_RE.search (P01 section 9): the lookbehind/lookahead
// (?<![A-Za-z]) / (?![A-Za-z]) under re.I become consumed non-letters or string edges.
var levelWordSearch = regexp.MustCompile(`(?i)(?:^|` + py.LNW + `)` +
	`(?:TRACE|DEBUG|` + py.I + `NFO|NOT` + py.I + `CE|WARN(?:` + py.I + `NG)?|ERR(?:OR)?|CR` + py.I + `T(?:` + py.I + `CAL)?|FATAL|ALERT|` +
	`EMERG(?:ENCY)?|SEVERE|PAN` + py.I + `C|` + py.I + `NF|WRN|FTL|DBG|VRB)` +
	`(?:` + py.LNW + `|$)`)

// recordKeys is the union of record keys in first-seen order.
func recordKeys(recs []Record) []string {
	var keys []string
	seen := map[string]bool{}
	for _, r := range recs {
		for _, k := range r.Rec.Keys() {
			if !seen[k] {
				seen[k] = true
				keys = append(keys, k)
			}
		}
	}
	return keys
}

func headDicts(recs []Record) []*py.Dict {
	out := make([]*py.Dict, len(recs))
	for i, r := range recs {
		out[i] = r.Rec
	}
	return out
}

// strOr is str(v or ""): falsy values (None, "", 0, 0.0, False, empty containers) give "".
func strOr(v py.Value) string {
	if !v.Truthy() {
		return ""
	}
	return v.Str()
}

// recordsToObservations is common.records_to_observations (P01 section 9).
func recordsToObservations(n *Normalizer, recs []Record, source, parser, kind string, mapping *Mapping, conf float64, lines []string) ([]Observation, error) {
	head := recs
	if len(head) > headN {
		head = head[:headN]
	}
	roles, err := n.AutoMap(recordKeys(head), headDicts(head), mapping)
	if err != nil {
		return nil, err
	}
	used := map[string]bool{}
	for i := 0; i < NumRoles; i++ {
		if k, ok := roles.Key(i); ok {
			used[k] = true
		}
	}
	lsrc := py.Lower(source)
	if msgKey, ok := roles.Get(RoleMessage); strings.Contains(lsrc, "alert") || (ok && (msgKey == "alert" || msgKey == "alertname")) {
		kind = "alert"
	} else if strings.Contains(lsrc, "incident") {
		kind = "incident"
	}
	rTS, hasTS := roles.Key(RoleTimestamp)
	rMsg, hasMsg := roles.Key(RoleMessage)
	rSev, hasSev := roles.Key(RoleSeverity)
	rSvc, hasSvc := roles.Key(RoleService)
	rHost, hasHost := roles.Key(RoleHost)
	rEnv, hasEnv := roles.Key(RoleEnvironment)
	rOrigin, hasOrigin := roles.Key(RoleOrigin)
	useScale := parser != "syslog"
	envOf := map[[2]string]string{}
	nLines := len(lines)
	var lastTS py.DateTime
	haveLast := false
	out := make([]Observation, 0, len(recs))
	for _, r := range recs {
		rec := r.Rec
		get := func(k string) py.Value {
			v, _ := rec.Get(k)
			return v
		}
		// 1. timestamp
		var ts py.DateTime
		found := false
		if hasTS {
			ts, found, err = n.ParseTimestamp(get(rTS))
			if err != nil {
				return nil, err
			}
		}
		missing := !found
		if missing {
			if haveLast {
				ts = lastTS
			} else {
				ts = py.Epoch
			}
		} else {
			lastTS, haveLast = ts, true
		}
		// 2. message
		var msg string
		if hasMsg {
			msg = strOr(get(rMsg))
		} else {
			parts := make([]string, 0, rec.Len())
			rec.Range(func(k string, v py.Value) bool {
				parts = append(parts, k+"="+v.Str())
				return true
			})
			msg = strings.Join(parts, " ")
		}
		// 3. severity
		sevRaw := py.Null
		if hasSev {
			sevRaw = get(rSev)
		}
		var sev, sevSrc string
		if !sevRaw.IsNoneOrEmpty() {
			if sev, err = n.ColumnSeverity(sevRaw, useScale); err != nil {
				return nil, err
			}
			sevSrc = "field"
		} else {
			sev = SeverityFromText(msg)
			switch {
			case levelWordSearch.MatchString(msg):
				sevSrc = "text"
			case sev != "INFO":
				sevSrc = "hint"
			default:
				sevSrc = "default"
			}
		}
		// 4. service, host
		service, host := "", ""
		if hasSvc {
			service = strOr(get(rSvc))
		}
		if hasHost {
			host = strOr(get(rHost))
		}
		// 5. environment
		env := ""
		if hasEnv {
			if env, err = NormalizeEnvironment(get(rEnv)); err != nil {
				return nil, err
			}
		}
		if env == "" {
			pair := [2]string{host, service}
			e, ok := envOf[pair]
			if !ok {
				if e, err = n.InferEnvironment(host, service, source); err != nil {
					return nil, err
				}
				envOf[pair] = e
			}
			env = e
			if env == "" {
				if env, err = n.InferEnvironment(msg); err != nil {
					return nil, err
				}
			}
		}
		// 6. attributes
		attrs := py.NewDict(0)
		if rec.Len() > len(used) {
			attrs = py.NewDict(rec.Len())
			rec.Range(func(k string, v py.Value) bool {
				if !used[k] && !v.IsNoneOrEmpty() {
					attrs.Set(k, v)
				}
				return true
			})
		}
		// 7-9.
		if sevRaw.K != py.KNone && py.IsDigitStr(py.Strip(sevRaw.Str())) {
			attrs.Set("severity_raw", sevRaw)
		}
		if missing {
			attrs.Set("_no_ts", py.B(true))
		}
		if sevSrc != "field" {
			attrs.Set("_sev_src", py.S(sevSrc))
		}
		// 10. emit
		origin := ""
		if hasOrigin {
			origin = strOr(get(rOrigin))
		}
		var raw string
		if r.LineNo > 0 && r.LineNo <= nLines {
			raw = lines[r.LineNo-1]
		} else {
			raw, _ = py.Dumps(py.MapOf(rec), false)
		}
		out = append(out, Observation{
			TS: ts, Message: msg, Kind: kind, Severity: sev, Service: service, Host: host, Environment: env, Origin: origin,
			Attributes: attrs, Source: source, LineNo: r.LineNo, Parser: parser, ParserConfidence: conf, Raw: raw,
		})
	}
	return out, nil
}
