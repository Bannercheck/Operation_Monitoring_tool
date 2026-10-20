"""Log explorer query language (docs/design/logs.md section 2): tokenizer, parser, normalizer, planner, compilers, rewrite.

    level:(err OR crit) service:nav* "connection lost" -host:test-*  duration_ms>500  @status>=500  session:s_c4a3...

One query string is the single source of truth for every filter of the log explorer: facet clicks (rewrite), the operations
card link and saved searches carry this string. Standard library only.

* `parse(text)` -> AST (Or / And / Not / Field / Text / All); errors raise QueryError(code, pos, len) with Turkish and English text.
* `normalize(node)` -> canonical text (cache keys, cursor hashes, saved searches).
* `plan(node)` -> facet_only, fields, top_and (facet "selected" marks), sev_mask (row pruning), hosts, prefilter (row text regex).
* `compile(node, ctx)` -> predicate over a line view (ring Observation or stored v2 dict, see logstore.LineView).
* `compile_key(node, ctx)` -> predicate over a facet cube key (host, service, source, severity) for facet-only plans.
* `rewrite(text, op, field, value)` -> the query with one facet value added / removed / excluded / made the only one.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field as dfield
from typing import Callable, List, Optional

from . import privacy

MAX_LEN = 2000
MAX_LEAVES = 64
MAX_DEPTH = 16
SEVS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
SEV_BIT = {s: 1 << i for i, s in enumerate(SEVS)}           # DEBUG=1 .. CRITICAL=16 (live_batches.sevmask)
ALL_SEV = 31
SEV_RANK = {s: i for i, s in enumerate(SEVS)}

LEVEL_ALIASES = {}
for _canon, _names in (("ERROR", ("err", "error", "hata")),
                       ("CRITICAL", ("crit", "critical", "fatal", "emerg", "emergency", "alert", "panic", "kritik")),
                       ("WARN", ("warn", "warning", "uyari", "uyarı")),
                       ("INFO", ("info", "information", "notice", "ok", "success", "bilgi")),
                       ("DEBUG", ("debug", "trace", "verbose"))):
    for _n in _names:
        LEVEL_ALIASES[_n] = _canon

ALIASES = {"lvl": "level", "severity": "level", "sev": "level", "svc": "service", "application": "app", "uygulama": "app", "hostname": "host",
           "environment": "env", "file": "source", "sender": "agent", "type": "kind", "component": "origin", "trace_id": "trace",
           "ses": "session", "ip": "client", "msg": "message", "line_no": "line", "latency_ms": "duration_ms", "elapsed_ms": "duration_ms"}
BUILTIN = ("level", "service", "host", "env", "pool", "source", "agent", "kind", "origin", "trace", "session", "user", "client",
           "message", "raw", "line", "group", "duration_ms", "app")
FACETS = ("level", "service", "host", "env", "pool", "source")     # answerable from the per-minute facet cube
NUMERIC = ("line", "duration_ms")
TEXT_FIELDS = ("message", "raw")
PSEUDO_PREFIX = {"session": "s", "user": "u", "client": "c"}
PSEUDO_RE = re.compile(r"^[suc]_[0-9a-f]{16}$")
FIELD_RE = re.compile(r"^[A-Za-z_@][\w.\-@]*$")
CMPS = (">=", "<=", ">", "<")

_TR = str.maketrans({"ı": "i", "İ": "i", "I": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})
_CLASS = {"i": "iIıİ", "s": "sSşŞ", "g": "gGğĞ", "u": "uUüÜ", "o": "oOöÖ", "c": "cCçÇ"}


def fold(s: str) -> str:
    """Turkish-aware case folding: İşlem -> islem, KAPALI -> kapali."""
    return (s or "").translate(_TR).lower()


# ---------------------------------------------------------------- errors and warnings
MESSAGES = {
    "unclosed_quote": ("Kapanmamış tırnak (sütun {pos}).", "Unclosed quote (column {pos})."),
    "unclosed_paren": ('Kapanmamış parantez: ")" eksik.', 'Missing ")".'),
    "stray_paren": ('Fazladan ")" (sütun {pos}).', 'Unexpected ")" (column {pos}).'),
    "empty_group": ("Boş parantez.", "Empty parentheses."),
    "missing_value": ('"{field}:" için değer eksik.', '"{field}:" needs a value.'),
    "dangling_op": ('"{op}" sonrasında bir ifade bekleniyordu.', 'Expected a term after "{op}".'),
    "leading_op": ('"{op}" sorgunun ya da parantezin başında olamaz.', '"{op}" cannot start a query or group.'),
    "unknown_level": ('Bilinmeyen seviye "{v}". Geçerli: error (err), warn (warning), info (ok), debug, critical (crit, fatal, emerg, alert).',
                      'Unknown level "{v}". Valid: error (err), warn (warning), info (ok), debug, critical (crit, fatal, emerg, alert).'),
    "bad_number": ('"{field}{op}" bir sayı bekliyor; "{v}" sayı değil.', '"{field}{op}" expects a number; "{v}" is not one.'),
    "cmp_not_numeric": ('"{op}" yalnız sayısal alanlarla kullanılır (ör. duration_ms>500, level>=warn); "{field}" sayısal değil.',
                        '"{op}" only works on numeric fields (e.g. duration_ms>500, level>=warn); "{field}" is not numeric.'),
    "group_not": ("Değer grubunun içinde NOT kullanılamaz; -{field}:değer yazın.", "NOT is not allowed inside a value group; write -{field}:value."),
    "bad_escape": ('Geçersiz kaçış "\\{c}".', 'Invalid escape "\\{c}".'),
    "too_long": ("Sorgu çok uzun (en fazla 2000 karakter).", "Query too long (max 2000 characters)."),
    "too_complex": ("Sorgu çok karmaşık (en fazla 64 koşul, 16 parantez derinliği).", "Query too complex (max 64 conditions, depth 16)."),
    "pii_term": ("Kişisel veri ya da gizli bilgi (e-posta, TCKN, kart, IBAN, telefon, parola/token) ile arama yapılamaz; oturum ve kullanıcı için takma adları kullanın (session:s_…, user:u_…).",
                 "Searching for personal data or secrets (e-mail, national id, card, IBAN, phone, password/token) is not allowed; use the pseudonyms (session:s_…, user:u_…)."),
    "cursor_mismatch": ("Sayfa imleci bu sorguya ait değil; aramayı yeniden başlatın.", "The page cursor does not belong to this query; start the search again."),
    "bad_cursor": ("Geçersiz sayfa imleci.", "Invalid page cursor."),
}
WARNINGS = {
    "unknown_field": ('"{f}" bilinen bir alan değil; öznitelik olarak arandı.{hint}', '"{f}" is not a known field; searched as an attribute.{hint_en}'),
    "lowercase_op": ('"{op}" metin olarak arandı; mantıksal {OP} için "{OP_EN}" yazın.', '"{op}" was searched as text; write "{OP_EN}" for the logical operator.'),
    "history_unsupported": ('Bu aralıkta yalnız sunucu, ortam, pool ve seviye (hata/diğer) filtrelenebilir; "{leaf}" uygulanmadı.',
                            'Only host, environment, pool and level (error/other) can be filtered in this range; "{leaf}" was not applied.'),
    "scan_needed": ("Serbest metin ya da öznitelik araması tarama gerektirir; sayılar tahmini olabilir.",
                    "Free text or attribute searches need a scan; counts may be estimates."),
    "app_unknown": ('"{v}" adında bir uygulama yok; servis adı olarak arandı.', 'No application named "{v}"; searched as a service name.'),
}


class QueryError(ValueError):
    def __init__(self, code: str, pos: int = 0, length: int = 0, **params):
        self.code, self.pos, self.len, self.params = code, pos, length, params
        tr, en = MESSAGES.get(code, (code, code))
        p = {"pos": pos + 1, **params}
        self.tr, self.en = _fmt(tr, p), _fmt(en, p)
        super().__init__(self.en)

    def as_dict(self) -> dict:
        return {"code": self.code, "pos": self.pos, "len": self.len, "tr": self.tr, "en": self.en}


def _fmt(t: str, p: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(p.get(m.group(1), m.group(0))), t)


def warning(code: str, **params) -> dict:
    tr, en = WARNINGS[code]
    return {"code": code, "tr": _fmt(tr, params), "en": _fmt(en, params), **{k: v for k, v in params.items() if k in ("f", "s", "leaf", "op")}}


# ---------------------------------------------------------------- tokens
@dataclass
class Val:
    """A value as typed: the literal text, which characters are (unescaped) wildcards, and whether it was quoted."""
    text: str
    wild: frozenset = frozenset()
    quoted: bool = False

    @property
    def has_wild(self) -> bool:
        return bool(self.wild)

    def is_star(self) -> bool:
        return self.text == "*" and 0 in self.wild


@dataclass
class Tok:
    kind: str            # LP RP QUOTED WORD FIELD COLON CMP OR AND NOT NEG EOF
    pos: int
    len: int
    text: str = ""
    val: Optional[Val] = None
    ws: bool = False     # whitespace before this token


ESC_OK = set('"\\():*? ')
OPS = {"OR": "OR", "||": "OR", "VEYA": "OR", "AND": "AND", "&&": "AND", "VE": "AND", "NOT": "NOT", "DEĞİL": "NOT"}


def _read_quoted(s: str, i: int) -> tuple:
    """s[i] == '"': (Val, end index after the closing quote)."""
    out, j = [], i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            if j + 1 >= len(s):
                raise QueryError("bad_escape", j, 1, c="")
            n = s[j + 1]
            if n not in ESC_OK:
                raise QueryError("bad_escape", j, 2, c=n)
            out.append(n); j += 2; continue
        if c == '"':
            return Val("".join(out), frozenset(), True), j + 1
        out.append(c); j += 1
    raise QueryError("unclosed_quote", i, len(s) - i)


def _read_word(s: str, i: int) -> tuple:
    """A bare word up to whitespace, '"', '(' or ')': (Val, raw text, end)."""
    out, wild, j = [], set(), i
    while j < len(s):
        c = s[j]
        if c.isspace() or c in '"()':
            break
        if c == "\\":
            if j + 1 >= len(s):
                raise QueryError("bad_escape", j, 1, c="")
            n = s[j + 1]
            if n not in ESC_OK:
                raise QueryError("bad_escape", j, 2, c=n)
            out.append(n); j += 2; continue
        if c in "*?":
            wild.add(len(out))
        out.append(c); j += 1
    return Val("".join(out), frozenset(wild), False), s[i:j], j


def _split_field(raw: str) -> Optional[tuple]:
    """'service:nav*' -> ('service', ':', 'nav*'); 'duration_ms>=500' -> ('duration_ms', '>=', '500'); None when not a field term.
    Works on the raw text (escapes intact) so an escaped ':' never splits."""
    j = 0
    while j < len(raw):
        c = raw[j]
        if c == "\\":
            j += 2; continue
        if c == ":" or c in "<>":
            name = raw[:j]
            if not name or not FIELD_RE.match(name):
                return None
            if c == ":":
                rest = raw[j + 1:]
                if rest.startswith("//"):                 # http://x is text, not the field "http"
                    return None
                for op in CMPS:
                    if rest.startswith(op):
                        return name, op, rest[len(op):], j + 1 + len(op)
                return name, ":", rest, j + 1
            op = raw[j:j + 2] if raw[j:j + 2] in (">=", "<=") else c
            rest = raw[j + len(op):]
            cname, attr = canonical_field(name)
            if attr and not name.startswith("@") and _num(rest) is None:
                return None                               # "a->b" is text; only a known field, @key or a number makes a comparison
            return name, op, rest, j + len(op)
        j += 1
    return None


def tokenize(s: str) -> List[Tok]:
    toks: List[Tok] = []
    i, n = 0, len(s)
    while True:
        ws = False
        while i < n and s[i].isspace():
            i += 1; ws = True
        if i >= n:
            toks.append(Tok("EOF", n, 0, ws=ws)); return toks
        c = s[i]
        if c == "(":
            toks.append(Tok("LP", i, 1, "(", ws=ws)); i += 1; continue
        if c == ")":
            toks.append(Tok("RP", i, 1, ")", ws=ws)); i += 1; continue
        if c == '"':
            v, j = _read_quoted(s, i)
            toks.append(Tok("QUOTED", i, j - i, s[i:j], v, ws=ws)); i = j; continue
        if c == "-" and i + 1 < n and not s[i + 1].isspace() and s[i + 1] != ")" and (ws or not toks or toks[-1].kind in ("LP",)):
            toks.append(Tok("NEG", i, 1, "-", ws=ws)); i += 1; continue
        if s.startswith(("||", "&&"), i) and (i + 2 >= n or s[i + 2].isspace() or s[i + 2] in "()"):
            toks.append(Tok(OPS[s[i:i + 2]], i, 2, s[i:i + 2], ws=ws)); i += 2; continue
        v, raw, j = _read_word(s, i)
        if raw in OPS:
            toks.append(Tok(OPS[raw], i, j - i, raw, ws=ws)); i = j; continue
        sp = _split_field(raw)
        if sp is not None:
            name, op, rest, off = sp
            toks.append(Tok("FIELD", i, len(name), name, ws=ws))
            if op == ":":
                toks.append(Tok("COLON", i + len(name), 1, ":"))
            else:
                toks.append(Tok("CMP", i + off - len(op), len(op), op))
            if rest:
                rv, _r, _j = _read_word(rest, 0)
                toks.append(Tok("WORD", i + off, len(rest), rest, rv))
            i = j; continue
        toks.append(Tok("WORD", i, j - i, raw, v, ws=ws)); i = j


# ---------------------------------------------------------------- AST
@dataclass
class Node:
    pass


@dataclass
class All(Node):
    pass


@dataclass
class Or(Node):
    items: list


@dataclass
class And(Node):
    items: list


@dataclass
class Not(Node):
    item: Node


@dataclass
class Field(Node):
    name: str                         # canonical name (level, service, ... or "@key" for attributes)
    op: str                           # eq | glob | present | gt | ge | lt | le
    values: list                      # list[Val] (OR inside a value group) or [number] for comparisons
    pos: int = 0
    len: int = 0
    typed: str = ""                   # the field name as typed
    attr: bool = False
    num: Optional[float] = None
    levels: frozenset = frozenset()   # level: canonical severities this leaf matches


@dataclass
class Text(Node):
    val: Val
    phrase: bool
    pos: int = 0
    len: int = 0
    target: str = "message"           # message | raw


@dataclass
class Parsed:
    node: Node
    warnings: list = dfield(default_factory=list)
    text: str = ""


def canonical_field(name: str) -> tuple:
    """(canonical name, is attribute). '@x' is always the attribute x; a bare unknown name is an attribute too."""
    if name.startswith("@"):
        return "@" + name[1:], True
    low = name.lower()
    low = ALIASES.get(low, low)
    if low in BUILTIN:
        return low, False
    return "@" + name, True


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.warnings: list = []
        self.leaves = 0
        self.depth = 0

    @property
    def t(self) -> Tok:
        return self.toks[self.i]

    def adv(self) -> Tok:
        t = self.toks[self.i]; self.i += 1; return t

    def starts_unary(self, t: Tok) -> bool:
        return t.kind in ("LP", "QUOTED", "WORD", "FIELD", "NOT", "NEG")

    def parse(self) -> Node:
        if self.t.kind == "EOF":
            return All()
        node = self.or_expr()
        if self.t.kind == "RP":
            raise QueryError("stray_paren", self.t.pos, 1)
        if self.t.kind != "EOF":                                     # cannot happen: and_expr eats every unary start
            raise QueryError("dangling_op", self.t.pos, self.t.len, op=self.t.text)
        return node

    def or_expr(self) -> Node:
        if self.t.kind in ("OR", "AND"):
            raise QueryError("leading_op", self.t.pos, self.t.len, op=self.t.text)
        items = [self.and_expr()]
        while self.t.kind == "OR":
            op = self.adv()
            if not self.starts_unary(self.t):
                raise QueryError("dangling_op", op.pos, op.len, op=op.text)
            items.append(self.and_expr())
        return items[0] if len(items) == 1 else Or(items)

    def and_expr(self) -> Node:
        items = [self.unary()]
        while True:
            if self.t.kind == "AND":
                op = self.adv()
                if not self.starts_unary(self.t):
                    raise QueryError("dangling_op", op.pos, op.len, op=op.text)
                items.append(self.unary())
            elif self.starts_unary(self.t):
                items.append(self.unary())
            else:
                break
        return items[0] if len(items) == 1 else And(items)

    def unary(self) -> Node:
        t = self.t
        if t.kind == "NOT":
            self.adv()
            if not self.starts_unary(self.t):
                raise QueryError("dangling_op", t.pos, t.len, op=t.text)
            return Not(self.unary())
        if t.kind == "NEG":
            self.adv()
            if self.t.ws or not self.starts_unary(self.t) or self.t.kind in ("NOT", "NEG"):
                raise QueryError("dangling_op", t.pos, t.len, op="-")
            return Not(self.primary())
        return self.primary()

    def primary(self) -> Node:
        t = self.t
        if t.kind == "LP":
            self.adv()
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise QueryError("too_complex", t.pos, 1)
            if self.t.kind == "RP":
                raise QueryError("empty_group", t.pos, self.t.pos - t.pos + 1)
            if self.t.kind == "EOF":
                raise QueryError("unclosed_paren", t.pos, 1)
            node = self.or_expr()
            if self.t.kind != "RP":
                raise QueryError("unclosed_paren", t.pos, 1)
            self.adv()
            self.depth -= 1
            return node
        if t.kind == "FIELD":
            return self.field_term()
        if t.kind in ("QUOTED", "WORD"):
            self.adv()
            self._leaf(t)
            if t.kind == "WORD" and t.text in ("or", "and", "not", "veya", "ve", "değil"):
                OP = {"or": "VEYA", "and": "VE", "not": "DEĞİL", "veya": "VEYA", "ve": "VE", "değil": "DEĞİL"}[t.text]
                OP_EN = {"or": "OR", "and": "AND", "not": "NOT", "veya": "OR", "ve": "AND", "değil": "NOT"}[t.text]
                self.warnings.append(warning("lowercase_op", op=t.text, OP=OP, OP_EN=OP_EN))
            return self._text(t.val, t.kind == "QUOTED", t.pos, t.len, "message")
        if t.kind == "RP":
            raise QueryError("stray_paren", t.pos, 1)
        if t.kind in ("OR", "AND"):
            raise QueryError("leading_op", t.pos, t.len, op=t.text)
        raise QueryError("dangling_op", t.pos, t.len, op=t.text or "")

    def _leaf(self, t: Tok) -> None:
        self.leaves += 1
        if self.leaves > MAX_LEAVES:
            raise QueryError("too_complex", t.pos, t.len)

    def _text(self, v: Val, phrase: bool, pos: int, ln: int, target: str) -> Node:
        if v.is_star() and not phrase:
            return All()
        _pii(v.text, pos, ln)
        return Text(v, phrase, pos, ln, target)

    def field_term(self) -> Node:
        ft = self.adv()
        self._leaf(ft)
        name, attr = canonical_field(ft.text)
        sep = self.adv()                                           # COLON or CMP
        if sep.kind == "CMP":
            vt = self.t
            if vt.kind not in ("WORD", "QUOTED") or vt.ws:
                raise QueryError("missing_value", ft.pos, sep.pos + sep.len - ft.pos, field=ft.text)
            self.adv()
            return self._cmp(ft, name, attr, sep.text, vt)
        nt = self.t
        if nt.kind == "CMP" and not nt.ws:                         # field:>=500
            self.adv()
            vt = self.t
            if vt.kind not in ("WORD", "QUOTED") or vt.ws:
                raise QueryError("missing_value", ft.pos, nt.pos + nt.len - ft.pos, field=ft.text)
            self.adv()
            return self._cmp(ft, name, attr, nt.text, vt)
        if nt.ws or nt.kind not in ("WORD", "QUOTED", "LP"):
            raise QueryError("missing_value", ft.pos, sep.pos + 1 - ft.pos, field=ft.text)
        if nt.kind == "LP":
            lp = self.adv()
            vals: list = []
            while True:
                t = self.t
                if t.kind in ("WORD", "QUOTED"):
                    vals.append(self.adv().val)
                    if len(vals) > 1:
                        self._leaf(t)
                elif t.kind == "OR":
                    op = self.adv()
                    if self.t.kind not in ("WORD", "QUOTED"):
                        raise QueryError("dangling_op", op.pos, op.len, op=op.text)
                elif t.kind in ("NOT", "NEG"):
                    raise QueryError("group_not", t.pos, t.len, field=ft.text)
                elif t.kind == "RP":
                    if not vals:
                        raise QueryError("empty_group", lp.pos, t.pos - lp.pos + 1)
                    end = self.adv()
                    break
                elif t.kind == "AND":
                    self.adv()                                       # service:(a AND b): a value cannot be two things; read as OR
                elif t.kind == "EOF":
                    raise QueryError("unclosed_paren", lp.pos, 1)
                else:
                    raise QueryError("group_not" if t.kind == "FIELD" else "dangling_op", t.pos, t.len, field=ft.text, op=t.text)
            return self._field(ft, name, attr, vals, end.pos + 1 - ft.pos)
        vt = self.adv()
        return self._field(ft, name, attr, [vt.val], vt.pos + vt.len - ft.pos)

    def _field(self, ft: Tok, name: str, attr: bool, vals: list, ln: int) -> Node:
        if name in TEXT_FIELDS:
            nodes = [self._text(v, v.quoted or " " in v.text, ft.pos, ln, name) for v in vals]
            return nodes[0] if len(nodes) == 1 else Or(nodes)
        if any(v.is_star() for v in vals):
            return Field(name, "present", [], ft.pos, ln, ft.text, attr)
        if name == "level":
            lv = set()
            for v in vals:
                if v.has_wild:
                    rx = _glob_rx(v, whole=True)
                    hit = {s for s in SEVS if rx.match(fold(s))} | {LEVEL_ALIASES[a] for a in LEVEL_ALIASES if rx.match(fold(a))}
                    if not hit:
                        raise QueryError("unknown_level", ft.pos, ln, v=v.text)
                    lv |= hit
                else:
                    c = LEVEL_ALIASES.get(fold(v.text)) or (v.text.upper() if v.text.upper() in SEVS else None)
                    if c is None:
                        raise QueryError("unknown_level", ft.pos, ln, v=v.text)
                    lv.add(c)
            return Field(name, "eq", vals, ft.pos, ln, ft.text, attr, levels=frozenset(lv))
        if name in PSEUDO_PREFIX:
            out = []
            for v in vals:
                t = v.text.strip()
                if not PSEUDO_RE.match(t.lower()):
                    t = privacy.pseudonym(t[:128] if name == "session" else t, PSEUDO_PREFIX[name])
                out.append(Val(t.lower(), frozenset(), v.quoted))
            return Field(name, "eq", out, ft.pos, ln, ft.text, attr)
        if name in NUMERIC:
            nums = []
            for v in vals:
                n = _num(v.text)
                if n is None:
                    raise QueryError("bad_number", ft.pos, ln, field=ft.text, op=":", v=v.text)
                nums.append(n)
            return Field(name, "eq", vals, ft.pos, ln, ft.text, attr, num=nums[0] if len(nums) == 1 else None)
        if attr or name in ("trace",):
            for v in vals:
                _pii(v.text, ft.pos, ln)
        return Field(name, "glob" if any(v.has_wild for v in vals) else "eq", vals, ft.pos, ln, ft.text, attr)

    def _cmp(self, ft: Tok, name: str, attr: bool, op: str, vt: Tok) -> Node:
        opn = {">": "gt", ">=": "ge", "<": "lt", "<=": "le"}[op]
        ln = vt.pos + vt.len - ft.pos
        if name == "level":
            c = LEVEL_ALIASES.get(fold(vt.val.text)) or (vt.val.text.upper() if vt.val.text.upper() in SEVS else None)
            if c is None:
                raise QueryError("unknown_level", vt.pos, vt.len, v=vt.val.text)
            r = SEV_RANK[c]
            ok = {s for s in SEVS if {"gt": SEV_RANK[s] > r, "ge": SEV_RANK[s] >= r, "lt": SEV_RANK[s] < r, "le": SEV_RANK[s] <= r}[opn]}
            return Field(name, opn, [vt.val], ft.pos, ln, ft.text, attr, levels=frozenset(ok))
        if not attr and name not in NUMERIC:
            raise QueryError("cmp_not_numeric", ft.pos, ln, op=op, field=ft.text)
        n = _num(vt.val.text)
        if n is None:
            raise QueryError("bad_number", vt.pos, vt.len, field=ft.text, op=op, v=vt.val.text)
        return Field(name, opn, [vt.val], ft.pos, ln, ft.text, attr, num=n)


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ".")) if re.fullmatch(r"-?\d+(?:[.,]\d+)?", s.strip()) else None
    except ValueError:
        return None


def _pii(text: str, pos: int, ln: int) -> None:
    """A term that privacy.scrub would change (e-mail, IBAN, phone, TCKN / card digits, bearer, secret key=value) is refused:
    messages are shown scrubbed, so a search for the clear value would confirm it (an oracle)."""
    if text and privacy.scrub(text) != text:
        raise QueryError("pii_term", pos, ln)


def parse(text: str) -> Parsed:
    text = text or ""
    if len(text) > MAX_LEN:
        raise QueryError("too_long", MAX_LEN, len(text) - MAX_LEN)
    p = _Parser(text)
    node = _simplify(p.parse())
    return Parsed(node, p.warnings, text)


def _simplify(node: Node) -> Node:
    """level:err OR level:crit -> level:(ERROR OR CRITICAL): an OR of plain value clauses on one field becomes one value group."""
    if isinstance(node, (And, Or)):
        items = [_simplify(x) for x in node.items]
        if isinstance(node, Or):
            merged: list = []
            for x in items:
                prev = merged[-1] if merged else None
                if (isinstance(x, Field) and isinstance(prev, Field) and x.name == prev.name and x.op in ("eq", "glob") and prev.op in ("eq", "glob")
                        and x.name not in NUMERIC):
                    vals = prev.values + [v for v in x.values if all(v.text != w.text for w in prev.values)]
                    op = "glob" if "glob" in (x.op, prev.op) else "eq"
                    merged[-1] = Field(prev.name, op, vals, prev.pos, x.pos + x.len - prev.pos, prev.typed, prev.attr, levels=prev.levels | x.levels)
                else:
                    merged.append(x)
            return merged[0] if len(merged) == 1 else Or(merged)
        return And(items)
    if isinstance(node, Not):
        return Not(_simplify(node.item))
    return node


# ---------------------------------------------------------------- normalize
def _qv(v: Val, keep_wild: bool = True) -> str:
    """A value as query text: quoted when needed, wildcards kept, literal * ? escaped."""
    t = v.text
    special = any(c.isspace() or c in '"():\\' for c in t) or t in OPS or not t or t.startswith("-")
    if v.quoted or special:
        return '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'
    out = []
    for i, c in enumerate(t):
        if c in "*?" and not (keep_wild and i in v.wild):
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def normalize(node: Node) -> str:
    if isinstance(node, All):
        return ""
    if isinstance(node, Or):
        return " OR ".join(normalize(x) for x in node.items)
    if isinstance(node, And):
        return " ".join(("(" + normalize(x) + ")") if isinstance(x, Or) else normalize(x) for x in node.items)
    if isinstance(node, Not):
        inner = node.item
        if isinstance(inner, (Field, Text)):
            return "-" + normalize(inner)
        return "-(" + normalize(inner) + ")"
    if isinstance(node, Text):
        s = _qv(node.val) if not node.phrase else '"' + node.val.text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return s if node.target == "message" else f"{node.target}:{s}"
    if isinstance(node, Field):
        name = node.name
        if node.op == "present":
            return f"{name}:*"
        if node.op in ("gt", "ge", "lt", "le"):
            sym = {"gt": ">", "ge": ">=", "lt": "<", "le": "<="}[node.op]
            if name == "level":
                return f"level{sym}{_level_of(node)}"
            return f"{name}{sym}{_numtxt(node.num)}"
        if name == "level":
            lv = [s for s in reversed(SEVS) if s in node.levels]
            return "level:" + (lv[0] if len(lv) == 1 else "(" + " OR ".join(lv) + ")")
        vals = [_qv(v) for v in node.values]
        return f"{name}:" + (vals[0] if len(vals) == 1 else "(" + " OR ".join(vals) + ")")
    return ""


def _numtxt(n) -> str:
    return str(int(n)) if n is not None and float(n).is_integer() else str(n)


def _level_of(node: Field) -> str:
    v = node.values[0].text
    return LEVEL_ALIASES.get(fold(v)) or v.upper()


# ---------------------------------------------------------------- plan
@dataclass
class Plan:
    facet_only: bool
    fields: set
    top_and: dict                       # field -> {"pos": [values], "neg": [values]} for the top-level conjunction (facet fields)
    conj: bool                          # the top level is a conjunction (facet counts can drop a field's own clauses)
    sev_mask: int
    hosts: Optional[set]                # exact host names the query pins (folded), None when not pinned
    sources: Optional[set]
    prefilter: Optional[object]         # compiled bytes regex every matching line's stored text contains (row pruning)
    leaves: int
    depth: int
    attrs: set                          # attribute keys used
    group_keys: set                     # group:<key> values used


def _leaves(node: Node):
    if isinstance(node, (Or, And)):
        for x in node.items:
            yield from _leaves(x)
    elif isinstance(node, Not):
        yield from _leaves(node.item)
    elif isinstance(node, (Field, Text)):
        yield node


def _depth(node: Node) -> int:
    if isinstance(node, (Or, And)):
        return 1 + max((_depth(x) for x in node.items), default=0)
    if isinstance(node, Not):
        return 1 + _depth(node.item)
    return 0


def top_items(node: Node) -> list:
    """The items of the top-level conjunction (a single non-Or node is a conjunction of one)."""
    if isinstance(node, All):
        return []
    if isinstance(node, And):
        return list(node.items)
    if isinstance(node, Or):
        return []
    return [node]


def _sev_mask(node: Node) -> int:
    if isinstance(node, Field) and node.name == "level":
        if node.op == "present":
            return ALL_SEV
        m = 0
        for s in node.levels:
            m |= SEV_BIT[s]
        return m
    if isinstance(node, Not):
        inner = node.item
        if isinstance(inner, Field) and inner.name == "level":
            return ALL_SEV & ~_sev_mask(inner) if inner.op != "present" else 0
        return ALL_SEV
    if isinstance(node, And):
        m = ALL_SEV
        for x in node.items:
            m &= _sev_mask(x)
        return m
    if isinstance(node, Or):
        m = 0
        for x in node.items:
            m |= _sev_mask(x)
        return m
    return ALL_SEV


def plan(node: Node) -> Plan:
    leaves = list(_leaves(node))
    facet_only = all(isinstance(x, Field) and not x.attr and x.name in FACETS for x in leaves)
    fields = {x.name if isinstance(x, Field) else x.target for x in leaves}
    items = top_items(node)
    conj = isinstance(node, (And, Field, Not, Text, All)) and not isinstance(node, Or)
    top: dict = {}
    hosts = sources = None
    texts = []
    for it in items:
        neg = isinstance(it, Not)
        x = it.item if neg else it
        if isinstance(x, Field) and not x.attr and x.name in FACETS and x.op in ("eq", "glob"):
            d = top.setdefault(x.name, {"pos": [], "neg": []})
            vals = sorted(x.levels, key=lambda s: -SEV_RANK[s]) if x.name == "level" else [v.text for v in x.values]
            d["neg" if neg else "pos"].extend(vals)
            if not neg and x.op == "eq" and x.name in ("host", "source"):
                exact = {fold(v.text) for v in x.values}
                if x.name == "host":
                    hosts = exact if hosts is None else hosts & exact
                else:
                    sources = exact if sources is None else sources & exact
        if isinstance(x, Text) and not neg:
            texts.append(x)
    pre = None
    best = None
    for t in texts:                                   # the longest literal piece of an ANDed text term
        lit = _longest_literal(t.val, t.phrase)
        if lit and _mask_safe(lit) and (best is None or len(lit) > len(best)):
            best = lit
    if best and len(best) >= 2:
        pre = Prefilter(best)
    return Plan(facet_only, fields, top, conj, _sev_mask(node), hosts, sources, pre, len(leaves), _depth(node),
                {x.name[1:] for x in leaves if isinstance(x, Field) and x.attr}, {v.text for x in leaves if isinstance(x, Field) and x.name == "group" for v in x.values})


def _longest_literal(v: Val, phrase: bool) -> str:
    if phrase or not v.wild:
        parts = [v.text]
    else:
        parts, cur = [], []
        for i, c in enumerate(v.text):
            if i in v.wild:
                parts.append("".join(cur)); cur = []
            else:
                cur.append(c)
        parts.append("".join(cur))
    best = max(parts, key=len) if parts else ""
    if any(c in best for c in '"\\') or any(ord(c) < 32 for c in best):
        return ""                                     # JSON escapes these: the stored text would not contain them as typed
    return best


def _bytes_rx(lit: str):
    out = []
    for c in lit:
        k = fold(c)
        if k in _CLASS:
            alts = sorted({x.encode() for x in _CLASS[k]}, key=len, reverse=True)
            out.append(b"(?:" + b"|".join(re.escape(a) for a in alts) + b")")
        else:
            out.append(re.escape(c.encode()))
    return re.compile(b"".join(out), re.I)


class Prefilter:
    """Does a stored row / line (UTF-8 JSON text) contain the literal, case-insensitively with Turkish folding? Same answer as the
    `_bytes_rx` regex (kept as `pattern`), about 5-10x faster on 12 KB rows: the text is ASCII-lowercased in C, the Turkish letters
    of the classes the literal uses are folded with bytes.replace, then one substring search. An IGNORECASE alternation regex
    tries every position in Python's regex engine instead."""
    __slots__ = ("lit", "fold", "pattern")

    def __init__(self, lit: str):
        out, folds = [], []
        for c in lit:
            k = fold(c)
            if k in _CLASS:
                out.append(k.encode())
                folds += [(x.encode(), k.encode()) for x in _CLASS[k] if not x.isascii() and (x.encode(), k.encode()) not in folds]
            else:
                out.append(c.encode().lower())                # bytes.lower: ASCII only, as re.I on a bytes pattern
        self.lit = b"".join(out)
        self.fold = tuple(folds)
        self.pattern = _bytes_rx(lit).pattern

    def search(self, b: bytes) -> bool:
        t = b.lower()
        if self.fold and not t.isascii():
            for a, k in self.fold:
                if a in t:
                    t = t.replace(a, k)
        return self.lit in t


