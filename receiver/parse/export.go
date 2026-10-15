package parse

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"strconv"

	"watchover/receiver/parse/py"
)

// Owner: WP7 (receiver integration). Small exported views of package internals for the receiver's parse stage; no
// parsing logic lives here.

// IsSideTable is tables.is_side_table(name): such files are parsed as side tables by Python (report format "table",
// no hint learned), so the receiver forwards them raw.
func IsSideTable(name string) bool { return isSideTable(name) }

// ReportJSON is one canonical report entry (the object inside CanonReport's list): sorted keys, Python separators,
// without sha/table.
func ReportJSON(r Report) []byte { return appendReport(nil, r) }

// Version identifies the Go parser build in v=2 spool records: package version + the first 12 hex digits of
// sha1(allow.txt), so a record says which allow-list (and therefore which parity-tested set) produced it.
func Version() string {
	h := sha1.Sum([]byte(allowTxt))
	return "go-parse/1+" + hex.EncodeToString(h[:])[:12]
}

// HintFormat is the format live.py stores in its per-stream hint for an unhinted batch with this report: the report
// format, or "jsonl" for a "json" batch whose stripped body is one line starting with "{". The receiver puts it in v=2
// headers because Python no longer sees the raw body of a parsed batch.
func HintFormat(r Report, body []byte) string {
	if h := learnedHint(nil, []Report{r}, body); h != nil {
		return h.Format
	}
	return r.Format
}

// Present reports whether the snapshot file exists and holds a JSON object (what the dashboard's export always
// writes, even with no profiles: {"_scenario_mapping": {}}). The receiver parses only while it is present.
func (f *FileSnapshot) Present() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.refresh()
	return f.profiles != nil
}

// UserGrok reports enabled user grok patterns (<home>/grok.json, made on the Parser page) in the dashboard's export
// ("_grok_user": their names). They take part in format detection and grok parsing, which this package does not
// port (P03 section 5.5), so while any exist the receiver forwards every batch.
func (f *FileSnapshot) UserGrok() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.refresh()
	if f.profiles == nil {
		return false
	}
	v, ok := f.profiles.Get("_grok_user")
	return ok && (v.K != py.KList || len(v.L) > 0) // anything but an empty list counts as present
}

// compiledScenarioParse is the canonical JSON of the scenario constants this package compiled in (pipeline.go,
// severity.go), in the shape the dashboard exports them.
var compiledScenarioParse = func() string {
	b, _ := json.Marshal(map[string]any{"SEVERITY_MAP": scenarioSeverityMap, "SIDE_TABLES": sideTables, "DEDUP_KEY": dedupKey,
		"DEDUP_PREFER": dedupPrefer})
	v, _ := py.Loads(string(b))
	s, _ := py.Dumps(v, true)
	return s
}()

// ScenarioDiffers reports whether the dashboard exported parse-path scenario constants ("_scenario_parse":
// SEVERITY_MAP, SIDE_TABLES, DEDUP_KEY, DEDUP_PREFER from src/watchover/scenario) other than the ones compiled into
// this package: the scenario is edited per event, and a changed constant changes Python's output. No file or no key =
// the defaults.
func (f *FileSnapshot) ScenarioDiffers() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.refresh()
	if f.profiles == nil {
		return false
	}
	v, ok := f.profiles.Get("_scenario_parse")
	if !ok {
		return false
	}
	s, err := py.Dumps(v, true)
	return err != nil || s != compiledScenarioParse
}

// SpoolLine is the body line of a v=2 spool record: CanonLine except that the attributes keep their insertion order
// (nested values too), because the dashboard iterates attributes after parsing (live.metric_of, hooks) and Python
// dicts keep the parser's order. Top-level keys are sorted as in CanonLine. NaN/Infinity -> ErrForward.
func SpoolLine(o Observation) ([]byte, error) {
	b := make([]byte, 0, 256+len(o.Raw)+len(o.Message))
	b = append(b, '{')
	b = appendKey(b, "attributes")
	b, err := py.AppendDict(b, o.Attributes, false, false)
	if err != nil {
		return nil, forward("NaN/Infinity attribute")
	}
	str := func(k, v string) {
		b = append(b, ", "...)
		b = appendKey(b, k)
		b = py.AppendString(b, v)
	}
	str("environment", o.Environment)
	str("host", o.Host)
	str("kind", o.Kind)
	b = append(b, ", "...)
	b = appendKey(b, "line_no")
	b = strconv.AppendInt(b, int64(o.LineNo), 10)
	str("message", o.Message)
	str("origin", o.Origin)
	str("parser", o.Parser)
	b = append(b, ", "...)
	b = appendKey(b, "parser_confidence")
	b = append(b, py.FloatRepr(py.Round(o.ParserConfidence, 4))...)
	str("raw", o.Raw)
	str("service", o.Service)
	str("severity", o.Severity)
	str("source", o.Source)
	str("ts", o.TS.ISO())
	return append(b, '}'), nil
}
