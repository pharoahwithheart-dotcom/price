"""Playbook: learns WHICH setups keep working, trades only by those, and drops them when they stop.

Every finished confirmed trade is a pretend trade with a known result, so the history is a table of (what the setup looked like) ->
(did price CONTINUE the way the signal pointed). From it we mine RULES: one condition ("trend into the signal over 10 candles = against it")
or two conditions together ("... AND confirmation case 1"; this is how two candle characteristics are paired). Each rule also has a direction:
trade WITH the signal or AGAINST it, whichever way the history leans.

A rule is only ACTIVE when it passes every one of these (the point is to refuse luck):
  1. enough trades: at least MIN_N matching trades;
  2. significant: on the older 70% of the history its win rate's 95% range, widened for how many groups were tested, is above 50%;
  3. out of sample: on the newest 30% (never used to pick the rule or its direction) it still wins more than half, and over the whole
     history it beats the break-even rate;
  4. persistent: each third of the history with 5+ trades wins more than half, and so do its latest RECENT matches.
An ACTIVE rule is RETIRED when its latest matches fall below DECAY, or its whole-history win rate drops under break-even. A retired
rule can only come back by passing everything above again. The data window is the newest WINDOW trades, so old behaviour ages out and a
new pattern that passes the same tests is learned automatically. replay() runs this whole learn/trade/retire process over your history
one step at a time, each time using only the past, so you can see whether adapting would really have helped before trusting it.
"""
import json, math, os, time
from statistics import NormalDist

import adapt
import paper

WINDOW = int(os.getenv("PLAYBOOK_WINDOW", "600"))  # newest decided confirmed trades used for learning
MIN_N = int(os.getenv("PLAYBOOK_MIN_N", "30"))  # trades a rule must match
RECENT, RECENT_MIN, DECAY = 20, 10, 0.45
MAX_RULES = 12
MAX_CONDS = 120  # single conditions considered when pairing
REFRESH = 300  # seconds between re-learning passes
MODES = ("off", "direction", "only")
_wilson = adapt._wilson
LEVELS = ("strict", "balanced", "relaxed")  # how hard a rule must prove itself on the older part of the history


def _tail(w: int, n: int) -> float:
    """Two-sided chance of a result at least this lopsided from a fair coin (exact binomial)."""
    k = max(w, n - w)
    p = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return min(1.0, 2 * p)


def _be() -> float:
    return paper.BREAKEVEN


# ------------------------------------------------------------------ the data
def load(db, limit: int = WINDOW) -> list:
    """Newest `limit` decided confirmed trades, oldest first: {"t", "f" (features in the signal's frame), "cont" (price went the signal's way)}."""
    with db() as c:
        raw = [dict(r) for r in c.execute(
            "SELECT symbol, signal, direction, sig_dir, entry_time, entry_price, results, ctx, hist FROM paper "
            "WHERE settled=1 AND signal LIKE '%confirmation case%' ORDER BY entry_time DESC LIMIT ?", (limit * 2,))]
    out = []
    for r in reversed(raw):
        f = adapt._features(r)
        try:
            ex = list(json.loads(r["results"] or "{}").values())[0]["exit"]
        except Exception:  # noqa
            continue
        if not f or ex == r["entry_price"]:
            continue
        f["confirmation case"] = r["signal"].split("confirmation ")[-1]
        s = 1 if (r["sig_dir"] or r["direction"]) == "up" else -1
        out.append({"t": r["entry_time"], "f": f, "cont": s * (ex - r["entry_price"]) > 0})
    return out[-limit:]


# ------------------------------------------------------------------ mining
def _key(conds, direction) -> str:
    return " & ".join(f"{k}={v}" for k, v in sorted(conds)) + " => " + direction


def words(conds, direction) -> str:
    when = " AND ".join(f"{k}: {v}" for k, v in sorted(conds))
    return f"When {when} -> trade {'WITH' if direction == 'with' else 'AGAINST'} the signal"


