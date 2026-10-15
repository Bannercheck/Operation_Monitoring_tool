package parse

import (
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02 sections 8-9).

// envMap is normalize.ENV_MAP.
var envMap = map[string]string{"prod": "prod", "production": "prod", "prd": "prod", "live": "prod", "canli": "prod", "uretim": "prod",
	"test": "test", "tst": "test", "testing": "test", "qa": "qa", "uat": "uat", "acceptance": "uat", "kabul": "uat",
	"dev": "dev", "development": "dev", "develop": "dev", "gelistirme": "dev", "local": "dev", "sandbox": "dev",
	"staging": "staging", "stage": "staging", "stg": "staging", "preprod": "staging", "pre-prod": "staging", "canary": "staging",
	"dr": "dr", "disaster": "dr"}

// envTokens is ENV_TOKEN_RE's alternation in order.
var envTokens = []string{"prod", "production", "prd", "test", "tst", "qa", "uat", "dev", "development", "staging",
	"stage", "stg", "preprod", "canary", "sandbox", "live"}

// isEnvBoundary is [a-z0-9] under Python re.I: ASCII letters/digits plus İ ı ſ K.
func isEnvBoundary(r rune) bool { return isLetterI(r) || (r >= '0' && r <= '9') }

// envTokenSearch is ENV_TOKEN_RE.search(s)[1] (lookbehind/lookahead (?<![a-z0-9]) / (?![a-z0-9]), re.I).
func envTokenSearch(s string) (string, bool) {
	prev := rune(-1)
	for i := 0; i < len(s); {
		r, w := utf8.DecodeRuneInString(s[i:])
		if prev < 0 || !isEnvBoundary(prev) {
			for _, tok := range envTokens {
				k, ok := py.HasFoldPrefix(s[i:], tok)
				if !ok {
					continue
				}
				if j := i + k; j < len(s) {
					if nr, _ := utf8.DecodeRuneInString(s[j:]); isEnvBoundary(nr) {
						continue
					}
				}
				return s[i : i+k], true
			}
		}
		prev = r
		i += w
	}
	return "", false
}

// envOfToken is ENV_MAP[m[1].lower()] (KeyError for tokens matched through ı / İ / ſ).
func envOfToken(tok string) (string, error) {
	if e, ok := envMap[py.Lower(tok)]; ok {
		return e, nil
	}
	return "", pyRaise("KeyError: %q", py.Lower(tok))
}

// NormalizeEnvironment is normalize.normalize_environment(v) (KeyError on ı/ſ tokens -> ErrPyRaise).
func NormalizeEnvironment(v py.Value) (string, error) {
	if v.K == py.KNone {
		return "", nil
	}
	s := py.Lower(py.Strip(v.Str()))
	if s == "" {
		return "", nil
	}
	if e, ok := envMap[s]; ok {
		return e, nil
	}
	if tok, ok := envTokenSearch(s); ok {
		return envOfToken(tok)
	}
	return py.Head(s, 20), nil
}

// InferEnvironment is normalize.infer_environment(*texts) (memoised on the first three texts).
func (n *Normalizer) InferEnvironment(texts ...string) (string, error) {
	var key [3]string
	head := texts
	if len(head) > 3 {
		head = head[:3]
	}
	copy(key[:], head)
	n.mu.Lock()
	hit, ok := n.envCache[key]
	n.mu.Unlock()
	if !ok {
		hit = ""
		for _, t := range head {
			if t == "" {
				continue
			}
			if tok, found := envTokenSearch(t); found {
				e, err := envOfToken(tok)
				if err != nil {
					return "", err
				}
				hit = e
				break
			}
		}
		n.mu.Lock()
		if len(n.envCache) < envCacheMax {
			n.envCache[key] = hit
		}
		n.mu.Unlock()
	}
	if hit != "" {
		return hit, nil
	}
	if len(texts) > 3 {
		for _, t := range texts[3:] {
			if t == "" {
				continue
			}
			if tok, found := envTokenSearch(t); found {
				return envOfToken(tok)
			}
		}
	}
	return "", nil
}
