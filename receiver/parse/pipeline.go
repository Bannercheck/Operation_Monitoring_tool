package parse

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"watchover/receiver/parse/py"
)

// Scenario constants read by parsing (src/watchover/scenario/__init__.py).
var (
	sideTables   = []string{"depend", "bagimlilik", "inventory", "envanter", "sozlugu", "brifing", "readme"}
	dedupKey     = "alarm_id"
	dedupPrefer  = "json"
	learnFormats = map[string]bool{"json": true, "jsonl": true, "csv": true, "kv": true, "grok": true}
)

// probeChars is the line-oriented probe prefix of _parse_one (code points).
const probeChars = 400_000

// Options are the per-call inputs of IngestBytes.
type Options struct {
	Hint     *Hint
	Profiles ProfileStore // nil = empty store
	Norm     *Normalizer  // required (process-global caches + injected year)
}

// isSideTable is tables.is_side_table(fname).
func isSideTable(fname string) bool {
	base := fname
	if i := strings.LastIndexByte(fname, '/'); i >= 0 {
		base = fname[i+1:]
	}
	base = py.Lower(base)
	for _, p := range sideTables {
		if strings.Contains(base, p) {
			return true
		}
	}
	return false
}

// IngestBytes is pipeline.ingest_bytes for ONE file body (P01 sections 4-6, 10). Archives and side tables are
// ErrForward; Python exceptions are ErrPyRaise.
func IngestBytes(name string, data []byte, o Options) (obs []Observation, reps []Report, err error) {
	if o.Norm == nil {
		return nil, nil, errors.New("parse: Options.Norm is required")
	}
	text, err := Decode(name, data)
	if errors.Is(err, errNoText) {
		return nil, nil, nil // ingest of zero items: ([], [])
	}
	if err != nil {
		return nil, nil, err
	}
	rows, rep, err := parseOne(name, text, nil, o)
	if err != nil {
		return nil, nil, err
	}
	reps = []Report{rep}
	rows = dedupe(rows, reps)
	fillMissingTimestamps(rows)
	sortObservations(rows)
	return rows, reps, nil
}

// collect runs parser.Records and returns up to max records (all when max < 0).
func collect(p Parser, text string, fc *FamilyCtx, max int) (recs []Record, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("%w: panic in %s parser: %v", ErrForward, p.Name(), r)
		}
	}()
	err = p.Records(text, fc, func(r Record) bool {
		recs = append(recs, r)
		return max < 0 || len(recs) < max
	})
	return recs, err
}

// parseOne is pipeline._parse_one (P01 section 6) without the side-table branch (forwarded).
func parseOne(fname, text string, mapping *Mapping, o Options) ([]Observation, Report, error) {
	if isSideTable(fname) {
		return nil, Report{}, forward("side table %q", fname)
	}
	n := o.Norm
	var format string
	var conf float64
	if h := o.Hint; h != nil && Registry[h.Format] != nil {
		format, conf = h.Format, h.Confidence
	} else {
		var err error
		if format, conf, err = DetectFormat(text, n); err != nil {
			return nil, Report{}, err
		}
	}
	parser := Registry[format]
	fc := &FamilyCtx{}
	if h := o.Hint; h != nil && h.HasFamily {
		fam := h.Family
		fc.Hint = &fam
	}
	probe := text
	if parser.LineOriented() {
		probe = py.Head(text, probeChars)
	}
	head, err := collect(parser, probe, fc, headN)
	if err != nil {
		return nil, Report{}, err
	}
	keys := recordKeys(head)
	var learned *Profile
	if mapping == nil && len(keys) > 0 && learnFormats[format] && o.Profiles != nil {
		if learned = o.Profiles.Lookup(format, keys); learned != nil {
			mapping = NewMapping(learned.Mapping.Dict().Clone())
		}
	}
	roles, err := n.AutoMap(keys, headDicts(head), mapping)
	if err != nil {
		return nil, Report{}, err
	}
	recs, err := collect(parser, text, fc, -1)
	if err != nil {
		return nil, Report{}, err
	}
	var lines []string
	if parser.LineOriented() {
		lines = py.SplitLines(text)
	}
	rows, err := recordsToObservations(n, recs, fname, parser.Name(), parser.Kind(), mapping, conf, lines)
	if err != nil {
		return nil, Report{}, err
	}
	if len(keys) > 0 && learnFormats[format] && o.Profiles != nil {
		source := "auto"
		if mapping.Len() > 0 && learned == nil {
			source = "user"
		}
		o.Profiles.Remember(format, keys, roles, fname, source)
	}
	if format == "csv" {
		stripped := map[string]bool{}
		for _, k := range keys {
			stripped[py.Strip(k)] = true
		}
		if stripped["EventID"] && stripped["Correlation"] && stripped["Area"] {
			sharepointULS(rows)
		}
	}
	rep := Report{File: fname, Format: format, Confidence: conf, Rows: len(rows), Kind: "-", Keys: keys, Roles: roles}
	if fc.Seen != nil {
		rep.Family = *fc.Seen
	}
	if len(rows) > 0 {
		rep.Kind = rows[0].Kind
	}
	if learned != nil {
		rep.Profile = learned.ID
	}
	if rep.Keys == nil {
		rep.Keys = []string{}
	}
	return rows, rep, nil
}