# ---------------------------------------------------------------- compile
def _char_rx(c: str) -> str:
    k = fold(c)
    if len(k) == 1 and k in _CLASS:
        return "[" + _CLASS[k] + "]"
    return re.escape(c)


MAX_GLOB_STARS = 6                    # star runs in one value: each is a backtracking point, *?*?*?…! over a long token took seconds


def _glob_rx(v: Val, whole: bool, free: bool = False):
    """Folded glob: whole-value (fullmatch on a folded value) or free text (search, a wildcard never crosses whitespace). A run of
    wildcards becomes one quantifier (*?*? -> X{2,}) and a value holds at most MAX_GLOB_STARS star runs, so a typed query cannot
    make the scan backtrack catastrophically (security review, v1.60.2)."""
    any_c = r"\S" if free else "."
    parts, run, stars = [], None, 0

    def close():
        nonlocal run, stars
        if run is not None:
            n, star = run
            parts.append(any_c + ("{%d,}" % n if n and star else "*" if star else "{%d}" % n if n > 1 else ""))
            stars += 1 if star else 0
            run = None
    for i, c in enumerate(v.text):
        if i in v.wild:
            n, star = run or (0, False)
            run = (n + (c == "?"), star or c == "*")
        else:
            close()
            parts.append(re.escape(fold(c)) if whole else _char_rx(c))
    close()
    if stars > MAX_GLOB_STARS:
        raise ValueError(f"too many wildcards in one value (at most {MAX_GLOB_STARS} '*')")
    body = "".join(parts)
    return re.compile(("^" + body + "$") if whole else body, re.I | re.S)


