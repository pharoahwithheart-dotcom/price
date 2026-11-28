"""Deriv trading bot, driven by the same candle patterns as your alerts. Demo by default; real money only if you switch it on.

Every time a candle closes on a symbol you listed, the bot looks at that closed candle. If one of the patterns you ticked
matches, it buys ONE Rise (bullish pattern) or Fall (bearish pattern) contract. The result is read back from Deriv itself
(the actual profit or loss of the contract, not an estimate).

It is OFF until you switch it on on the /deriv page and needs a token, app id and account id first. Safety limits you set
(and sensible defaults): a hard maximum stake, trades per hour and per day, how many may be open at once, a maximum LOSS in
money per 24 hours, and it switches itself OFF after too many losses in a row. Every signal is logged, including the ones not traded.
Mode: "demo" or "real" (deriv.MODE). Results, streaks and loss limits are counted per mode, so a demo record never mixes with live.
"""
import json, os, re, threading, time

import candles as cdl
import deriv
import paper

DEFAULTS = {"symbols": [], "gran": 300, "patterns": [], "stake": 1.0, "duration": 300, "currency": "USD",
            "max_stake": 50.0, "max_day_loss": 20.0, "max_per_hour": 6, "max_per_day": 30, "max_open": 3, "max_streak": 4, "max_day_losses": 10, "max_late": 20}
PATTERN_CHOICES = {k: v for k, v in cdl.PATTERNS.items() if k != "custom"}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9_]{2,30}$")
KEEP = 60 * 86400
_seen: dict = {}      # symbol -> open time of the last candle already examined
_tried: dict = {}     # symbol -> how many times we tried to fetch that candle
_last_reconcile = [0.0]
_lock = threading.Lock()


