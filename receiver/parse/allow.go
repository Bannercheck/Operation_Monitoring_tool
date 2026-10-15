package parse

import (
	_ "embed"
	"strings"
)

//go:embed testdata/allow.txt
var allowTxt string

// allowList is testdata/allow.txt parsed once at init.
type allowList struct {
	inputs  map[string]map[string]bool // input -> mode -> listed
	formats map[string]bool            // FormatKey values enabled for production
	order   []string                   // inputs in file order
}

var allow = parseAllow(allowTxt)

// FormatKey is the allow-list key of a batch format: "format", or "format:family" for text and grok (family ""
// gives "text:").
func FormatKey(format, family string) string {
	if format == "text" || format == "grok" {
		return format + ":" + family
	}
	if family != "" {
		return format + ":" + family
	}
	return format
}

func parseAllow(txt string) allowList {
	a := allowList{inputs: map[string]map[string]bool{}, formats: map[string]bool{}}
	for _, ln := range strings.Split(txt, "\n") {
		if i := strings.IndexByte(ln, '#'); i >= 0 {
			ln = ln[:i]
		}
		f := strings.Fields(ln)
		if len(f) == 0 {
			continue
		}
		format, input, modes := "", "", "whole,live"
		switch len(f) {
		case 1:
			input = f[0]
		case 2:
			if isModes(f[1]) {
				input, modes = f[0], f[1]
			} else {
				format, input = f[0], f[1]
			}
		default:
			format, input, modes = f[0], f[1], f[2]
		}
		m := a.inputs[input]
		if m == nil {
			m = map[string]bool{}
			a.inputs[input] = m
			a.order = append(a.order, input)
		}
		for _, md := range strings.FieldsFunc(modes, func(r rune) bool { return r == ',' || r == '|' }) {
			m[md] = true
		}
		if format != "" {
			a.formats[format] = true
		}
	}
	return a
}

func isModes(s string) bool {
	for _, md := range strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == '|' }) {
		if md != "whole" && md != "live" {
			return false
		}
	}
	return s != ""
}

// allowedInput reports whether (golden input, mode) is listed in allow.txt.
func allowedInput(input, mode string) bool { return allow.inputs[input][mode] }

// Allowed reports whether batches of this format[:family] may be emitted as parsed records (allow.txt format column).
// The runtime environment (WATCHOVER_GO_PARSE_FORMATS / _DENY) can only narrow it (receiver side).
func Allowed(format, family string) bool { return allow.formats[FormatKey(format, family)] }
