"""WP5 case generator: runs the Python document parsers (json, jsonl, csv, winevt) on hand-picked edge cases
(P04 sections 6-9 and 12, mostly branches no golden input covers) plus seeded random CSV / XML texts, and writes the
expected outputs as a Go table.

    cd <repo> && PYTHONPATH=src python3 receiver/parse/testdata/wp5gen/gen_cases.py > receiver/parse/wp5_cases_test.go

Expected value: json.dumps([[line_no, rec], ...], ensure_ascii=False) of list(parser.records(text)), or "!<Exception>"
when records() raises (the Go parser must then return an ErrPyRaise error).
"""
import json, os, random, sys, tempfile

os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())
from watchover.parsers.json_parser import JsonParser
from watchover.parsers.jsonl_parser import JsonlParser
from watchover.parsers.csv_parser import CsvParser
from watchover.parsers.winevt_parser import WinEvtParser

PARSERS = {"json": JsonParser(), "jsonl": JsonlParser(), "csv": CsvParser(), "winevt": WinEvtParser()}


def gq(s: str) -> str:
    """A Go interpreted string literal (non-ASCII as \\U escapes, so the file stays ASCII)."""
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif 0x20 <= c < 0x7F:
            out.append(ch)
        elif c < 0x80:
            out.append("\\x%02x" % c)
        else:
            out.append("\\U%08x" % c)
    out.append('"')
    return "".join(out)


BIG = "1" * 4301
X = 'xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'


def ev(system="", data="", attrs=X):
    return f"<Event {attrs}><System>{system}</System><EventData>{data}</EventData></Event>"


SYS = '<Provider Name="Prov"/><EventID>4625</EventID><Level>2</Level><TimeCreated SystemTime="2026-09-22T06:14:02.0000000Z"/><Computer>HOST1</Computer>'

