"""The deterministic matcher (docs/design/action-agent.md §8.2; C55): always runs, needs no LLM, and is a selector of its own.

    candidates(case) = approved runbooks visible to the case's team, applies_to.os matching the host, and a hard match:
                       match.sources ∋ case source, or a match.templates glob hits the root template / title
    score(rb) = w1·trig + w2·sim + w3·hist + w4·fill + w5·scope        default w = (0.40, 0.20, 0.20, 0.10, 0.10)
    list      = top 5 with score ≥ 0.30, plus NONE and ESCALATE
    pick      = top1 when score ≥ 0.60 and top1 − top2 ≥ 0.15, else ambiguous
    params    = every `from` source of a parameter in order; several distinct values ⇒ ambiguous, and the proposal still names one
                by a fixed precedence (trusted / trusted_signed provenance first, then `from` order) so its parameter agreement
                is measured like any selector's

The rules proposal names top1 (or ESCALATE when nothing qualifies) and lists what is ambiguous ("choice", parameter names)."""
from __future__ import annotations

import fnmatch
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from ..playbook import similarity
from .evidence import LEVEL_OF

WEIGHTS = (0.40, 0.20, 0.20, 0.10, 0.10)
LIST_MIN, PICK_MIN, PICK_GAP, TOP_N = 0.30, 0.60, 0.15, 5
NONE, ESCALATE = "NONE", "ESCALATE"
SELECTOR = "rules@1"


def wilson_lower(ok: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = ok / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d)


def _glob_any(patterns, *texts) -> bool:
    for p in patterns or []:
        pl = str(p).lower()
        for t in texts:
            if t and fnmatch.fnmatch(str(t).lower(), pl if any(ch in pl for ch in "*?[") else f"*{pl}*"):
                return True
    return False


def _list(v) -> List[str]:
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else ([str(v)] if v not in (None, "") else [])


