"""WP6 case generator: runs the Python SAP parser (parsers/sap_parser.py) on hand-picked edge cases (P05 section 9,
"Branches NOT covered by any golden", plus the Unicode behaviour of section 2) and writes the expected outputs as a
Go table.

    cd <repo> && PYTHONPATH=src python3 receiver/parse/testdata/wp6gen/gen_cases.py > receiver/parse/wp6_cases_test.go

Records cases: want = json.dumps([[line_no, rec], ...], ensure_ascii=False) of list(SapParser().records(text)), or
"!<Exception>" when records() raises (Go must return an ErrPyRaise error).
Score cases: want = repr(sap_score(lines)). Word cases: want = _sev_from_words(text, "D"). Logger cases: _svc_from_logger.
"""
import json, os, sys, tempfile

os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())
from watchover.parsers.sap_parser import SapParser, sap_score, _sev_from_words, _svc_from_logger


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


NWJ_FULL = "#2.0 #2026 09 16 02:14:15:100#+0300#Error#/System/Server/Deployment#com.sap.ASJ.dpl_dc.001028#BC-JAS-DPL#tc~bl~deploy_controller#com.sap.engine.Foo#Administrator#0#text here#"
RECORDS = [
    # LOGHEADER meta (4.4) feeding NW Java records, single-line record with every lead type (5)
    "<!--LOGHEADER[START]/-->\n<!--NAME[/usr/sap/TPD/J00/work/deploy.log]/-->\n<!--FORMATTER[com.sap.FOO]/-->\n<!--ENGINEVERSION[7.50]/-->\n" + NWJ_FULL,
    NWJ_FULL,
    # NW Java multi-line: blank-line termination, non-# continuation, no tz, empty origin
    "#2.0 #2026 09 16 02:14:15#XYZ#Fatal##\n#line two\nat foo.bar(Baz.java:1)\n\nplain after",
    "#2.0 #2026 09 16 02:14:15:000#-0130#warning#com.acme.Foo#\nstack 1\n  stack 2\n#2.0 #2026 09 16 02:14:16#+0000#PATH#com.sap.engine.services.dbpool.deploy.ContainerImpl#com.sap.engine.services.dbpool.deploy.ContainerImpl#j.doe#tail#",
    "#2.0 #2026 09 16 02:14:15##\n#\n#",
    # open record flushed by a single-line record: Python leaves the finished record in nwj -> KeyError on the next line
    "#2.0 #2026 09 16 02:14:15#+0300#Info#a#\n#2.0 #2026 09 16 02:14:16#+0300#Info#b#c#d#e#\n\nmore",
    "#2.0 #2026 09 16 02:14:15#+0300#Info#a#\n#2.0 #2026 09 16 02:14:16#+0300#Info#b#c#d#e#\n#2.0 #2026 09 16 02:14:17#+0300#Info#b#",
    "#2.0 #2026 09 16 02:14:16#+0300#Info#b#c#d#e#\n#2.0 #2026 09 16 02:14:16#+0300#Info#b#c#d#e#\nnext",
    "#1.3\x08#2026 09 16 02:14:15#+0300#Info#com.sap.x#com.sap.x#1user#m#",
    "#2.0 #2026 09 16 02:14:15#+0300#Info#com.sap.x#com.sap.x#",
    # availability log (4.8): rounding ties-to-even, negative, invalid date -> ValueError
    "Available 24.10.2021 18:51:33 - 24.10.2021 19:34:33",
    "Unavailable 24.10.2021 18:51:33 - 24.10.2021 18:52:03",
    "Unavailable 24.10.2021 18:51:33 - 24.10.2021 18:53:03",
    "Available 24.10.2021 18:51:33 - 24.10.2021 18:54:03",
    "Available 24.10.2021 18:51:33 - 24.10.2021 18:51:03",
    "Available 24.10.2021 18:51:33 - 24.10.2021 18:50:03",
    "Available 31.09.2021 18:51:33 - 24.10.2021 18:50:03",
    "Available 24.10.2021 18:51:33 - 29.02.2021 18:50:03",
    "Available 24.10.2021 24:00:00 - 24.10.2021 18:50:03",
    "Available 24.10.0000 10:00:00 - 24.10.2021 18:50:03",
    "Available ٢٤.10.2021 18:51:33 - ٢٤.10.2021 19:34:33",
    # tp / R3trans / SUM (4.17)
    "4 ETW000  date&time   : 16.09.2026 - 02:14:07\n2EETW125 SQL error \"8\" during import\n1 ETP111 exit code : \"8\"\n4 ETW000 R3trans version 6.26 20260916021500\n3WETW000 warn flag\n4 ETW000 rc = 4 here\nplain text later",
    "4 ETW000 stamp é20260916021500 here\n4 ETW000 stamp x20260916021500y\n4 ETW000 stamp -20260916021500-\n",
    "4 ETW000 timestamp 13.13.2026 - 25:61:61\n4 ETW000 done",
    "٤ ETW٠٠٠ ١٦.09.2026 - 02:14:07 deprecated api",
    # HANA d level, tab separator, long fraction
    "[1]{-1}[-1/-1] 2026-09-16\t02:14:10.1234567 d Basis  TrexNet.cpp(00123) : debug msg  ",
    # GC unified: level tags, 1000-5000 WARN, Full GC elif, no tags, uptime/os tags dropped
    "[2026-09-16T02:14:07.123+0300][12.345s][warning][gc,os] GC(3) Pause Young 12.345ms",
    "[2026-09-16T02:14:07.123+0300][12.345s][info][gc] GC(3) Pause Young 1234.5ms",
    "[2026-09-16T02:14:07.123+0300][12.345s][info][gc] Pause Full (System.gc()) 999ms",
    "[2026-09-16T02:14:07Z][trace][error][gc,class] Full GC 45msé then 7ms,",
    "[2026-09-16T02:14:07][debug] nothing here",
    "[2026-09-16T02:14:07.1Z][error][ms][info] 5000ms",
    # GC classic: < 1 s, 1-5 s, no secs, rounding
    "2026-09-16T02:14:07.123+0300: 12.345: [GC (Allocation Failure) 0.0012345 secs]",
    "2026-09-16T02:14:07.123+0300: 12.345: [Full GC (Ergonomics) 2.5 secs]",
    "2026-09-16T02:14:07.123+0300: 12.345: [Full GC (Ergonomics) ]",
    "2026-09-16T02:14:07.123+0300: 12.345: [GC 0.0025 secs]\n2026-09-16T02:14:07.123+0300: 12.345: [GC 0.0005 secs]",
    "2026-09-16T02:14:07.123+0300: 12.345: [GC 7.2345678 secs]",
    # JUL: PM / 12 AM, header without method, unpaired / overwritten header, CONFIG/FINE*, unpaired SEVERE
    "Sep 16, 2026 12:14:18 PM com.acme.Foo\nSEVERE: boom",
    "Sep 16, 2026 12:14:18 AM com.sap.engine.core.Framework start\nnoise line\nCONFIG:   cfg  ",
    "SEVERE: without header\nSep 16, 2026 1:02:03 PM com.sap.engine.services Foo\nSep 16, 2026 1:02:04 PM x.y bar\nFINEST:\nFINER: x\nFINE:y",
    "Sep 16, 2026 13:02:03 PM Foo\nWARNING: w",
    # dev trace: nodename/hostname, *** without =>, LOG with words, empty body before ts, sap-kernel default, META swallow
    "M  sysno 00\nM  Profile parameter x = y\nM  pid 4711 started\nM  node name   hostA\nM  *** something odd\nM  *** LOG R49=> connection failed\nM  *** LOG R49=> retry later\nM  *** LOG => all good\nM  Wed Sep 16 02:14:07 2026\nM  [Thr 1] plain\nM  hostname hostB\nM  ſid X\nM  SİD Y\nM  SID PRD\nX  after sid",
    "*  ACTIVE TRACE LEVEL 1\n* * *\n***\nB  ***ERROR=>x\nB  *** ERROR ABC => y\nB  *** WARNING => z\nB  ***LOGXYZ=>",
    "B  [Thr 7] *** LOG Q0I=>\nB  Wed Sep 16 02:14:07:000 2026\nB  [Thr 7] *** LOG Q0I=>",
    "trc file: \"dev_disp.new\", trc level: 1, release: \"753\"\nM  profile /usr/sap/PRD/SYS/profile/PRD_D00_sapprd01\nM  profile /x/y_z2\nM  hello\nunmatched other line",
    # SM21 without date, ALOG WRN / START, date-only line > 40
    "02:14:07 DIA  001 100 USER1  SE38  R6 8 Database error -1 at OPC access to table X",
    "System log  PRD  16.09.2026\n02:14:07 BTC  003     BATCH  SM36  EP 2 job ok\n02:14:08 XY   ٣٤٥ 100 U T BY ٨ x",
    "This line is long and has a date 16.09.2026 in it, more than forty\n112.09.2026\n12.09.20261\nx12.09.2026y",
    "WRN SAPK1 PRD       0004 20260916021407 user host\nSTART SAPK1 IMP PRD 0000 20260916021407\nERR S1 X 0012 2026091602140٧",
    # class list: stable sort ties, top 5, default package
    "a/A.class\nb/B.class\nb/C.class\nc/D.class\nd/E.class\ne/F.class\nf/G.class\nTop.class\nTop.class",
    "4 ETW000 16.09.2026 - 02:14:07\nx/Y.class",
    # JVM properties: zero properties, last header wins, no SID path, properties after other lines
    "# created at Sun Oct 24 18:52:12 2021",
    "# created at Sun Oct 24 18:52:12 2021\nname = A\n# created at Mon Oct 25 18:52:12 2021\nversion = 1\nhome = /opt/x\nplain words\ncpu=amd64 \nname = B\nname = C",
    # separators, # comment lines, Unicode words
    "#Software: IIS\n#Version: 1.0\n-----\n=====\n_-_",
    "db faıled now\nTİMEOUT reached\nſlow disk\nIOException\nx-error-y\n_error\nnot  found\nnot found\nrc=128\nrc = 12\nexit code : \"16\"\ndumps",
    # LOGHEADER NAME without SID path, other names
    "<!--NAME[/tmp/x.log]/-->\n<!--FOO[bar]/-->\n<!--NAME[a]/--> trailing\nother",
    "",
    "\n\n   \n",
]

