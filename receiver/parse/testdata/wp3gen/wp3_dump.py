"""Differential fixture for WP3: every TextParser/GrokParser/grok.detect call made by the golden generator loop,
plus parse_head / CONT_RE / family / grok-line results on every distinct line and edge cases."""
import gzip, json, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())
from watchover import grok
from watchover.parsers import text_parser, grok_parser, family
from watchover.pipeline import ingest_bytes

out = gzip.open(sys.argv[1], "wt", encoding="utf-8")
seen = set()
def emit(o):
    s = json.dumps(o, ensure_ascii=False)
    if s not in seen:
        seen.add(s); out.write(s + "\n")

orig_text = text_parser.TextParser.records
orig_grok = grok_parser.GrokParser.records
orig_detect = grok.detect

def wrap(kind, orig):
    def records(self, text):
        hint = family.FAMILY_HINT.get()
        recs = list(orig(self, text))
        emit({"t": "rec", "parser": kind, "text": text, "hint": hint, "seen": family.FAMILY_SEEN.get() if kind == "text" else None,
              "out": json.dumps([[i, r] for i, r in recs], ensure_ascii=False)})
        yield from recs
    return records
text_parser.TextParser.records = wrap("text", orig_text)
grok_parser.GrokParser.records = wrap("grok", orig_grok)
def detect(lines, min_share=0.6):
    lp, sh = orig_detect(lines, min_share)
    emit({"t": "gdet", "lines": list(lines), "name": lp.name if lp else None, "share": sh})
    return lp, sh
grok.detect = detect

inputs = ROOT / "receiver/parse/testdata/inputs"
files = [p for p in sorted((ROOT / "samples/corpus").iterdir()) if p.suffix != ".md" and p.is_file()] + sorted(inputs.iterdir())
alllines = set()
for p in files:
    data = p.read_bytes()
    alllines.update(data.decode("utf-8", "replace").splitlines())
    ingest_bytes(p.name, data)
    lines = data.split(b"\n"); hint = None
    for i in range(0, len(lines), 40):
        chunk = b"\n".join(lines[i:i + 40])
        if not chunk.strip(): continue
        if hint is not None:
            hint["n"] += 1
            if hint["n"] % 200 == 0: hint = None
        o, r = ingest_bytes(p.name, chunk, hint=hint)
        if r and r[0].get("format") not in (None, "table") and (hint is None or hint.get("n", 0) % 200 == 0):
            fmt = r[0]["format"]
            if fmt == "json" and chunk.strip().count(b"\n") == 0 and chunk.lstrip()[:1] == b"{": fmt = "jsonl"
            hint = {"format": fmt, "confidence": r[0].get("confidence", 0.9), "family": r[0].get("family", ""), "n": 1}

edge = [l.rstrip("\n") for l in open(sys.argv[2], encoding="utf-8")] if len(sys.argv) > 2 else []
for ln in sorted(alllines) + edge:
    rec, rest = text_parser.parse_head(ln)
    fams = [f for f, pats in family.FAMILIES if any(p.search(ln) for p in pats)]
    g = {}
    for lp in grok.LINES:
        m = grok.compile_pattern(lp.pattern, trusted=True).search(ln)
        g[lp.name] = None if not m else [[k, v] for k, v in m.groupdict().items()]
    emit({"t": "line", "ln": ln, "head": json.dumps([rec, rest], ensure_ascii=False), "cont": bool(text_parser.CONT_RE.match(ln)), "fams": fams, "grok": g})
out.close()
