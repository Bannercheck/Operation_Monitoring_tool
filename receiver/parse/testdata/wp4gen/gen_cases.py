"""WP4 case generator: runs the Python line-structured parsers (syslog, access, w3c, kv, cef) on hand-picked edge cases
(P04 sections 1-5 and 12, mostly branches no golden input covers) and writes the expected outputs as a Go table.

    cd <repo> && PYTHONPATH=src python3 receiver/parse/testdata/wp4gen/gen_cases.py > receiver/parse/wp4_cases_test.go

Expected value: json.dumps([[line_no, rec], ...], ensure_ascii=False) of list(parser.records(text)), or "!<Exception>"
when records() raises (the Go parser must then return an ErrPyRaise error).
"""
import json, os, sys, tempfile

os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())
from watchover.parsers.syslog_parser import SyslogParser
from watchover.parsers.access_parser import AccessParser
from watchover.parsers.w3c_parser import W3cParser
from watchover.parsers.kv_parser import KvParser
from watchover.parsers.cef_parser import CefParser

PARSERS = {"syslog": SyslogParser(), "access": AccessParser(), "w3c": W3cParser(), "kv": KvParser(), "cef": CefParser()}


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
PAN_T = "1,2026/09/22 09:14:02,0011C101,TRAFFIC,end,2049,2026/09/22 09:14:02,10.0.0.1,8.8.8.8,0.0.0.0,0.0.0.0,allow-dns,,,dns,vsys1,trust,untrust,ethernet1/1,ethernet1/2,fwd,2026/09/22 09:14:02,1234,1,53000,53,0,0,0x0,udp,allow"
def pan_threat(sev_fields, threat='"Eicar File(39040)"'):
    f = ["1", " 2026/09/22 09:15:03 ", "0011C101", " THREAT ", " virus ", "2049", "2026/09/22", "10.0.0.9", "10.1.1.1"] + ["x"] * 21
    f[30:30] = [threat] + sev_fields
    return ",".join(f)