MASK_TOKENS = ("***", "Bearer ***", "[E-POSTA]", "[IBAN]", "[TEL]", "[TCKN]", "[KART]")    # what privacy.scrub writes in place of a value


def may_hit_mask(v: Val, rx=None) -> bool:
    """Could the term match text that only exists after privacy.scrub (a mask token, or across one)? Then the clear message
    cannot rule a line out (a wildcard can also span a masked phone number, which loses its spaces)."""
    if v.has_wild or any(c in v.text for c in "[]*"):
        return True
    rx = rx or text_rx(v, True)
    return any(rx.search(m) for m in MASK_TOKENS)


def _mask_safe(lit: str) -> bool:
    """A prefilter literal must be in the stored (clear) text whenever the shown text holds it: not part of a mask token."""
    f = fold(lit)
    return not any(c in lit for c in "[]*") and not any(f in fold(m) for m in MASK_TOKENS)


def text_rx(v: Val, phrase: bool):
    if phrase:
        return re.compile("".join(_char_rx(c) for c in v.text), re.I | re.S)
    return _glob_rx(v, whole=False, free=True)


def value_matcher(node: Field) -> Callable[[object], bool]:
    """A memoised test on one field value (a string; numbers are compared as numbers)."""
    if node.op == "present":
        return lambda v: v not in (None, "", "-")
    if node.op in ("gt", "ge", "lt", "le"):
        n, op = node.num, node.op

        def cmp(v):
            if v is None or v == "" or isinstance(v, bool):
                return False
            try:
                x = float(v)
            except (TypeError, ValueError):
                return False
            return x > n if op == "gt" else x >= n if op == "ge" else x < n if op == "lt" else x <= n
        return cmp
    if node.name in NUMERIC:
        nums = {_num(v.text) for v in node.values}

        def numeq(v):
            try:
                return v is not None and not isinstance(v, bool) and float(v) in nums
            except (TypeError, ValueError):
                return False
        return numeq
    if node.name in ("kind", "group"):
        want = {v.text.lower() for v in node.values}
        return lambda v: v is not None and str(v).lower() in want
    if node.name in PSEUDO_PREFIX:
        want = {v.text for v in node.values}
        return lambda v: v in want
    exact = {fold(v.text) for v in node.values if not v.has_wild}
    globs = [_glob_rx(v, whole=True) for v in node.values if v.has_wild]
    memo: dict = {}

    def m(v):
        if v is None:
            return False
        r = memo.get(v)
        if r is None:
            fv = fold(v if v.__class__ is str else str(v))
            r = fv in exact or any(g.match(fv) for g in globs)
            if len(memo) < 50_000:
                memo[v] = r
        return r
    return m