def _masks(rows):
    n = len(rows)
    idx, cont = {}, 0
    for i, r in enumerate(rows):
        if r["cont"]:
            cont |= 1 << i
        for k, v in r["f"].items():
            idx[(k, v)] = idx.get((k, v), 0) | (1 << i)
    full = (1 << n) - 1
    cut = int(n * 0.7)
    tr = (1 << cut) - 1
    thirds = [((1 << (n * (j + 1) // 3)) - 1) ^ ((1 << (n * j // 3)) - 1) for j in range(3)]
    return idx, cont, full, tr, full ^ tr, thirds


def _stats(mask, direction, cont, tr, te, thirds, rows, z):
    n = mask.bit_count()
    up = (mask & cont).bit_count()
    wins = up if direction == "with" else n - up
    ntr, utr = (mask & tr).bit_count(), (mask & tr & cont).bit_count()
    nte, ute = (mask & te).bit_count(), (mask & te & cont).bit_count()
    wtr = utr if direction == "with" else ntr - utr
    wte = ute if direction == "with" else nte - ute
    chunks = []
    for t in thirds:
        k, u = (mask & t).bit_count(), (mask & t & cont).bit_count()
        chunks.append((k, u if direction == "with" else k - u))
    idx = [i for i in range(len(rows)) if mask >> i & 1][-RECENT:]
    rn = len(idx)
    rw = sum((rows[i]["cont"] if direction == "with" else not rows[i]["cont"]) for i in idx)
    return {"n": n, "wins": wins, "rate": wins / n if n else 0, "ntr": ntr, "wtr": wtr, "nte": nte, "wte": wte,
            "tr_low": _wilson(wtr, ntr, z)[0] if ntr else 0, "all_low": _wilson(wins, n, 1.96)[0] if n else 0,
            "all_high": _wilson(wins, n, 1.96)[1] if n else 0, "chunks": chunks, "rn": rn, "rw": rw}


def _verdict(s, be) -> list:
    """The reasons a rule is NOT active yet (empty list = passes everything)."""
    why = []
    if s["n"] < MIN_N:
        why.append(f"only {s['n']} matching trades (needs {MIN_N})")
    if s["ntr"] < 20 or s["nte"] < 8:
        why.append("not enough trades in the older/newer parts of the history to test it out of sample")
    elif not s["tr_ok"]:
        why.append("not significant after allowing for how many groups were tested")
    elif s["nte"] and s["wte"] / s["nte"] <= 0.5:
        why.append("did not hold on the newest 30% of trades")
    if s["n"] and s["rate"] <= be:
        why.append(f"win rate {round(100 * s['rate'])}% is not above break-even {round(100 * be)}%")
    big = [(k, w) for k, w in s["chunks"] if k >= 5]
    if len(big) < 2 or any(w / k <= 0.5 for k, w in big):
        why.append("not consistent across the three parts of the history")
    if s["rn"] >= RECENT_MIN and s["rw"] / s["rn"] <= 0.5:
        why.append("its latest trades are losing")
    return why


def mine(rows: list, level: str = "balanced") -> dict:
    """All rules with 1 or 2 conditions. Returns {"passed": [...best first, non-overlapping...], "watch": [...close but not passing...], "tested": N, "z": z}."""
    n, be = len(rows), _be()
    if n < MIN_N:
        return {"passed": [], "watch": [], "tested": 0, "z": 1.96, "n": n}
    idx, cont, full, tr, te, thirds = _masks(rows)
    conds = sorted(((k, m) for k, m in idx.items() if m.bit_count() >= MIN_N), key=lambda kv: -kv[1].bit_count())[:MAX_CONDS]
    groups = [([k], m) for k, m in conds]
    for i in range(len(conds)):
        for j in range(i + 1, len(conds)):
            (k1, m1), (k2, m2) = conds[i], conds[j]
            if k1[0] == k2[0]:
                continue
            m = m1 & m2
            if m.bit_count() >= MIN_N:
                groups.append(([k1, k2], m))
    z = max(1.96, NormalDist().inv_cdf(1 - 0.025 / max(len(groups), 1))) if level == "strict" else 1.96
    found = []
    for cs, m in groups:
        ntr, utr = (m & tr).bit_count(), (m & tr & cont).bit_count()
        direction = "with" if utr * 2 >= ntr else "against"  # chosen on the OLDER part only
        s = _stats(m, direction, cont, tr, te, thirds, rows, z)
        s["p"] = _tail(s["wtr"], ntr) if ntr else 1.0
        s["tr_ok"] = s["tr_low"] > 0.5
        found.append({"conds": cs, "dir": direction, "mask": m, "stats": s})
    if level == "balanced":  # Benjamini-Hochberg: keep the share of false discoveries among the groups that pass near 10%
        order = sorted(range(len(found)), key=lambda i: found[i]["stats"]["p"])
        cut = max([r for r, i in enumerate(order, 1) if found[i]["stats"]["p"] <= 0.10 * r / len(found)] or [0])
        for r, i in enumerate(order, 1):
            found[i]["stats"]["tr_ok"] = r <= cut
    for g in found:
        g["why"] = _verdict(g["stats"], be)
    ok = sorted((g for g in found if not g["why"]), key=lambda g: -g["stats"]["all_low"])
    kept = []
    for g in ok:
        if any(h["dir"] == g["dir"] and (g["mask"] & h["mask"]).bit_count() > 0.8 * min(g["mask"].bit_count(), h["mask"].bit_count()) for h in kept):
            continue  # nearly the same trades as a better rule already kept
        kept.append(g)
        if len(kept) >= MAX_RULES:
            break
    watch = sorted((g for g in found if g["why"]), key=lambda g: -abs(g["stats"]["rate"] - 0.5) * math.sqrt(g["stats"]["n"]))[:10]
    return {"passed": kept, "watch": watch, "tested": len(groups), "z": z, "n": n}


def _public(g) -> dict:
    s = g["stats"]
    return {"key": _key(g["conds"], g["dir"]), "conds": [list(c) for c in g["conds"]], "dir": g["dir"], "text": words(g["conds"], g["dir"]),
            "n": s["n"], "wins": s["wins"], "rate": round(100 * s["rate"]), "low": round(100 * s["all_low"]), "high": round(100 * s["all_high"]),
            "test_n": s["nte"], "test_rate": round(100 * s["wte"] / s["nte"]) if s["nte"] else None,
            "recent_n": s["rn"], "recent_rate": round(100 * s["rw"] / s["rn"]) if s["rn"] else None,
            "thirds": [round(100 * w / k) if k else None for k, w in s["chunks"]], "why": g.get("why", [])}


def evaluate(rule: dict, rows: list) -> dict:
    """Current stats of a stored rule (conds + dir) on `rows`, without re-selecting anything."""
    if not rows:
        return {"n": 0, "rate": 0, "rn": 0, "rw": 0}
    idx, cont, full, tr, te, thirds = _masks(rows)
    m = full
    for k, v in rule["conds"]:
        m &= idx.get((k, v), 0)
    return _stats(m, rule["dir"], cont, tr, te, thirds, rows, 1.96)


def evolve(active: dict, rows: list, level: str = "balanced"):
    """One learning pass. `active` is {key: rule}. Returns (new_active, events, mined). Events are ("learned"|"retired", text)."""
    mined = mine(rows, level)
    be, new, events = _be(), {}, []
    for g in mined["passed"]:
        p = _public(g)
        if p["key"] not in active:
            events.append(("learned", p["text"] + f" ({p['rate']}% of {p['n']} trades)"))
        new[p["key"]] = {**p, "since": active.get(p["key"], {}).get("since", time.time())}
    for key, r in active.items():
        if key in new:
            continue
        s = evaluate(r, rows)
        decayed = s["rn"] >= RECENT_MIN and s["rw"] / s["rn"] < DECAY
        below = s["n"] >= MIN_N and s["rate"] < be
        if decayed or below:
            why = (f"its latest {s['rn']} trades won only {round(100 * s['rw'] / s['rn'])}%" if decayed
                   else f"its win rate fell to {round(100 * s['rate'])}% (break-even {round(100 * be)}%)")
            events.append(("retired", r["text"] + " - " + why))
        else:
            new[key] = {**r, "n": s["n"], "rate": round(100 * s["rate"]), "recent_n": s["rn"],
                        "recent_rate": round(100 * s["rw"] / s["rn"]) if s["rn"] else None}  # no longer top-ranked, still healthy: stays
    return new, events, mined


# ------------------------------------------------------------------ using it
def _matches(rule: dict, f: dict) -> bool:
    return all(f.get(k) == v for k, v in rule["conds"])


def decide_with(active: dict, f: dict) -> dict:
    """{"action": "none"|"match"|"conflict", "dir": "with"|"against"|None, "rules": [text...]}"""
    hit = [r for r in active.values() if _matches(r, f)]
    if not hit:
        return {"action": "none", "dir": None, "rules": []}
    dirs = {r["dir"] for r in hit}
    if len(dirs) > 1:
        return {"action": "conflict", "dir": None, "rules": [r["text"] for r in hit]}
    return {"action": "match", "dir": dirs.pop(), "rules": [r["text"] for r in hit]}


def replay(rows: list, level: str = "balanced", step: int = 10, warm: int = 80) -> dict:
    """Walk through the history in order. Before each block of `step` trades, re-learn from the window of trades BEFORE it only, then see what the
    playbook would have done with the block. Compares: every confirmed trade traded its case default, vs only the trades a rule matched, traded as the rule says."""
    be = _be()
    active, events = {}, []
    base = [0, 0]  # wins, losses of every trade by its case's default direction
    book = [0, 0]  # the playbook's trades
    same = [0, 0]  # the default direction on those SAME trades
    skipped = 0
    for i in range(warm, len(rows), step):
        active, ev, _ = evolve(active, rows[max(0, i - WINDOW):i], level)
        events += [(i, k, t) for k, t in ev]
        for r in rows[i:i + step]:
            case = r["f"].get("confirmation case")
            dflt = "with" if case in ("case 1", "case 3") else "against"
            bw = r["cont"] if dflt == "with" else not r["cont"]
            base[0 if bw else 1] += 1
            d = decide_with(active, r["f"])
            if d["action"] != "match":
                skipped += 1
                continue
            pw = r["cont"] if d["dir"] == "with" else not r["cont"]
            book[0 if pw else 1] += 1
            same[0 if bw else 1] += 1

    def row(c):
        n = c[0] + c[1]
        lo, hi = _wilson(c[0], n) if n else (0, 0)
        return {"trades": n, "wins": c[0], "rate": round(100 * c[0] / n) if n else None, "low": round(100 * lo), "high": round(100 * hi),
                "net": round(c[0] * paper.PAYOUT - c[1], 1)}
    b, p, s = row(base), row(book), row(same)
    if p["trades"] < 20:
        verdict = "Too few playbook trades in this history to judge (need 20+): keep collecting trades."
    elif p["low"] / 100 > be:
        verdict = "Adapting would have worked on this history: the playbook's win rate is above break-even with 95% confidence."
    elif p["rate"] / 100 > be:
        verdict = "Promising but not proven: the playbook beat break-even, but the range still includes it."
    else:
        verdict = "Adapting would NOT have helped on this history. Do not rely on it."
    return {"trades": len(rows), "start": warm, "all_default": b, "playbook": p, "default_on_same_trades": s, "skipped": skipped,
            "events": [{"at": i, "kind": k, "text": t} for i, k, t in events][-30:], "verdict": verdict, "breakeven": round(100 * be)}


# ------------------------------------------------------------------ storage and the loop
def init(db):
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS playbook_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT)")


def _get(db, key, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(db, key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def mode(db) -> str:
    m = _get(db, "playbook_mode", "off")
    return m if m in MODES else "off"


def set_mode(db, m: str):
    if m not in MODES:
        raise ValueError("mode must be off, direction or only")
    _set(db, "playbook_mode", m)
    log(db, "mode", f"Playbook mode set to {m}")


def level(db) -> str:
    v = _get(db, "playbook_level", "balanced")
    return v if v in LEVELS else "balanced"


def set_level(db, v: str):
    if v not in LEVELS:
        raise ValueError("level must be strict, balanced or relaxed")
    _set(db, "playbook_level", v)
    log(db, "level", f"Strictness set to {v}")


def active(db) -> dict:
    try:
        return json.loads(_get(db, "playbook_active", "{}") or "{}")
    except ValueError:
        return {}


def log(db, kind, text):
    with db() as c:
        c.execute("INSERT INTO playbook_log(ts,kind,text) VALUES(?,?,?)", (time.time(), kind, text))
        c.execute("DELETE FROM playbook_log WHERE id < (SELECT MAX(id) FROM playbook_log) - 300")


def refresh(db, notify=None) -> dict:
    """Learn from the history now; store the active rules and what was learned or retired; tell the phone about changes."""
    rows = load(db)
    new, events, mined = evolve(active(db), rows, level(db))
    _set(db, "playbook_active", json.dumps(new))
    _set(db, "playbook_watch", json.dumps({"at": time.time(), "n": len(rows), "tested": mined["tested"], "z": round(mined["z"], 2),
                                           "watch": [_public(g) for g in mined["watch"]]}))
    _set(db, "playbook_seen", str(len(rows)) + ":" + str(rows[-1]["t"] if rows else 0))
    for kind, text in events:
        log(db, kind, text)
    if notify and events:  # ONE quiet message per learning pass, not one per rule
        learned, retired = [t for k, t in events if k == "learned"], [t for k, t in events if k == "retired"]
        body = (f"Learned {len(learned)} rule(s). " if learned else "") + (f"Retired {len(retired)} rule(s). " if retired else "")
        body += " | ".join((learned + retired)[:2])
        try:
            notify("Playbook changed", body[:300], "books", False)
        except Exception:  # noqa
            pass
    return {"rows": len(rows), "events": events}


_last = [0.0]


def step(db, notify=None):
    """Background loop: re-learn at most every REFRESH seconds, and only when there are new finished trades."""
    if time.time() - _last[0] < REFRESH:
        return
    _last[0] = time.time()
    rows_key = None
    with db() as c:
        r = c.execute("SELECT COUNT(*), MAX(entry_time) FROM paper WHERE settled=1 AND signal LIKE '%confirmation case%'").fetchone()
        rows_key = f"{r[0]}:{r[1]}"
    if _get(db, "playbook_key") == rows_key:
        return
    refresh(db, notify)
    _set(db, "playbook_key", rows_key)


def decide(db, features: dict) -> dict:
    """What the playbook says about a live setup. Only meaningful when the mode is not off."""
    if mode(db) == "off":
        return {"action": "off", "dir": None, "rules": []}
    return decide_with(active(db), features)


def apply(db, sig_dir: str, default_dir: str, features: dict):
    """The trader's hook. Returns (direction to trade or None for no trade, note).
    direction mode: a matching rule decides the direction; otherwise the usual (adapted) default stands.
    only mode: trade only when a rule matches; no match or a conflict means no trade."""
    m = mode(db)
    if m == "off":
        return default_dir, ""
    d = decide_with(active(db), features)
    if d["action"] == "match":
        want = sig_dir if d["dir"] == "with" else ("down" if sig_dir == "up" else "up")
        return want, "playbook: " + "; ".join(d["rules"][:2])
    if m == "only":
        return None, "playbook: no active rule matches this setup" if d["action"] == "none" else "playbook: active rules disagree on this setup"
    return default_dir, ""


def signals_on(db) -> bool:
    return _get(db, "playbook_signals", "1") == "1"


def set_signals(db, on: bool):
    _set(db, "playbook_signals", "1" if on else "0")


def signal(db, sig_dir: str, features: dict):
    """An ENTRY SIGNAL for a person trading by hand: the direction to enter ("up"/"down") when active rules match and agree, else None.
    Independent of the mode and of the auto-trade switch, so it works as a pure alert."""
    if not signals_on(db):
        return None
    d = decide_with(active(db), features)
    if d["action"] != "match":
        return None
    return {"dir": sig_dir if d["dir"] == "with" else ("down" if sig_dir == "up" else "up"), "rules": d["rules"]}


CONF_KEYS = {"confirmation candle colour", "ending wick vs (body + starting wick)", "confirmation body size", "where the confirmation closed",
             "confirmation size vs signal candle", "confirmation close vs signal candle", "confirmation case"}


def heads_up(db, sig_dir: str, features: dict):
    """Right after the SIGNAL candle closes the confirmation candle is still unknown. An active rule is still POSSIBLE when every condition on the
    signal side holds (conditions on the confirmation candle stay open). Returns {"dir": "up"|"down"|None (rules disagree), "rules": [...]} or None."""
    if not signals_on(db):
        return None
    poss = [r for r in active(db).values() if all(k in CONF_KEYS or features.get(k) == v for k, v in r["conds"])]
    if not poss:
        return None
    flip = {"up": "down", "down": "up"}
    dirs = {sig_dir if r["dir"] == "with" else flip[sig_dir] for r in poss}
    return {"dir": dirs.pop() if len(dirs) == 1 else None, "rules": [r["text"] for r in poss]}


def overview(db) -> dict:
    act = sorted(active(db).values(), key=lambda r: -r.get("low", 0))
    try:
        w = json.loads(_get(db, "playbook_watch", "{}") or "{}")
    except ValueError:
        w = {}
    with db() as c:
        ev = [dict(r) for r in c.execute("SELECT ts, kind, text FROM playbook_log ORDER BY id DESC LIMIT 15")]
        n = c.execute("SELECT COUNT(*) FROM paper WHERE settled=1 AND signal LIKE '%confirmation case%'").fetchone()[0]
    return {"mode": mode(db), "level": level(db), "signals": signals_on(db), "min_n": MIN_N, "window": WINDOW, "breakeven": round(100 * _be()), "decided_trades": n,
            "active": act, "watch": w.get("watch", []), "tested": w.get("tested"), "z": w.get("z"), "learned_at": w.get("at"),
            "used": w.get("n"), "events": ev,
            "note": (f"Only {n} finished confirmed trades so far: a rule needs {MIN_N}+ matching trades, so expect nothing to qualify until there are "
                     "a few hundred." if n < 200 else "")}