CASES = [
    # ---- csv (P04 7)
    ("csv", "a,b\n1\n1,2,3\n\n4,5"),
    ("csv", "\na,b\n1,2\n3,4"),
    ("csv", 'a,b\n"q""r"s,2\n'),
    ("csv", 'a,b\n"multi\nline",2\n3,4\n'),
    ("csv", "a,b\r\n1,2\r\n3,4\r\n"),
    ("csv", "a,b\n1\r2,3\n"),
    ("csv", "a,b\n1,2\r"),
    ("csv", "a,b\r1,2\r3,4"),
    ("csv", "a,a, a ,b\n1,2,3,4\n"),
    ("csv", "a,b,c\n1,2\n"),
    ("csv", "a;b;c\n1;2;3\n4;5;6"),
    ("csv", "a\tb\n1\t2\n"),
    ("csv", "a|b\n1|2\n"),
    ("csv", "x\ny\n"),
    ("csv", " \na,b\n1,2"),
    ("csv", 'a,b\n"unterminated,2\n3,4'),
    ("csv", 'a,b\n1,"x"\n'),
    ("csv", 'a,"b\nc",d\n1,2,3'),
    ("csv", "a,b\n1\x00,2\n"),
    ("csv", "a,b\n1,2\n\n\n3,4\n"),
    ("csv", "a,b\n,\n"),
    ("csv", 'a,b\n"",""\n'),
    ("csv", 'a,b\n1,2"x\n'),
    ('csv', '"a" ,b\n1,2'),
    ("csv", ""),
    ("csv", "a,b"),
    ("csv", "﻿a,b\n1,2"),
    ("csv", "a,b\n1,2 x,y\n"),
    ("csv", "a,b\n1,2\x1c3,4\n"),
    ("csv", "a,b\n1,2\x85"),
    ("csv", "a,b\n1,2\n \n3,4"),
    ("csv", "a,b\n1,2,3,4\n"),
    ("csv", 'a,b\n"x"\r\n'),
    ("csv", 'a,b\n"x"\ry\n'),
    ("csv", "a,b\n\r\n1,2"),
    ("csv", "a,b\n\r1,2"),
    ("csv", "a;b,c\n1;2,3\n"),
    # ---- json (P04 8)
    ("json", '[{"a":1},{"b":2}]'),
    ("json", '[1, {"a":1}]'),
    ("json", '[{"a":1}, 2, {"b":{"c":3}}]'),
    ("json", '{"x":{"y":{"z":{"w":[{"a":1}]}}}}'),
    ("json", '{"x":{"y":{"z":{"w":{"v":[{"a":1}]}}}}}'),
    ("json", '{"a":[], "b":[1], "c":[[{"x":1}]], "d":[{"e":1}], "f":[{"g":2}]}'),
    ("json", '{"a":1,"a":2,"b":NaN,"c":1e400,"d":10000000000000000000000,"e":-Infinity,"f":1.0,"g":[1,{"h":2}]}'),
    ("json", '"str"'),
    ("json", "5"),
    ("json", "null"),
    ("json", "not json"),
    ("json", "﻿{\"a\":1}"),
    ("json", ' \n{"a":1}\n '),
    ("json", '\x0c{"a":1}'),
    ("json", '{"a": ' + BIG + "}"),
    ("json", '{"a": {"b": {}}, "c": 1}'),
    ("json", '{"a.b": 1, "a": {"b": 2}, "z": 0}'),
    ("json", "{}"),
    ("json", "[]"),
    ("json", '[{}]'),
    ("json", '{"resourceLogs": null}'),
    ("json", '{"resourceLogs": []}'),
    ("json", '{"resourceLogs": ""}'),
    ("json", '{"resourceLogs": "ab"}'),
    ("json", '{"resourceLogs": {}}'),
    ("json", '{"resourceLogs": {"k":1}}'),
    ("json", '{"resourceLogs": 0}'),
    ("json", '{"resourceLogs": [1]}'),
    ("json", '{"resourceLogs": [{"resource": null}]}'),
    ("json", '{"resourceLogs": [{"resource": {"attributes": "ab"}}]}'),
    ("json", '{"resourceLogs": [{"resource": {"attributes": {"k": 1}}}]}'),
    ("json", '{"resourceLogs": [{"resource": {"attributes": 5}}]}'),
    ("json", '{"resourceLogs": [{"resource": {"attributes": 0}, "scopeLogs": []}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": null}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": null, "instrumentationLibraryLogs": null}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": 0, "instrumentationLibraryLogs": ""}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": null}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [1]}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [{}]}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [{"timeUnixNano": "0", "severityText": "", "severityNumber": 0}]}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [{"timeUnixNano": "", "observedTimeUnixNano": "1790068503500000000", "severityNumber": 9, "body": "plain"}]}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [{"body": {}}, {"body": 5}, {"body": [1, 2]}, {"body": {"kvlistValue": {"values": []}}}]}]}]}'),
    ("json", '{"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}, {"key": "message", "value": {"stringValue": "res-msg"}}, {"value": {}}, {"key": "n", "value": 7}, {"key": "kv", "value": {"kvlistValue": {"values": [{"key": "a"}]}}}, {"key": "arr", "value": {"arrayValue": {"values": [{"intValue": "1"}]}}}]}, '
             '"instrumentationLibraryLogs": [{"logRecords": [{"timeUnixNano": "1790068503500000000", "severityText": "WARN", "body": {"stringValue": "hello"}, "attributes": [{"key": "severity", "value": {"intValue": "3"}}, {"key": "k2", "value": {"doubleValue": 1.5}}, {"key": "k2", "value": {"boolValue": true}}]}]}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [1]}]}]}]}'),
    ("json", '{"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [{"key": [1], "value": {}}]}]}]}]}'),
    ("json", '{"resourceLogs": [], "other": [{"a": 1}]}'),
    ("json", '[{"log": "2026-01-01T00:00:00Z ERROR [app] boom\\n"}]'),
    ("json", '[{"log": "2026-01-01T00:00:00Z ERROR [app] boom", "service": "s"}]'),
    ("json", '[{"log": "2026-01-01T00:00:00Z ERROR boom", "level": "x"}]'),
    ("json", '[{"message": "", "msg": "WARN y"}]'),
    ("json", '[{"message": 5, "msg": "ERROR [svc] z"}]'),
    ("json", '[{"short_message": "2026-01-01 12:00:00 WARN"}]'),
    ("json", '[{"msg": "INFO\\n\\n"}]'),
    # ---- jsonl (P04 9)
    ("jsonl", '{"a":1}\n[1]\n5\nbad\n\n  {"b":2}  \n{"c":NaN,"d":1e400,"e":10000000000000000000000}'),
    ("jsonl", '{"a":1}\r\n{"b":2} {"c":3}\x1c{"d":4}'),
    ("jsonl", '\xa0{"a":1}\xa0'),
    ("jsonl", '{"a":1}\x0c'),
    ("jsonl", '{"x":1}\n{"a": ' + BIG + "}"),
    ("jsonl", '﻿{"a":1}\n{"b":2}'),
    ("jsonl", '{"a":1,"a":{"b":2}}'),
    ("jsonl", '{"message":"2026-01-01 12:00:00 WARN worker - something"}'),
    ("jsonl", '{"msg":"INFO x","app":"y"}'),
    ("jsonl", '{"log":"2026-09-22T09:14:02.123Z stdout F 2026-09-22 09:14:02 ERROR [api] failed\\n"}'),
    ("jsonl", '{"a": {"b": {"c": {"d": {"e": 1}}}}, "l": [{"x": 1}]}'),
    ("jsonl", '{"a":1}\n{"a":1'),
    ("jsonl", '{"a":"\\u00e9\\n\\t"}'),
    # ---- winevt (P04 6)
    ("winevt", ev(SYS, '<Data Name="param1">Print Spooler</Data><Data>x</Data><Data Name="">y</Data><Data Name="e"></Data>')),
    ("winevt", ev("<Level>3</Level>", "<Data>a</Data>")),
    ("winevt", ev('<Provider Name=""/><Channel>Security</Channel><Level>5</Level>')),
    ("winevt", ev("<Level>9</Level><EventID> 7 </EventID>")),
    ("winevt", ev(SYS, '<Data Name="host">h2</Data><Data Name="message">m</Data><Data Name="provider">p</Data><Data Name="severity">s</Data><Data Name="zz">z</Data>')),
    ("winevt", ev(SYS) + "\n" + ev("<Level>2</Level>", "<Data>&lt;bad</Data></Oops>") + "\n" + ev("<Level>4</Level>")),
    ("winevt", "<Events>" + ev(SYS) + "</Events>"),
    ("winevt", "<Event</Event>" + ev(SYS)),
    ("winevt", "<Event-x></Event>" + ev("<Level>1</Level>")),
    ("winevt", "<Eventé>" + ev("<Level>1</Level>")),
    ("winevt", "<Event_x>" + ev("<Level>1</Level>")),
    ("winevt", '<e:Event xmlns:e="u"><e:System><e:Level>2</e:Level></e:System></e:Event>'),
    ("winevt", '<Event xmlns:e="u"><e:System><e:Level>2</e:Level><e:Provider e:Name="pp" Name="qq"/></e:System></Event>'),
    ("winevt", "<Event>\n  <System>\r\n    <Provider Name='Multi\tLine\r\nProv'/>\n    <EventID>1</EventID>\n  </System>\n  <EventData>\n    <Data Name='a'>  x &amp; y &#65;&#x42; <![CDATA[<z>]]> <!-- c --> w  </Data>\n  </EventData>\n</Event>"),
    ("winevt", ev('<TimeCreated SystemTime="\n2026-09-22T06:14:02Z"/><TimeCreated/>')),
    ("winevt", ev("", '<Data Name="a">pre<b>child</b>post</Data><Data Name="c"><?pi x?>t1<!--x-->t2</Data>')),
    ("winevt", ev("", '<Data Name="a">x</Data><Data Name="a">y</Data>')),
    ("winevt", ev("<Level>2</Level>") + ev("<Level>3</Level>")),
    ("winevt", "<Event><Event></Event></Event>"),
    ("winevt", "<Event/></Event>"),
    ("winevt", "<Event />x</Event>" + ev("<Level>1</Level>")),
    ("winevt", "<Event>\x01</Event><Event><Level>2</Level></Event>"),
    ("winevt", ev("<Level>2</Level><Task>t</Task><Keywords>0x8</Keywords><Opcode>0</Opcode><Channel>C</Channel>")),
    ("winevt", '<Event a="1" a="2"></Event><Event xmlns:p="u" xmlns:q="u" p:a="1" q:a="2"></Event><Event p:a="1"></Event><Event xmlns:p=""></Event><Event xmlns=""><Level>4</Level></Event>'),
    ("winevt", "<Event>&foo;</Event><Event>&#0;</Event><Event>]]></Event><Event><!-- a -- b --></Event><Event><?xml x?></Event><Event><!DOCTYPE x></Event><Event><Level>&#x32;</Level></Event>"),
    ("winevt", '<Event xml:lang="en"><Level>2</Level></Event><Event xmlns:xml="x"></Event><Event xmlns:xmlns="x"></Event><Event a="x<y"></Event><Event><Data Name="x&amp;&#9;y\r\nz">v</Data></Event>'),
    ("winevt", "<Event>￾</Event><Event>\x85 </Event><Event><Level>\U0001F600</Level></Event><Event><Data Name=\"é\">é</Data></Event>"),
    ("winevt", "<Event><Evént/><Level>1</Level></Event><Event><Level>2</Level></Event>"),
    ("winevt", "<Event a='1'b='2'></Event><Event / ></Event><Event a=1></Event><Event><Level >2</Level ></Event>"),
    ("winevt", "<Event><a:b:c/></Event><Event><:a/></Event><Event><a: x='1'/></Event><Event xmlns:a='u'><a:x/></Event>"),
    ("winevt", "<Event><![CDATA[x]]y]]></Event><Event><![cdata[x]]></Event><Event><!----></Event><Event><!--></Event><Event><!--->--></Event>"),
    ("winevt", "<Event><?XmL x?></Event><Event><?pi?></Event><Event><?pi\"x\"?></Event><Event><? pi?></Event><Event><?pi:x y?></Event><Event><?xml-foo?></Event>"),
    ("winevt", "<Event>&#xD800;</Event><Event>&#x10FFFF;</Event><Event>&#x110000;</Event><Event>&#X41;</Event><Event>&#65</Event><Event>&#;</Event><Event>& </Event><Event>&GT;</Event>"),
    ("winevt", "<Event xmlns='http://www.w3.org/XML/1998/namespace'/></Event><Event xmlns:p='http://www.w3.org/2000/xmlns/'></Event><Event xmlns:xml='http://www.w3.org/XML/1998/namespace'><Level>2</Level></Event>"),
    ("winevt", "no events here"),
    ("winevt", "<Event"),
    ("winevt", "<Event>"),
]

