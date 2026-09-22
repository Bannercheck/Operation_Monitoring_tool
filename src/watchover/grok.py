"""Grok: Logstash-style pattern language on top of Python regular expressions.

    %{IP:client} %{WORD:method} %{URIPATH:path}   ->   (?P<client>…)(?P<method>…)(?P<path>…)

`BASE` holds the building blocks (numbers, hosts, dates, syslog pieces), `LINES` the ready line patterns for whole log
families with the role of each captured field (timestamp / severity / host / service / message). Users add their own
patterns from the Parser page; they are stored in <home>/grok.json and take part in format detection like the built-in
ones. Only the standard library is used, so patterns are safe to ship inside the deterministic core."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ base patterns (a faithful subset of the Logstash core set)
BASE: dict[str, str] = {
    "USERNAME": r"[a-zA-Z0-9._-]+", "USER": r"%{USERNAME}", "EMAILLOCALPART": r"[a-zA-Z][a-zA-Z0-9_.+-=:]+", "EMAILADDRESS": r"%{EMAILLOCALPART}@%{HOSTNAME}",
    "INT": r"(?:[+-]?(?:[0-9]+))", "BASE10NUM": r"(?<![0-9.+-])(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+)))", "NUMBER": r"(?:%{BASE10NUM})",
    "BASE16NUM": r"(?<![0-9A-Fa-f])(?:[+-]?(?:0x)?(?:[0-9A-Fa-f]+))", "POSINT": r"\b(?:[1-9][0-9]*)\b", "NONNEGINT": r"\b(?:[0-9]+)\b",
    "WORD": r"\b\w+\b", "NOTSPACE": r"\S+", "SPACE": r"\s*", "DATA": r".*?", "GREEDYDATA": r".*",
    "QUOTEDSTRING": r"(?:\"(?:\\.|[^\\\"])*\"|'(?:\\.|[^\\'])*'|`(?:\\.|[^\\`])*`)", "QS": r"%{QUOTEDSTRING}",
    "UUID": r"[A-Fa-f0-9]{8}-(?:[A-Fa-f0-9]{4}-){3}[A-Fa-f0-9]{12}",
    "MAC": r"(?:%{CISCOMAC}|%{WINDOWSMAC}|%{COMMONMAC})", "CISCOMAC": r"(?:[A-Fa-f0-9]{4}\.){2}[A-Fa-f0-9]{4}", "WINDOWSMAC": r"(?:[A-Fa-f0-9]{2}-){5}[A-Fa-f0-9]{2}", "COMMONMAC": r"(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}",
    "IPV6": r"(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)|::(?:ffff(?::0{1,4})?:)?(?:(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9])\.){3}(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9]))",
    "IPV4": r"(?<![0-9])(?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5]))(?![0-9])",
    "IP": r"(?:%{IPV6}|%{IPV4})", "HOSTNAME": r"\b(?:[0-9A-Za-z][0-9A-Za-z-]{0,62})(?:\.(?:[0-9A-Za-z][0-9A-Za-z-]{0,62}))*(?:\.?|\b)", "IPORHOST": r"(?:%{IP}|%{HOSTNAME})",
    "HOSTPORT": r"%{IPORHOST}:%{POSINT}", "PATH": r"(?:%{UNIXPATH}|%{WINPATH})", "UNIXPATH": r"(/[\w_%!$@:.,+~-]*)+", "WINPATH": r"(?:[A-Za-z]:|\\)(?:\\[^\\?*]*)+", "TTY": r"(?:/dev/(pts|tty(?:[pq])?)(?:\w+)?/?(?:[0-9]+))",
    "URIPROTO": r"[A-Za-z]([A-Za-z0-9+\-.]+)+", "URIHOST": r"%{IPORHOST}(?::%{POSINT:port})?", "URIPATH": r"(?:/[A-Za-z0-9$.+!*'(){},~:;=@#%&_\-]*)+", "URIPARAM": r"\?[A-Za-z0-9$.+!*'|(){},~@#%&/=:;_?\-\[\]<>]*",
    "URIPATHPARAM": r"%{URIPATH}(?:%{URIPARAM})?", "URI": r"%{URIPROTO}://(?:%{USER}(?::[^@]*)?@)?(?:%{URIHOST})?(?:%{URIPATHPARAM})?",
    "MONTH": r"\b(?:[Jj]an(?:uary|uar)?|[Ff]eb(?:ruary|ruar)?|[Mm](?:a|ä)?r(?:ch|z)?|[Aa]pr(?:il)?|[Mm]a(?:y|i)?|[Jj]un(?:e|i)?|[Jj]ul(?:y|i)?|[Aa]ug(?:ust)?|[Ss]ep(?:tember)?|[Oo](?:c|k)?t(?:ober)?|[Nn]ov(?:ember)?|[Dd]e(?:c|z)(?:ember)?)\b",
    "MONTHNUM": r"(?:0?[1-9]|1[0-2])", "MONTHNUM2": r"(?:0[1-9]|1[0-2])", "MONTHDAY": r"(?:(?:0[1-9])|(?:[12][0-9])|(?:3[01])|[1-9])",
    "DAY": r"(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)", "YEAR": r"(?>\d\d){1,2}", "HOUR": r"(?:2[0123]|[01]?[0-9])", "MINUTE": r"(?:[0-5][0-9])",
    "SECOND": r"(?:(?:[0-5]?[0-9]|60)(?:[:.,][0-9]+)?)", "TIME": r"(?!<[0-9])%{HOUR}:%{MINUTE}(?::%{SECOND})(?![0-9])",
    "DATE_US": r"%{MONTHNUM}[/-]%{MONTHDAY}[/-]%{YEAR}", "DATE_EU": r"%{MONTHDAY}[./-]%{MONTHNUM}[./-]%{YEAR}", "ISO8601_TIMEZONE": r"(?:Z|[+-]%{HOUR}(?::?%{MINUTE}))", "ISO8601_SECOND": r"(?:%{SECOND}|60)",
    "TIMESTAMP_ISO8601": r"%{YEAR}-%{MONTHNUM}-%{MONTHDAY}[T ]%{HOUR}:?%{MINUTE}(?::?%{SECOND})?%{ISO8601_TIMEZONE}?", "DATE": r"%{DATE_US}|%{DATE_EU}", "DATESTAMP": r"%{DATE}[- ]%{TIME}", "TZ": r"(?:[APMCE][SD]T|UTC|GMT|TRT)",
    "DATESTAMP_RFC822": r"%{DAY} %{MONTH} %{MONTHDAY} %{YEAR} %{TIME} %{TZ}", "DATESTAMP_RFC2822": r"%{DAY}, %{MONTHDAY} %{MONTH} %{YEAR} %{TIME} %{ISO8601_TIMEZONE}", "DATESTAMP_OTHER": r"%{DAY} %{MONTH} %{MONTHDAY} %{TIME} %{TZ} %{YEAR}",
    "DATESTAMP_EVENTLOG": r"%{YEAR}%{MONTHNUM2}%{MONTHDAY}%{HOUR}%{MINUTE}%{SECOND}", "HTTPDERROR_DATE": r"%{DAY} %{MONTH} %{MONTHDAY} %{TIME} %{YEAR}",
    "SYSLOGTIMESTAMP": r"%{MONTH} +%{MONTHDAY} %{TIME}", "PROG": r"[\x21-\x5a\x5c\x5e-\x7e]+", "SYSLOGPROG": r"%{PROG:program}(?:\[%{POSINT:pid}\])?", "SYSLOGHOST": r"%{IPORHOST}", "SYSLOGFACILITY": r"<%{NONNEGINT:facility}.%{NONNEGINT:priority}>",
    "HTTPDATE": r"%{MONTHDAY}/%{MONTH}/%{YEAR}:%{TIME} %{INT}", "LOGLEVEL": r"(?:[Aa]lert|ALERT|[Tt]race|TRACE|[Dd]ebug|DEBUG|[Nn]otice|NOTICE|[Ii]nfo?(?:rmation)?|INFO?(?:RMATION)?|[Ww]arn?(?:ing)?|WARN?(?:ING)?|[Ee]rr?(?:or)?|ERR?(?:OR)?|[Cc]rit?(?:ical)?|CRIT?(?:ICAL)?|[Ff]atal|FATAL|[Ss]evere|SEVERE|EMERG(?:ENCY)?|[Ee]merg(?:ency)?)",
    "JAVACLASS": r"(?:[a-zA-Z$_][a-zA-Z$_0-9]*\.)*[a-zA-Z$_][a-zA-Z$_0-9]*", "JAVAFILE": r"(?:[a-zA-Z$_0-9. -]+)", "JAVAMETHOD": r"(?:(<(?:cl)?init>)|[a-zA-Z$_][a-zA-Z$_0-9]*)",
    "JAVASTACKTRACEPART": r"%{SPACE}at %{JAVACLASS:class}\.%{JAVAMETHOD:method}\(%{JAVAFILE:file}(?::%{NUMBER:line})?\)", "JAVALOGMESSAGE": r"(?:.*)",
    "CATALINA_DATESTAMP": r"(?:%{MONTH} %{MONTHDAY}, %{YEAR} %{HOUR}:%{MINUTE}:%{SECOND} (?:AM|PM))", "TOMCAT_DATESTAMP": r"%{YEAR}-%{MONTHNUM}-%{MONTHDAY} %{HOUR}:%{MINUTE}:%{SECOND}(?:,%{INT})? %{ISO8601_TIMEZONE}?",
    "SQUID_STATUS": r"(?:TCP|UDP|NONE)_[A-Z_]+", "NAGIOSTIME": r"\[%{NUMBER:nagios_epoch}\]",
}

# ------------------------------------------------------------------ line packs: whole-line patterns with the role of each field
@dataclass
class LinePattern:
    name: str
    pattern: str
    family: str = ""                         # default service when the pattern has no service field
    roles: dict = field(default_factory=dict)   # role -> captured field: timestamp / severity / host / service / message
    description: str = ""
    builtin: bool = True
    enabled: bool = True
    id: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id or self.name, "name": self.name, "pattern": self.pattern, "family": self.family, "roles": self.roles, "description": self.description,
                "builtin": self.builtin, "enabled": self.enabled}


LINES: list[LinePattern] = [
    LinePattern("SYSLOGLINE", r"^%{SYSLOGTIMESTAMP:timestamp} (?:%{SYSLOGFACILITY} )?%{SYSLOGHOST:host} %{SYSLOGPROG}: %{GREEDYDATA:message}$", "syslog",
                {"timestamp": "timestamp", "host": "host", "service": "program", "message": "message"}, "BSD syslog line"),
    LinePattern("COMMONAPACHELOG", r'^%{IPORHOST:clientip} %{USER:ident} %{USER:auth} \[%{HTTPDATE:timestamp}\] "(?:%{WORD:verb} %{NOTSPACE:request}(?: HTTP/%{NUMBER:httpversion})?|%{DATA:rawrequest})" %{NUMBER:response} (?:%{NUMBER:bytes}|-)', "http",
                {"timestamp": "timestamp", "message": "request", "status": "response"}, "Apache / nginx common log"),
    LinePattern("COMBINEDAPACHELOG", r'^%{IPORHOST:clientip} %{USER:ident} %{USER:auth} \[%{HTTPDATE:timestamp}\] "(?:%{WORD:verb} %{NOTSPACE:request}(?: HTTP/%{NUMBER:httpversion})?|%{DATA:rawrequest})" %{NUMBER:response} (?:%{NUMBER:bytes}|-) %{QS:referrer} %{QS:agent}', "http",
                {"timestamp": "timestamp", "message": "request", "status": "response"}, "Apache / nginx combined log"),
    LinePattern("HTTPD_ERRORLOG", r"^\[%{HTTPDERROR_DATE:timestamp}\] \[(?:%{WORD:module}:)?%{LOGLEVEL:loglevel}\] \[pid %{POSINT:pid}(?::tid %{NUMBER:tid})?\](?: \[client %{IPORHOST:clientip}(?::%{POSINT:clientport})?\])? %{GREEDYDATA:message}$", "apache",
                {"timestamp": "timestamp", "severity": "loglevel", "message": "message"}, "Apache httpd error log (2.4)"),
    LinePattern("TOMCAT_CATALINA", r"^%{CATALINA_DATESTAMP:timestamp} %{JAVACLASS:class} %{JAVAMETHOD:method}\s*$", "tomcat",
                {"timestamp": "timestamp", "service": "class"}, "Tomcat catalina.out header line"),
    LinePattern("TOMCAT_JULI", r"^%{TOMCAT_DATESTAMP:timestamp} %{LOGLEVEL:level} \[%{DATA:thread}\] %{JAVACLASS:class}\.%{JAVAMETHOD:method} %{GREEDYDATA:message}$", "tomcat",
                {"timestamp": "timestamp", "severity": "level", "service": "class", "message": "message"}, "Tomcat 8.5+ JULI one-line format"),
    LinePattern("SQUID_ACCESS", r"^%{NUMBER:timestamp}\s+%{NUMBER:duration}\s+%{IP:clientip}\s+%{SQUID_STATUS:squid_status}/%{NUMBER:response}\s+%{NUMBER:bytes}\s+%{WORD:verb}\s+%{NOTSPACE:request}\s+%{NOTSPACE:user}\s+%{NOTSPACE:hierarchy}\s+%{NOTSPACE:content_type}", "squid",
                {"timestamp": "timestamp", "message": "request", "status": "response"}, "Squid native access log"),
    LinePattern("BIND9_QUERY", r"^%{MONTHDAY:day}-%{MONTH:month}-%{YEAR:year} %{TIME:time}(?:\.%{INT:ms})? (?:queries: )?(?:%{LOGLEVEL:level}: )?client(?: @0x[0-9a-f]+)? %{IP:clientip}#%{POSINT:clientport}(?: \(%{DATA:qname}\))?: query: %{NOTSPACE:query} %{WORD:qclass} %{WORD:qtype} %{GREEDYDATA:flags}$", "bind",
                {"severity": "level", "message": "query"}, "BIND 9 query log"),
    LinePattern("IPTABLES", r"^(?:%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} kernel: )?(?:\[\s*%{NUMBER:uptime}\] )?\[?%{DATA:prefix}\]?\s*IN=%{DATA:in_if} OUT=%{DATA:out_if} (?:MAC=%{DATA:mac} )?SRC=%{IP:src} DST=%{IP:dst} %{GREEDYDATA:rest}$", "iptables",
                {"timestamp": "timestamp", "host": "host", "message": "prefix"}, "iptables / ufw / nftables kernel log"),
    LinePattern("NAGIOS_LOG", r"^%{NAGIOSTIME} (?:%{WORD:nagios_type}: )?%{GREEDYDATA:message}$", "nagios", {"timestamp": "nagios_epoch", "message": "message"}, "Nagios / Icinga nagios.log"),
    LinePattern("SSHD_AUTH", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} sshd\[%{POSINT:pid}\]: (?:%{WORD:result}) (?:password|publickey) for (?:invalid user )?%{USERNAME:user} from %{IP:src} port %{POSINT:port}(?: ssh2)?", "sshd",
                {"timestamp": "timestamp", "host": "host", "message": "result"}, "sshd authentication result"),
    LinePattern("SUDO", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} sudo(?:\[%{POSINT:pid}\])?:\s+%{DATA:user} : (?:%{DATA:error} ; )?TTY=%{TTY:tty} ; PWD=%{DATA:pwd} ; USER=%{USERNAME:runas} ; COMMAND=%{GREEDYDATA:command}$", "sudo",
                {"timestamp": "timestamp", "host": "host", "message": "command"}, "sudo command record"),
    LinePattern("CRON", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} (?:CRON|crond)\[%{POSINT:pid}\]: \(%{USERNAME:user}\) CMD \(%{GREEDYDATA:command}\)$", "cron", {"timestamp": "timestamp", "host": "host", "message": "command"}, "cron job start"),
    LinePattern("DHCPD", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} dhcpd(?:\[%{POSINT:pid}\])?: DHCP(?:%{WORD:dhcp_op}) (?:on|for|from) %{IP:ip}(?: \(%{DATA:client}\))? (?:from|to|via) %{GREEDYDATA:rest}$", "dhcpd", {"timestamp": "timestamp", "host": "host", "message": "dhcp_op"}, "ISC DHCP lease traffic"),
    LinePattern("VMWARE_ESXI", r"^%{TIMESTAMP_ISO8601:timestamp} (?:%{LOGLEVEL:level}\s+)?%{NOTSPACE:program}\[%{DATA:context}\](?: \[%{DATA:opid}\])?:? %{GREEDYDATA:message}$", "esxi",
                {"timestamp": "timestamp", "severity": "level", "service": "program", "message": "message"}, "VMware ESXi hostd / vmkernel"),
    LinePattern("RABBITMQ", r"^%{TIMESTAMP_ISO8601:timestamp} \[%{LOGLEVEL:level}\] <%{DATA:pid}> %{GREEDYDATA:message}$", "rabbitmq", {"timestamp": "timestamp", "severity": "level", "message": "message"}, "RabbitMQ 3.7+ log"),
    LinePattern("DOCKER_DAEMON", r'^time="%{TIMESTAMP_ISO8601:timestamp}" level=%{LOGLEVEL:level} msg="%{DATA:message}"(?: %{GREEDYDATA:rest})?$', "dockerd", {"timestamp": "timestamp", "severity": "level", "message": "message"}, "Docker daemon (logrus) line"),
    LinePattern("EXIM_MAIN", r"^%{YEAR}-%{MONTHNUM}-%{MONTHDAY} %{TIME} (?:\[%{POSINT:pid}\] )?(?:%{NOTSPACE:msgid} )?(?:<=|=>|->|\*\*|==) %{GREEDYDATA:message}$", "exim", {"message": "message"}, "Exim mainlog"),
    LinePattern("DOVECOT", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} dovecot: %{WORD:proc}(?:\(%{DATA:user}\))?(?:<%{DATA:session}>)?: %{GREEDYDATA:message}$", "dovecot", {"timestamp": "timestamp", "host": "host", "message": "message"}, "Dovecot IMAP / POP"),
    LinePattern("F5_BIGIP", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} %{WORD:level} %{NOTSPACE:program}\[%{POSINT:pid}\]: %{NOTSPACE:msgid}: %{GREEDYDATA:message}$", "bigip",
                {"timestamp": "timestamp", "host": "host", "severity": "level", "service": "program", "message": "message"}, "F5 BIG-IP ltm log"),
    LinePattern("JUNIPER_JUNOS", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} %{NOTSPACE:program}(?:\[%{POSINT:pid}\])?: %{WORD:msgid}: %{GREEDYDATA:message}$", "junos",
                {"timestamp": "timestamp", "host": "host", "service": "program", "message": "message"}, "Juniper Junos syslog"),
    LinePattern("CHECKPOINT_LEA", r"^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} (?:CP-GW|CheckPoint) - (?:%{DATA:product}) - (?:%{GREEDYDATA:message})$", "checkpoint", {"timestamp": "timestamp", "host": "host", "message": "message"}, "Check Point gateway syslog"),
    LinePattern("ZEEK_CONN", r"^%{NUMBER:timestamp}\t%{NOTSPACE:uid}\t%{IP:src}\t%{POSINT:src_port}\t%{IP:dst}\t%{POSINT:dst_port}\t%{WORD:proto}\t%{GREEDYDATA:rest}$", "zeek", {"timestamp": "timestamp", "message": "proto"}, "Zeek / Bro conn.log (TSV)"),
    LinePattern("WINDOWS_TEXT_EVENT", r"^%{TIMESTAMP_ISO8601:timestamp}\s+(?:%{LOGLEVEL:level})\s+%{POSINT:event_id}\s+%{DATA:source}\s+%{GREEDYDATA:message}$", "windows", {"timestamp": "timestamp", "severity": "level", "service": "source", "message": "message"}, "Windows event exported as text"),
    LinePattern("RUBY_LOGGER", r"^%{WORD:level_letter}, \[%{TIMESTAMP_ISO8601:timestamp} #%{POSINT:pid}\] +%{LOGLEVEL:level} -- (?:%{DATA:progname})?: %{GREEDYDATA:message}$", "ruby", {"timestamp": "timestamp", "severity": "level", "service": "progname", "message": "message"}, "Ruby / Rails Logger"),
    LinePattern("NODE_WINSTON", r"^%{TIMESTAMP_ISO8601:timestamp} \[%{LOGLEVEL:level}\](?: \[%{DATA:label}\])?:? %{GREEDYDATA:message}$", "node", {"timestamp": "timestamp", "severity": "level", "service": "label", "message": "message"}, "Node.js winston / pino pretty"),
    LinePattern("SAP_CCMS_ALERT", r"^%{TIMESTAMP_ISO8601:timestamp}\s+%{LOGLEVEL:level}\s+(?P<mte>\\\\?[\w.-]+\\[^\s]+)\s+%{GREEDYDATA:message}$", "sap-ccms", {"timestamp": "timestamp", "severity": "level", "service": "mte", "message": "message"}, "SAP CCMS alert export (MTE name with backslashes)"),
]

_SUB = re.compile(r"%\{(\w+)(?::(\w+))?(?::(\w+))?\}")
_cache: dict[str, re.Pattern] = {}


def expand(pattern: str, extra: dict[str, str] | None = None, depth: int = 0) -> str:
    """Replace %{NAME} / %{NAME:field} / %{NAME:field:type} with named regex groups; nested references expand recursively."""
    if depth > 20:
        raise ValueError("pattern nests too deep")
    lib = {**BASE, **(extra or {})}

    def repl(m: re.Match) -> str:
        name, fld = m.group(1), m.group(2)
        if name not in lib:
            raise ValueError(f"unknown pattern %{{{name}}}")
        inner = expand(lib[name], extra, depth + 1)
        return f"(?P<{fld}>{inner})" if fld else f"(?:{inner})"
    out = _SUB.sub(repl, pattern)
    return out.replace("(?>", "(?:")     # Oniguruma atomic groups: plain groups here


def compile_pattern(pattern: str, extra: dict[str, str] | None = None) -> re.Pattern:
    key = pattern + "\x00" + json.dumps(extra or {}, sort_keys=True)
    rx = _cache.get(key)
    if rx is None:
        rx = re.compile(expand(pattern, extra))
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = rx
    return rx


def match(pattern: str, line: str, extra: dict[str, str] | None = None) -> dict | None:
    m = compile_pattern(pattern, extra).search(line)
    return {k: v for k, v in m.groupdict().items() if v is not None} if m else None


def test_pattern(pattern: str, sample: str, extra: dict[str, str] | None = None) -> dict:
    """The Parser page's test box: per line, the captured fields or None; plus an error when the pattern does not compile."""
    try:
        rx = compile_pattern(pattern, extra)
    except (re.error, ValueError) as e:
        return {"ok": False, "error": str(e), "lines": []}
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    rows = []
    for ln in lines:
        m = rx.search(ln)
        rows.append({"line": ln, "fields": {k: v for k, v in m.groupdict().items() if v is not None} if m else None})
    hit = sum(1 for r in rows if r["fields"] is not None)
    return {"ok": True, "matched": hit, "total": len(lines), "lines": rows, "fields": sorted({k for r in rows if r["fields"] for k in r["fields"]})}


