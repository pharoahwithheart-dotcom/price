"""Paper trading: pretend trades on Olymp Trade signals, so you can see the real win rate BEFORE any money is involved.

Whenever a candle alert fires on an Olymp Trade asset, one pretend trade is saved: direction (up/down), the signal candle's
close as the entry price, and the entry time (that candle's close). A few minutes later its result is read from Olymp Trade's
1-minute candles for each expiry in PAPER_EXPIRIES (default 1,3,5 minutes): win, loss or draw (same price = refund).
Nothing here ever places a real trade.
"""
import json, math, os, time

import candles as cdl
import olymp

EXPIRIES = sorted({int(x) for x in os.getenv("PAPER_EXPIRIES", "1,3,5").split(",") if x.strip().isdigit() and int(x) > 0}) or [1, 3, 5]
PAYOUT = min(max(float(os.getenv("PAPER_PAYOUT", "0.85")), 0.1), 1.5)  # profit per 1 staked on a win; 0.85 means +85%
BREAKEVEN = 1 / (1 + PAYOUT)  # win rate (draws ignored) at which you neither gain nor lose
KEEP = 30 * 86400
UP = {"surge_up", "bull_engulf", "hammer", "any_bullish", "first5_up", "first10_up"}
DOWN = {"surge_down", "bear_engulf", "shooting_star", "any_bearish", "first5_down", "first10_down"}
NOTE = ("Past pretend results do not promise future ones. The more signals and expiries you compare, the more likely one looks "
        "good by pure luck, so trust a result only with a big sample (hundreds of trades) that stays good on fresh days. "
        "Entry is the signal candle's close; a real trade is placed a few seconds later at a slightly different price.")