# Seeded random texts (differential fuzz baked into the table).
rng = random.Random(20261015)
XML_TOK = ["<Event>", "</Event>", "<Event ", " ", "\n", "\r", "\t", "<Data", " Name=", '"', "'", "x", ">", "</Data>", "/>", "<", "&", "&amp;",
           "&lt;", "&#65;", "&#x;", ";", "<!--", "-->", "--", "<![CDATA[", "]]>", "<?pi ", "?>", "=", "a", ":", "p:", "xmlns", "xmlns:p=\"u\"",
           "<Level>", "</Level>", "2", "<Provider Name=\"P\"/>", "é", "\x01", "<EventID>", "</EventID>", "<System>", "</System>", "]", "-", ".", "_"]
for _ in range(500):
    CASES.append(("winevt", "<Event>" + "".join(rng.choice(XML_TOK) for _ in range(rng.randint(1, 14))) + "</Event>"))
CSV_TOK = ["a", "b", ",", ",", ";", "\t", "|", '"', '"', '""', "\n", "\n", "\r\n", "\r", " ", "x y", "é", " ", "\x00", "1"]
for _ in range(400):
    CASES.append(("csv", "".join(rng.choice(CSV_TOK) for _ in range(rng.randint(1, 24)))))
JSONL_TOK = ['{"a":1}', '{"msg":"ERROR x"}', "[1]", "\n", "\r\n", " ", "\xa0", "{", "}", '"', "1", "NaN", " ", '{"log":"2026-01-01 WARN [s] m"}']
for _ in range(150):
    CASES.append(("jsonl", "".join(rng.choice(JSONL_TOK) for _ in range(rng.randint(1, 10)))))


