import json, sys
from watchover import grok
q = lambda s: json.dumps(s, ensure_ascii=False)
out = []
out.append("// Code generated from src/watchover/grok.py (BASE, LINES) by WP3; DO NOT EDIT by hand.\n// Regenerate: PYTHONPATH=src python3 receiver/parse/testdata/wp3gen/gen_grok.py > receiver/parse/grok_lib.go && gofmt -w receiver/parse/grok_lib.go\n\npackage parse\n")
out.append("// grokBase is grok.BASE verbatim (Python regex syntax; the Go overrides live in grok.go).")
out.append("var grokBase = map[string]string{")
for k, v in grok.BASE.items():
    out.append(f"\t{q(k)}: {q(v)},")
out.append("}\n")
out.append("// grokLineSrc is grok.LINES verbatim, in order (detection keeps the first pattern on ties).")
out.append("var grokLineSrc = []grokLineDef{")
for lp in grok.LINES:
    roles = ", ".join("{" + q(r) + ", " + q(f) + "}" for r, f in lp.roles.items())
    out.append(f"\t{{Name: {q(lp.name)}, Pattern: {q(lp.pattern)}, Family: {q(lp.family)}, Roles: [][2]string{{{roles}}}}},")
out.append("}")
print("\n".join(out))
