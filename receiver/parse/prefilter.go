package parse

import (
	"regexp"
	"regexp/syntax"
	"strings"
)

// litFilter is a boolean-search regexp guarded by a literal prefilter: lits is a set of case-sensitive substrings,
// derived from the compiled syntax tree, of which every match must contain at least one. Lines containing none of
// them cannot match and skip the (slow, Unicode-class heavy) regexp. nil lits = no filter. Results are identical to
// re.MatchString (TestLitFilterExact); this only saves time.
type litFilter struct {
	re   *regexp.Regexp
	lits []string
}

func newLitFilter(re *regexp.Regexp) litFilter {
	var lits []string
	if t, err := syntax.Parse(re.String(), syntax.Perl); err == nil {
		lits = requiredLits(t.Simplify())
	}
	for _, l := range lits {
		if l == "" {
			lits = nil
			break
		}
	}
	return litFilter{re: re, lits: lits}
}

// MatchString is re.MatchString(s).
func (f litFilter) MatchString(s string) bool {
	if f.lits != nil {
		hit := false
		for _, l := range f.lits {
			if strings.Contains(s, l) {
				hit = true
				break
			}
		}
		if !hit {
			return false
		}
	}
	return f.re.MatchString(s)
}

// requiredLits returns literals of which every string matched by re contains at least one, or nil when no such
// set is known.
func requiredLits(re *syntax.Regexp) []string {
	switch re.Op {
	case syntax.OpLiteral:
		if re.Flags&syntax.FoldCase != 0 {
			return nil
		}
		return []string{string(re.Rune)}
	case syntax.OpCapture:
		return requiredLits(re.Sub[0])
	case syntax.OpPlus:
		return requiredLits(re.Sub[0])
	case syntax.OpRepeat:
		if re.Min >= 1 {
			return requiredLits(re.Sub[0])
		}
		return nil
	case syntax.OpAlternate:
		var out []string
		for _, s := range re.Sub {
			l := requiredLits(s)
			if l == nil {
				return nil
			}
			out = append(out, l...)
		}
		return out
	case syntax.OpConcat:
		var best []string
		bestScore := 0
		for _, s := range re.Sub {
			l := requiredLits(s)
			if l == nil {
				continue
			}
			score := len(l[0])
			for _, x := range l[1:] {
				if len(x) < score {
					score = len(x)
				}
			}
			if best == nil || score > bestScore {
				best, bestScore = l, score
			}
		}
		return best
	}
	return nil
}
