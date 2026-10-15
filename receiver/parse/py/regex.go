package py

import (
	"regexp"
	"strings"
)

// wsClass is the Python \s set as an RE2 character-class body.
const wsClass = `\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`

// wordClass is the Python \w set as an RE2 character-class body.
const wordClass = `\p{L}\p{N}_`

// RE2 fragments with the exact Python 3.11 (str pattern, no re.ASCII) meaning.
const (
	D   = `\p{Nd}`                                 // Python \d
	ND  = `\P{Nd}`                                 // Python \D
	W   = `[` + wordClass + `]`                    // Python \w
	NW  = `[^` + wordClass + `]`                   // Python \W
	PS  = `[` + wsClass + `]`                      // Python \s
	PNS = `[^` + wsClass + `]`                     // Python \S
	I   = `[iIıİ]`                                 // the letter i under Python re.I
	LW  = `[A-Za-z\x{130}\x{131}\x{17f}\x{212a}]`  // [A-Za-z] under Python re.I
	LNW = `[^A-Za-z\x{130}\x{131}\x{17f}\x{212a}]` // [^A-Za-z] under Python re.I
	WSC = wsClass                                  // class body of Python \s, for use inside [...]
	WC  = wordClass                                // class body of Python \w, for use inside [...]
)

// Translate rewrites a Python regex (str pattern) into an RE2 pattern with identical meaning for the supported subset:
// \d \D \w \W \s \S are mapped outside classes, \d \D \w \s inside classes, \uXXXX / \UXXXXXXXX become \x{...}.
// It panics on constructs RE2 cannot express or that change meaning silently (lookarounds, \b, \B, \Z, backreferences,
// possessive quantifiers, atomic groups, conditionals, \W or \S inside a class): those must be rewritten by hand
// (docs/go-parse/PLAN.md section 5).
func Translate(pyPattern string) string { return translate(pyPattern, false) }

// TranslateKeepB is Translate but copies \b unchanged (RE2 ASCII \b). Only for patterns where both neighbours of every
// \b are forced to be ASCII by the pattern itself (P03 section 0, rule b), e.g. grok POSINT / NONNEGINT.
func TranslateKeepB(pyPattern string) string { return translate(pyPattern, true) }

// MustCompile is regexp.MustCompile(Translate(pyPattern)).
func MustCompile(pyPattern string) *regexp.Regexp { return regexp.MustCompile(Translate(pyPattern)) }

func translate(p string, keepB bool) string {
	var b strings.Builder
	in := false
	prevQuant := false // previous token was an unescaped quantifier outside a class
	for i := 0; i < len(p); i++ {
		c := p[i]
		wasQuant := prevQuant
		prevQuant = false
		if c == '\\' && i+1 < len(p) {
			n := p[i+1]
			i++
			switch n {
			case 'd':
				b.WriteString(D)
			case 'D':
				b.WriteString(ND)
			case 'w':
				if in {
					b.WriteString(wordClass)
				} else {
					b.WriteString(W)
				}
			case 's':
				if in {
					b.WriteString(wsClass)
				} else {
					b.WriteString(PS)
				}
			case 'W':
				if in {
					panic(`py.Translate: \W inside a class: ` + p)
				}
				b.WriteString(NW)
			case 'S':
				if in {
					panic(`py.Translate: \S inside a class: ` + p)
				}
				b.WriteString(PNS)
			case 'b':
				if in {
					b.WriteString(`\x08`) // backspace inside a class
				} else if keepB {
					b.WriteString(`\b`)
				} else {
					panic(`py.Translate: \b (Unicode word boundary) needs a hand rewrite: ` + p)
				}
			case 'B', 'Z':
				panic(`py.Translate: unsupported escape \` + string(n) + `: ` + p)
			case 'u', 'U':
				w := 4
				if n == 'U' {
					w = 8
				}
				if i+1+w > len(p) {
					panic(`py.Translate: truncated \` + string(n) + ` escape: ` + p)
				}
				b.WriteString(`\x{` + p[i+1:i+1+w] + `}`)
				i += w
			default:
				if n >= '1' && n <= '9' && !in {
					panic(`py.Translate: backreference \` + string(n) + `: ` + p)
				}
				b.WriteByte('\\')
				b.WriteByte(n)
			}
			continue
		}
		if in {
			switch c {
			case '[':
				b.WriteString(`\[`) // Python allows a bare '[' inside a class; RE2 would read "[:" as a POSIX class
				continue
			case ']':
				in = false
			}
			b.WriteByte(c)
			continue
		}
		switch c {
		case '[':
			in = true
			b.WriteByte('[')
			if i+1 < len(p) && p[i+1] == '^' {
				b.WriteByte('^')
				i++
			}
			if i+1 < len(p) && p[i+1] == ']' { // Python: ']' first in a class is a literal
				b.WriteString(`\]`)
				i++
			}
			continue
		case '(':
			rest := p[i:]
			for _, bad := range []string{"(?=", "(?!", "(?<=", "(?<!", "(?P=", "(?>", "(?("} {
				if strings.HasPrefix(rest, bad) {
					panic("py.Translate: unsupported construct " + bad + " in " + p)
				}
			}
		case '*', '+', '?', '}':
			if c == '+' && wasQuant {
				panic("py.Translate: possessive quantifier in " + p)
			}
			if c == '?' && i > 0 && p[i-1] == '(' {
				break // group syntax, not a quantifier
			}
			prevQuant = !(c == '?' && wasQuant) // "*?" is lazy: a following '+' is then a quantifier error in RE2 anyway
		}
		b.WriteByte(c)
	}
	return b.String()
}