def compile(node: Node, ctx=None) -> Callable[[object], bool]:   # noqa: A001 - the design's name
    """Predicate over a line view (logstore.LineView: .sev .svc .host .env .pool() .src .agent .kind .org .msg .raw() .attr(k)
    .trace() .session() .user() .client() .ln .group() .duration())."""
    if isinstance(node, All):
        return lambda line: True
    if isinstance(node, And):
        fs = [compile(x, ctx) for x in node.items]
        return lambda line: all(f(line) for f in fs)
    if isinstance(node, Or):
        fs = [compile(x, ctx) for x in node.items]
        return lambda line: any(f(line) for f in fs)
    if isinstance(node, Not):
        f = compile(node.item, ctx)
        return lambda line: not f(line)
    if isinstance(node, Text):
        rx = text_rx(node.val, node.phrase)
        if node.target == "raw":
            return lambda line: bool(rx.search(line.raw_text()))
        # free text matches the message as shown (privacy.scrub), never the clear one: "yilmaz" must not find the line that shows
        # [E-POSTA], or search becomes an oracle for masked values. The scrub runs only for lines whose clear text matches, unless
        # the term could match a mask token itself ("posta", "[tel]", a wildcard).
        if may_hit_mask(node.val, rx):
            return lambda line: bool(rx.search(line.shown()))
        s = rx.search
        return lambda line: s(line.msg) is not None and s(line.shown()) is not None
    if isinstance(node, Field):
        name = node.name
        if name == "level":
            lv = node.levels if node.op != "present" else frozenset(SEVS)
            return lambda line: line.sev in lv
        m = value_matcher(node)
        if node.attr:
            key = name[1:]
            return lambda line: m(line.attr(key))
        get = GETTERS[name]
        return lambda line: m(get(line))
    raise TypeError(node)