def init(db):
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS deriv_trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, pattern TEXT, direction TEXT, candle_ts REAL,
            stake REAL, duration INTEGER, status TEXT, sent INTEGER DEFAULT 0, contract_id TEXT, buy_price REAL, payout REAL,
            profit REAL, result TEXT, settled INTEGER DEFAULT 0, note TEXT, checked REAL DEFAULT 0, mode TEXT DEFAULT 'demo')""")
        if "mode" not in {r[1] for r in c.execute("PRAGMA table_info(deriv_trades)")}:
            c.execute("ALTER TABLE deriv_trades ADD COLUMN mode TEXT DEFAULT 'demo'")
        c.execute("DELETE FROM deriv_trades WHERE ts<?", (time.time() - KEEP,))
        g = lambda k: (c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or [None])[0]
        pat, app_id, acct, mode = g("deriv_pat"), g("deriv_app_id"), g("deriv_account_id"), g("deriv_mode")
    # Real-money mode can only come from the page (saved with a typed confirmation), never from an environment variable.
    deriv.set_credentials(pat or os.getenv("DERIV_PAT", ""), app_id or os.getenv("DERIV_APP_ID", ""), acct or os.getenv("DERIV_ACCOUNT_ID", ""),
                          "real" if mode == "real" else "demo")


def _setting(c, k, default=""):
    r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r[0] if r else default


def _put(c, k, v):
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))


def config(db) -> dict:
    with db() as c:
        raw = _setting(c, "deriv_cfg", "")
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_config(db, raw: dict) -> dict:
    """Validate what the page sent; raises ValueError with a plain message."""
    cur = config(db)
    out = dict(cur)
    if "symbols" in raw:
        syms = []
        for s in raw["symbols"]:
            s = str(s).strip()
            if s and s not in syms:
                if not SYMBOL_RE.match(s):
                    raise ValueError(f"'{s}' is not a Deriv symbol name (examples: frxEURUSD, R_100, 1HZ100V)")
                syms.append(s)
        if len(syms) > 20:
            raise ValueError("at most 20 symbols")
        out["symbols"] = syms
    if "gran" in raw:
        if int(raw["gran"]) not in deriv.GRANULARITIES:
            raise ValueError(f"candle size must be one of {deriv.GRANULARITIES} seconds")
        out["gran"] = int(raw["gran"])
    if "patterns" in raw:
        bad = [p for p in raw["patterns"] if p not in PATTERN_CHOICES]
        if bad:
            raise ValueError(f"unknown pattern: {bad[0]}")
        out["patterns"] = list(dict.fromkeys(raw["patterns"]))
    for k, lo, hi, typ in (("max_stake", 0.35, 1000, float), ("max_day_loss", 0, 100000, float), ("stake", 0.35, 1000, float), ("duration", 15, 86400, int),
                           ("max_per_hour", 1, 100, int), ("max_per_day", 1, 500, int), ("max_open", 1, 20, int),
                           ("max_streak", 0, 50, int), ("max_day_losses", 0, 500, int), ("max_late", 5, 120, int)):
        if k in raw:
            v = typ(raw[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            out[k] = v
    if "currency" in raw:
        cur_ = str(raw["currency"]).strip().upper()
        if not re.match(r"^[A-Z]{3,4}$", cur_):
            raise ValueError("currency must be like USD")
        out["currency"] = cur_
    if out["stake"] > out["max_stake"]:
        raise ValueError(f"stake {out['stake']} is above your maximum stake {out['max_stake']}")
    with db() as c:
        _put(c, "deriv_cfg", json.dumps(out))
    return out


def is_on(db) -> bool:
    with db() as c:
        return _setting(c, "deriv_on") == "1"


def set_on(db, on: bool):
    with db() as c:
        _put(c, "deriv_on", "1" if on else "0")
        if on:
            _put(c, "deriv_since", str(time.time()))  # the loss streak is counted from here


def missing(db) -> list:
    cfg, m = config(db), []
    if not deriv.configured():
        m.append("token, app id and account id")
    if not cfg["symbols"]:
        m.append("at least one symbol")
    if not cfg["patterns"]:
        m.append("at least one pattern")
    return m


def _log(db, sym, pattern, d, cts, status, **kw):
    with db() as c:
        cur = c.execute("INSERT INTO deriv_trades(ts,symbol,pattern,direction,candle_ts,stake,duration,status,sent,note,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), sym, pattern, d, cts, kw.get("stake"), kw.get("duration"), status, kw.get("sent", 0), kw.get("note"), deriv.MODE))
        return cur.lastrowid


def _upd(db, tid, **kw):
    with db() as c:
        c.execute(f"UPDATE deriv_trades SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), tid))


def _settle(db, tid, r: dict, notify=None):
    p = r.get("profit")
    res = "win" if p and p > 0 else "loss" if p is not None and p < 0 else "draw"
    _upd(db, tid, profit=p, result=res, settled=1, status="settled")
    check_stop(db, notify)


def _worker(db, tid, sym, d, stake, dur, cur, notify):
    def bought(info):
        _upd(db, tid, contract_id=str(info["contract_id"]), buy_price=info["buy_price"], payout=info["payout"], status="open", sent=1)
    try:
        r = deriv.place(sym, d, stake, dur, cur, on_bought=bought)
    except Exception as e:  # noqa
        with db() as c:
            row = c.execute("SELECT contract_id FROM deriv_trades WHERE id=?", (tid,)).fetchone()
        if row and row["contract_id"]:
            _upd(db, tid, note=f"connection problem after buying, result will be read later: {e}")
        else:
            _upd(db, tid, status="failed, nothing bought", note=str(e), sent=0)
        return
    if r.get("sold"):
        _settle(db, tid, r, notify)
    else:
        _upd(db, tid, note="bought; result not seen yet, will be read later")


def _caps(db, cfg) -> str:
    now = time.time()
    with db() as c:
        h = c.execute("SELECT COUNT(*) FROM deriv_trades WHERE sent=1 AND mode=? AND ts>?", (deriv.MODE, now - 3600)).fetchone()[0]
        d = c.execute("SELECT COUNT(*) FROM deriv_trades WHERE sent=1 AND mode=? AND ts>?", (deriv.MODE, now - 86400)).fetchone()[0]
        o = c.execute("SELECT COUNT(*) FROM deriv_trades WHERE settled=0 AND mode=? AND status IN ('sending','open')", (deriv.MODE,)).fetchone()[0]
    if h >= cfg["max_per_hour"]:
        return f"hourly limit of {cfg['max_per_hour']} trades reached"
    if d >= cfg["max_per_day"]:
        return f"daily limit of {cfg['max_per_day']} trades reached"
    if o >= cfg["max_open"]:
        return f"{o} trades still open (limit {cfg['max_open']})"
    return ""


def _scan(db, cfg, notify):
    now = time.time()
    g = cfg["gran"]
    cur_open = now // g * g
    last_open = cur_open - g
    for sym in cfg["symbols"]:
        if _seen.get(sym) == last_open or now < cur_open + 2:
            continue  # already looked at this candle, or it closed less than 2 seconds ago
        if now - cur_open > cfg["max_late"]:
            _seen[sym] = last_open  # too late to trade this candle's signal; wait for the next close
            continue
        try:
            cs = deriv.candles(sym, g, cdl.LOOKBACK + 10)
        except Exception as e:  # noqa
            _tried[sym] = _tried.get(sym, 0) + 1
            if _tried[sym] >= 3:
                _seen[sym] = last_open
                _log(db, sym, "-", None, last_open, "not traded: could not read candles", note=str(e)[:200])
            continue
        if not cs or cs[-1][0] != last_open:
            continue  # Deriv has not published the closed candle yet; try again in 2 seconds
        _seen[sym], _tried[sym] = last_open, 0
        for pat in cfg["patterns"]:
            if not cdl.detect(cs, pat, False):
                continue
            d = paper.direction(pat, cs[-1])
            if d is None:
                _log(db, sym, pat, None, last_open, "not traded: the signal has no clear direction")
                break
            why = _caps(db, cfg)
            if why:
                _log(db, sym, pat, d, last_open, f"not traded: {why}")
                break
            stake = min(cfg["stake"], cfg["max_stake"])
            tid = _log(db, sym, pat, d, last_open, "sending", stake=stake, duration=cfg["duration"], sent=1)  # marked first: never sent twice
            threading.Thread(target=_worker, args=(db, tid, sym, d, stake, cfg["duration"], cfg["currency"], notify), daemon=True).start()
            break  # one trade per symbol per candle


def reconcile(db, notify=None):
    """Read back contracts whose result was not seen (a restart, a dropped connection), and close out stale rows."""
    now = time.time()
    if now - _last_reconcile[0] < 30:
        return
    _last_reconcile[0] = now
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM deriv_trades WHERE settled=0 AND mode=? AND status IN ('sending','open') AND ts<?", (deriv.MODE, now - 20))]
    for r in rows:
        age = now - r["ts"]
        if not r["contract_id"]:
            if age > 600:
                _upd(db, r["id"], status="unknown: no confirmation from Deriv, check your Deriv statement", settled=1, result="unknown")
            continue
        if age < (r["duration"] or 0) + 20 or now - (r["checked"] or 0) < 60:
            continue
        _upd(db, r["id"], checked=now)
        try:
            info = deriv.lookup(r["contract_id"])
        except Exception as e:  # noqa
            if age > 86400:
                _upd(db, r["id"], status="unknown: result could not be read", settled=1, result="unknown", note=str(e)[:200])
            continue
        if info.get("sold"):
            _settle(db, r["id"], info, notify)


def check_stop(db, notify=None):
    """Switch OFF after too many losses in a row (since last switched on), too many in 24 hours, or too much money lost in 24 hours."""
    cfg = config(db)
    with db() as c:
        if _setting(c, "deriv_on") != "1":
            return
        since = float(_setting(c, "deriv_since", "0") or 0)
        res = [x[0] for x in c.execute("SELECT result FROM deriv_trades WHERE settled=1 AND mode=? AND result IN ('win','loss') AND ts>=? ORDER BY ts", (deriv.MODE, since))]
        day = c.execute("SELECT COUNT(*) FROM deriv_trades WHERE result='loss' AND mode=? AND ts>?", (deriv.MODE, time.time() - 86400)).fetchone()[0]
        net = c.execute("SELECT COALESCE(SUM(profit),0) FROM deriv_trades WHERE settled=1 AND mode=? AND ts>?", (deriv.MODE, time.time() - 86400)).fetchone()[0]
    streak = 0
    for x in reversed(res):
        if x != "loss":
            break
        streak += 1
    why = (f"{streak} losses in a row" if cfg["max_streak"] and streak >= cfg["max_streak"]
           else f"{day} losses in the last 24 hours" if cfg["max_day_losses"] and day >= cfg["max_day_losses"]
           else f"{round(-net, 2)} lost in the last 24 hours (your limit is {cfg['max_day_loss']})" if cfg["max_day_loss"] and net <= -cfg["max_day_loss"] else "")
    if not why:
        return
    set_on(db, False)
    _log(db, "-", "-", None, time.time(), f"bot switched OFF: {why}. Switch it on again when you have looked at it")
    if notify:
        try:
            notify("Deriv bot switched OFF", f"Stopped after {why}. It stays off until you switch it on again on the page.", "warning", False)
        except Exception:  # noqa
            pass


def step(db, notify=None):
    """One pass, called every couple of seconds by the app. Reading results always runs; new trades only when switched on."""
    with _lock:
        reconcile(db, notify)
        if not is_on(db) or missing(db):
            return
        _scan(db, config(db), notify)


def stats(db, hours=None) -> dict:
    q, a = "SELECT result, profit, buy_price, payout FROM deriv_trades WHERE settled=1 AND mode=? AND result IN ('win','loss','draw')", [deriv.MODE]
    if hours:
        q, a = q + " AND ts>?", a + [time.time() - hours * 3600]
    with db() as c:
        rows = [dict(r) for r in c.execute(q, a)]
    w = sum(r["result"] == "win" for r in rows)
    l = sum(r["result"] == "loss" for r in rows)
    ratios = [r["payout"] / r["buy_price"] - 1 for r in rows if r["payout"] and r["buy_price"]]
    ratio = sum(ratios) / len(ratios) if ratios else None
    return {"trades": len(rows), "wins": w, "losses": l, "draws": len(rows) - w - l, "win_pct": round(100 * w / (w + l), 1) if w + l else None,
            "net": round(sum(r["profit"] or 0 for r in rows), 2), "breakeven_pct": round(100 / (1 + ratio), 1) if ratio else None}


def overview(db) -> dict:
    with db() as c:
        trades = [dict(r) for r in c.execute("SELECT * FROM deriv_trades ORDER BY id DESC LIMIT 40")]
    return {"on": is_on(db), "missing": missing(db), "config": config(db), "patterns": PATTERN_CHOICES, "granularities": deriv.GRANULARITIES,
            "mode": deriv.MODE, "credentials": {"set": deriv.configured(), "app_id": deriv.APP_ID, "account_id": deriv.ACCOUNT_ID},
            "stats_24h": stats(db, 24), "stats_all": stats(db), "trades": trades, "last_ok": deriv.status["last_ok"], "error": deriv.status["error"]}
