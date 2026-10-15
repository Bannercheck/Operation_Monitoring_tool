package parse

import (
	"strings"

	"watchover/receiver/parse/py"
)

// Owner: WP5 (P04 section 7).

// CsvParser is the "csv" parser (parsers/csv_parser.py): csv.DictReader(io.StringIO(text), delimiter=d) with
// d = detect_delimiter(non-blank lines) or ",".
type CsvParser struct{}

func (CsvParser) Name() string       { return "csv" }
func (CsvParser) Kind() string       { return "event" }
func (CsvParser) LineOriented() bool { return true }

// Records yields (row index + 1, {key.strip(): value}) for every non-empty data row. line_no is NOT the physical
// line (blank lines and multi-line fields shift it), exactly as in Python. csv.Error escapes (ErrPyRaise).
func (CsvParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	var lines []string
	for _, ln := range py.SplitLines(text) {
		if !isBlank(ln) {
			lines = append(lines, ln)
		}
	}
	d, err := DetectDelimiter(lines)
	if err != nil {
		return err
	}
	if d == 0 {
		d = ','
	}
	rd := newCSVReader(text, d)
	header, ok, err := rd.next()
	if err != nil || !ok {
		return err
	}
	i := 2
	for {
		row, ok, err := rd.next()
		if err != nil || !ok {
			return err
		}
		if len(row) == 0 { // DictReader skips [] rows without consuming an index
			continue
		}
		// dict(zip(fieldnames, row)); extra values go to restkey None (dropped by the parser); missing -> restval None.
		d := py.NewDict(len(header))
		for j := 0; j < len(header) && j < len(row); j++ {
			d.Set(header[j], py.S(row[j]))
		}
		for j := len(row); j < len(header); j++ {
			d.Set(header[j], py.Null)
		}
		rec := py.NewDict(d.Len())
		d.Range(func(k string, v py.Value) bool {
			rec.Set(py.Strip(k), v)
			return true
		})
		if !yield(Record{LineNo: i, Rec: rec}) {
			return nil
		}
		i++
	}
}

// csvReader is CPython 3.11 _csv.reader (excel dialect: quotechar '"', doublequote, no escapechar,
// skipinitialspace off, strict off, field_size_limit 131072) iterating io.StringIO(text), i.e. lines split after
// every "\n" only (no newline translation).
type csvReader struct {
	text   string
	pos    int
	delim  rune
	state  int
	fields []string
	field  []rune
}

const (
	csvStartRecord = iota
	csvStartField
	csvInField
	csvInQuoted
	csvQuoteInQuoted
	csvEatCRNL
)

const csvEOL = rune(-2)

func newCSVReader(text string, delim rune) *csvReader {
	return &csvReader{text: text, delim: delim}
}

// nextLine is the StringIO iterator: the next piece ending in "\n" (included), or the unterminated tail.
func (r *csvReader) nextLine() (string, bool) {
	if r.pos >= len(r.text) {
		return "", false
	}
	end := strings.IndexByte(r.text[r.pos:], '\n')
	if end < 0 {
		end = len(r.text)
	} else {
		end += r.pos + 1
	}
	ln := r.text[r.pos:end]
	r.pos = end
	return ln, true
}

func (r *csvReader) add(c rune) error {
	if len(r.field) >= csvFieldLimit {
		return pyRaise("csv.Error: field larger than field limit (%d)", csvFieldLimit)
	}
	r.field = append(r.field, c)
	return nil
}

func (r *csvReader) save() {
	r.fields = append(r.fields, string(r.field))
	r.field = r.field[:0]
}

// process is parse_process_char.
func (r *csvReader) process(c rune) error {
	nl := c == '\n' || c == '\r'
	endState := func() int {
		if c == csvEOL {
			return csvStartRecord
		}
		return csvEatCRNL
	}
	switch r.state {
	case csvStartRecord:
		if c == csvEOL {
			return nil // empty line: the record is []
		}
		if nl {
			r.state = csvEatCRNL
			return nil
		}
		r.state = csvStartField
		fallthrough
	case csvStartField:
		switch {
		case nl || c == csvEOL:
			r.save()
			r.state = endState()
		case c == '"':
			r.state = csvInQuoted
		case c == r.delim:
			r.save()
		default:
			if err := r.add(c); err != nil {
				return err
			}
			r.state = csvInField
		}
	case csvInField:
		switch {
		case nl || c == csvEOL:
			r.save()
			r.state = endState()
		case c == r.delim:
			r.save()
			r.state = csvStartField
		default:
			return r.add(c)
		}
	case csvInQuoted:
		switch {
		case c == csvEOL:
		case c == '"':
			r.state = csvQuoteInQuoted
		default:
			return r.add(c)
		}
	case csvQuoteInQuoted:
		switch {
		case c == '"':
			if err := r.add(c); err != nil {
				return err
			}
			r.state = csvInQuoted
		case c == r.delim:
			r.save()
			r.state = csvStartField
		case nl || c == csvEOL:
			r.save()
			r.state = endState()
		default: // strict=False: the character joins the field, which continues unquoted
			if err := r.add(c); err != nil {
				return err
			}
			r.state = csvInField
		}
	case csvEatCRNL:
		switch {
		case nl:
		case c == csvEOL:
			r.state = csvStartRecord
		default:
			return pyRaise("csv.Error: new-line character seen in unquoted field")
		}
	}
	return nil
}

// next is Reader_iternext: the next record ([] for an empty line); ok=false at the end of input.
func (r *csvReader) next() ([]string, bool, error) {
	r.fields = []string{}
	r.field = r.field[:0]
	r.state = csvStartRecord
	for {
		ln, ok := r.nextLine()
		if !ok {
			// End of input inside a record (unclosed quote): strict=False saves the field and returns the record.
			if len(r.field) != 0 || r.state == csvInQuoted {
				r.save()
				return r.fields, true, nil
			}
			return nil, false, nil
		}
		for _, c := range ln {
			if err := r.process(c); err != nil {
				return nil, false, err
			}
		}
		if err := r.process(csvEOL); err != nil {
			return nil, false, err
		}
		if r.state == csvStartRecord {
			return r.fields, true, nil
		}
	}
}
