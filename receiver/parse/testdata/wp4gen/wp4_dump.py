"""WP4 differential fixture: the Python syslog / access / w3c / kv / cef parsers run on every golden input, on the whole
decoded text and on every 40-line live chunk (scripts/parse_golden.py split), whatever format the input really is.

    cd <repo> && PYTHONPATH=src python3 receiver/parse/testdata/wp4gen/wp4_dump.py /tmp/wp4.jsonl.gz
    cd receiver && WP4_DIFF=/tmp/wp4.jsonl.gz go test ./parse/ -run TestWP4PythonDiff -count=1

Each fixture line: {"text": ..., "out": {parser: json.dumps([[line_no, rec], ...]) or "!<Exception>"}}.
"""
import gzip, json, os, sys, tempfile
from pathlib import Path

os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())
from watchover.loader import decode
from watchover.parsers.syslog_parser import SyslogParser
from watchover.parsers.access_parser import AccessParser
from watchover.parsers.w3c_parser import W3cParser
from watchover.parsers.kv_parser import KvParser
from watchover.parsers.cef_parser import CefParser

ROOT = Path(__file__).resolve().parents[4]
PARSERS = {"syslog": SyslogParser(), "access": AccessParser(), "w3c": W3cParser(), "kv": KvParser(), "cef": CefParser()}


def run(p, text):
    try:
        return json.dumps([[i, r] for i, r in p.records(text)], ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return "!" + type(e).__name__


def main(out):
    files = [p for p in sorted((ROOT / "samples" / "corpus").iterdir()) if p.suffix != ".md" and p.is_file()]
    files += sorted((ROOT / "receiver" / "parse" / "testdata" / "inputs").iterdir())
    n = 0
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for p in files:
            data = p.read_bytes()
            lines = data.split(b"\n")
            chunks = [data] + [b"\n".join(lines[i:i + 40]) for i in range(0, len(lines), 40)]
            for c in chunks:
                text = decode(c)
                if not text or not c.strip():
                    continue
                f.write(json.dumps({"text": text, "out": {k: run(v, text) for k, v in PARSERS.items()}}, ensure_ascii=False) + "\n")
                n += 1
    print(n, "texts", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