def run(name, text):
    try:
        return json.dumps([[i, r] for i, r in PARSERS[name].records(text)], ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return "!" + type(e).__name__


print("// Code generated by receiver/parse/testdata/wp5gen/gen_cases.py from the Python parsers (json_parser.py,")
print("// jsonl_parser.py, csv_parser.py, winevt_parser.py). Expected values are Python 3.11 outputs. DO NOT EDIT by hand.")
print("// Regenerate:")
print("//   PYTHONPATH=src python3 receiver/parse/testdata/wp5gen/gen_cases.py > receiver/parse/wp5_cases_test.go")
print()
print("package parse")
print()
print("// wp5Cases: want = json.dumps([[line_no, rec], ...]) or \"!<Python exception>\".")
print("var wp5Cases = []struct{ parser, text, want string }{")
for name, text in CASES:
    print(f"\t{{{gq(name)}, {gq(text)}, {gq(run(name, text))}}},")
print("}")

# Pipeline cases: pipeline.ingest_bytes(name, data) in this order with one WATCHOVER_HOME (learned profiles carry over),
# written like the goldens (report line, then one canonical line per observation).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "scripts"))
from parse_golden import canon  # noqa: E402
from watchover.pipeline import ingest_bytes  # noqa: E402

ULS = ("Timestamp\tProcess\tTID\tArea\tCategory\tEventID\tLevel\tMessage\tCorrelation\n"
       "09/22/2026 09:14:02.12 \tw3wp.exe (0x1A2B)\t0x0F10\tSharePoint Foundation\tMonitoring\tnasq\tHigh\t"
       "Leaving Monitored Scope (Request (GET:https://sp.corp/sites/a)). Execution Time=245.12\tc0ffee01-0000\n"
       "09/22/2026 09:14:03.50 \tw3wp.exe (0x1A2B)\t0x0F10\tSharePoint Server Search\tQuery\tb4z1\tUnexpected\t"
       "Search failed  \tc0ffee02-0000\n")
PIPE = [
    ("events.xml", ev(SYS, '<Data Name="param1">Print Spooler</Data><Data>x</Data>') + "\n" + ev("<Level>3</Level>", "") + "\n"),
    ("doc.json", '{"items": [{"time": "2026-09-22T09:14:02Z", "level": "warn", "msg": "a", "n": 1.0, "big": 10000000000000000000000}, 7, {"time": "2026-09-22T09:14:03Z", "msg": "b", "nested": {"x": [1, {"y": 2}]}}]}'),
    ("blank.csv", "time,level,msg\n2026-09-22 09:14:02,ERROR,first\n\n2026-09-22 09:14:03,INFO,\"multi\nline\"\n2026-09-22 09:14:04,WARN,last\n"),
    ("uls.log", ULS),
    ("app.jsonl", '{"ts": "2026-09-22T09:14:02Z", "log": "2026-09-22 09:14:02 ERROR [api] boom\\n"}\n\n{"ts": "2026-09-22T09:14:03Z", "message": "ok", "level": "info"}\n'),
]
print()
print("// wp5PipelineCases: want = the golden-style lines of pipeline.ingest_bytes(name, text), joined by \"\\n\".")
print("var wp5PipelineCases = []struct{ name, text, want string }{")
for name, text in PIPE:
    obs, rep = ingest_bytes(name, text.encode("utf-8"))
    rep = [{k: v for k, v in r.items() if k not in ("sha", "table")} for r in rep]
    lines = [json.dumps({"report": rep}, sort_keys=True, ensure_ascii=False, default=str)]
    lines += [json.dumps(canon(o), sort_keys=True, ensure_ascii=False, default=str) for o in obs]
    print(f"\t{{{gq(name)}, {gq(text)}, {gq(chr(10).join(lines))}}},")
print("}")