GETTERS = {
    "service": lambda l: l.svc or "-", "host": lambda l: l.host or "-", "env": lambda l: l.env_name(), "pool": lambda l: l.pool(),
    "source": lambda l: l.src or "-", "agent": lambda l: l.agent, "kind": lambda l: l.kind, "origin": lambda l: l.org,
    "trace": lambda l: l.trace(), "session": lambda l: l.session(), "user": lambda l: l.user(), "client": lambda l: l.client(),
    "line": lambda l: l.ln, "group": lambda l: l.group(), "duration_ms": lambda l: l.duration(),
    "app": lambda l: l.svc or "-",       # an app: clause that was not expanded (no application store): the old service alias
}


def compile_key(node: Node, ctx) -> Callable[[str, str, str, str], bool]:
    """Predicate over (severity, service, source, host) of a facet cube key; env and pool follow from the host (ctx.env_of,
    ctx.pool_of). Only for plans with facet_only."""
    if isinstance(node, All):
        return lambda sev, svc, src, host: True
    if isinstance(node, And):
        fs = [compile_key(x, ctx) for x in node.items]
        return lambda sev, svc, src, host: all(f(sev, svc, src, host) for f in fs)
    if isinstance(node, Or):
        fs = [compile_key(x, ctx) for x in node.items]
        return lambda sev, svc, src, host: any(f(sev, svc, src, host) for f in fs)
    if isinstance(node, Not):
        f = compile_key(node.item, ctx)
        return lambda sev, svc, src, host: not f(sev, svc, src, host)
    if isinstance(node, Field):
        if node.name == "level":
            lv = node.levels if node.op != "present" else frozenset(SEVS)
            return lambda sev, svc, src, host: sev in lv
        m = value_matcher(node)
        if node.name in ("service", "app"):
            return lambda sev, svc, src, host: m(svc)
        if node.name == "source":
            return lambda sev, svc, src, host: m(src)
        if node.name == "host":
            return lambda sev, svc, src, host: m(host)
        if node.name == "env":
            return lambda sev, svc, src, host: m(ctx.env_of(host))
        if node.name == "pool":
            return lambda sev, svc, src, host: m(ctx.pool_of(host))
    raise ValueError("not a facet query")


