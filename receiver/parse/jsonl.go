package parse

import (
	"errors"

	"watchover/receiver/parse/py"
)

// Owner: WP5 (P04 section 9).

// JsonlParser is the "jsonl" parser (parsers/jsonl_parser.py).
type JsonlParser struct{}

func (JsonlParser) Name() string       { return "jsonl" }
func (JsonlParser) Kind() string       { return "event" }
func (JsonlParser) LineOriented() bool { return true }

// Records yields one record per physical line holding a JSON object. Lines that are not valid JSON
// (JSONDecodeError) or not an object are skipped; ValueError (int > 4300 digits) escapes (ErrPyRaise) and the
// RecursionError depth is not reproducible (ErrForward).
func (JsonlParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	for i, ln := range py.SplitLines(text) {
		ln = py.Strip(ln)
		if ln == "" {
			continue
		}
		r, err := py.Loads(ln)
		if err != nil {
			if errors.Is(err, py.ErrSyntax) {
				continue
			}
			if errors.Is(err, py.ErrDepth) {
				return forward("jsonl line %d nesting: %v", i+1, err)
			}
			return pyRaise("json.loads line %d: %v", i+1, err)
		}
		if r.K != py.KMap {
			continue
		}
		if !yield(Record{LineNo: i + 1, Rec: enrich(Flatten(r.M, ""))}) {
			return nil
		}
	}
	return nil
}