# ------------------------------------------------------------------ user patterns: <home>/grok.json (shared by every worker process)
def _store_path() -> Path:
    from . import settings
    return settings.home() / "grok.json"


_user_cache: tuple[float, list[LinePattern]] = (0.0, [])


def user_patterns() -> list[LinePattern]:
    p = _store_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    global _user_cache
    if _user_cache[0] == mtime:
        return _user_cache[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = []
    out = [LinePattern(d["name"], d["pattern"], d.get("family", ""), d.get("roles", {}), d.get("description", ""), False, d.get("enabled", True), d.get("id", d["name"])) for d in raw if d.get("pattern")]
    _user_cache = (mtime, out)
    return out


def save_user_patterns(items: list[LinePattern]) -> None:
    _store_path().write_text(json.dumps([{**x.as_dict(), "builtin": False} for x in items], ensure_ascii=False, indent=1), encoding="utf-8")


def all_patterns() -> list[LinePattern]:
    """User patterns first (they win on ties), then the built-in packs."""
    return [x for x in user_patterns() if x.enabled] + [x for x in LINES if x.enabled]


def detect(lines: list[str], min_share: float = 0.6) -> tuple[LinePattern | None, float]:
    """The line pattern that matches the largest share of the sample (at least min_share); the more specific pattern wins ties."""
    sample = [ln for ln in lines[:120] if ln.strip()]
    if not sample:
        return None, 0.0
    best, best_share = None, 0.0
    for lp in all_patterns():
        try:
            rx = compile_pattern(lp.pattern)
        except (re.error, ValueError):
            continue
        hits = sum(1 for ln in sample if rx.search(ln))
        share = hits / len(sample)
        if share > best_share:
            best, best_share = lp, share
    return (best, round(best_share, 2)) if best and best_share >= min_share else (None, 0.0)


def coverage_of(lp: LinePattern, corpus_dir: Path) -> dict:
    """How many corpus files / lines a pattern matches (shown next to each pattern on the Parser page)."""
    files = 0; lines_hit = 0
    try:
        rx = compile_pattern(lp.pattern)
    except (re.error, ValueError):
        return {"files": 0, "lines": 0}
    for p in sorted(corpus_dir.glob("*")):
        if p.suffix not in (".log", ".jsonl", ".json", ".csv") or p.name == "baseline.json":
            continue
        hits = sum(1 for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and rx.search(ln))
        if hits:
            files += 1; lines_hit += hits
    return {"files": files, "lines": lines_hit}


def now() -> float:
    return time.time()