// uLSLevels is pipeline.ULS_LEVELS.
var uLSLevels = map[string]string{"critical": "CRITICAL", "unexpected": "ERROR", "monitorable": "WARN", "high": "INFO",
	"medium": "INFO", "verbose": "DEBUG", "verboseex": "DEBUG"}

var (
	execTimeRE   = regexp.MustCompile(`Execution Time=(` + py.D + `+(?:\.` + py.D + `+)?)`)
	ulsRequestRE = regexp.MustCompile(`Request \((GET|POST|PUT|DELETE|PATCH|HEAD):(` + py.PNS + `+?)\)`)
)

// sharepointULS is pipeline._sharepoint_uls (P01 section 7). Not covered by goldens.
func sharepointULS(obs []Observation) {
	for i := range obs {
		o := &obs[i]
		parts := strings.Split(o.Raw, "\t")
		cols := make([]string, len(parts))
		for j, c := range parts {
			cols[j] = py.Strip(c)
		}
		for _, c := range cols {
			if lvl, ok := uLSLevels[py.Lower(c)]; ok {
				o.Severity = lvl
				break
			}
		}
		areaV, _ := o.Attributes.Get("Area")
		area := ""
		if o.Attributes.Has("Area") {
			area = py.Strip(areaV.Str())
		}
		if area != "" {
			cat := ""
			for j := 1; j < len(cols); j++ {
				if cols[j-1] == area {
					cat = cols[j]
					break
				}
			}
			svc := "sharepoint"
			if cat != "" {
				svc = "sharepoint:" + cat
			}
			o.Service = py.Head(svc, 80)
		}
		for _, k := range append([]string(nil), o.Attributes.Keys()...) {
			if v, _ := o.Attributes.Get(k); v.K == py.KStr {
				o.Attributes.Set(k, py.S(py.Strip(v.S)))
			}
		}
		if m := execTimeRE.FindStringSubmatch(o.Message); m != nil {
			if f, err := py.ParseFloat(py.ToASCIIDigits(m[1])); err == nil {
				o.Attributes.Set("duration_ms", py.F(f))
			}
		}
		if m := ulsRequestRE.FindStringSubmatch(o.Message); m != nil {
			o.Attributes.Set("http.method", py.S(m[1]))
			o.Attributes.Set("url", py.S(m[2]))
		}
	}
}

// dedupe is pipeline.dedupe (P01 section 10.1) with scenario DEDUP_KEY "alarm_id" and DEDUP_PREFER "json".
// It adds DedupDropped to the report entries of the files that lost rows.
func dedupe(obs []Observation, rep []Report) []Observation {
	if dedupKey == "" {
		return obs
	}
	keep := map[string]int{} // key -> index into obs
	var order []string
	dropped := map[string]int{}
	var passthrough []int
	for i := range obs {
		o := &obs[i]
		v, _ := o.Attributes.Get(dedupKey)
		if v.IsNoneOrEmpty() {
			passthrough = append(passthrough, i)
			continue
		}
		k := v.Str()
		cur, ok := keep[k]
		if !ok {
			keep[k] = i
			order = append(order, k)
			continue
		}
		if o.Parser == dedupPrefer && obs[cur].Parser != dedupPrefer {
			dropped[obs[cur].Source]++
			keep[k] = i
		} else {
			dropped[o.Source]++
		}
	}
	if len(dropped) == 0 {
		return obs
	}
	for j := range rep {
		if c, ok := dropped[rep[j].File]; ok {
			rep[j].DedupDropped = c
		}
	}
	out := make([]Observation, 0, len(passthrough)+len(order))
	for _, i := range passthrough {
		out = append(out, obs[i])
	}
	for _, k := range order {
		out = append(out, obs[keep[k]])
	}
	return out
}

// fillMissingTimestamps is pipeline.fill_missing_timestamps (P01 section 10.2).
func fillMissingTimestamps(obs []Observation) {
	var fallback py.DateTime
	have := false
	for i := range obs {
		if v, _ := obs[i].Attributes.Get("_no_ts"); v.Truthy() {
			continue
		}
		if !have || obs[i].TS.Before(fallback) {
			fallback, have = obs[i].TS, true
		}
	}
	if !have {
		if len(obs) == 0 {
			return
		}
		fallback = obs[0].TS
	}
	for i := range obs {
		if v, ok := obs[i].Attributes.Del("_no_ts"); ok && v.Truthy() {
			obs[i].TS = fallback
		}
	}
}

// sortObservations is observations.sort(key=lambda o: o.timestamp): stable, by instant.
func sortObservations(obs []Observation) {
	sort.SliceStable(obs, func(i, j int) bool { return obs[i].TS.Before(obs[j].TS) })
}