CASES = [
    # syslog: _NOT_LEVEL host check (P04 1.2)
    ("syslog", "Sep 22 09:14:02 INFO sshd[42]: x"),
    ("syslog", "Sep 22 09:14:02 info-svc sshd: x"),
    ("syslog", "Sep 22 09:14:02 İNFO sshd: x"),
    ("syslog", "Sep 22 09:14:02 ınfo sshd: x"),
    ("syslog", "Sep 22 09:14:02 ERRORS sshd: x"),
    ("syslog", "Sep 22 09:14:02 ERR_x sshd: x"),
    ("syslog", "Sep 22 09:14:02 Warning2 sshd: x"),
    ("syslog", "Sep 22 09:14:02 WARNING something happened"),
    ("syslog", "Sep 22 09:14:02 web01 something happened"),
    ("syslog", "Sep 22 09:14:02 dbg.local note: x"),
    # BSD with <PRI>, pid, Unicode digits
    ("syslog", "<13>Sep 22 09:14:02 host prog[1]: msg"),
    ("syslog", "<0013>Sep 22 09:14:02 host prog: msg"),
    ("syslog", "<١٣>Sep  2 09:14:02 host prog: msg"),
    ("syslog", "<191>Sep 22 2026 09:14:02 host  prog:    spaced msg  "),
    ("syslog", "<" + BIG + ">Sep 22 09:14:02 host prog: msg"),
    # ASA vendors
    ("syslog", "Sep 22 2026 09:15:10 fw01 : %ASA-4-106023: Deny tcp"),
    ("syslog", "Sep 22 2026 09:15:10 fw01 %FTD-3-430002: x"),
    ("syslog", "<166>Sep 22 2026 09:15:10 fw01 %PIX-6-302013: Built"),
    ("syslog", "Sep 22 09:15:10 fw01 %FWSM-1-1: x"),
    ("syslog", "Sep 22 09:15:10 fw01 %ASA-44-1: x"),
    # ISO ts "syslog" lines
    ("syslog", "2026-10-14T09:30:00.987Z Ax32Serv: Error: boom"),
    ("syslog", "2026-10-14T09:30:00.003Z Logon       Error: 604223, x"),
    # RFC 5424
    ("syslog", "<165>1 2026-09-22T09:15:03.500+03:00 host app 12 ID47 - ﻿BOM message"),
    ("syslog", "<165>1 2026-09-22T09:15:03.500+03:00 host - - - - plain"),
    ("syslog", '<14>1 2026-09-22T09:15:03Z h a p m [ex@1 k="v" k2="a\\"b"][ex@2 k="w" k="z"] tail'),
    ("syslog", '<14>1 2026-09-22T09:15:03Z h a p m [ex@1 k="a\\]b"] rest'),
    ("syslog", '<14>1 2026-09-22T09:15:03Z h a p m [bad block] m'),
    ("syslog", "<" + BIG + ">1 2026-09-22T09:15:03Z h a p m - m"),
    # PAN
    ("syslog", "Sep 22 09:14:02 pa-fw01 " + PAN_T),
    ("syslog", "<14>Sep 22 09:14:02 pa-fw01 " + PAN_T.replace("TRAFFIC", "SYSTEM")),
    ("syslog", "Sep 22 09:14:02 pa-fw01 " + PAN_T.replace("TRAFFIC", "WEIRD")),
    ("syslog", "Sep 22 09:14:02 pa-fw01 " + pan_threat(["x", " Critical ", "high"])),
    ("syslog", "Sep 22 09:14:02 pa-fw01 " + pan_threat(["İNFORMATIONAL", "low"])),
    ("syslog", "Sep 22 09:14:02 pa-fw01 " + pan_threat(["nothing"])),
    ("syslog", "Sep 22 09:14:02 pa-fw01 " + pan_threat(["medium"], threat='"A, B"')),
    ("syslog", "Sep 22 09:14:02 pa-fw01 1,,a,TRAFFIC,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r"),
    ("syslog", "Sep 22 09:14:02 pa-fw01 1,a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r"),
    ("syslog", "\n  \nSep 22 09:14:02 h p: a\r\nnot syslog\x1cSep 22 09:14:03 h p: b"),
    # access: VHOST, CLF variants, parse_extra
    ("access", 'www.example.com:443 10.0.0.1 - - [22/Sep/2026:09:14:02 +0300] "GET / HTTP/1.1" 200 512'),
    ("access", 'vh 10.0.0.1 - ali [22/Sep/2026:09:14:02 +0300] "GET / HTTP/1.1" 404 -'),
    ("access", 'vh:80x 10.0.0.1 - - [22/Sep/2026:09:14:02 +0300] "GET / HTTP/1.1" 200 1'),
    ("access", '10.0.0.1 - - [22/Sep/2026:09:14:02 +0300] "GET / HTTP/1.1" - 0'),
    ("access", '10.0.0.1 - - [22/Sep/2026:09:14:02 +0300] "GET / HTTP/1.1" 200 5 "" ""'),
    ("access", '10.0.0.1 - - [x] "GET / HTTP/1.1" 200 5 "-" "curl" rt=0.12 Status=999 007 0.000 1.5 2.5 deadbeefdeadbeefdeadbeef foo "bar" - ""'),
    ("access", '10.0.0.1 - - [x] "GET /" 200 5 "-" "c" 12 34 0123456789abcdef0123456789abcdef 9f1c2d3e-1111-2222-3333-444455556666'),
    ("access", '10.0.0.1 - - [x] "GET /" 200 5 "-" "c" duration_s=1 5 req="a b" x=y'),
    ("access", '10.0.0.1 - - [x] "GET /" 503 5 ١٢.٣'),
    ("access", '10.0.0.1 - - [x] "GET /" 200 5 ' + BIG),
    ("access", '10.0.0.1 - - [x] "GET /" 200 5 99999999999999999999999'),
    # ALB
    ("access", 'https 2026-09-22T06:14:02.003412Z app/my-lb/50dc 10.0.0.1:2817 - -1 -1 -1 - 502 0 0 "GET https://x:443/ HTTP/1.1" "curl/8" - - arn:tg "Root=1" "-" "arn" "-"'),
    ("access", 'h2 2026-09-22T06:14:02.003412Z app/my-lb/50dc 10.0.0.1 10.0.1.5:80 0.001 0.116 0.000 404 404 34 366 "GET / HTTP/2.0" "ua" ECDHE TLSv1.2 arn:tg "Root=1" "api.example.com"'),
    # Envoy
    ("access", '[2026-09-22T06:14:02.003Z] "GET /api HTTP/1.1" 503 UF 0 91 3 - "-" "curl/8" "abc-123" "api.example.com:8080" "10.0.0.5:8080"'),
    ("access", '[2026-09-22T06:14:02.003Z] "POST /api HTTP/2" 200 - via_upstream 10 20 30 25 "1.2.3.4" "ua" "-" "-" "-"'),
    ("access", '[2026-09-22T06:14:02.003Z] "GET /x HTTP/1.1" - - 0 0 - - "" "" "" "" ""'),
    ("access", '[2026-09-22T06:14:02.003Z] "GET /x HTTP/1.1" 200 - 0 0 ' + BIG + ' 1 "" "" "" "" ""'),
    ("access", "not an access line\n\n"),
    # w3c
    ("w3c", "#Software: Microsoft Internet Information Services 10.0\n#Version: 1.0\n#Date: 2026-09-22\n"
            "#Fields: date time s-ip cs-method cs-uri-stem cs-uri-query s-port sc-status\n"
            "2026-09-22 06:14:02 10.0.0.5 GET /index.html - 443 200\n"
            "2026-09-22 06:14:03 10.0.0.5 POST /api  443 500 extra cols\n"
            "  \n"
            "short row\n"
            "#Fields: date-time s-computername s-sitename cs-uri status\n"
            "2026-09-22T06:14:04Z WEB01 W3SVC1 /x?y=1 404\n"
            "2026-09-22T06:14:04Z - - /x 302\n"
            "- - - - -\n"),
    ("w3c", "#Fields: time cs-method cs-uri-stem cs-uri\nx GET - /fallback\n"),
    ("w3c", "#Fields:  a a b\n1 2 3\n- 2 -\n"),
    ("w3c", "rows before fields\n#Fields: message severity host service timestamp\nm s h v t\n"),
    ("w3c", "#Fields: s-computername cs-method\nWEB01 GET\n GET\n"),
    ("w3c", "#Fields: s-computername s-ip\n 10.0.0.1\n"),
    # kv
    ("kv", 'ts=2026-09-22T09:14:02Z level=info msg="hello world" user=ali'),
    ("kv", 'level=info msg="hello world'),
    ("kv", 'a=" b=2'),
    ("kv", 'type=LOGIN, realmId=master; clientId=web,'),
    ("kv", "type=LOGIN clientId= client_id=abc"),
    ("kv", "type=LOGIN clientId= x=1"),
    ("kv", "type=LOGIN client_id= x=1"),
    ("kv", "type=LOGIN clientId=c msg=already"),
    ("kv", "only=1"),
    ("kv", "2026-09-22 09:14:02 only=1"),
    ("kv", "[2026-09-22 09:14:02] WARN auth-svc: only=1"),
    ("kv", "2026-09-22 09:14:02 İNFO kv: a=1 b=2"),
    ("kv", "1790068560 level=x timestamp=override severity=s2 a=1"),
    ("kv", 'a=1 a=2 b="x\\"y\\\\z" c=é;,'),
    ("kv", 'date=2026-09-22 time=09:14:05 devname="FG100" msg="a=b"'),
    ("kv", "\n\nk=v l=w\n"),
    # cef / leef
    ("cef", "CEF:0|Vendor|Product|1.0|100|Name|5|xk=v abc=foo=bar baz=1 cs1=Web Application Attack end="),
    ("cef", "CEF:0|V|P|1|1|N|9|a=1 baz=1 tail\\"),
    ("cef", "CEF:0|V|P|1|1|N|High|msg=a\\=b\\nc rt=Sep 22 2026 09:14:02 dvchost=h1 host=h2 message=m vendor=x"),
    ("cef", "CEF:0|V|  |1|1|N| 7 |suser=CORP\\\\ayse dvc=10.0.0.1"),
    ("cef", "CEF:0|V\\|X|P\\\\|1|1|N|3|"),
    ("cef", "CEF:0|V|P|1|1|N|  |start=s end= devTime=d"),
    ("cef", "<13>Sep 22 09:14:02 host1 CEF:0|V|P|1|1|N|4|a=1"),
    ("cef", "2026-09-22T09:14:02Z fw CEF:0|V|P|1|1|N|4|dvchost=x"),
    ("cef", "junk prefix CEF:0|V|P|1|1|N|4|a=1"),
    ("cef", "CEF:0|V|P|1|1|N|²|a=1"),
    ("cef", "CEF:0|a|b"),
    ("cef", "LEEF:1.0|V|P|1|E|cat=a|b\tsev=x"),
    ("cef", "LEEF:2.0|V|P|1|E|x09|a=1x09sev=8x09identHostName=h"),
    ("cef", "LEEF:2.0|V|P|1|E|^|devTime=Sep 22 2026 09:14:02^sev=3^msg=hi^ k = v ^k=w"),
    ("cef", "LEEF:1.0|V|P|1|E||a=1\tsev=3"),
    ("cef", "LEEF:1.0|V|P|1|E|a=1\tsev=10\tdevname=d"),
    ("cef", "LEEF:1.0|V|P|1|E| a|=1"),
    ("cef", "LEEF:1.0|V|P|1|E|x"),
    ("cef", "LEEF:1.0|V|P|1|E|é|=1"),
    ("cef", "LEEF:1.0|V|P|1|E|sev=³"),
    ("cef", "LEEF:1.0|a|b"),
    ("cef", "plain line\nCEF:0|V|P|1|1|N|1|x LEEF:1.0|"),
]


def run(name, text):
    try:
        return json.dumps([[i, r] for i, r in PARSERS[name].records(text)], ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return "!" + type(e).__name__


o = ["// Code generated by receiver/parse/testdata/wp4gen/gen_cases.py from the Python parsers (syslog_parser.py,",
     "// access_parser.py, w3c_parser.py, kv_parser.py, cef_parser.py). Expected values are Python 3.11 outputs.",
     "// DO NOT EDIT by hand. Regenerate:",
     "//   PYTHONPATH=src python3 receiver/parse/testdata/wp4gen/gen_cases.py > receiver/parse/wp4_cases_test.go",
     "", "package parse", "",
     "// wp4Cases: want = json.dumps([[line_no, rec], ...]) or \"!<Python exception>\".",
     "var wp4Cases = []struct{ parser, text, want string }{"]
for name, text in CASES:
    o.append(f"\t{{{gq(name)}, {gq(text)}, {gq(run(name, text))}}},")
o.append("}")
sys.stdout.write("\n".join(o) + "\n")
