"""Adaptive direction for the confirmation cases (auto-trade's "wait for confirmation" mode).

Every confirmed trade is also a pretend trade with a result, and the OTHER direction's result is simply the reverse of it
(a win is a loss the other way; a draw is a draw). So for each case we can keep score of its default direction over the
last WINDOW settled trades no matter which way we actually traded. When we are 95% sure (Wilson range) that the default
direction wins LESS than half of its decided trades, that case trades the other way; it goes back once we are 95% sure the
default wins more than half again. Below MIN decided trades nothing changes. Each case adapts on its own.
Default directions: case 1 same as the signal, case 2 opposite, case 3 same. Can be switched off on the page.
"""
import json, math, os, re, time
from statistics import NormalDist

WINDOW = int(os.getenv("ADAPT_WINDOW", "40"))
MIN = int(os.getenv("ADAPT_MIN", "8"))
BASE = {"case 1": "same", "case 2": "opposite", "case 3": "same"}
_flip = lambda d: "down" if d == "up" else "up"


def init(db):
    """Add the signal-direction column and fill it in for older rows (their direction rules were: case 1 same, cases 2 and 3 opposite)."""
    with db() as c:
        if "sig_dir" not in {r[1] for r in c.execute("PRAGMA table_info(paper)")}:
            c.execute("ALTER TABLE paper ADD COLUMN sig_dir TEXT")
        for r in c.execute("SELECT id, signal, direction FROM paper WHERE sig_dir IS NULL AND signal LIKE '%confirmation case%'").fetchall():
            c.execute("UPDATE paper SET sig_dir=? WHERE id=?", (r["direction"] if r["signal"].endswith("case 1") else _flip(r["direction"]), r["id"]))


def _get(db, key, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(db, key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def is_on(db) -> bool:
    return _get(db, "adapt", "1") == "1"


def set_on(db, on: bool):
    _set(db, "adapt", "1" if on else "0")


def _wilson(w: int, n: int, z: float = 1.96):
    p = w / n
    d = 1 + z * z / n
    mid, half = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return mid - half, mid + half


def score(db, case: str) -> tuple:
    """(wins, losses) of the case's DEFAULT direction over the last WINDOW settled confirmed trades."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT direction, sig_dir, results FROM paper WHERE settled=1 AND sig_dir IS NOT NULL "
                                           "AND signal LIKE ? ORDER BY entry_time DESC LIMIT ?", (f"%confirmation {case}", WINDOW * 2))]
    w = l = 0
    for r in rows:
        res = list(json.loads(r["results"] or "{}").values())
        if not res or res[0]["r"] == "draw":
            continue
        base_dir = r["sig_dir"] if BASE[case] == "same" else _flip(r["sig_dir"])
        base_win = (res[0]["r"] == "win") == (r["direction"] == base_dir)  # won in its own direction AND that was the default, or lost the other way
        w, l = (w + 1, l) if base_win else (w, l + 1)
        if w + l >= WINDOW:
            break
    return w, l


def state(db, case: str) -> dict:
    w, l = score(db, case)
    n = w + l
    lo, hi = _wilson(w, n) if n else (None, None)
    return {"case": case, "default": BASE[case], "mode": _get(db, f"adapt_{case}", "default"), "n": n, "wins": w, "losses": l,
            "rate": round(100 * w / n) if n else None, "low": round(100 * lo) if n else None, "high": round(100 * hi) if n else None}


def pick(db, case: str, sig_dir: str, default_dir: str):
    """The direction to trade for a matched case, and a short note saying so when adaptation changed it."""
    if not is_on(db):
        return default_dir, ""
    s = state(db, case)
    mode = s["mode"]
    if s["n"] >= MIN:
        if mode == "default" and s["high"] < 50:
            mode = "flipped"
        elif mode == "flipped" and s["low"] > 50:
            mode = "default"
        if mode != s["mode"]:
            _set(db, f"adapt_{case}", mode)
    if mode == "flipped":
        return _flip(default_dir), (f"adapted: its default direction won {s['wins']} of {s['n']} recently, so it trades the other way")
    return default_dir, ""


def overview(db) -> dict:
    return {"on": is_on(db), "min": MIN, "window": WINDOW, "cases": [state(db, k) for k in BASE]}


def case1_whatif(db) -> dict:
    """Score every settled case-1 trade three ways, using its saved confirmation candle and exit price: the signal's own direction (how
    case 1 used to trade), WITH the confirmation candle's colour, and AGAINST it. The same trades, three different direction rules."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT sig_dir, direction, entry_price, results, ctx FROM paper WHERE settled=1 "
                                           "AND signal LIKE '%confirmation case 1'")]
    opts = {k: {"wins": 0, "losses": 0, "draws": 0} for k in ("signal", "with", "against")}
    used = 0
    for r in rows:
        try:
            res = list(json.loads(r["results"] or "{}").values())
            conf = json.loads(r["ctx"])["conf"]
            exit_, entry = res[0]["exit"], r["entry_price"]
        except Exception:  # noqa
            continue
        if not conf or conf[4] == conf[1] or not r["sig_dir"]:
            continue
        used += 1
        colour = "up" if conf[4] > conf[1] else "down"
        for k, d in (("signal", r["sig_dir"]), ("with", colour), ("against", _flip(colour))):
            moved = "up" if exit_ > entry else "down" if exit_ < entry else None
            opts[k]["draws" if moved is None else "wins" if moved == d else "losses"] += 1
    out = []
    for k, label in (("signal", "same as the signal (how it traded before)"), ("with", "with the confirmation candle's colour"),
                     ("against", "against the confirmation candle's colour")):
        o = opts[k]
        n = o["wins"] + o["losses"]
        lo, hi = _wilson(o["wins"], n) if n else (None, None)
        out.append({"name": label, **o, "rate": round(100 * o["wins"] / n) if n else None,
                    "low": round(100 * lo) if n else None, "high": round(100 * hi) if n else None})
    return {"trades": used, "options": out}