def init(db):
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS paper(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, alert_id INTEGER, symbol TEXT, signal TEXT, direction TEXT,
            entry_time REAL, entry_price REAL, results TEXT, settled INTEGER DEFAULT 0)""")
        if "expiries" not in {r[1] for r in c.execute("PRAGMA table_info(paper)")}:
            c.execute("ALTER TABLE paper ADD COLUMN expiries TEXT")
        if "ctx" not in {r[1] for r in c.execute("PRAGMA table_info(paper)")}:
            c.execute("ALTER TABLE paper ADD COLUMN ctx TEXT")  # the candles of the setup, saved when the signal fired (for the chart)
        if "path" not in {r[1] for r in c.execute("PRAGMA table_info(paper)")}:
            c.execute("ALTER TABLE paper ADD COLUMN path TEXT")  # what price did between entry and the longest expiry
        if "hist" not in {r[1] for r in c.execute("PRAGMA table_info(paper)")}:
            c.execute("ALTER TABLE paper ADD COLUMN hist TEXT")  # the ~40 candles before the signal candle, for the deeper analysis
        if "retry" not in {r[1] for r in c.execute("PRAGMA table_info(paper)")}:
            c.execute("ALTER TABLE paper ADD COLUMN retry INTEGER DEFAULT 0")  # a result that could not be read is retried once, with more history


def direction(pattern: str, candle):
    if pattern in UP:
        return "up"
    if pattern in DOWN:
        return "down"
    o, c = candle[1], candle[4]  # custom / "either direction" signals: follow the signal candle's own colour
    return "up" if c > o else "down" if c < o else None


def _ohlc(x) -> list:
    return [int(x[0]), x[1], x[2], x[3], x[4]]  # time, open, high, low, close


def make_ctx(tf: int, setup, k: int = 1, conf=None) -> str:
    """The candles to draw: up to 3 setup candles (the last k are the pattern, the very last is the signal candle) and, if
    the trade waited for one, the confirmation candle."""
    return json.dumps({"tf": tf, "k": k, "setup": [_ohlc(x) for x in (setup or [])[-3:]], "conf": _ohlc(conf) if conf else None})


def open_trade(c, a, sym: str, candle, sec: int, hit: str, history=None):
    """Save one pretend trade. `c` is an open DB connection; `a` the alert (row or dict); `candle` the signal candle;
    `history` the closed candles up to and including it (kept for the chart)."""
    if not sym.startswith(olymp.PREFIX):
        return
    d = direction(a["pattern"], candle)
    if d is None:
        return
    entry_time = candle[0] + sec
    if c.execute("SELECT 1 FROM paper WHERE alert_id=? AND symbol=? AND entry_time=?", (a["id"], sym, entry_time)).fetchone():
        return
    name = a["pattern"]
    if name == "custom":
        try:
            name = "custom: " + (json.loads(a["rule"]).get("name") or f"#{a['id']}")
        except Exception:  # noqa
            name = f"custom: #{a['id']}"
    k = 1
    if a["pattern"] == "custom":
        try:
            k = max(1, len(json.loads(a["rule"]).get("steps") or [1]))
        except Exception:  # noqa
            pass
    c.execute("INSERT INTO paper(ts,alert_id,symbol,signal,direction,entry_time,entry_price,results,ctx) VALUES(?,?,?,?,?,?,?,NULL,?)",
              (time.time(), a["id"], sym, name, d, entry_time, candle[4], make_ctx(sec, history or [candle], k)))


def open_confirmed(c, alert_id, sym: str, name: str, d: str, entry_time: float, entry_price: float, sec: int, ctx=None, sig_dir=None, hist=None):
    """Pretend trade for the "wait for confirmation" strategy: enter at the start of the trading candle, expire at its end."""
    if sec % 60 or not sym.startswith(olymp.PREFIX):
        return
    if c.execute("SELECT 1 FROM paper WHERE alert_id=? AND symbol=? AND entry_time=? AND signal=?", (alert_id, sym, entry_time, name)).fetchone():
        return
    c.execute("INSERT INTO paper(ts,alert_id,symbol,signal,direction,entry_time,entry_price,results,expiries,ctx,sig_dir,hist) VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?)",
              (time.time(), alert_id, sym, name, d, entry_time, entry_price, json.dumps([sec // 60]), ctx, sig_dir, hist))


def _exps(r) -> list:
    try:
        return json.loads(r["expiries"]) if r.get("expiries") else EXPIRIES
    except ValueError:
        return EXPIRIES


def settle_notify_on(db) -> bool:
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='notify_settle'").fetchone()
    return bool(r and r[0] == "1")


def set_settle_notify(db, on: bool):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('notify_settle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("1" if on else "0",))


def send_results(db, notify, title: str, lines: list):
    """One bundled, quiet phone message for results that just came in (only when the page's switch is on)."""
    if not lines or notify is None or not settle_notify_on(db):
        return
    body = "\n".join(lines[:10]) + (f"\n+{len(lines) - 10} more" if len(lines) > 10 else "")
    try:
        notify(title, body, "bar_chart", False)
    except Exception:  # noqa
        pass


def _closed(asset: str, sec: int, age: float) -> list:
    """Closed candles of `sec` seconds, oldest first, reaching back about `age` seconds (up to 600 candles) when Olymp serves that many."""
    now = time.time()
    n = min(600, max(60, int(age // sec) + 10))
    try:
        if sec in (60, 300):
            rows = olymp.raw_candles(asset, sec, n)
        elif sec % 300 == 0 and sec <= 3600:
            rows = cdl.aggregate(olymp.raw_candles(asset, 300, min(600, n * sec // 300 + 12)), sec)
        else:
            return []
    except Exception:  # noqa
        return []
    return [c for c in rows if c[0] + sec <= now + 1]


def one_minute(asset: str, age: float) -> list:
    """1-minute candles reaching back about `age` seconds. Falls back to the usual 60 candles if Olymp will not serve more."""
    got = _closed(asset, 60, age) if age > 3000 else []
    return got or olymp.candles(asset, 60)


def _tf_sec(tf) -> int:
    try:
        return int(str(tf)[:-1]) * {"m": 60, "h": 3600, "d": 86400}[str(tf)[-1]]
    except Exception:  # noqa
        return 300


def backfill_ctx(db):
    """Draw trades that were saved before charts existed: rebuild their setup, confirmation and after-entry candles from Olymp's history."""
    now = time.time()
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM paper WHERE ctx IS NULL AND entry_time>?", (now - 12 * 3600,))]
        try:
            tf = {r[0]: r[1] for r in c.execute("SELECT id, timeframe FROM alerts")}
        except Exception:  # noqa
            tf = {}
    for r in rows[:15]:  # a few per pass keeps the requests low
        asset, confirm = r["symbol"][len(olymp.PREFIX):], "confirmation" in (r["signal"] or "")
        sec = _exps(r)[0] * 60 if confirm else _tf_sec(tf.get(r["alert_id"]))
        ctx = "null"
        cs = _closed(asset, sec, now - r["entry_time"] + 4 * sec)
        i = next((k for k, x in enumerate(cs) if int(x[0]) == int(r["entry_time"]) - sec * (2 if confirm else 1)), None)
        if i is not None:
            conf = next((x for x in cs if int(x[0]) == int(r["entry_time"]) - sec), None) if confirm else None
            if conf or not confirm:
                ctx = make_ctx(sec, cs[max(0, i - 2): i + 1], 1, conf)
        path = r.get("path")
        if not path and r["settled"]:
            end = r["entry_time"] + max(_exps(r)) * 60
            span = [x for x in one_minute(asset, now - r["entry_time"]) if r["entry_time"] - 1 <= x[0] < end]
            if span:
                path = json.dumps({"high": max(x[2] for x in span), "low": min(x[3] for x in span), "after": [_ohlc(x) for x in span]})
        with db() as c:
            c.execute("UPDATE paper SET ctx=?, path=COALESCE(?, path) WHERE id=?", (ctx, path, r["id"]))


def fill_hist(db):
    """Save the ~40 candles before each trade's signal candle (needs a saved chart). Olymp serves about 50 hours of 5-minute history, so
    trades older than that cannot be filled and are marked empty. A failed request just tries again later."""
    now = time.time()
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT id, symbol, entry_time, ctx FROM paper WHERE hist IS NULL AND ctx IS NOT NULL "
                                           "AND entry_time<? ORDER BY entry_time DESC LIMIT 80", (now - 90,))]
    by: dict = {}
    for r in rows:
        try:
            x = json.loads(r["ctx"])
            by.setdefault((r["symbol"], int(x["tf"])), []).append((r["id"], x["setup"][-1][0]))
        except Exception:  # noqa
            with db() as c:
                c.execute("UPDATE paper SET hist='[]' WHERE id=?", (r["id"],))
    for (sym, tf), items in by.items():
        cs = _closed(sym[len(olymp.PREFIX):], tf, now - min(t for _, t in items) + 45 * tf)
        if not cs:
            continue  # could not read candles right now; try again later
        for rid, t in items:
            h = [_ohlc(x) for x in cs if x[0] < t][-40:]
            ok = len(h) >= 25 and h[-1][0] == t - tf  # must end with the candle right before the signal candle
            with db() as c:
                c.execute("UPDATE paper SET hist=? WHERE id=?", (json.dumps(h if ok else []), rid))


def match_real(trades: list, alert_id, symbol: str, t: float):
    """The real demo trade (if any) that was made for a pretend trade entered at time t: same alert and asset, within a couple of minutes."""
    best = None
    for x in trades:
        if x["alert_id"] == alert_id and olymp.PREFIX + x["asset"] == symbol and abs(x["ts"] - t) < 150:
            if best is None or abs(x["ts"] - t) < abs(best["ts"] - t):
                best = x
    return best


def settle_due(db, notify=None) -> str:
    """Fill in the results of trades whose longest expiry has passed. Returns an error text, or ''.
    When `notify` is given and the page's switch is on, sends ONE bundled message for the trades settled in this pass."""
    now, err, news = time.time(), "", []
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM paper WHERE settled=0 OR (settled=1 AND (results IS NULL OR results='{}') AND COALESCE(retry,0)=0 AND entry_time>?)",
            (now - 12 * 3600,))]
        rows = [r for r in rows if r["entry_time"] <= now - max(_exps(r)) * 60 - 20]
        c.execute("DELETE FROM paper WHERE ts<?", (now - KEEP,))
    by: dict = {}
    for r in rows:
        by.setdefault(r["symbol"], []).append(r)
    for sym, trades in by.items():
        try:
            mins = one_minute(sym[len(olymp.PREFIX):], now - min(t["entry_time"] for t in trades))  # 1-minute candles, as far back as needed
            closes = {int(x[0]): x[4] for x in mins}
        except Exception as e:  # noqa
            err = f"paper trading: could not read {sym}: {e}"
            continue
        for r in trades:
            res = {}
            for m in _exps(r):
                px = closes.get(int(r["entry_time"]) + m * 60 - 60)  # the 1-minute candle that ENDS at the expiry
                if px is None:
                    continue
                up = px > r["entry_price"] if r["direction"] == "up" else px < r["entry_price"]
                res[str(m)] = {"exit": px, "r": "draw" if px == r["entry_price"] else "win" if up else "loss"}
            too_old = (bool(mins) and r["entry_time"] + 60 < mins[0][0]) or now - r["entry_time"] > 13 * 3600  # the history no longer reaches back
            if len(res) == len(_exps(r)) or too_old:
                end = r["entry_time"] + max(_exps(r)) * 60
                span = [x for x in mins if r["entry_time"] - 1 <= x[0] < end]  # 1-minute candles inside the trade window
                path = json.dumps({"high": max(x[2] for x in span), "low": min(x[3] for x in span), "after": [_ohlc(x) for x in span]}) if span else None
                with db() as c:
                    c.execute("UPDATE paper SET results=?, settled=1, path=?, retry=1 WHERE id=?", (json.dumps(res), path, r["id"]))
                if res and now - r["entry_time"] < 3600:  # not the old backlog after a restart
                    news.append((r, res))
    if news:
        w = sum(1 for _, res in news if all(x["r"] == "win" for x in res.values()))
        l = sum(1 for _, res in news if all(x["r"] == "loss" for x in res.values()))
        send_results(db, notify, f"{len(news)} pretend result{'s' if len(news) != 1 else ''}: {w} win, {l} loss" + (f", {len(news) - w - l} mixed" if len(news) - w - l else ""),
                     [f"{r['symbol'][len(olymp.PREFIX):]} {r['signal']} {r['direction']}: " + ", ".join(f"{m}m {x['r']}" for m, x in sorted(res.items(), key=lambda kv: int(kv[0])))
                      for r, res in news])
    try:
        backfill_ctx(db)
    except Exception as e:  # noqa
        err = err or f"paper trading charts: {e}"
    return err


def _wilson(w: int, n: int):
    if n == 0:
        return None, None
    p, z = w / n, 1.96
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(100 * (mid - half), 1), round(100 * (mid + half), 1)


def _row(signal: str, expiry: int, w: int, l: int, dr: int) -> dict:
    n = w + l
    lo, hi = _wilson(w, n)
    if n < 50:
        verdict = "Too few trades to say anything yet."
    elif lo / 100 > BREAKEVEN:
        verdict = "Above break-even even at the low end of the range (still only past results)."
    elif w / n > BREAKEVEN:
        verdict = "Above break-even, but the range still includes losing."
    else:
        verdict = "Below break-even."
    return {"signal": signal, "expiry": expiry, "trades": n + dr, "wins": w, "losses": l, "draws": dr,
            "win_rate": round(100 * w / n, 1) if n else None, "low": lo, "high": hi,
            "net": round(w * PAYOUT - l, 2), "verdict": verdict}


def stats(db, hours: float) -> dict:
    since = time.time() - hours * 3600
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM paper WHERE entry_time>=?", (since,))]
    agg: dict = {}
    open_n = 0
    for r in rows:
        if not r["settled"]:
            open_n += 1
            continue
        for m, x in json.loads(r["results"] or "{}").items():
            for sig in (r["signal"], "ALL SIGNALS"):
                d = agg.setdefault((sig, int(m)), {"win": 0, "loss": 0, "draw": 0})
                d[x["r"]] += 1
    out = [_row(s, m, d["win"], d["loss"], d["draw"]) for (s, m), d in agg.items()]
    out.sort(key=lambda x: (x["signal"] != "ALL SIGNALS", x["signal"], x["expiry"]))
    return {"total": len(rows), "open": open_n, "payout": PAYOUT, "breakeven": BREAKEVEN, "expiries": EXPIRIES, "rows": out, "note": NOTE}


def _pip(price: float) -> float:
    return 0.01 if price and price > 20 else 0.0001  # JPY-style prices vs the usual 4/5-decimal pairs


def _pips(diff: float, price: float) -> float:
    return round(diff / _pip(price), 1)


def _instead(r: dict, res: dict, path) -> str:
    """Plain words: what price did, compared with the direction the signal called."""
    entry, d = r["entry_price"], r["direction"]
    if not res:
        return "no result could be read"
    longest = res[max(res, key=int)]
    diff = longest["exit"] - entry
    went = "up" if diff > 0 else "down" if diff < 0 else "nowhere"
    out = f"called {d}; after {max(res, key=int)} min price was {went}" + (f" {abs(_pips(diff, entry))} pips" if diff else "")
    if path:
        up_max, down_max = _pips(path["high"] - entry, entry), _pips(entry - path["low"], entry)
        fav, adv = (up_max, down_max) if d == "up" else (down_max, up_max)
        out += ("; best it went your way " + f"{fav} pips" if fav > 0 else "; it never went your way")
        out += (f", worst against you {adv} pips" if adv > 0 else ", and it never went against you")
    return out


def journal(db, hours: float, limit: int = 300) -> list:
    """One entry per pretend trade, newest first: what the signal called, the result at each expiry, and what price did instead."""
    since = time.time() - hours * 3600
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM paper WHERE entry_time>=? ORDER BY entry_time DESC LIMIT ?", (since, limit))]
    try:
        with db() as c:
            real = [dict(x) for x in c.execute("SELECT alert_id, asset, ts, direction, amount, result, pips, settled, entry_px FROM trades WHERE status LIKE 'sent%' AND ts>=?", (since - 7200,))]
    except Exception:  # noqa  (no trades table yet)
        real = []
    out = []
    for r in rows:
        res = json.loads(r["results"] or "{}") if r["settled"] else {}
        path = json.loads(r["path"]) if r.get("path") else None
        rt = match_real(real, r["alert_id"], r["symbol"], r["entry_time"])
        exps = {}
        for m, x in res.items():
            exps[m] = {"result": x["r"], "exit": x["exit"], "pips": _pips(x["exit"] - r["entry_price"], r["entry_price"])}
        results = [x["r"] for x in res.values()]
        out.append({"entry_time": r["entry_time"], "symbol": r["symbol"], "signal": r["signal"], "direction": r["direction"],
                    "entry_price": r["entry_price"], "settled": bool(r["settled"]), "expiries": exps,
                    "ctx": json.loads(r["ctx"]) if r.get("ctx") else None, "after": (path or {}).get("after", []),
                    "real": ({"result": rt["result"], "pips": rt["pips"], "amount": rt["amount"], "settled": bool(rt["settled"]), "entry_px": rt["entry_px"]} if rt else None),
                    "overall": ("pending" if not r["settled"] else "win" if results and all(x == "win" for x in results)
                                else "loss" if results and all(x == "loss" for x in results) else "mixed" if results else "no result"),
                    "instead": _instead(r, res, path) if r["settled"] else "still waiting for its result"})
    return out


def by_pair(db, hours: float) -> list:
    """Win rate per asset and expiry, over all signals together."""
    since = time.time() - hours * 3600
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT symbol, results FROM paper WHERE entry_time>=? AND settled=1", (since,))]
    agg: dict = {}
    for r in rows:
        for m, x in json.loads(r["results"] or "{}").items():
            d = agg.setdefault((r["symbol"][len(olymp.PREFIX):], int(m)), {"win": 0, "loss": 0, "draw": 0})
            d[x["r"]] += 1
    out = []
    for (sym, m), d in sorted(agg.items()):
        n = d["win"] + d["loss"]
        out.append({"pair": sym, "expiry": m, "wins": d["win"], "losses": d["loss"], "draws": d["draw"],
                    "win_rate": round(100 * d["win"] / n, 1) if n else None})
    return out


def _decided(db, since: float):
    """(signal, expiry, entry_time, 'win'|'loss', hour_utc) for every decided result since `since`, oldest first."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT signal, entry_time, results FROM paper WHERE entry_time>=? AND settled=1 ORDER BY entry_time", (since,))]
    out = []
    for r in rows:
        for m, x in json.loads(r["results"] or "{}").items():
            if x["r"] in ("win", "loss"):
                out.append((r["signal"], int(m), r["entry_time"], x["r"], int(time.gmtime(r["entry_time"]).tm_hour)))
    return out


def analysis(db, hours: float, exp: int = 0, signal: str = "ALL SIGNALS") -> dict:
    """Win rate by hour of the day (UTC) for one signal and expiry, plus a first-half vs second-half stability check."""
    d = _decided(db, time.time() - hours * 3600)
    signals = ["ALL SIGNALS"] + sorted({x[0] for x in d})
    exps = sorted({x[1] for x in d}) or list(EXPIRIES)
    exp = exp if exp in exps else exps[0]
    sel = [x for x in d if x[1] == exp and (signal == "ALL SIGNALS" or x[0] == signal)]
    hrs = [{"hour": h, "wins": 0, "losses": 0} for h in range(24)]
    for x in sel:
        hrs[x[4]]["wins" if x[3] == "win" else "losses"] += 1
    luck = []
    groups: dict = {}
    for x in d:
        for sg in (x[0], "ALL SIGNALS"):
            groups.setdefault((sg, x[1]), []).append(x[3])
    for (sg, m), res in sorted(groups.items(), key=lambda kv: (kv[0][0] != "ALL SIGNALS", kv[0][0], kv[0][1])):
        n = len(res)
        h1, h2 = res[: n // 2], res[n // 2:]
        r1 = sum(1 for z in h1 if z == "win") / len(h1) if h1 else None
        r2 = sum(1 for z in h2 if z == "win") / len(h2) if h2 else None
        if n < 20:
            verdict = "Too few decided trades (need 20+)."
        elif r1 > BREAKEVEN and r2 > BREAKEVEN:
            verdict = "Above break-even in BOTH halves."
        elif r1 > BREAKEVEN:
            verdict = "Good early, fading: be careful."
        elif r2 > BREAKEVEN:
            verdict = "Weak early, better lately."
        else:
            verdict = "Not above break-even in either half."
        luck.append({"signal": sg, "expiry": m, "n": n, "first": None if r1 is None else round(100 * r1), "second": None if r2 is None else round(100 * r2),
                     "verdict": verdict, "ok": n >= 20 and r1 > BREAKEVEN and r2 > BREAKEVEN})
    return {"signals": signals, "expiries": exps, "signal": signal if signal in signals else "ALL SIGNALS", "exp": exp, "breakeven": BREAKEVEN,
            "by_hour": hrs, "luck": luck, "notify_on": settle_notify_on(db)}
