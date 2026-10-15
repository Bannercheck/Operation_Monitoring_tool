"""Log explorer (docs/design/logs.md): query language, stored lines v2, keyset paging exactly once, counters vs brute force, live
tail, detail / context, group similar, saved searches, alerts from a query, team scope on every endpoint, and a 100k-line smoke."""
import json
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from watchover import logquery as lq
from watchover import logsearch as ls_mod
from watchover import logstore
from watchover.db import Database
from watchover.live import LiveStore
from watchover.logsearch import LogSearch
from watchover.models import Observation

UTC = timezone.utc
PW = "Str0ng!Passw0rd"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "logs-test")
    import watchover.privacy as pv
    monkeypatch.setattr(pv, "_KEY", None)
    logstore._SCRUB.clear()


def ob(ts, host="prd-ebs-01", sev="INFO", svc="nav", msg="ok", src="nav.log", env="prod", ln=0, raw="", **attrs):
    return Observation(timestamp=ts, message=msg, severity=sev, service=svc, host=host, environment=env, source=src, line_no=ln, raw=raw, attributes=dict(attrs))


def make(tmp_path, maxlen=50_000, db=True):
    st = LiveStore(spool=None, maxlen=maxlen)
    if db:
        st.attach_db(Database(str(tmp_path / "k.db")))
    return st, LogSearch(st, kb=st.db)


def truth(st, s, e, pred=None):
    """Every stored line in [s, e) as (ts µs, id), newest first: the ground truth read straight from live_batches."""
    out = []
    for r in st.db._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches"):
        for i, d in enumerate(logstore.row_dicts(r)):
            t = ls_mod._us(d["ts"])
            if s.timestamp() * 1e6 <= t < e.timestamp() * 1e6 and (pred is None or pred(d)):
                out.append((t, logstore.line_id(d, r["id"], i)))
    return sorted(out, reverse=True)


def pages(ls, q, s, e, limit, **kw):
    ids, cur, n = [], None, 0
    while True:
        r = ls.search(q, start=s.isoformat(), end=e.isoformat(), limit=limit, cursor=cur, **kw)
        ids += [int(x["id"]) for x in r["lines"]]
        cur = r["next"]
        n += 1
        assert n < 500
        if not cur:
            return ids, r


# ---------------------------------------------------------------- query language
def test_parser_precedence_aliases_and_values():
    p = lq.parse("a b OR c").node
    assert isinstance(p, lq.Or) and isinstance(p.items[0], lq.And) and len(p.items[0].items) == 2      # AND binds tighter than OR
    p = lq.parse("NOT a b").node
    assert isinstance(p, lq.And) and isinstance(p.items[0], lq.Not)                                      # NOT binds tightest
    p = lq.parse("-host:test-* prd-sap-01").node
    assert isinstance(p.items[0], lq.Not) and p.items[0].item.name == "host" and p.items[0].item.op == "glob"
    assert isinstance(p.items[1], lq.Text) and p.items[1].val.text == "prd-sap-01"                       # a dash inside a word is not NOT
    assert lq.normalize(lq.parse("level:err OR level:crit OR level:fatal OR level:emerg OR level:alert").node) == "level:(CRITICAL OR ERROR)"
    assert lq.normalize(lq.parse("lvl:hata svc:nav* VE NOT hostname:x VEYA env:prod").node) == "level:ERROR service:nav* -host:x OR env:prod"
    assert lq.normalize(lq.parse("level>=warn duration_ms:>500 @status<=499").node) == "level>=WARN duration_ms>500 @status<=499"
    assert lq.normalize(lq.parse('service:(a b) "connection lost" message:x raw:"y z"').node) == 'service:(a OR b) "connection lost" x raw:"y z"'
    assert lq.normalize(lq.parse("http://x/y a->b 08:45:00").node) == '"http://x/y" a->b "08:45:00"'           # not fields
    assert isinstance(lq.parse("*").node, lq.All) and isinstance(lq.parse("").node, lq.All)
    f = lq.parse("service:*").node
    assert f.op == "present"
    f = lq.parse(r"service:x\*y").node
    assert f.op == "eq" and f.values[0].text == "x*y"                                                    # escaped wildcard is literal
    assert lq.parse("foo or bar").warnings[0]["code"] == "lowercase_op"
    assert lq.parse("session:JSX-1").node.values[0].text.startswith("s_")                              # raw ids are pseudonymised
    assert lq.parse("@host:a").node.attr and not lq.parse("host:a").node.attr
    for q in ("level:(err OR crit) NOT service:(Error OR Critical)", 'level>=warn -host:test-* "connection lost"', "pool:prd-sap-app (timeout OR deadlock)",
              "@status>=500 source:access.log", "duration_ms>2000 service:http", "session:s_c4a37ea736be305e", "a (b OR -c) NOT (d e)"):
        n1 = lq.normalize(lq.parse(q).node)
        assert lq.normalize(lq.parse(n1).node) == n1, q                                                  # normalize round trip


@pytest.mark.parametrize("q,code,pos", [
    ('"unclosed', "unclosed_quote", 0), ("(a", "unclosed_paren", 0), ("a)", "stray_paren", 1), ("()", "empty_group", 0),
    ("service:", "missing_value", 0), ("service: x", "missing_value", 0), ("a OR", "dangling_op", 2), ("OR a", "leading_op", 0), ("(AND a)", "leading_op", 1),
    ("level:blah", "unknown_level", 0), ("duration_ms>abc", "bad_number", 12), ("service>5", "cmp_not_numeric", 0), ("line:x", "bad_number", 0),
    ("service:(a NOT b)", "group_not", 11), (r"x\q", "bad_escape", 1), ("a" * 2001, "too_long", 2000),
    (" ".join(f"t{i}" for i in range(65)), "too_complex", 246), ("(" * 17 + "a" + ")" * 17, "too_complex", 16),
    ("ayse@corp.com", "pii_term", 0), ("@iban:TR330006100519786457841326", "pii_term", 0), ("password=hunter22", "pii_term", 0), ('"0532 111 22 33"', "pii_term", 0),
])
def test_parser_errors_with_positions(q, code, pos):
    with pytest.raises(lq.QueryError) as ei:
        lq.parse(q)
    d = ei.value.as_dict()
    assert d["code"] == code and d["pos"] == pos and d["len"] >= 0 and d["tr"] and d["en"]


def test_plan_and_matching():
    pl = lq.plan(lq.parse("level>=warn service:nav* -host:x").node)
    assert pl.facet_only and pl.conj and pl.sev_mask == 4 | 8 | 16 and pl.top_and["service"]["pos"] == ["nav*"] and pl.top_and["host"]["neg"] == ["x"]
    assert lq.plan(lq.parse("-level:debug").node).sev_mask == 31 - 1
    pl = lq.plan(lq.parse('host:PRD-1 "connection lost" timeout').node)
    assert not pl.facet_only and pl.hosts == {"prd-1"} and pl.prefilter.search("SQL Connection Lost".encode()) and not pl.prefilter.search(b"lost connection")
    assert lq.plan(lq.parse("a OR b").node).prefilter is None and not lq.plan(lq.parse("a OR b").node).conj

    class L(logstore.LineView):
        __slots__ = ("a",)

        def __init__(self, msg, sev="INFO", svc="nav", host="h1", **a):
            self.msg, self.sev, self.svc, self.host, self.env, self.src, self.agent, self.kind, self.org, self.ln, self.seq, self.ts, self.ctx, self._c = \
                msg, sev, svc, host, "prod", "f.log", "ag", "log", "", 0, 1, 0.0, None, {}
            self.a = a

        def attrs(self):
            return self.a

        def ses(self):
            return None

    m = lambda q, line: lq.compile(lq.parse(q).node)(line)   # noqa: E731
    assert m("islem kapali", L("İşlem KAPALI kaldı")) and m('"islem kapali"', L("İŞLEM KAPALI")) and not m('"kapali islem"', L("İşlem kapalı"))
    assert m("time*out", L("a timed-out b")) and not m("time*out", L("time is out"))                   # a wildcard never crosses whitespace
    assert m("service:NAV*", L("x", svc="NavServer")) and not m("service:nav", L("x", svc="NavServer"))
    assert m("@status>=500", L("x", status=503)) and not m("@status>=500", L("x", status="abc")) and m("-@status:500", L("x"))
    assert m("duration_ms>100", L("took 250 ms")) and m("duration_ms<100", L("x", duration_ms=12)) and not m("duration_ms>1", L("no number"))
    assert m("level:(err OR crit)", L("x", sev="CRITICAL")) and not m("level>=error", L("x", sev="WARN"))
    assert m("a b OR c", L("only c")) and not m("a b OR c", L("only a")) and m("NOT a", L("b"))


