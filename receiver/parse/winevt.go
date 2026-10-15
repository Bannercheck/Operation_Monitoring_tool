package parse

import (
	"errors"
	"strconv"
	"strings"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// Owner: WP5 (P04 section 6).

// WinEvtParser is the "winevt" parser (parsers/winevt_parser.py): every `<Event\b.*?</Event>` chunk (re.S) is
// parsed with xml.etree.ElementTree.fromstring; chunks that raise ParseError are skipped but still counted.
type WinEvtParser struct{}

func (WinEvtParser) Name() string       { return "winevt" }
func (WinEvtParser) Kind() string       { return "event" }
func (WinEvtParser) LineOriented() bool { return false }

var winevtLevel = map[string]string{"1": "CRITICAL", "2": "ERROR", "3": "WARN", "4": "INFO", "5": "DEBUG", "0": "INFO"}

// winevtSimple maps the text-valued System tags to their record keys.
var winevtSimple = map[string]string{"EventID": "event_id", "Level": "level", "Task": "task", "Channel": "channel",
	"Computer": "host", "Keywords": "keywords", "Opcode": "opcode"}

// Records yields (chunk index, record) per well-formed event chunk.
func (WinEvtParser) Records(text string, fc *FamilyCtx, yield func(Record) bool) error {
	for i, chunk := range eventChunks(text) {
		nodes, err := xmlParse(chunk)
		if err != nil {
			if errors.Is(err, errXML) {
				continue
			}
			return err
		}
		rec := py.NewDict(10)
		fields := py.NewDict(0)
		for _, nd := range nodes {
			txt := py.Strip(nd.text)
			switch {
			case nd.tag == "Provider":
				rec.Set("provider", py.S(nd.attr("Name")))
			case nd.tag == "TimeCreated":
				rec.Set("timestamp", py.S(nd.attr("SystemTime")))
			case winevtSimple[nd.tag] != "" && txt != "":
				rec.Set(winevtSimple[nd.tag], py.S(txt))
			case nd.tag == "Data":
				name := nd.attr("Name")
				if name == "" {
					name = "data" + strconv.Itoa(fields.Len()+1)
				}
				fields.Set(name, py.S(txt))
			}
		}
		level := ""
		if v, ok := rec.Get("level"); ok {
			level = v.S
		}
		sev, ok := winevtLevel[level]
		if !ok {
			sev = "INFO"
		}
		rec.Set("severity", py.S(sev))
		svc := "windows"
		for _, k := range []string{"provider", "channel"} {
			if v, ok := rec.Get(k); ok && v.S != "" {
				svc = v.S
				break
			}
		}
		rec.Set("service", py.S(svc))
		eid, prov := "", ""
		if v, ok := rec.Get("event_id"); ok {
			eid = v.S
		}
		if v, ok := rec.Get("provider"); ok {
			prov = v.S
		}
		parts := []string{"EventID " + eid}
		if prov != "" {
			parts = append(parts, prov)
		}
		fields.Range(func(k string, v py.Value) bool {
			parts = append(parts, k+"="+v.S) // never empty: contains "="
			return true
		})
		rec.Set("message", py.S(strings.Join(parts, " ")))
		// rec.update({k: v for k, v in fields.items() if k not in rec}): the filter sees rec before the update.
		var add []int
		for j := 0; j < fields.Len(); j++ {
			if k, _ := fields.At(j); !rec.Has(k) {
				add = append(add, j)
			}
		}
		for _, j := range add {
			k, v := fields.At(j)
			rec.Set(k, v)
		}
		if !yield(Record{LineNo: i + 1, Rec: rec}) {
			return nil
		}
	}
	return nil
}

// eventChunks is EVENT_RE.findall(text) for `<Event\b.*?</Event>` with re.S: "<Event" not followed by a Python
// word character, up to the first "</Event>" after it; scanning resumes after the chunk (or at start+1 when the
// occurrence is rejected). Once an accepted occurrence has no closing tag, no later one can have one either.
func eventChunks(text string) []string {
	var out []string
	i := 0
	for {
		j := strings.Index(text[i:], "<Event")
		if j < 0 {
			return out
		}
		start := i + j
		after := start + len("<Event")
		if after < len(text) {
			if r, _ := utf8.DecodeRuneInString(text[after:]); py.IsWord(r) {
				i = start + 1
				continue
			}
		}
		k := strings.Index(text[after:], "</Event>")
		if k < 0 {
			return out
		}
		end := after + k + len("</Event>")
		out = append(out, text[start:end])
		i = end
	}
}

// xmlNode is one element of ElementTree's iter() order: local tag name, the attributes without a namespace
// prefix, and .text (the character data before the first child element, comments and PIs dropped).
type xmlNode struct {
	tag   string
	attrs [][2]string
	text  string
}

func (n *xmlNode) attr(name string) string {
	for _, a := range n.attrs {
		if a[0] == name {
			return a[1]
		}
	}
	return ""
}

// errXML is ElementTree.ParseError (the chunk is skipped).
var errXML = errors.New("xml ParseError")

const (
	nsXML   = "http://www.w3.org/XML/1998/namespace"
	nsXMLNS = "http://www.w3.org/2000/xmlns/"
)

type xmlParser struct {
	s     string
	i     int
	nodes []*xmlNode
}

type xmlElem struct {
	name     string // raw QName (end tag must match)
	node     *xmlNode
	hasChild bool
	text     strings.Builder
	ns       map[string]string // prefix -> uri declared on this element ("" = default namespace)
}

// xmlParse is ElementTree.fromstring(chunk) as far as this parser observes it, for a chunk that starts with
// "<Event" (no prolog, no DOCTYPE possible). It reproduces expat in namespace mode: well-formedness, the five
// predefined entities and character references, CDATA, comments and PIs (dropped, surrounding text joined),
// line-end normalisation (CRLF/CR -> LF), attribute value normalisation (TAB/CR/LF -> space), duplicate
// attributes (raw and expanded), unbound/undeclared/reserved prefixes, expat's name character tables. Returns
// errXML for a ParseError.
func xmlParse(chunk string) ([]*xmlNode, error) {
	p := &xmlParser{s: chunk}
	if err := p.document(); err != nil {
		return nil, err
	}
	return p.nodes, nil
}

func xmlChar(r rune) bool {
	return r == 0x9 || r == 0xA || r == 0xD || (r >= 0x20 && r <= 0xD7FF) || (r >= 0xE000 && r <= 0xFFFD) ||
		(r >= 0x10000 && r <= 0x10FFFF)
}

func xmlSpace(c byte) bool { return c == ' ' || c == '\t' || c == '\n' || c == '\r' }

func nameStart(c byte) bool {
	return c == '_' || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == ':'
}

func nameChar(c byte) bool {
	return nameStart(c) || (c >= '0' && c <= '9') || c == '.' || c == '-'
}

func inRanges(rs [][2]rune, r rune) bool {
	lo, hi := 0, len(rs)
	for lo < hi {
		m := (lo + hi) / 2
		switch {
		case r < rs[m][0]:
			hi = m
		case r > rs[m][1]:
			lo = m + 1
		default:
			return true
		}
	}
	return false
}

// isNameRune reports whether r may start (first) or continue an XML name for expat (ASCII rules plus the
// pyexpat-probed non-ASCII tables of winevt_names.go).
func isNameRune(r rune, first bool) bool {
	if r < 0x80 {
		if first {
			return nameStart(byte(r))
		}
		return nameChar(byte(r))
	}
	if first {
		return inRanges(xmlNameStartRanges, r)
	}
	return inRanges(xmlNameCharRanges, r)
}

// name reads an XML Name at p.i.
func (p *xmlParser) name() (string, error) {
	st := p.i
	for p.i < len(p.s) {
		r, sz := utf8.DecodeRuneInString(p.s[p.i:])
		if !isNameRune(r, p.i == st) || (r == utf8.RuneError && sz == 1) {
			break
		}
		p.i += sz
	}
	if p.i == st {
		return "", errXML
	}
	return p.s[st:p.i], nil
}

// splitQName splits a namespace-mode QName (NCName or NCName:NCName); anything else is not well-formed.
func splitQName(n string) (prefix, local string, err error) {
	c := strings.IndexByte(n, ':')
	if c < 0 {
		return "", n, nil
	}
	prefix, local = n[:c], n[c+1:]
	if r, _ := utf8.DecodeRuneInString(local); prefix == "" || local == "" || strings.IndexByte(local, ':') >= 0 || !isNameRune(r, true) {
		return "", "", errXML
	}
	return prefix, local, nil
}

func (p *xmlParser) skipSpace() bool {
	st := p.i
	for p.i < len(p.s) && xmlSpace(p.s[p.i]) {
		p.i++
	}
	return p.i > st
}

// ref decodes an entity or character reference at p.i ('&').
func (p *xmlParser) ref() (rune, error) {
	p.i++ // '&'
	semi := strings.IndexByte(p.s[p.i:], ';')
	if semi < 0 {
		return 0, errXML
	}
	body := p.s[p.i : p.i+semi]
	p.i += semi + 1
	if strings.HasPrefix(body, "#") {
		digits, base := body[1:], 10
		if strings.HasPrefix(digits, "x") {
			digits, base = digits[1:], 16
		}
		if digits == "" {
			return 0, errXML
		}
		var n int64
		for k := 0; k < len(digits); k++ {
			c := digits[k]
			var d int64
			switch {
			case c >= '0' && c <= '9':
				d = int64(c - '0')
			case base == 16 && c >= 'a' && c <= 'f':
				d = int64(c-'a') + 10
			case base == 16 && c >= 'A' && c <= 'F':
				d = int64(c-'A') + 10
			default:
				return 0, errXML
			}
			n = n*int64(base) + d
			if n > 0x10FFFF {
				n = 0x110000
			}
		}
		if !xmlChar(rune(n)) {
			return 0, errXML
		}
		return rune(n), nil
	}
	switch body {
	case "lt":
		return '<', nil
	case "gt":
		return '>', nil
	case "amp":
		return '&', nil
	case "quot":
		return '"', nil
	case "apos":
		return '\'', nil
	}
	return 0, errXML // undefined entity or malformed reference
}

// literal reads the next literal character at p.i with CRLF/CR -> LF; ok=false for an invalid XML character.
func (p *xmlParser) literal() (rune, bool) {
	r, sz := utf8.DecodeRuneInString(p.s[p.i:])
	p.i += sz
	if r == '\r' {
		if p.i < len(p.s) && p.s[p.i] == '\n' {
			p.i++
		}
		return '\n', true
	}
	if r == utf8.RuneError && sz == 1 {
		return r, false
	}
	return r, xmlChar(r)
}

func (p *xmlParser) attValue() (string, error) {
	if p.i >= len(p.s) || (p.s[p.i] != '"' && p.s[p.i] != '\'') {
		return "", errXML
	}
	q := p.s[p.i]
	p.i++
	var b strings.Builder
	for {
		if p.i >= len(p.s) {
			return "", errXML
		}
		switch c := p.s[p.i]; {
		case c == q:
			p.i++
			return b.String(), nil
		case c == '<':
			return "", errXML
		case c == '&':
			r, err := p.ref()
			if err != nil {
				return "", err
			}
			b.WriteRune(r)
		default:
			r, ok := p.literal()
			if !ok {
				return "", errXML
			}
			if r == '\n' || r == '\t' {
				r = ' '
			}
			b.WriteRune(r)
		}
	}
}

// comment skips "<!--...-->" at p.i ("--" inside is not well-formed).
func (p *xmlParser) comment() error {
	p.i += 4
	k := strings.Index(p.s[p.i:], "--")
	if k < 0 {
		return errXML
	}
	if err := p.checkChars(p.s[p.i : p.i+k]); err != nil {
		return err
	}
	p.i += k + 2
	if p.i >= len(p.s) || p.s[p.i] != '>' {
		return errXML
	}
	p.i++
	return nil
}

// pi skips "<?target ...?>" at p.i. The target is an NCName that is not "xml" in any case.
func (p *xmlParser) pi() error {
	p.i += 2
	tgt, err := p.name()
	if err != nil {
		return err
	}
	if strings.IndexByte(tgt, ':') >= 0 || strings.EqualFold(tgt, "xml") {
		return errXML
	}
	if strings.HasPrefix(p.s[p.i:], "?>") {
		p.i += 2
		return nil
	}
	if !p.skipSpace() {
		return errXML
	}
	k := strings.Index(p.s[p.i:], "?>")
	if k < 0 {
		return errXML
	}
	if err := p.checkChars(p.s[p.i : p.i+k]); err != nil {
		return err
	}
	p.i += k + 2
	return nil
}

func (p *xmlParser) checkChars(s string) error {
	for _, r := range s {
		if !xmlChar(r) {
			return errXML
		}
	}
	if !utf8.ValidString(s) {
		return errXML
	}
	return nil
}

func (p *xmlParser) lookup(stack []*xmlElem, el *xmlElem, prefix string) (string, bool) {
	if prefix == "xml" {
		return nsXML, true
	}
	if uri, ok := el.ns[prefix]; ok {
		return uri, true
	}
	for k := len(stack) - 1; k >= 0; k-- {
		if uri, ok := stack[k].ns[prefix]; ok {
			return uri, true
		}
	}
	return "", false
}

// startTag parses "<name attrs...>" or "/>" at p.i ('<'). Returns the element (nil when empty-element tag).
func (p *xmlParser) startTag(stack []*xmlElem) (*xmlElem, bool, error) {
	p.i++
	qn, err := p.name()
	if err != nil {
		return nil, false, err
	}
	prefix, local, err := splitQName(qn)
	if err != nil {
		return nil, false, err
	}
	type rawAttr struct{ name, val string }
	var raw []rawAttr
	empty := false
	for {
		sp := p.skipSpace()
		if p.i >= len(p.s) {
			return nil, false, errXML
		}
		if p.s[p.i] == '>' {
			p.i++
			break
		}
		if strings.HasPrefix(p.s[p.i:], "/>") {
			p.i += 2
			empty = true
			break
		}
		if !sp {
			return nil, false, errXML
		}
		an, err := p.name()
		if err != nil {
			return nil, false, err
		}
		p.skipSpace()
		if p.i >= len(p.s) || p.s[p.i] != '=' {
			return nil, false, errXML
		}
		p.i++
		p.skipSpace()
		av, err := p.attValue()
		if err != nil {
			return nil, false, err
		}
		for _, a := range raw {
			if a.name == an {
				return nil, false, errXML // duplicate attribute
			}
		}
		raw = append(raw, rawAttr{an, av})
	}
	el := &xmlElem{name: qn, node: &xmlNode{tag: local}}
	// Namespace declarations first: they apply to the element's own name and attributes.
	for _, a := range raw {
		switch {
		case a.name == "xmlns":
			if a.val == nsXML || a.val == nsXMLNS {
				return nil, false, errXML
			}
			if el.ns == nil {
				el.ns = map[string]string{}
			}
			el.ns[""] = a.val
		case strings.HasPrefix(a.name, "xmlns:"):
			_, lp, err := splitQName(a.name)
			if err != nil {
				return nil, false, err
			}
			switch {
			case lp == "xmlns":
				return nil, false, errXML
			case lp == "xml":
				if a.val != nsXML {
					return nil, false, errXML
				}
				continue
			case a.val == "" || a.val == nsXML || a.val == nsXMLNS:
				return nil, false, errXML
			}
			if el.ns == nil {
				el.ns = map[string]string{}
			}
			el.ns[lp] = a.val
		}
	}
	if prefix != "" {
		if _, ok := p.lookup(stack, el, prefix); !ok {
			return nil, false, errXML // unbound prefix
		}
	}
	seen := map[string]bool{}
	for _, a := range raw {
		if a.name == "xmlns" || strings.HasPrefix(a.name, "xmlns:") {
			continue
		}
		ap, al, err := splitQName(a.name)
		if err != nil {
			return nil, false, err
		}
		key := a.name
		if ap != "" {
			uri, ok := p.lookup(stack, el, ap)
			if !ok {
				return nil, false, errXML
			}
			key = "{" + uri + "}" + al
		} else {
			el.node.attrs = append(el.node.attrs, [2]string{a.name, a.val})
		}
		if seen[key] {
			return nil, false, errXML // duplicate expanded attribute
		}
		seen[key] = true
	}
	return el, empty, nil
}

func (p *xmlParser) document() error {
	var stack []*xmlElem
	started := false
	for p.i < len(p.s) {
		if started && len(stack) == 0 {
			return errXML // anything after the root element: the chunk ends with "</Event>", an end tag
		}
		c := p.s[p.i]
		switch {
		case c == '<' && strings.HasPrefix(p.s[p.i:], "</"):
			if len(stack) == 0 {
				return errXML
			}
			p.i += 2
			qn, err := p.name()
			if err != nil {
				return err
			}
			p.skipSpace()
			if p.i >= len(p.s) || p.s[p.i] != '>' {
				return errXML
			}
			p.i++
			top := stack[len(stack)-1]
			if qn != top.name {
				return errXML // mismatched tag
			}
			if !top.hasChild {
				top.node.text = top.text.String()
			}
			stack = stack[:len(stack)-1]
		case c == '<' && strings.HasPrefix(p.s[p.i:], "<!--"):
			if err := p.comment(); err != nil {
				return err
			}
		case c == '<' && strings.HasPrefix(p.s[p.i:], "<![CDATA["):
			if len(stack) == 0 {
				return errXML
			}
			p.i += len("<![CDATA[")
			k := strings.Index(p.s[p.i:], "]]>")
			if k < 0 {
				return errXML
			}
			end := p.i + k
			top := stack[len(stack)-1]
			for p.i < end {
				r, ok := p.literal()
				if !ok {
					return errXML
				}
				if !top.hasChild {
					top.text.WriteRune(r)
				}
			}
			p.i = end + 3
		case c == '<' && strings.HasPrefix(p.s[p.i:], "<?"):
			if err := p.pi(); err != nil {
				return err
			}
		case c == '<' && strings.HasPrefix(p.s[p.i:], "<!"):
			return errXML
		case c == '<':
			el, empty, err := p.startTag(stack)
			if err != nil {
				return err
			}
			if len(stack) > 0 {
				if parent := stack[len(stack)-1]; !parent.hasChild {
					parent.node.text = parent.text.String()
					parent.hasChild = true
				}
			}
			p.nodes = append(p.nodes, el.node)
			started = true
			if !empty {
				stack = append(stack, el)
			}
		default:
			if len(stack) == 0 {
				return errXML
			}
			top := stack[len(stack)-1]
			var r rune
			if c == '&' {
				var err error
				if r, err = p.ref(); err != nil {
					return err
				}
			} else {
				if strings.HasPrefix(p.s[p.i:], "]]>") {
					return errXML
				}
				var ok bool
				if r, ok = p.literal(); !ok {
					return errXML
				}
			}
			if !top.hasChild {
				top.text.WriteRune(r)
			}
		}
	}
	if len(stack) > 0 || !started {
		return errXML // no element found / unclosed token
	}
	return nil
}