def without_field(node: Node, fieldname: str) -> Node:
    """The top-level conjunction without the clauses on one field (facet counts in multi mode)."""
    items = top_items(node)
    if not items and not isinstance(node, All):
        return node
    keep = []
    for it in items:
        x = it.item if isinstance(it, Not) else it
        if isinstance(x, Field) and x.name == fieldname:
            continue
        keep.append(it)
    if not keep:
        return All()
    return keep[0] if len(keep) == 1 else And(keep)


def facet_part(node: Node) -> Node:
    """The facet-only conjuncts of the top-level conjunction (the part the cube can count), All when there are none."""
    items = top_items(node)
    keep = [it for it in items if all(isinstance(x, Field) and not x.attr and x.name in FACETS for x in _leaves(it))]
    if not keep:
        return All()
    return keep[0] if len(keep) == 1 else And(keep)


# ---------------------------------------------------------------- rewrite (facet clicks, "filter by this value")
def _render_clause(fname: str, vals: list, neg: bool) -> str:
    vv = [_qv(Val(v)) for v in vals]
    body = f"{fname}:" + (vv[0] if len(vv) == 1 else "(" + " OR ".join(vv) + ")")
    return ("-" if neg else "") + body


def rewrite(text: str, op: str, fieldname: str, value: str) -> str:
    """add | remove | only | exclude one value of one field in the top-level conjunction, keeping the rest as typed."""
    if op not in ("add", "remove", "only", "exclude"):
        raise ValueError("op must be add, remove, only or exclude")
    name, attr = canonical_field(fieldname)
    shown = name if not attr else (fieldname if fieldname.startswith("@") else "@" + fieldname)
    p = parse(text)
    node = p.node
    items = top_items(node)
    if isinstance(node, Or):
        if op == "remove":
            return text
        clause = _render_clause(shown, [value], op == "exclude")
        return f"({text.strip()}) {clause}"

    def same(a: str, b: str) -> bool:
        if name == "level":
            ca = LEVEL_ALIASES.get(fold(a)) or a.upper()
            cb = LEVEL_ALIASES.get(fold(b)) or b.upper()
            return ca == cb
        return fold(a) == fold(b)
    spans = _item_spans(text, node)
    pieces = [text[a:b] for a, b in spans] if spans is not None else [normalize(it) for it in items]
    out: list = []
    pos_clause = None
    neg_has = False
    for it, piece in zip(items, pieces):
        neg = isinstance(it, Not)
        x = it.item if neg else it
        if isinstance(x, Field) and x.name == name and x.op in ("eq", "glob"):
            vals = [v.text for v in x.values]
            label = x.typed or shown
            if op == "only":
                continue
            if neg:
                keep = [v for v in vals if not (op in ("add", "remove") and same(v, value))]
                if op == "exclude" and any(same(v, value) for v in vals):
                    neg_has = True
                if keep:
                    out.append(piece if keep == vals else _render_clause(label, keep, True))
            else:
                keep = [v for v in vals if not (op in ("exclude", "remove") and same(v, value))]
                if op == "add" and pos_clause is None:
                    if not any(same(v, value) for v in keep):
                        keep = keep + [value]
                    pos_clause = len(out)
                if keep:
                    out.append(piece if keep == vals else _render_clause(label, keep, False))
            continue
        out.append(piece)
    if op == "only":
        out.append(_render_clause(shown, [value], False))
    elif op == "add" and pos_clause is None:
        out.append(_render_clause(shown, [value], False))
    elif op == "exclude" and not neg_has:
        out.append(_render_clause(shown, [value], True))
    return " ".join(s for s in out if s.strip())