SCORES = [
    [],
    ["M  hello", "A  x", "Z  y", "Q  ***"],
    ["M\thello", "plain"],
    ["x"] * 20 + ["trc file: \"a\", trc level: 1, release: \"7\""],
    ["x"] * 19 + ["<!--NAME[a]/-->"],
    ["#a", "SEVERE: x", "plain", "plain", "plain", "plain"],
    ["02:14:07 DIA  001 100 USER1  SE38  R6 8 m", "System log 16.09.2026", "x", "y", "z", "*  ab"],
    ["Available 24.10.2021 18:51:33 - 24.10.2021 19:34:33", "B  q", "B  r", "q"],
]

WORDS = ["error", "ERROR", "Exception", "IOException", "faıled", "TİMEOUT", "rc = 8", "rc=4", "rc=48", "exit code :\"4\"",
         "warnıng", "WARNING!", "ſhortdump", "not found", "deprecated.", "x_retry", "retry_x", "é error", "eerror", "error9"]
LOGGERS = ["com.sap.engine.services.deploy", "com.sap.engine.core.Framework", "com.sap.engine.services.dbpool.deploy.ContainerImpl",
           "com.sap.engine.services", "com.acme.Foo", "Foo", ".x", "com..sap.Tc.X", "com.sap.İnfo", "COM.SAP.x.y", "", "a..b"]


