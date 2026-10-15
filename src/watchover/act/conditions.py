"""`when` / `expect` / `dry_expect` expressions of runbooks and templates (docs/design/action-agent.md §6.4): a small
recursive-descent parser, never eval. Unknown names are rejected at lint time; a missing value at run time makes a comparison
false (fail closed).

    expr   := or_expr ;  or_expr := and_expr ("or" and_expr)* ;  and_expr := term ("and" term)*
    term   := "not" term | "(" expr ")" | cmp | flag
    cmp    := ref op literal              op := < <= > >= == != in
    flag   := ref                          (steps.ID.ran|ok|failed|skipped, or a bare ref: true when non-empty)
    ref    := steps.ID.CAPTURE[.FIELD] | case.(env|severity|source) | inventory.ATTR | CAPTURE (bare, inside expect)
    literal:= number | "quoted string" | [literal, ...]
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

TOKEN = re.compile(r"""\s*(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>"(?:[^"\\]|\\.)*")|(?P<op><=|>=|==|!=|<|>)|(?P<p>[()\[\],])|(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*))""")
KEYWORDS = ("and", "or", "not", "in")
CASE_FIELDS = ("env", "severity", "source", "host", "service")
STEP_FLAGS = ("ran", "ok", "failed", "skipped")
MAX_LEN = 500


class ConditionError(ValueError):
    pass


def tokenize(text: str) -> List[Tuple[str, str]]:
    if text is None or len(text) > MAX_LEN:
        raise ConditionError("expression missing or too long")
    out, pos = [], 0
    s = text.strip()
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise ConditionError(f"unexpected character at {pos}: {s[pos:pos + 10]!r}")
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "name" and val in KEYWORDS:
            kind = val
        out.append((kind, val))
        pos = m.end()
        while pos < len(s) and s[pos].isspace():
            pos += 1
    return out


class _P:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        tk = self.peek()
        if tk[0] is None or (kind and tk[0] != kind) or (val and tk[1] != val):
            raise ConditionError(f"expected {val or kind}, got {tk[1]!r}")
        self.i += 1
        return tk

    def expr(self):
        node = self.and_()
        while self.peek()[0] == "or":
            self.take("or")
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.term()
        while self.peek()[0] == "and":
            self.take("and")
            node = ("and", node, self.term())
        return node

    def term(self):
        k, v = self.peek()
        if k == "not":
            self.take("not")
            return ("not", self.term())
        if k == "p" and v == "(":
            self.take("p", "(")
            node = self.expr()
            self.take("p", ")")
            return node
        if k != "name":
            raise ConditionError(f"expected a reference, got {v!r}")
        ref = self.take("name")[1]
        k2, v2 = self.peek()
        if k2 == "op" or k2 == "in":
            op = self.take()[1]
            return ("cmp", op, ref, self.literal())
        return ("flag", ref)

    def literal(self):
        k, v = self.peek()
        if k == "num":
            self.take()
            return float(v) if "." in v else int(v)
        if k == "str":
            self.take()
            return bytes(v[1:-1], "utf-8").decode("unicode_escape") if "\\" in v else v[1:-1]
        if k == "p" and v == "[":
            self.take("p", "[")
            items = [self.literal()]
            while self.peek() == ("p", ","):
                self.take("p", ",")
                items.append(self.literal())
            self.take("p", "]")
            return items
        raise ConditionError(f"expected a literal, got {v!r}")


def parse(text: str):
    p = _P(tokenize(text))
    node = p.expr()
    if p.peek()[0] is not None:
        raise ConditionError(f"unexpected {p.peek()[1]!r}")
    return node


def refs(node) -> Set[str]:
    if node[0] in ("and", "or"):
        return refs(node[1]) | refs(node[2])
    if node[0] == "not":
        return refs(node[1])
    if node[0] == "cmp":
        return {node[2]}
    return {node[1]}


def check(text: str, steps: Iterable[str] = (), captures: Optional[Dict[str, Iterable[str]]] = None, bare: Iterable[str] = (),
          inventory_ok: bool = True) -> List[str]:
    """Lint: parse errors and unknown names. `steps` are step ids, `captures` {step id: capture names} (None = any capture),
    `bare` the capture names allowed without a prefix (inside a step's expect)."""
    try:
        node = parse(text)
    except ConditionError as e:
        return [f"parse: {e}"]
    steps, bare = set(steps), set(bare)
    errs = []
    for r in sorted(refs(node)):
        parts = r.split(".")
        if parts[0] == "steps":
            if len(parts) < 3 or parts[1] not in steps:
                errs.append(f"unknown step in {r}")
            elif captures is not None and parts[2] not in STEP_FLAGS and parts[2] != "out" \
                    and parts[2] not in set(captures.get(parts[1]) or ()):
                errs.append(f"unknown capture {r}")
        elif parts[0] == "case":
            if len(parts) != 2 or parts[1] not in CASE_FIELDS:
                errs.append(f"unknown case field {r}")
        elif parts[0] == "inventory":
            if len(parts) != 2 or not inventory_ok:
                errs.append(f"bad inventory reference {r}")
        elif len(parts) == 1 and parts[0] in bare:
            continue
        else:
            errs.append(f"unknown name {r}")
    return errs


def _lookup(ref: str, ctx: Dict[str, Any]):
    parts = ref.split(".")
    if parts[0] in ("steps", "case", "inventory"):
        cur: Any = ctx.get(parts[0]) or {}
        for p in parts[1:]:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return cur
    bare = ctx.get("captures") or {}
    cur = bare
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d+(?:[.,]\d+)?", v.strip()):
        return float(v.strip().replace(",", "."))
    return None


def _cmp(op, a, b) -> bool:
    if a is None:
        return False
    if op == "in":
        return isinstance(b, list) and any(_cmp("==", a, x) for x in b)
    if op in ("==", "!="):
        na, nb = _num(a), _num(b)
        eq = (na == nb) if (na is not None and nb is not None) else (str(a) == str(b))
        return eq if op == "==" else not eq
    na, nb = _num(a), _num(b)
    if na is None or nb is None:
        return False
    return {"<": na < nb, "<=": na <= nb, ">": na > nb, ">=": na >= nb}[op]


def evaluate(node_or_text, ctx: Dict[str, Any]) -> bool:
    node = parse(node_or_text) if isinstance(node_or_text, str) else node_or_text
    k = node[0]
    if k == "and":
        return evaluate(node[1], ctx) and evaluate(node[2], ctx)
    if k == "or":
        return evaluate(node[1], ctx) or evaluate(node[2], ctx)
    if k == "not":
        return not evaluate(node[1], ctx)
    if k == "cmp":
        return _cmp(node[1], _lookup(node[2], ctx), node[3])
    v = _lookup(node[1], ctx)
    return bool(v) if not isinstance(v, (int, float)) or isinstance(v, bool) else True