def _item_spans(text: str, node: Node):
    """Source spans [a, b) of the top-level conjunction items, found by re-tokenizing (items are separated at depth 0);
    None when the shape is unusual (then the items are re-rendered from their normalized text)."""
    items = top_items(node)
    if not items:
        return []
    groups: list = []
    cur: list = []
    depth = 0
    for t in tokenize(text):
        if t.kind == "EOF":
            break
        if depth == 0 and t.kind == "AND":
            if cur:
                groups.append(cur); cur = []
            continue
        if depth == 0 and cur and t.ws and t.kind in ("LP", "QUOTED", "WORD", "FIELD", "NOT", "NEG") and cur[-1].kind not in ("NOT", "NEG", "COLON", "CMP"):
            groups.append(cur); cur = []
        cur.append(t)
        if t.kind == "LP":
            depth += 1
        elif t.kind == "RP":
            depth -= 1
    if cur:
        groups.append(cur)
    if len(groups) != len(items):
        return None
    return [(g[0].pos, g[-1].pos + g[-1].len) for g in groups]


# ---------------------------------------------------------------- applications (watchover.applications)
def _vals(names, wild: bool) -> list:
    out = []
    for n in sorted(names):
        w = frozenset(i for i, c in enumerate(n) if c in "*?") if wild else frozenset()
        out.append(Val(n, w, False))
    return out