def _avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _features(r: dict):
    """What the setup looked like, in the SIGNAL'S frame ("with the signal" = the way the signal pointed), as plain buckets. Uses the
    confirmation candle, the signal candle and (when saved) ~40 candles of history before it. None if the chart was not saved."""
    try:
        ctx = json.loads(r["ctx"])
        sig = ctx["setup"][-1]
        conf = ctx.get("conf")
        hist = json.loads(r["hist"]) if r.get("hist") else []
    except Exception:  # noqa
        return None
    sd = r["sig_dir"] or r["direction"]
    if sd not in ("up", "down"):
        return None
    s = 1 if sd == "up" else -1
    _, so, sh, sl, sc = sig
    srng = sh - sl
    hr = int(time.gmtime(r["entry_time"]).tm_hour) // 4 * 4
    f = {"time of day (UTC)": f"{hr:02d}:00-{hr + 4:02d}:00", "asset": r["symbol"][len("OLYMP:"):], "signal pattern": r["signal"].split(" + ")[0],
         "signal candle size": "tiny body" if srng and abs(sc - so) < 0.25 * srng else "normal body" if srng and abs(sc - so) < 0.7 * srng else "big body"}
    if conf:
        _, o, h, l, c = conf
        rng, body = h - l, abs(c - o)
        if rng > 0 and c != o:
            start, end = (o - l, h - c) if c > o else (h - o, c - l)
            ew = end / (body + start) if body + start > 0 else 0
            pos = ((c - l) / rng) if s > 0 else 1 - (c - l) / rng
            f["confirmation candle colour"] = "with the signal's direction" if (c > o) == (s > 0) else "against the signal's direction"
            f["ending wick vs (body + starting wick)"] = "0.5-1x" if ew < 1 else "1-2x" if ew < 2 else "2x or more"
            f["confirmation body size"] = "small body (under 20% of range)" if body < 0.2 * rng else "medium body (20-40%)" if body < 0.4 * rng else "large body (over 40%)"
            f["where the confirmation closed"] = "toward the signal's direction" if pos > 2 / 3 else "away from it" if pos < 1 / 3 else "middle"
            if srng > 0:
                k = rng / srng
                f["confirmation size vs signal candle"] = "smaller" if k < 0.7 else "bigger" if k > 1.3 else "about the same"
                up_x, dn_x = (sh, sl) if s > 0 else (-sl, -sh)  # extreme in the signal's direction / the other one
                cc = c if s > 0 else -c
                f["confirmation close vs signal candle"] = "beyond its extreme in the signal's direction" if cc > up_x else "beyond its far extreme" if cc < dn_x else "inside its range"
    if len(hist) >= 25:
        S = hist + [list(sig)]
        cl, atr = [x[4] for x in S], _avg([x[2] - x[3] for x in S[-15:-1]])
        if atr > 0:
            def z(k): return s * (cl[-1] - cl[-1 - k]) / atr
            def tb(v): return "strong move against the signal" if v < -2 else "against" if v < -0.7 else "flat" if v < 0.7 else "with the signal" if v < 2 else "strong move with the signal"
            f["trend into the signal, last 5 candles"] = tb(z(5))
            f["trend into the signal, last 10 candles"] = tb(z(10))
            f["trend into the signal, last 20 candles"] = tb(z(20))
            hi20, lo20 = max(x[2] for x in S[-21:-1]), min(x[3] for x in S[-21:-1])
            pos20 = ((cl[-1] - lo20) / (hi20 - lo20)) if hi20 > lo20 else 0.5
            pos20 = pos20 if s > 0 else 1 - pos20
            f["where the signal closed in the last 20 candles"] = "near the extreme in its direction" if pos20 > 0.8 else "near the opposite extreme" if pos20 < 0.2 else "middle"
            f["20-candle breakout"] = ("broke the 20-candle extreme in its direction" if (sh > hi20 if s > 0 else sl < lo20)
                                      else "broke the opposite extreme" if (sl < lo20 if s > 0 else sh > hi20) else "inside the 20-candle range")
            f["signal candle size vs normal"] = "small" if srng < 0.7 * atr else "big" if srng > 1.5 * atr else "normal"
            a7, a28 = _avg([x[2] - x[3] for x in S[-8:-1]]), _avg([x[2] - x[3] for x in S[-29:-1]])
            if a28 > 0:
                f["volatility"] = "expanding" if a7 > 1.2 * a28 else "contracting" if a7 < 0.8 * a28 else "steady"
            run = 1
            for x in reversed(S[:-1]):
                if (x[4] > x[1]) == (sc > so) and x[4] != x[1]:
                    run += 1
                else:
                    break
            f["same-colour run ending at the signal candle"] = "1 (just this one)" if run == 1 else "2" if run == 2 else "3 or more"
            prev = S[-2]
            f["candle before the signal"] = "same colour as the signal" if (prev[4] > prev[1]) == (sc > so) else "opposite colour"
            ups = sum(1 for i in range(len(cl) - 14, len(cl)) if cl[i] > cl[i - 1]) / 14
            ups = ups if s > 0 else 1 - ups
            f["momentum (share of last 14 closes moving the signal's way)"] = "strong (over 65%)" if ups > 0.65 else "weak (under 35%)" if ups < 0.35 else "mixed"
            f["signal close vs 20-candle average"] = ("beyond it, in the signal's direction" if (s * (cl[-1] - _avg(cl[-20:])) > 0) else "beyond it, against the signal")
    return f