def test_rewrite_keeps_user_text():
    rw = lq.rewrite
    assert rw("level:error  \"conn lost\"", "add", "service", "NavServer") == 'level:error "conn lost" service:NavServer'
    assert rw("level:error service:http", "add", "service", "NavServer") == "level:error service:(http OR NavServer)"
    assert rw("level:error service:(http OR nav)", "remove", "service", "nav") == "level:error service:http"
    assert rw("level:error service:(http OR nav)", "exclude", "service", "nav") == "level:error service:http -service:nav"
    assert rw("level:error -service:nav", "add", "service", "nav") == "level:error service:nav"
    assert rw("level:error service:(http OR nav) -service:x", "only", "service", "db") == "level:error service:db"
    assert rw("a OR b", "add", "host", "h 1") == '(a OR b) host:"h 1"'
    assert rw("level:err", "add", "level", "crit") == "level:(err OR crit)" and rw("", "exclude", "@status", "500") == "-@status:500"
    assert rw("x", "add", "service", "a*b") == r"x service:a\*b"                                          # a clicked value is literal


# ---------------------------------------------------------------- storage v2 and privacy
def test_stored_v2_privacy_and_v1_compatibility(tmp_path):
    st, ls = make(tmp_path)
    now = datetime.now(UTC) - timedelta(seconds=5)
    raw = '10.20.30.40 - ayse.yilmaz [x] "GET /pay?card=4111111111111111 HTTP/1.1" 500 12 token=abcdefghijklmnop'
    st._accept([ob(now, msg="payment failed for ayse@corp.com", sev="ERROR", raw=raw, ln=7, client="10.20.30.40", user="ayse.yilmaz",
                   session_id="JSX-1", password="hunter2", thread="w-7", status=500, traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")],
               "prd-ebs-01", "prod", "", None)
    row = st.db._exec("SELECT * FROM live_batches")[0]
    assert row["v"] == 2 and row["payload"] == "" and row["seq_min"] == row["seq_max"] == 1 and row["sevmask"] == 8 and row["host"] == "prd-ebs-01"
    d = next(logstore.row_dicts(row))
    text = json.dumps({k: v for k, v in d.items() if k != "msg"})
    for secret in ("10.20.30.40", "ayse.yilmaz", "JSX-1", "hunter2", "4111111111111111", "abcdefghijklmnop"):
        assert secret not in text, secret
    assert d["at"]["password"] == "***" and d["at"]["client"].startswith("c_") and d["at"]["user"].startswith("u_") and d["at"]["status"] == 500
    assert d["raw"].startswith(d["at"]["client"]) and d["tr"] == "4bf92f3577b34da6a3ce929d0e0e4736" and d["ln"] == 7
    det = ls.line(1)
    assert det["message"] == "payment failed for [E-POSTA]" and det["raw_state"] == "stored" and det["trace"]["id"] == d["tr"] and det["line_no"] == 7
    assert det["session"]["id"].startswith("s_") and det["source_of_detail"] == "ring"
    st.buf.clear()                                                                                         # gone from the ring: the stored copy answers the same
    det2 = ls.line(1)
    assert det2["source_of_detail"] == "stored" and {k: v for k, v in det2.items() if k not in ("source_of_detail", "received")} == \
        {k: v for k, v in det.items() if k not in ("source_of_detail", "received")}
    # an old v1 row (before the upgrade): found by search with a synthetic id, detail says the raw line is not kept
    v1 = json.dumps({"ts": now.isoformat(), "sev": "WARN", "svc": "old", "host": "prd-old-01", "env": "prod", "msg": "legacy line", "src": "x.log", "agent": "a", "kind": "log"})
    rid = st.db._insert("INSERT INTO live_batches (ts_min, ts_max, received_at, agent, env, n, payload) VALUES (?,?,?,?,?,?,?)",
                        (logstore.iso_s(now), logstore.iso_s(now), logstore.iso_s(now), "a", "prod", 1, v1))
    ls._v1 = (0.0, True)
    r = ls.search("service:old", last=5)
    assert [x["message"] for x in r["lines"]] == ["legacy line"] and int(r["lines"][0]["id"]) == -(rid * logstore.V1_ID_BASE)
    d1 = ls.line(int(r["lines"][0]["id"]))
    assert d1["raw_state"] == "absent" and d1["service"] == "old"
    assert st.events_between(now - timedelta(minutes=1), now + timedelta(minutes=1))[0].__len__() == 2      # old readers see both formats


def test_pii_precheck_never_misses_what_scrub_masks():
    """logstore.maybe_pii skips privacy.scrub on the write path; it must flag every text scrub would change (ASCII, Turkish letters,
    Arabic-Indic / full-width digits, Unicode spaces, the letters `re` case-folds: İ ı ſ and the Kelvin sign)."""
    import watchover.privacy as pv
    rng = random.Random(42)
    udig = "٠١٢٣٤٥٦٧٨٩０１２３４５６７８９"
    noise = "abcxyz ABCXYZ :;,./=\"'{}[]_-()+\t" + "0123456789" * 3 + "@İıŞşçğüöſKµ\xa0 \x1c" + udig
    kws = ["password", "PASSWD", "pwd", "Parola", "şifre", "ŞİFRE", "sıfre", "paſſword", "toKen", "secret", "api_key", "Api-Key", "APİKEY",
           "authorization", "AUTHORİZATİON", "Bearer"]

    def tckn():
        d = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(8)]
        d.append((sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10)
        d.append(sum(d) % 10)
        return "".join(map(str, d))

    def card():
        d = [rng.randint(0, 9) for _ in range(15)]
        return next(s for s in ("".join(map(str, d + [c])) for c in range(10)) if pv._luhn_ok(s))

    def piece():
        k = rng.randrange(9)
        if k == 0:
            s = tckn()
        elif k == 1:
            s = " ".join((lambda c: [c[i:i + 4] for i in range(0, 16, 4)])(card()))
        elif k == 2:
            c = "5" + "".join(rng.choice("0123456789") for _ in range(9))
            s = rng.choice([c, "0" + c, "+90 " + c, f"(0{c[:3]}) {c[3:6]} {c[6:8]} {c[8:]}", f"{c[:3]}\xa0{c[3:6]}\xa0{c[6:8]}\xa0{c[8:]}"])
        elif k == 3:
            s = "TR" + "".join(rng.choice("0123456789 ") for _ in range(rng.randint(20, 30)))
        elif k == 4:
            s = "ab.c@ex-1.com" if rng.random() < 0.5 else "x@y"
        elif k == 5:
            s = rng.choice(kws) + rng.choice(["=", ": ", '":"']) + rng.choice(["", "Bearer "]) + "s3cr3t"
        elif k == 6:
            s = "Bearer " + "abcXYZ019._-" * rng.randint(1, 3)
        else:
            s = "".join(rng.choice(noise) for _ in range(rng.randint(1, 30)))
        if rng.random() < 0.3:                                    # some digits written in other scripts
            s = "".join(udig[int(c) + 10 * rng.randint(0, 1)] if c.isdigit() and c.isascii() and rng.random() < 0.4 else c for c in s)
        return s

    changed = 0
    for i in range(30_000):
        s = "".join(piece() for _ in range(rng.randint(1, 3)))
        if i % 3 == 0:
            s = s.encode("ascii", "ignore").decode()
        out = pv.scrub(s)
        if out != s:
            changed += 1
            assert logstore.maybe_pii(s), s
        assert logstore.scrub_text(s) == out
    assert changed > 5_000
    for s in ("Kullanıcı girişi başarısız: sipariş 123456 iptal", "2026-10-15 08:44:59,995 ERROR [NavServer] SQL Server connection lost",
              'GET /api/v1/orders/42 HTTP/1.1" 500 512 "-" "Mozilla/5.0"'):
        assert not logstore.maybe_pii(s), s                       # ordinary lines skip the seven substitutions


def test_detail_of_a_post_spread_over_many_minutes(tmp_path):
    st, ls = make(tmp_path, maxlen=10)
    t = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=50)
    st._accept([ob(t + timedelta(minutes=i % 40, seconds=i // 40), msg=f"replay {i}") for i in range(120)], "a", "prod", "", None)
    st._accept([ob(t, msg="next post")], "a", "prod", "", None)
    assert len(st.db._exec("SELECT id FROM live_batches")) == 41                     # one row per minute of line time
    st.buf.clear()
    assert [ls.line(i)["message"] for i in range(1, 122)] == [f"replay {i}" for i in range(120)] + ["next post"]


# ---------------------------------------------------------------- paging: every line exactly once (D1), stable answers, D2, D3
def test_keyset_paging_exactly_once_with_ties_and_out_of_order_batches(tmp_path):
    st, ls = make(tmp_path)
    rng = random.Random(7)
    T = (datetime.now(UTC) - timedelta(minutes=3)).replace(microsecond=0)
    batches = []
    for b in range(40):                                                    # 2,000 lines stamped in ONE second (second-precision syslog)
        batches.append([ob(T, host=f"prd-{b % 7:02d}", sev=rng.choice(["INFO", "ERROR", "WARN"]), msg=f"tie {b}-{i}") for i in range(50)])
    for b in range(30):                                                    # and 600 lines around it, sub-second stamps
        batches.append([ob(T - timedelta(seconds=rng.randint(-2, 8), microseconds=rng.randint(0, 999_999)), host=f"prd-{b % 5:02d}",
                           sev=rng.choice(["INFO", "ERROR"]), msg=f"spread {b}-{i}") for i in range(20)])
    rng.shuffle(batches)                                                  # arrival order has nothing to do with the stamps
    for i, bt in enumerate(batches):
        st._accept(bt, f"agent-{i % 9}", "prod", "", None)
    s, e = T - timedelta(seconds=10), T + timedelta(seconds=5)
    want = [i for _t, i in truth(st, s, e)]
    assert len(want) == 2600
    got, last = pages(ls, "", s, e, 137)
    assert got == want and len(set(got)) == 2600 and last["next"] is None                                # newest first, ties by id, none lost
    want_err = [i for _t, i in truth(st, s, e, lambda d: d["sev"] == "ERROR")]
    assert pages(ls, "level:error", s, e, 100)[0] == want_err
    assert pages(ls, "tie", s, e, 333)[0] == [i for _t, i in truth(st, s, e, lambda d: "tie" in d["msg"])]
    first = ls.search("level:error", start=s.isoformat(), end=e.isoformat(), limit=50)
    assert first["total"] == {"value": len(want_err), "exact": True, "source": "scan"} and first["scan"]["complete"]
    answers = {json.dumps(ls._search(ls.query("level:error", None), s, e, False, None, 50, [], "h", None)["lines"]) for _ in range(12)}
    assert len(answers) == 1                                                                           # the same query, the same answer
    with pytest.raises(ls_mod.BadCursor):                                                              # a cursor from another query
        ls.search("level:warn", start=s.isoformat(), end=e.isoformat(), cursor=first["next"])


def test_stale_stamp_at_ring_head_does_not_empty_answers(tmp_path):
    st, ls = make(tmp_path, maxlen=500)
    now = datetime.now(UTC)
    st._accept([ob(now - timedelta(days=23), host="prd-ebs-03", msg="EBS stale stamp")], "prd-ebs-03", "prod", "", None)
    for i in range(10):
        st._accept([ob(now - timedelta(seconds=20 * j + i), msg=f"fresh {i}-{j}") for j in range(40)], "prd-ebs-01", "prod", "", None)
    assert st.buf[0].message == "EBS stale stamp" and not st._ring_covers(now - timedelta(minutes=15))    # by arrival, not by buf[0]'s stamp
    s = now - timedelta(minutes=15)
    assert len(st.events_between(s, now + timedelta(seconds=1))[0]) == 400
    assert {ls.search("fresh", last=15, limit=500)["total"]["value"] for _ in range(5)} == {400}
    sn = st.snapshot(since=s)
    assert len(sn) == 400


def test_budget_partial_scan_never_says_no_logs_and_quiet_host_found(tmp_path, monkeypatch):
    st, ls = make(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    for b in range(60):                                                     # 60 rows of 100 lines over 6 minutes, one quiet host in between
        t0 = now - timedelta(seconds=6 * (60 - b))
        st._accept([ob(t0 + timedelta(milliseconds=10 * i), host=f"prd-busy-{b % 10}", msg=f"line {b} {i}") for i in range(100)], f"busy-{b % 10}", "prod", "", None)
        if b % 20 == 5:
            st._accept([ob(t0, host="prd-batch-12", sev="ERROR", msg=f"quiet {b}")], "prd-batch-12", "prod", "", None)
    monkeypatch.setattr(ls_mod, "BUDGET_LINES", 60)
    s, e = now - timedelta(minutes=8), now + timedelta(seconds=1)
    r = ls.search("zzzz-not-there", start=s.isoformat(), end=e.isoformat())
    assert r["lines"] == [] and r["scan"]["complete"] is False and r["scan"]["stopped_by"] == "budget_lines" and r["next"] and r["total"].get("at_least")
    calls, cur, frontiers = 0, r["next"], [r["scan"]["frontier"]]
    while cur:                                                              # "scan further back" until the range start
        r = ls.search("zzzz-not-there", start=s.isoformat(), end=e.isoformat(), cursor=cur)
        cur = r["next"]; calls += 1; frontiers.append(r["scan"]["frontier"])
        assert calls < 100
    assert r["scan"]["complete"] and frontiers == sorted(frontiers, reverse=True) and calls > 3
    q = ls.search("host:prd-batch-12", start=s.isoformat(), end=e.isoformat())                            # rows found by the host index
    assert [x["message"] for x in q["lines"]] == ["quiet 45", "quiet 25", "quiet 5"] and q["scan"]["rows"] <= 10


# ---------------------------------------------------------------- counters equal brute force; histogram, facets, groups
def _cube_data(st, now):
    rng = random.Random(3)
    lines = []
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=16)
    for b in range(80):
        host = rng.choice(["prd-sap-app-01", "prd-sap-app-02", "prd-web-01", "test-web-01", "prd-db-01"])
        env = "test" if host.startswith("test") else "prod"
        bt = [ob(base + timedelta(seconds=rng.randint(0, 6 * 60 - 1), microseconds=rng.randint(0, 999_999)), host=host, env=env,
                 sev=rng.choice(["DEBUG", "INFO", "INFO", "WARN", "ERROR", "CRITICAL"]), svc=rng.choice(["nav", "http", "Error", "sap"]),
                 src=rng.choice(["a.log", "b.log"]), msg=f"order {rng.randint(1, 9)} failed after {rng.randint(1, 900)} ms") for _ in range(25)]
        st._accept(bt, host, env, "", None)
        lines += bt
    return lines, base


def test_counters_histogram_facets_equal_brute_force(tmp_path):
    st, ls = make(tmp_path)
    now = datetime.now(UTC)
    lines, base = _cube_data(st, now)
    st.rollup.started = 0                                                    # the counters predate the range
    s, e = base, base + timedelta(minutes=15)                                # 15 minutes and longer: one-minute bars (shorter: seconds)
    pool = st.rollup.pool_of
    for q, keep in (("", lambda o: True), ("level>=warn -service:Error", lambda o: lq.SEV_RANK[o.severity] >= 2 and o.service != "Error"),
                    ("pool:prd-sap-app OR env:test", lambda o: pool(o.host) == "prd-sap-app" or o.environment == "test"),
                    ("source:a.log level:(debug OR critical)", lambda o: o.source == "a.log" and o.severity in ("DEBUG", "CRITICAL"))):
        exp = [o for o in lines if keep(o)]
        # counters (whole minutes inside the kept hour) ...
        h = ls.histogram(q, start=s.isoformat(), end=e.isoformat())
        assert h["source"] == "counters" and h["exact"] and h["step_s"] == 60 and len(h["buckets"]) == 15
        for b in h["buckets"]:
            mine = [o for o in exp if b["t"] <= o.timestamp.timestamp() < b["t"] + 60]
            assert b["v"] == {k: sum(1 for o in mine if o.severity == k) for k in ls_mod.SEVS_DESC}, q
        f = ls.facets(q, start=s.isoformat(), end=e.isoformat(), limit=50)
        assert f["source"] == "counters" and f["exact"]
        r = ls.search(q, start=s.isoformat(), end=e.isoformat(), limit=10)
        assert r["total"] == {"value": len(exp), "exact": True, "source": "counters"}, q
        # ... and the same numbers from a scan (a range that is not whole minutes cannot use the counters)
        s2 = s + timedelta(microseconds=1)
        exp2 = [o for o in exp if o.timestamp >= s2]
        h2 = ls.histogram(q, start=s2.isoformat(), end=e.isoformat())
        assert h2["source"] == "stored" and h2["exact"] and sum(b["total"] for b in h2["buckets"]) == len(exp2)
        r2 = ls.search(q, start=s2.isoformat(), end=e.isoformat(), limit=10)
        assert r2["total"]["value"] == len(exp2) and r2["total"]["exact"]
    # multi-select facets: a field's own clause does not hide its other values
    f = ls.facets("service:nav level:error", start=s.isoformat(), end=e.isoformat(), limit=50)
    svc_counts = {x["v"]: x["n"] for x in f["facets"]["service"]["values"]}
    assert svc_counts == {k: sum(1 for o in lines if o.severity == "ERROR" and o.service == k) for k in {o.service for o in lines if o.severity == "ERROR"}}
    lvl = {x["v"]: x["n"] for x in f["facets"]["level"]["values"]}
    assert lvl["INFO"] == sum(1 for o in lines if o.service == "nav" and o.severity == "INFO")
    assert f["facets"]["service"]["selected"] == ["nav"] and f["facets"]["level"]["selected"] == ["ERROR"]
    f2 = ls.facets("service:nav level:error", start=(s + timedelta(seconds=1)).isoformat(), end=e.isoformat(), limit=50)    # same through the scan
    exp_svc = {k: sum(1 for o in lines if o.severity == "ERROR" and o.service == k and o.timestamp >= s + timedelta(seconds=1)) for k in svc_counts}
    assert {x["v"]: x["n"] for x in f2["facets"]["service"]["values"]} == {k: v for k, v in exp_svc.items() if v}
    assert ls.facets("", last=30, search="service:na")["facets"] == {"service": ls.facets("", last=30, search="service:na")["facets"]["service"]}
    assert [x["v"] for x in ls.facets("", last=30, search="service:na")["facets"]["service"]["values"]] == ["nav"]
    assert ls.facets("", last=30, search="service:zz")["facets"]["service"]["values"] == []
    # breakdown by host: top N + other
    hb = ls.histogram("", start=s.isoformat(), end=e.isoformat(), by="host", top=2)
    assert len(hb["keys"]) == 3 and hb["keys"][-1] == "_other" and sum(b["total"] for b in hb["buckets"]) == len(lines)
    # sub-minute steps
    hs = ls.histogram("", start=s.isoformat(), end=(s + timedelta(seconds=40)).isoformat())
    assert hs["step_s"] == 1 and len(hs["buckets"]) == 40 and sum(b["total"] for b in hs["buckets"]) == sum(1 for o in lines if o.timestamp < s + timedelta(seconds=40))


def test_group_similar_and_group_filter(tmp_path):
    st, ls = make(tmp_path)
    now = datetime.now(UTC)
    t = now - timedelta(minutes=2)
    st._accept([ob(t + timedelta(milliseconds=i), sev="ERROR", svc="ax", msg=f"SysOperationFramework task LedgerJournalPost voucher GJ-{i} failed in {i} ms")
                for i in range(60)] + [ob(t, svc="ax", msg="Batch job 7 finished")], "ax-01", "prod", "", None)
    st.rollup.started = 0
    g = ls.groups("level:error", last=10)
    assert g["source"] == "counters" and g["groups"][0]["count"] == 60 and g["distinct"] == 1
    key = g["groups"][0]["key"]
    r = ls.search(f"group:{key}", last=10, limit=500)
    assert len(r["lines"]) == 60 and all("LedgerJournalPost" in x["message"] for x in r["lines"])                 # D5: the group's own lines
    assert [x["message"] for x in ls.search("GJ-44", last=10)["lines"]] == ["SysOperationFramework task LedgerJournalPost voucher GJ-44 failed in 44 ms"]   # D4
    gs = ls.groups("voucher", last=10)                                                                    # free text: from a scan
    assert gs["source"] == "stored" and gs["groups"][0]["key"] == key and gs["groups"][0]["count"] == 60 and gs["groups"][0]["sample_id"]
    d = ls.line(int(r["lines"][0]["id"]))
    assert d["group"]["key"] == key and d["group"]["count_1h"] == 60


# ---------------------------------------------------------------- review fixes: what is shown is what is counted and searched
def test_free_text_matches_the_shown_message_never_masked_values(tmp_path):
    """The explorer shows privacy.scrub(message); a search on the clear message confirmed masked values piece by piece
    ("yilmaz", a password, a TCKN prefix). Group templates (lower-cased, only digits masked) showed e-mails, secrets and IBANs."""
    st, ls = make(tmp_path)
    t = datetime.now(UTC) - timedelta(minutes=1)
    msg = "login failed for ayse.yilmaz@corp.com password=Gizli123! iban TR330006100519786457841326 tckn 10000000146"
    st._accept([ob(t, host="hr-01", sev="ERROR", svc="hr", msg=msg), ob(t, host="hr-01", svc="hr", msg="login ok for u_1")], "hr-01", "prod", "", None)
    st.rollup.started = 0
    hits = lambda q: [x["message"] for x in ls.search(q, last=10)["lines"]]       # noqa: E731
    assert hits("login failed") == ["login failed for [E-POSTA] password=*** iban [IBAN] tckn [TCKN]"]
    for q in ("yilmaz", "ayse.yilmaz", "Gizli123", "1000000014", "TR3300", "raw:yilmaz", "yilmaz OR nothing"):
        assert hits(q) == [], q
    assert len(hits("posta")) == 1 and len(hits('"[TCKN]"')) == 1 and len(hits("host:hr-01 -yilmaz")) == 2      # the mask is what is searched
    assert ls.tail("yilmaz", who=("a", "1"))["lines"] == [] and len(ls.tail("posta", who=("a", "2"))["lines"]) == 1
    leaks = ("yilmaz", "gizli123", "tr3300", "10000000146")
    for g in (ls.groups("level:error", last=10), ls.groups("login", last=10)):                     # counters and scan paths
        assert g["groups"] and not any(x in json.dumps(g).lower() for x in leaks), g["source"]
    lid = int(ls.search("login failed", last=10)["lines"][0]["id"])
    assert not any(x in json.dumps(ls.line(lid)).lower() for x in leaks)


def test_env_is_the_hosts_env_in_counts_filters_and_lines(tmp_path):
    """Lines of one host do not all carry the environment (only some files name it). The counters count by the host's env, the
    scan filtered by the line's own: `env:prod` said 44,403 results and listed 19,335."""
    st, ls = make(tmp_path)
    t = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=3)
    st._accept([ob(t + timedelta(seconds=i + 1), host="prd-a-01", env="prod" if i % 2 else "") for i in range(10)]
               + [ob(t + timedelta(seconds=i + 1), host="tst-b-01", env="test") for i in range(4)], "agent", "", "", None)
    st.rollup.started = 0
    s, e = t, t + timedelta(minutes=2)
    r = ls.search("env:prod", start=s.isoformat(), end=e.isoformat(), limit=100)
    assert r["total"] == {"value": 10, "exact": True, "source": "counters"} and len(r["lines"]) == 10 and {x["env"] for x in r["lines"]} == {"prod"}
    for s2 in (s, s + timedelta(microseconds=1)):                                                  # counters, then a scan: same numbers
        f = ls.facets("", start=s2.isoformat(), end=e.isoformat())["facets"]["env"]["values"]
        assert {x["v"]: x["n"] for x in f} == {"prod": 10, "test": 4}
    assert ls.search("env:unknown", start=(s + timedelta(microseconds=1)).isoformat(), end=e.isoformat())["lines"] == []


def test_group_counts_follow_each_lines_level_and_equal_the_group_search(tmp_path):
    """A pattern holds lines of several levels (an access-log line with 200 and with 500). The counters filtered a group by its
    highest level, so `level:error` counted its INFO lines too (11,579 shown, 2,118 behind group:<key>) and `level:info` lost it."""
    st, ls = make(tmp_path)
    t = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=3)
    lines = [ob(t + timedelta(seconds=i), host="web-01", svc="http", sev="INFO", msg=f"GET /x HTTP/1.1 {200 + i % 3}") for i in range(20)]
    lines += [ob(t + timedelta(seconds=30 + i), host="web-02", svc="http", sev="ERROR", msg="GET /x HTTP/1.1 500") for i in range(5)]
    st._accept(lines, "agent", "prod", "", None)
    st.rollup.started = 0
    s, e = t, t + timedelta(minutes=2)
    rng = dict(start=s.isoformat(), end=e.isoformat())
    g = ls.groups("level:error", **rng)
    assert g["source"] == "counters" and g["exact"] and [(x["count"], x["level"]) for x in g["groups"]] == [(5, "ERROR")]
    key = g["groups"][0]["key"]
    r = ls.search(f"level:error group:{key}", limit=500, **rng)
    assert r["total"] == {"value": 5, "exact": True, "source": "counters"} and len(r["lines"]) == 5
    assert [(x["count"], x["level"]) for x in ls.groups("level:info", **rng)["groups"]] == [(20, "INFO")]
    assert ls.search(f"group:{key}", **rng)["total"]["value"] == 25
    h = ls.groups("host:web-02", **rng)                                                            # per host and minute: still exact
    assert h["source"] == "counters" and [(x["count"], x["hosts"], x["level"]) for x in h["groups"]] == [(5, 1, "ERROR")]
    assert ls.search(f"host:web-02 group:{key}", **rng)["total"] == {"value": 5, "exact": True, "source": "counters"}
    assert ls.groups("host:web-01 level:error", **rng)["groups"] == []


def test_stale_stamp_line_keeps_its_detail_after_the_ring(tmp_path):
    """A line stamped 23 days ago (EBS) was stored in a row bounded by its stamp and pruned at once: the live tail showed it, its
    detail answered 404 seconds later (12 % of the load-test lines). Such rows are bounded by the arrival now."""
    st, ls = make(tmp_path, maxlen=20)
    now = datetime.now(UTC)
    st._accept([ob(now - timedelta(days=23), host="prd-ebs-03", msg="EBS stale stamp")], "prd-ebs-03", "prod", "", None)
    lid = st.seq
    st._accept([ob(now - timedelta(seconds=i), host="prd-ebs-03", msg=f"fresh {i}") for i in range(40)], "prd-ebs-03", "prod", "", None)
    assert all(o.seq != lid for o in st.buf) and st.prune() == 0
    d = ls.line(lid)
    assert d["source_of_detail"] == "stored" and d["message"] == "EBS stale stamp" and d["skew_s"] > 20 * 86400
    assert ls.context(lid)["anchor"] == str(lid)
    assert "EBS stale stamp" not in [x["message"] for x in ls.search("", last=15, limit=500)["lines"]]
    assert ls.retention()["oldest"] >= logstore.iso_s(now - timedelta(minutes=5))
    assert st.prune(now + timedelta(hours=49)) >= 1 and pytest.raises(KeyError, ls.line, lid)


def test_rare_text_is_never_sampled_away(tmp_path, monkeypatch):
    """A long stored range is read as time slices (sample 1/k); a rare id outside the slices vanished from the chart, the facets
    and the groups while the list showed it (D4 again). A text term with a prefilter is scanned whole, newest first."""
    st, ls = make(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    for m in range(6):
        t0 = now - timedelta(minutes=6 - m)
        st._accept([ob(t0 + timedelta(seconds=i % 60, milliseconds=i), msg=f"routine {i}") for i in range(200)], "a", "prod", "", None)
    st._accept([ob(now - timedelta(minutes=4, seconds=17), sev="ERROR", msg="voucher GJ-280689 failed")], "a", "prod", "", None)
    monkeypatch.setattr(ls_mod, "SAMPLE_LINES", 100)
    monkeypatch.setattr(st.rollup, "keep", 1)                                     # the counters do not cover the range: stored scans
    rng = dict(start=(now - timedelta(minutes=7)).isoformat(), end=now.isoformat())
    assert ls.histogram("routine OR x", **rng)["sample"] > 1                       # without a prefilter: still sampled (whole range)
    assert ls.histogram("routine", **rng)["sample"] > 1                            # a common term too: it survives the sample
    assert sum(b["total"] for b in ls.histogram("GJ-280689", **rng)["buckets"]) == 1
    assert [x["v"] for x in ls.facets("GJ-280689", **rng)["facets"]["level"]["values"]] == ["ERROR"]
    assert ls.groups("GJ-280689", **rng)["total"] == 1 and len(ls.search("GJ-280689", **rng)["lines"]) == 1


def test_local_time_without_offset_is_brought_back_to_utc(tmp_path):
    """D9: a server in Türkiye writing local time without an offset lands 3 h in the future; no live range showed its lines. A
    stamp 15 min - 14 h ahead of its arrival by whole quarter hours (plus a plausible delay) is moved back and says so."""
    st, ls = make(tmp_path)
    now = datetime.now(UTC)
    st._accept([ob(now + timedelta(hours=3, seconds=-4), host="tr-01", msg="local time line"),
                ob(now + timedelta(minutes=20), host="tr-01", msg="fast clock"),                  # 20 min fast: not a time zone, left alone
                ob(now - timedelta(hours=3), host="tr-01", msg="old line")], "tr-01", "prod", "", None)
    r = ls.search("host:tr-01", last=15)
    assert [x["message"] for x in r["lines"]] == ["local time line"] and abs(r["lines"][0]["skew_s"] or 0) < 120
    assert ls.line(int(r["lines"][0]["id"]))["attrs"]["clock_offset_s"] == -3 * 3600
    assert {o.message: round((o.timestamp - now).total_seconds() / 60) for o in st.buf} == {"local time line": 0, "fast clock": 20, "old line": -180}


def test_estimated_total_counts_only_what_was_fully_scanned(tmp_path, monkeypatch):
    """The first page stops counting after COUNT_MS; the estimate floored the frontier to its minute (a 1-minute range looked
    fully scanned) and counted matches from rows straddling the frontier: 2,623 shown for 3,568 lines."""
    st, ls = make(tmp_path)
    t = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=3)
    for sec in range(60):                                                         # one minute, 40 lines a second, every 4th matches
        st._accept([ob(t + timedelta(seconds=sec, milliseconds=25 * i), msg=("timeout " if i % 4 == 0 else "ok ") + str(i)) for i in range(40)],
                   f"a{sec % 3}", "prod", "", None)
    st.rollup.started = 0
    monkeypatch.setattr(ls_mod, "COUNT_MS", 0)
    r = ls.search("timeout", start=t.isoformat(), end=(t + timedelta(minutes=1)).isoformat(), limit=100)
    assert r["total"].get("estimated") and not r["scan"]["complete"], r["total"]
    assert 540 <= r["total"]["value"] <= 660, r["total"]                         # truth: 600
    assert r["total"]["basis"]["range_lines"] == 2400 and r["total"]["basis"]["scanned"] < 2400
    # facets and groups of a partial scan are scaled to the whole range too (they showed the scanned seconds next to the estimate)
    monkeypatch.setattr(ls_mod, "BUDGET_LINES", 200)
    rng = dict(start=t.isoformat(), end=(t + timedelta(minutes=1)).isoformat())
    f = ls.facets("timeout", **rng)
    assert f["exact"] is False and f["scaled"] and 480 <= f["facets"]["level"]["values"][0]["n"] <= 720, f["facets"]["level"]
    g = ls.groups("timeout", **rng)
    assert g["scaled"] and 480 <= g["total"] <= 720 and g["sample"] == 1, (g["total"], g["scaled"])


def test_one_page_view_does_not_cancel_its_own_scans(tmp_path):
    """The page sends search, histogram and facets at once from one tab; a newer request of the same tab cancelled the older
    scan whatever the endpoint, so a text query showed an empty chart and empty facets (stopped_by: cancelled, 0 lines)."""
    st, ls = make(tmp_path)
    who = ("u@x", "tab1")
    a = ls._scan_slot((who, "search")); b = ls._scan_slot((who, "histogram"))
    assert not a.is_set() and not b.is_set()
    ls._release((who, "histogram"), b)
    c = ls._scan_slot((who, "search"))                                            # the same endpoint again (a new query): the old one stops
    assert a.is_set() and not c.is_set()
    ls._release((who, "search"), a); ls._release((who, "search"), c)
    t = datetime.now(UTC) - timedelta(minutes=1)
    st._accept([ob(t + timedelta(milliseconds=i), msg=f"connection lost {i}") for i in range(50)], "a", "prod", "", None)
    rng = dict(last=5, who=who)
    assert [ls.search('"connection lost"', **rng)["scan"]["stopped_by"], ls.histogram('"connection lost"', **rng)["scan"]["complete"]] == ["range_start", True]


def test_sqlite_runs_in_wal_mode(tmp_path):
    """One commit per ingested batch took ~3.7 ms in the rollback-journal mode: at 100k lines/min the live store fell minutes
    behind (tail, alerts). WAL + synchronous=NORMAL commits in ~0.1 ms."""
    db = Database(str(tmp_path / "w.db"))
    assert db._exec("PRAGMA journal_mode")[0]["journal_mode"] == "wal" and db._exec("PRAGMA synchronous")[0]["synchronous"] == 1
    assert Database(":memory:")._exec("PRAGMA journal_mode")[0]["journal_mode"] == "memory"


# ---------------------------------------------------------------- live tail, detail, context
def test_live_tail_cursor_caps_skipped_and_rate_limit(tmp_path, monkeypatch):
    st, ls = make(tmp_path, maxlen=300)
    now = datetime.now(UTC)
    who = ("u@x", "tab1")
    st._accept([ob(now, msg=f"warm {i}", sev="ERROR" if i % 2 else "INFO") for i in range(50)], "a1", "prod", "", None)
    r = ls.tail("level:error", None, 10, who=who)
    assert [int(x["id"]) for x in r["lines"]] == list(range(50, 30, -2)) and r["after"] == 50
    with pytest.raises(ls_mod.TooFast):                                     # faster than once a second per (user, tab)
        ls.tail("level:error", r["after"], 10, who=who)
    assert ls.tail("level:error", None, 1, who=("u@x", "tab2"))["returned"] == 1                          # another tab has its own limit
    # a late, old-stamped line still shows up (arrival order) with its clock skew
    st._accept([ob(now - timedelta(hours=3), sev="ERROR", msg="late line from a server in local time")], "a1", "prod", "", None)
    ls._polls[who] = time.time() - 2
    r2 = ls.tail("level:error", r["after"], 10, who=who)
    assert [x["message"] for x in r2["lines"]] == ["late line from a server in local time"] and r2["lines"][0]["skew_s"] >= 3 * 3600 - 5
    # caps: at most cap_per_s per second since the previous poll, the rest is counted as dropped
    monkeypatch.setattr(ls_mod, "TAIL_CAP_S", 5)
    st._accept([ob(now, sev="ERROR", msg=f"burst {i}") for i in range(40)], "a1", "prod", "", None)
    ls._polls[who] = time.time() - 2
    r3 = ls.tail("level:error", r2["after"], 200, who=who)
    assert r3["matched"] == 40 and r3["returned"] == 10 and r3["dropped"] == 30 and r3["lines"][0]["message"] == "burst 39"
    # the client fell behind the ring: skipped is reported and the cursor jumps to the newest
    st._accept([ob(now, msg=f"flood {i}") for i in range(400)], "a1", "prod", "", None)
    ls._polls[who] = time.time() - 2
    r4 = ls.tail("", r3["after"], 10, who=who)
    assert r4["skipped"] == 100 and r4["after"] == st.seq and r4["rate"]["in_per_s"] > 0


def test_context_order_and_matches(tmp_path):
    st, ls = make(tmp_path, maxlen=20)
    t = datetime.now(UTC) - timedelta(minutes=1)
    st._accept([ob(t + timedelta(seconds=i // 3), ln=100 + i, msg=f"step {i}", sev="ERROR" if i == 10 else "INFO") for i in range(30)], "prd-ebs-01", "prod", "", None)
    st._accept([ob(t, host="prd-ebs-01", src="other.log", msg="other file"), ob(t, host="prd-ebs-02", msg="other host")], "prd-ebs-01", "prod", "", None)
    anchor = next(int(x["id"]) for x in ls.search("step 10", last=5)["lines"] if x["message"] == "step 10")
    c = ls.context(anchor, 3, 4, "level:info")
    assert [x["message"] for x in c["before"]] == ["step 7", "step 8", "step 9"] and [x["message"] for x in c["after"]] == ["step 11", "step 12", "step 13", "step 14"]
    assert c["more_before"] and c["more_after"] and all(x["match"] for x in c["before"]) and [x["line_no"] for x in c["after"]] == [111, 112, 113, 114]
    with pytest.raises(KeyError):
        ls.line(999_999)


# ---------------------------------------------------------------- history and retention
def test_older_than_retention_says_history(tmp_path):
    st, ls = make(tmp_path)
    old_s = datetime.now(UTC) - timedelta(days=3)
    r = ls.search("", start=old_s.isoformat(), end=(old_s + timedelta(hours=1)).isoformat())
    assert r["source"] == "history" and r["lines"] == [] and r["next"] is None and r["retention"]["hours"] == 48
    h = ls.histogram("service:nav*", start=old_s.isoformat(), end=(old_s + timedelta(hours=1)).isoformat())
    assert h["source"] == "history" and h["unsupported"] == ["service:nav*"] and h["buckets"] == []


# ---------------------------------------------------------------- the HTTP layer: every endpoint, team scope, saved searches, alerts
@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(ls_mod, "TAIL_MIN_GAP", 0.0)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email, role in (("ea@corp.com", "operator"), ("hr@corp.com", "operator"), ("view@corp.com", "viewer")):
        svc.users.register(email, PW, role=role)
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"], "X-Tab-Id": email}


def feed(svc, minutes_ago=2):
    t = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    recs = []
    for host, s_name in (("ea-app-01", "ea-portal"), ("ea-db-01", "ea-db"), ("hr-app-01", "hr-api"), ("fin-01", "ledger")):
        for i in range(6):
            recs.append({"ts": (t + timedelta(seconds=i)).isoformat(), "level": "error" if i < 2 else "info", "service": s_name, "host": host, "env": "prod",
                         "msg": f"{s_name} request {i} failed with timeout" if i < 2 else f"{s_name} request {i} ok", "status": 500 if i < 2 else 200})
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps(x) for x in recs) + "\n").encode(), "agent")
    svc.live.rollup.started = 0


def test_endpoints_and_team_scope_everywhere(app):
    c, svc = app, app.svc
    feed(svc)
    admin = login(c, "admin@watchover.local", "Watchover!Admin2026")
    r = c.post("/api/teams", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}], "members": ["ea@corp.com"]}, headers=admin)
    assert r.status_code in (200, 201), r.text
    ea, view = login(c, "ea@corp.com"), login(c, "view@corp.com")
    hosts = lambda h, q="", **kw: {x["host"] for x in c.get("/api/logs/search", params={"q": q, "last": 15, "limit": 500, **kw}, headers=h).json()["lines"]}   # noqa: E731
    assert hosts(admin) == {"ea-app-01", "ea-db-01", "hr-app-01", "fin-01"} and hosts(view) == hosts(admin)
    assert hosts(ea) == {"ea-app-01", "ea-db-01"} and hosts(ea, "host:fin-01") == set() and hosts(ea, "timeout") == {"ea-app-01", "ea-db-01"}
    s = c.get("/api/logs/search", params={"q": "level:error", "last": 15}, headers=ea).json()
    assert s["total"] == {"value": 4, "exact": True, "source": "counters"} and s["range"]["live"] and s["query"]["normalized"] == "level:ERROR"
    h = c.get("/api/logs/histogram", params={"q": "", "last": 15}, headers=ea).json()
    assert sum(b["total"] for b in h["buckets"]) == 12 and h["source"] == "counters"
    assert sum(b["total"] for b in c.get("/api/logs/histogram", params={"last": 15}, headers=admin).json()["buckets"]) == 24
    f = c.get("/api/logs/facets", params={"q": "", "last": 15}, headers=ea).json()["facets"]
    assert {x["v"] for x in f["host"]["values"]} == {"ea-app-01", "ea-db-01"} and {x["v"] for x in f["service"]["values"]} == {"ea-portal", "ea-db"}
    assert {x["v"] for x in c.get("/api/logs/facets", params={"q": "timeout", "last": 15}, headers=ea).json()["facets"]["host"]["values"]} == {"ea-app-01", "ea-db-01"}
    g = c.get("/api/logs/groups", params={"q": "level:error", "last": 15}, headers=ea).json()
    assert {x["service"] for x in g["groups"]} == {"ea-portal", "ea-db"}
    assert {x["service"] for x in c.get("/api/logs/groups", params={"q": "timeout", "last": 15}, headers=ea).json()["groups"]} == {"ea-portal", "ea-db"}
    t = c.get("/api/logs/tail", params={"q": "", "limit": 500}, headers=ea).json()
    assert {x["host"] for x in t["lines"]} == {"ea-app-01", "ea-db-01"} and t["after"] == svc.live.seq
    fin_id = next(x["id"] for x in c.get("/api/logs/search", params={"q": "host:fin-01", "last": 15}, headers=admin).json()["lines"])
    ea_id = next(x["id"] for x in c.get("/api/logs/search", params={"q": "host:ea-app-01", "last": 15}, headers=ea).json()["lines"])
    assert c.get(f"/api/logs/line/{fin_id}", headers=ea).status_code == 404 and c.get(f"/api/logs/line/{fin_id}", headers=admin).status_code == 200
    assert c.get(f"/api/logs/context/{fin_id}", headers=ea).status_code == 404
    d = c.get(f"/api/logs/line/{ea_id}", headers=ea).json()
    assert d["host"] == "ea-app-01" and d["attrs"]["status"] in (200, 500) and d["host_link"]["ops"].startswith("/ops?host=ea-app-01")
    assert {x["host"] for x in c.get(f"/api/logs/context/{ea_id}?before=50&after=50", headers=ea).json()["before"]} <= {"ea-app-01"}
    assert c.get("/api/logs/line/999999", headers=ea).status_code == 404
    # query helpers
    bad = c.get("/api/logs/search", params={"q": "level:(err", "last": 15}, headers=ea)
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "unclosed_paren" and bad.json()["detail"]["pos"] == 6
    assert c.get("/api/logs/search", params={"q": "ayse@corp.com", "last": 15}, headers=ea).json()["detail"]["code"] == "pii_term"
    assert c.get("/api/logs/parse", params={"q": "level:error service:nav*"}, headers=ea).json()["facet_only"] is True
    assert c.get("/api/logs/parse", params={"q": "levle:error"}, headers=ea).json()["warnings"][0]["code"] == "unknown_field"
    assert c.get("/api/logs/rewrite", params={"q": "level:error", "op": "add", "field": "service", "value": "nav"}, headers=ea).json()["q"] == "level:error service:nav"
    fl = c.get("/api/logs/fields", headers=ea).json()
    assert fl["fields"][0]["name"] == "level" and "OR" in fl["operators"]
    s1 = c.get("/api/logs/search", params={"q": "", "last": 15, "limit": 5}, headers=ea).json()
    bc = c.get("/api/logs/search", params={"q": "level:info", "start": s1["range"]["start"], "end": s1["range"]["end"], "cursor": s1["next"]}, headers=ea)
    assert bc.status_code == 400 and bc.json()["detail"]["code"] == "cursor_mismatch"
    nxt = c.get("/api/logs/search", params={"q": "", "start": s1["range"]["start"], "end": s1["range"]["end"], "cursor": s1["next"], "limit": 500}, headers=ea).json()
    assert len(s1["lines"]) + len(nxt["lines"]) == 12 and not {x["id"] for x in s1["lines"]} & {x["id"] for x in nxt["lines"]}
    # permissions: no page.logs -> 403 everywhere; the viewer cannot create alerts
    svc.kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES ('nologs', 'x', '', ?, 0)", (json.dumps(["page.ops"]),))
    svc.users.register("nolog@corp.com", PW, role="viewer")
    svc.kb._exec("UPDATE users SET role='nologs' WHERE email='nolog@corp.com'")
    nl = login(c, "nolog@corp.com")
    for path in ("/api/logs/search", "/api/logs/histogram", "/api/logs/facets", "/api/logs/groups", "/api/logs/tail", "/api/logs/fields", "/api/logs/saved",
                 f"/api/logs/line/{ea_id}", f"/api/logs/context/{ea_id}", "/api/logs/parse"):
        assert c.get(path, headers=nl).status_code == 403, path
    assert c.post("/api/logs/alerts", json={"name": "x", "query": "level:error"}, headers=view).status_code == 403


def test_saved_searches_per_user(app):
    c = app
    ea, hr, admin = login(c, "ea@corp.com"), login(c, "hr@corp.com"), login(c, "admin@watchover.local", "Watchover!Admin2026")
    a = c.post("/api/logs/saved", json={"name": "NAV SQL hataları", "query": 'service:nav* "sql server"', "range": "last=60", "columns": ["time", "level", "message"]}, headers=ea)
    assert a.status_code == 200
    sid = a.json()["id"]
    assert c.post("/api/logs/saved", json={"name": "NAV SQL hataları", "query": "x"}, headers=ea).status_code == 409
    assert c.post("/api/logs/saved", json={"name": "bad", "query": "level:(x"}, headers=ea).status_code == 400
    assert c.post("/api/logs/saved", json={"name": "NAV SQL hataları", "query": "x"}, headers=hr).status_code == 200      # names are per user
    mine = c.get("/api/logs/saved", headers=ea).json()["items"]
    assert [x["name"] for x in mine] == ["NAV SQL hataları"] and mine[0]["mine"] and mine[0]["columns"] == ["time", "level", "message"]
    assert all(x["mine"] for x in c.get("/api/logs/saved", headers=hr).json()["items"])                                      # ea's private one is not shown
    assert c.put(f"/api/logs/saved/{sid}", json={"name": "taken"}, headers=hr).status_code == 403
    assert c.delete(f"/api/logs/saved/{sid}", headers=hr).status_code == 403
    assert c.put(f"/api/logs/saved/{sid}", json={"shared": True}, headers=ea).json() == {"ok": True}
    shared = [x for x in c.get("/api/logs/saved", headers=hr).json()["items"] if not x["mine"]]
    assert len(shared) == 1 and shared[0]["query"] == 'service:nav* "sql server"' and shared[0]["owner"]
    assert c.put(f"/api/logs/saved/{sid}", json={"name": "renamed by admin"}, headers=admin).json() == {"ok": True}
    assert c.delete(f"/api/logs/saved/{sid}", headers=admin).json() == {"ok": True} and c.get("/api/logs/saved", headers=ea).json()["items"] == []


def test_alerts_from_a_query_fire_through_dispatch(app):
    c, svc = app, app.svc
    feed(svc, minutes_ago=2)
    admin = login(c, "admin@watchover.local", "Watchover!Admin2026")
    c.post("/api/teams", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}], "members": ["ea@corp.com"]}, headers=admin)
    ea = login(c, "ea@corp.com")
    bad = c.post("/api/logs/alerts", json={"name": "x", "query": "level:error", "window_min": 90}, headers=ea)
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "bad_window"
    r = c.post("/api/logs/alerts", json={"name": "EA errors", "query": "level:error", "window_min": 5, "op": ">=", "threshold": 4}, headers=ea)
    assert r.status_code == 200, r.text
    rid, mode = r.json()["id"], r.json()["mode"]
    assert mode == "counters"
    r2 = c.post("/api/logs/alerts", json={"name": "timeouts (admin)", "query": "timeout", "window_min": 5, "threshold": 8}, headers=admin)
    assert r2.json()["mode"] == "stream"
    r3 = c.post("/api/logs/alerts", json={"name": "backup missing", "query": '"backup completed"', "window_min": 5, "op": "<", "threshold": 1}, headers=admin)
    rule = next(x for x in svc.notifier.rules() if x["id"] == rid)
    assert rule["condition"] == "query" and rule["owner"] == "ea@corp.com" and rule["window_min"] == 5
    # stream counting starts with the rule: feed lines after it exists (stamped in the previous minute)
    svc.logwatch.refresh(force=True)
    t = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(seconds=30)
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps({"ts": (t + timedelta(seconds=i)).isoformat(), "level": "warn", "service": "x", "host": h,
                                                        "msg": f"upstream timeout {i}"}) for i in range(5) for h in ("ea-app-01", "fin-01")) + "\n").encode(), "agent")
    fired = svc.alerts.evaluate()
    by = {f.get("rule_id"): f for f in fired if isinstance(f, dict) and "rule_id" in f}
    assert rid in by and by[rid]["key"] == f"query:{rid}" and by[rid]["title"].startswith("EA errors: 4 satır")             # ea's scope: 4 of the 8 errors
    assert "fin-01" not in by[rid]["body"] and "/logs?q=" in by[rid]["body"]
    assert r2.json()["id"] in by and "10 satır" in by[r2.json()["id"]]["title"]                                            # 4 old + 10 new timeouts? no: stream counts new ones
    assert r3.json()["id"] not in by                                                                                       # absence: not before a full window ran
    log = svc.kb._exec("SELECT key FROM alerts WHERE key LIKE 'query:%'")
    assert {x["key"] for x in log} >= {f"query:{rid}", f"query:{r2.json()['id']}"}
    again = svc.alerts.evaluate()                                                                                         # cooldown
    assert any(isinstance(f, dict) and f.get("skipped") == "cooldown" for f in again)
    pv = c.get("/api/logs/alerts/preview", params={"q": "level:error", "window_min": 5, "threshold": 1, "last": 60}, headers=ea).json()
    assert pv["would_fire"] >= 1 and pv["windows"]
    # the owner is gone: skipped, logged once a day
    svc.kb._exec("UPDATE users SET status='disabled' WHERE email='ea@corp.com'")
    svc.alerts.evaluate(); svc.alerts.evaluate()
    assert len(svc.kb._exec("SELECT 1 AS x FROM alerts WHERE key=?", (f"query:{rid}:owner_missing",))) == 1