def expand_apps(node: Node, resolve) -> tuple:
    """app:<name> -> its member hosts OR its member services, as `resolve(value, wild)` names them: (hosts, exact services, service
    globs) of the visible applications matching the value, or None when there is none (then the clause keeps its old meaning, the
    service name, with a warning). An application without members matches nothing. Returns (node, warnings, fingerprint): the
    fingerprint changes when a membership does, so cached answers keyed by the query text are not reused across it."""
    warns: list = []
    fp: list = []

    def leaf(f: Field) -> Node:
        if f.op not in ("eq", "glob", "present"):
            return f
        vals = [Val("*", frozenset({0}))] if f.op == "present" else f.values
        alts: list = []
        for v in vals:
            hit = resolve(v.text, v.has_wild) if resolve is not None else None
            if hit is None:
                alts.append(Field("service", "glob" if v.has_wild else "eq", [v], f.pos, f.len, f.typed, False))
                if resolve is not None and f.op != "present":
                    warns.append(warning("app_unknown", v=v.text))
                continue
            hosts, exact, globs = hit
            fp.append("%s|%s|%s|%s" % (v.text, ",".join(sorted(hosts)), ",".join(sorted(exact)), ",".join(globs)))
            parts: list = []
            if hosts:
                parts.append(Field("host", "eq", _vals(hosts, False), f.pos, f.len, f.typed, False))
            svcs = _vals(exact, False) + _vals(globs, True)
            if svcs:
                parts.append(Field("service", "glob" if globs else "eq", svcs, f.pos, f.len, f.typed, False))
            if not parts:
                parts.append(Field("host", "eq", [Val(NO_APP_MATCH)], f.pos, f.len, f.typed, False))
            alts.extend(parts)
        return alts[0] if len(alts) == 1 else Or(alts)

    def walk(n: Node) -> Node:
        if isinstance(n, Field) and n.name == "app" and not n.attr:
            return leaf(n)
        if isinstance(n, And):
            return And([walk(x) for x in n.items])
        if isinstance(n, Or):
            return Or([walk(x) for x in n.items])
        if isinstance(n, Not):
            return Not(walk(n.item))
        return n
    if not any(isinstance(x, Field) and x.name == "app" and not x.attr for x in _leaves(node)):
        return node, [], ""
    out = walk(node)
    return out, warns, (hashlib.sha1("\n".join(fp).encode()).hexdigest()[:10] if fp else "")


NO_APP_MATCH = "\x00no-application-member"