FAMILIES = {"case 1": "%confirmation case 1", "case 2": "%confirmation case 2", "case 3": "%confirmation case 3",
            "all confirmed trades": "%confirmation case%", "everything": "%"}


def drivers(db, family: str = "case 1", min_n: int = 8) -> dict:
    """For every setup feature, how often price CONTINUED the way the signal pointed after entry, in each bucket. A group is flagged HELD when its
    range (widened for how many groups were tested) excludes 50% AND the first and second half of the trades both lean the same way."""
    like = FAMILIES.get(family, FAMILIES["case 1"])
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT symbol, signal, direction, sig_dir, entry_time, entry_price, results, ctx, hist FROM paper "
                                           "WHERE settled=1 AND signal LIKE ? ORDER BY entry_time", (like,))]
    data, deep = [], 0
    for r in rows:
        f = _features(r)
        try:
            ex = list(json.loads(r["results"] or "{}").values())[0]["exit"]
        except Exception:  # noqa
            continue
        s = 1 if (r["sig_dir"] or r["direction"]) == "up" else -1
        if f and ex != r["entry_price"]:
            data.append((f, s * (ex - r["entry_price"]) > 0))
            deep += "trend into the signal, last 20 candles" in f
    half = len(data) // 2
    agg: dict = {}
    for i, (f, cont) in enumerate(data):
        for k, v in f.items():
            a = agg.setdefault((k, v), {"n": 0, "up": 0, "n1": 0, "up1": 0, "n2": 0, "up2": 0})
            a["n"] += 1; a["up"] += cont
            sfx = "1" if i < half else "2"
            a["n" + sfx] += 1; a["up" + sfx] += cont
    groups = [(kv, a) for kv, a in agg.items() if a["n"] >= min_n]
    z = max(1.96, NormalDist().inv_cdf(1 - 0.025 / max(len(groups), 1)))
    out = []
    for (k, v), a in groups:
        lo, hi = _wilson(a["up"], a["n"], z)
        lean = "CONTINUES the signal" if lo > 0.5 else "REVERSES the signal" if hi < 0.5 else None
        r1 = a["up1"] / a["n1"] if a["n1"] else None
        r2 = a["up2"] / a["n2"] if a["n2"] else None
        held = bool(lean and a["n1"] >= 4 and a["n2"] >= 4 and ((r1 > 0.5 and r2 > 0.5) if lo > 0.5 else (r1 < 0.5 and r2 < 0.5)))
        out.append({"feature": k, "bucket": v, "n": a["n"], "up": a["up"], "rate": round(100 * a["up"] / a["n"]), "low": round(100 * lo), "high": round(100 * hi),
                    "lean": lean, "held": held, "first": None if r1 is None else round(100 * r1), "second": None if r2 is None else round(100 * r2)})
    out.sort(key=lambda x: (not x["held"], not x["lean"], -abs(x["rate"] - 50) * math.sqrt(x["n"])))
    base = round(100 * sum(u for _, u in data) / len(data)) if data else None
    return {"family": family, "trades": len(data), "deep": deep, "continue_rate": base, "tested": len(out), "z": round(z, 2), "findings": out[:16]}