class Matcher:
    def __init__(self, act):
        self.act = act
        self.db = act.db

    def weights(self) -> Tuple[float, ...]:
        try:
            r = self.db._exec("SELECT weights FROM act_matcher_weights WHERE adopted=1 AND role='champion' ORDER BY id DESC LIMIT 1")
            if r:
                w = tuple(float(x) for x in json.loads(r[0]["weights"]))
                if len(w) == 5:
                    return w
        except Exception:  # noqa: BLE001
            pass
        return WEIGHTS

    # ---------------------------------------------------------------- the case as the matcher sees it
    def context(self, case: dict) -> dict:
        cid = case["id"]
        trigs = self.db._exec("SELECT source, facts FROM act_triggers WHERE case_id=? ORDER BY id", (cid,))
        facts: Dict[str, Any] = {}
        sources = []
        for t in trigs:
            sources.append(t["source"])
            try:
                for k, v in json.loads(t["facts"] or "{}").items():
                    facts.setdefault(k, v)
            except ValueError:
                pass
        items = self.act.evidence.items(cid)
        texts = [e["text"] for e in items if e.get("text") and not e.get("quarantined")]
        inv = {}
        try:
            inv = (self.act.svc.inventory.resolve(case.get("host") or "") or {}) if getattr(self.act, "svc", None) is not None and case.get("host") else {}
        except Exception:  # noqa: BLE001
            inv = {}
        return {"case": case, "sources": sources or [case.get("source") or ""], "facts": facts, "texts": texts,
                "root": str(facts.get("root_template") or (texts[0] if texts else "")), "slots": self.act.evidence.slots(cid),
                "inventory": inv, "os": str(inv.get("os") or "").lower()}

    # ---------------------------------------------------------------- candidates and scores
    def runbooks(self, team_id: Optional[int]) -> List[dict]:
        out = []
        for rb in self.db._exec("SELECT * FROM act_runbooks WHERE status='active' AND killed=0 AND current_version IS NOT NULL ORDER BY key"):
            if rb["team_id"] is not None and rb["team_id"] != team_id:
                continue
            v = self.db._exec("SELECT version, body, status FROM act_runbook_versions WHERE runbook_id=? AND version=?", (rb["id"], rb["current_version"]))
            if not v or v[0]["status"] != "approved":
                continue
            try:
                body = json.loads(v[0]["body"] or "{}")
            except ValueError:
                continue
            out.append({"id": rb["id"], "key": rb["key"], "version": int(v[0]["version"]), "title": rb.get("title") or body.get("title", rb["key"]),
                        "team_id": rb["team_id"], "body": body})
        return out

    def hard_match(self, rb: dict, ctx: dict) -> bool:
        m = rb["body"].get("match") or {}
        os_ok = not rb["body"].get("applies_to", {}).get("os") or not ctx["os"] or ctx["os"] in [o.lower() for o in _list(rb["body"]["applies_to"]["os"])]
        if not os_ok:
            return False
        return bool(set(_list(m.get("sources"))) & set(ctx["sources"])) or _glob_any(m.get("templates"), ctx["root"], ctx["case"].get("title"))

    def components(self, rb: dict, ctx: dict) -> Dict[str, float]:
        m = rb["body"].get("match") or {}
        case, facts = ctx["case"], ctx["facts"]
        declared, hits = 0, 0
        if m.get("sources"):
            declared += 1
            hits += bool(set(_list(m["sources"])) & set(ctx["sources"]))
        if m.get("metric"):
            declared += 1
            hits += str(m["metric"]).lower() in str(facts.get("metric") or case.get("family") or "").lower()
        if m.get("anomaly_kinds"):
            declared += 1
            hits += str(facts.get("kind") or "") in _list(m["anomaly_kinds"])
        if m.get("path_prefix"):
            declared += 1
            paths = [s["value"] for k in ("log_dir", "path", "mount") for s in ctx["slots"].get(k, [])]
            hits += any(p.startswith(pp) for p in paths for pp in _list(m["path_prefix"]))
        if m.get("templates"):
            declared += 1
            hits += _glob_any(m["templates"], ctx["root"], case.get("title"), *ctx["texts"][:5])
        trig = hits / declared if declared else 0.0
        refs = [str(t).replace("*", " ") for t in _list(m.get("templates"))]
        try:
            refs += [r["playbook_key"] for r in self.db._exec("SELECT playbook_key FROM act_playbook_links WHERE runbook_key=?", (rb["key"],))]
        except Exception:  # noqa: BLE001
            pass
        subject = ctx["root"] or case.get("title") or ""
        sim = max([similarity(subject, r) for r in refs] + [similarity(case.get("title") or "", r) for r in refs] + [0.0])
        hist = self.history(rb, case)
        fill = self.fill(rb, ctx)
        ap = rb["body"].get("applies_to") or {}
        env_ok = not ap.get("env") or _glob_any(ap.get("env"), case.get("env"), case.get("env_tier"))
        cls_ok = not ap.get("host_class") or _glob_any(ap.get("host_class"), case.get("host_class"), case.get("pool"), case.get("host"))
        scope = 1.0 if env_ok and cls_ok else 0.5 if (env_ok or cls_ok) else 0.0
        return {"trig": round(trig, 4), "sim": round(sim, 4), "hist": round(hist, 4), "fill": round(fill, 4), "scope": scope}

    def history(self, rb: dict, case: dict) -> float:
        """Wilson lower bound of verified-ok among past cases with the same signature where this runbook ran, in this team."""
        tid = case.get("team_id")
        rows = self.db._exec("SELECT r.outcome FROM act_runs r JOIN actions a ON a.id=r.case_id WHERE r.runbook_id=? AND a.signature=? "
                             "AND a.id<>? AND " + ("r.team_id IS NULL" if tid is None else "r.team_id=?") + " AND r.outcome<>''",
                             (rb["id"], case.get("signature") or "", case["id"]) + (() if tid is None else (tid,)))
        if not rows:
            return 0.5
        return wilson_lower(sum(1 for r in rows if r["outcome"] in ("ok", "ok_corrected")), len(rows))

    # ---------------------------------------------------------------- parameters
    def values(self, name: str, spec: dict, ctx: dict) -> List[dict]:
        """Every value the `from` sources produce, in order, with origin and provenance (distinct by value)."""
        out: List[dict] = []
        case, inv = ctx["case"], ctx["inventory"]
        for order, src in enumerate(_list(spec.get("from")) or ["default"]):
            vals: List[Tuple[str, str, str]] = []
            if src.startswith("case."):
                v = case.get(src[5:])
                if v not in (None, ""):
                    vals.append((str(v), src, "trusted"))
            elif src.startswith("evidence."):
                for s in ctx["slots"].get(src[9:], []):
                    vals.append((s["value"], s["eid"], s["provenance"]))
            elif src.startswith("inventory."):
                v = inv.get(src[10:])
                if v not in (None, ""):
                    vals.append((str(v), "inventory", "trusted"))
            elif src == "default" and "default" in spec:
                vals.append((str(spec["default"]) if not isinstance(spec["default"], (int, float)) else spec["default"], "default", "trusted"))
            for v, origin, prov in vals:
                if not self._valid(spec, v, case):
                    continue
                if any(str(x["value"]) == str(v) for x in out):
                    continue
                out.append({"value": v, "origin": origin, "provenance": prov, "order": order})
        return out

    def _valid(self, spec: dict, v, case: dict) -> bool:
        try:
            from .params import validate
            validate(spec, v, {"case_host": case.get("host") or ""})
            return True
        except Exception:  # noqa: BLE001 - a value that fails validation falls back to `ask`
            return False

    def fill(self, rb: dict, ctx: dict) -> float:
        params = rb["body"].get("params") or {}
        if not params:
            return 1.0
        ok = 0
        for name, spec in params.items():
            if any(s.startswith("steps.") for s in _list(spec.get("from"))) or self.values(name, spec, ctx):
                ok += 1
        return ok / len(params)

    def params(self, rb: dict, ctx: dict) -> Tuple[Dict[str, Any], List[str], Dict[str, List[dict]]]:
        """(chosen values, ambiguous names, all values) — precedence: trusted / trusted_signed first, then `from` order."""
        chosen, amb, allv = {}, [], {}
        for name, spec in (rb["body"].get("params") or {}).items():
            vals = self.values(name, spec, ctx)
            allv[name] = vals
            if not vals:
                continue
            if len(vals) > 1:
                amb.append(name)
            best = sorted(vals, key=lambda x: (0 if LEVEL_OF.get(x["provenance"], 0) >= 2 else 1, x["order"]))[0]
            chosen[name] = best["value"]
        return chosen, amb, allv

    # ---------------------------------------------------------------- the proposal
    def propose(self, case: dict) -> dict:
        ctx = self.context(case)
        w = self.weights()
        scored = []
        for rb in self.runbooks(case.get("team_id")):
            if not self.hard_match(rb, ctx):
                continue
            comp = self.components(rb, ctx)
            score = round(sum(wi * comp[k] for wi, k in zip(w, ("trig", "sim", "hist", "fill", "scope"))), 4)
            scored.append((score, rb, comp))
        scored.sort(key=lambda x: (-x[0], x[1]["key"]))
        listed = [x for x in scored if x[0] >= LIST_MIN][:TOP_N]
        cands = [{"key": rb["key"], "version": rb["version"], "title": rb["title"], "score": s, "components": comp} for s, rb, comp in listed]
        if not listed:
            return {"selector": SELECTOR, "choice": ESCALATE, "runbook_key": "", "version": None, "params": {}, "ambiguous": [],
                    "score": 0.0, "candidates": cands, "values": {}}
        top, rb, _comp = listed[0]
        second = listed[1][0] if len(listed) > 1 else 0.0
        chosen, amb, allv = self.params(rb, ctx)
        if not (top >= PICK_MIN and top - second >= PICK_GAP):
            amb = ["choice"] + amb
        return {"selector": SELECTOR, "choice": rb["key"], "runbook_key": rb["key"], "version": rb["version"], "params": chosen,
                "ambiguous": amb, "score": top, "candidates": cands, "values": allv}
