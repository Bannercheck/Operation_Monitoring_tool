"""Fixture for the opt-in TestWP5PythonDiff: the four WP5 parsers (json, jsonl, csv, winevt) run by Python on
  * every golden input (decoded whole text and every 40-line live chunk) that the parser's format owns, and
  * N seeded random texts per parser built from format-specific fragments (default 20000 each).

    PYTHONPATH=src python3 receiver/parse/testdata/wp5gen/wp5_dump.py /tmp/wp5.jsonl.gz [N]
    WP5_DIFF=/tmp/wp5.jsonl.gz go test ./parse/ -run TestWP5PythonDiff -count=1
"""
import gzip, json, os, random, sys, tempfile
from pathlib import Path

os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())
from watchover.parsers.json_parser import JsonParser
from watchover.parsers.jsonl_parser import JsonlParser
from watchover.parsers.csv_parser import CsvParser
from watchover.parsers.winevt_parser import WinEvtParser
from watchover.loader import decode

PARSERS = {"json": JsonParser(), "jsonl": JsonlParser(), "csv": CsvParser(), "winevt": WinEvtParser()}
ROOT = Path(__file__).resolve().parents[4]


def run(name, text):
    try:
        return json.dumps([[i, r] for i, r in PARSERS[name].records(text)], ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return "!" + type(e).__name__


def texts_of(p):
    data = p.read_bytes()
    out = [decode(data) or ""]
    lines = data.split(b"\n")
    for i in range(0, len(lines), 40):
        chunk = b"\n".join(lines[i:i + 40])
        if chunk.strip():
            out.append(chunk.decode("utf-8", "replace"))
    return out


OWN = {".json": ["json", "jsonl"], ".jsonl": ["jsonl", "json"], ".csv": ["csv"], ".log": []}
XML_TOK = ["<Event>", "</Event>", "<Event ", " ", "\n", "\r", "\r\n", "\t", "<Data", " Name=", '"', "'", "x", ">", "</Data>", "/>", "<", "&", "&amp;",
           "&lt;", "&#65;", "&#x;", "&#x41;", "&#9;", ";", "<!--", "-->", "--", "<![CDATA[", "]]>", "<?pi ", "?>", "=", "a", ":", "p:", "xmlns",
           'xmlns:p="u"', 'xmlns="v"', "<Level>", "</Level>", "2", "<Provider Name=\"P\"/>", "é", "̀", "×", "\x01", "<EventID>", "</EventID>",
           "<System>", "</System>", "]", "-", ".", "_", "<TimeCreated SystemTime='2026-01-01T00:00:00Z'/>", "<Channel>c</Channel>", "<Computer>h</Computer>"]
CSV_TOK = ["a", "b", ",", ",", ";", "\t", "|", '"', '"', '""', "\n", "\n", "\r\n", "\r", " ", "x y", "é", " ", "\x00", "1", "\x85", "\x0c"]
JSON_TOK = ['{"a":1}', '{"msg":"ERROR x"}', "[1]", "\n", "\r\n", " ", "\xa0", "{", "}", "[", "]", ",", ":", '"', '"k"', "1", "1.5", "NaN", " ",
            '{"log":"2026-01-01 WARN [s] m"}', '"resourceLogs"', '"scopeLogs"', '"logRecords"', '"body"', '"value"', '"key"', '"attributes"', "null", "true"]


def main():
    out = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    rng = random.Random(5)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        def emit(name, text):
            f.write(json.dumps({"parser": name, "text": text, "out": run(name, text)}, ensure_ascii=False) + "\n")
        inputs = [p for p in sorted((ROOT / "samples" / "corpus").iterdir()) if p.is_file() and p.suffix != ".md"]
        inputs += sorted((ROOT / "receiver" / "parse" / "testdata" / "inputs").iterdir())
        for p in inputs:
            names = OWN.get(p.suffix, [])
            if "windows_event" in p.name:
                names = ["winevt"]
            for t in texts_of(p):
                for name in names:
                    emit(name, t)
        for _ in range(n):
            emit("winevt", "<Event>" + "".join(rng.choice(XML_TOK) for _ in range(rng.randint(1, 20))) + "</Event>")
            emit("csv", "".join(rng.choice(CSV_TOK) for _ in range(rng.randint(1, 30))))
            t = "".join(rng.choice(JSON_TOK) for _ in range(rng.randint(1, 16)))
            emit("jsonl", t)
            emit("json", t)


main()