# ---------------------------------------------------------------- performance smoke: 100k lines
def test_performance_100k_lines(tmp_path):
    st, ls = make(tmp_path)
    rng = random.Random(11)
    now = datetime.now(UTC)
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=10)
    svcs = ["nav", "http", "sap", "ebs", "dwh"] + [f"svc{i}" for i in range(20)]
    t0 = time.perf_counter()
    for b in range(400):                                                    # 400 batches x 250 lines from 100 hosts over 10 minutes
        host = f"prd-{b % 100:03d}"
        bt = [ob(base + timedelta(milliseconds=rng.randint(0, 600_000)), host=host, sev=rng.choice(["INFO"] * 6 + ["WARN", "ERROR", "DEBUG"]),
                 svc=rng.choice(svcs), msg=f"request {rng.randint(1, 10**6)} took {rng.randint(1, 2000)} ms status={rng.choice([200, 200, 500])}") for _ in range(250)]
        st._accept(bt, host, "prod", "", None)
    ingest_s = time.perf_counter() - t0
    st.rollup.started = 0
    timings = {}

    def timed(name, fn):
        t = time.perf_counter(); out = fn(); timings[name] = time.perf_counter() - t
        return out
    r = timed("search_facet", lambda: ls.search("level:error service:nav", last=15, limit=100))
    assert len(r["lines"]) == 100 and r["total"]["exact"]
    timed("search_text", lambda: ls.search("status=500 took", last=15, limit=100))
    h = timed("histogram", lambda: ls.histogram("", last=15))
    assert sum(b["total"] for b in h["buckets"]) == 100_000 and h["source"] == "counters"
    f = timed("facets", lambda: ls.facets("level:error", last=15))
    assert f["facets"]["level"]["values"][0]["n"] > 0
    timed("groups", lambda: ls.groups("level:error", last=15))
    timed("tail", lambda: ls.tail("level:error", None, 200, who=("p", "t")))
    lid = int(r["lines"][0]["id"])
    st.buf.clear()
    timed("detail", lambda: ls.line(lid))
    print("ingest", round(ingest_s, 2), {k: round(v * 1000) for k, v in timings.items()})
    assert ingest_s < 60
    assert timings["search_facet"] < 2.5 and timings["histogram"] < 1.5 and timings["facets"] < 2.0 and timings["tail"] < 0.5 and timings["detail"] < 0.5