def run(text):
    try:
        return json.dumps([[i, r] for i, r in SapParser().records(text)], ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return "!" + type(e).__name__


o = ["// Code generated by receiver/parse/testdata/wp6gen/gen_cases.py from the Python SAP parser (sap_parser.py).",
     "// Expected values are Python 3.11 outputs. DO NOT EDIT by hand. Regenerate:",
     "//   PYTHONPATH=src python3 receiver/parse/testdata/wp6gen/gen_cases.py > receiver/parse/wp6_cases_test.go",
     "", "package parse", "",
     "// wp6Cases: want = json.dumps([[line_no, rec], ...]) or \"!<Python exception>\".",
     "var wp6Cases = []struct{ text, want string }{"]
for text in RECORDS:
    o.append(f"\t{{{gq(text)}, {gq(run(text))}}},")
o.append("}")
o += ["", "// wp6Scores: want = repr(sap_score(lines)).", "var wp6Scores = []struct {", "\tlines []string", "\twant  string", "}{"]
for lines in SCORES:
    o.append(f"\t{{[]string{{{', '.join(gq(x) for x in lines)}}}, {gq(repr(sap_score(lines)))}}},")
o.append("}")
o += ["", "// wp6Words: want = _sev_from_words(text, \"D\").", "var wp6Words = [][2]string{"]
for w in WORDS:
    o.append(f"\t{{{gq(w)}, {gq(_sev_from_words(w, 'D'))}}},")
o.append("}")
o += ["", "// wp6Loggers: want = _svc_from_logger(logger).", "var wp6Loggers = [][2]string{"]
for lg in LOGGERS:
    o.append(f"\t{{{gq(lg)}, {gq(_svc_from_logger(lg))}}},")
o.append("}")
sys.stdout.write("\n".join(o) + "\n")
