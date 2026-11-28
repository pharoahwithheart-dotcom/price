"""Price alerts for any Binance crypto pair or forex pair (EURUSD, GBPJPY, XAUUSD...), pushed to your phone via ntfy.

Env vars:
  OANDA_TOKEN  optional but recommended: forex/gold prices from OANDA (free practice account). OANDA_ENV=practice|live.
  DERIV_PAT + DERIV_APP_ID + DERIV_ACCOUNT_ID  optional: Deriv bot, demo by default (all settable on the /deriv page, see README).
  OLYMP_ACCESS_TOKEN + OLYMP_PAIRS  optional: Olymp Trade candles/prices (read-only, unofficial websocket client in olymp.py, see README).
  NTFY_PRIORITY ntfy priority: default|high|urgent (urgent = loudest, can break through Do Not Disturb on Android).
  PUSHOVER_TOKEN + PUSHOVER_USER  recommended: alarm-style alerts that repeat until acknowledged (see README).
  PUSHOVER_PRIORITY (2 = repeat until you tap Acknowledge [default], 1 = one loud alert), PUSHOVER_SOUND (default siren).
  NTFY_TOPIC   required unless Pushover is set; used as a backup if Pushover fails. A long random name; subscribe to it in the free ntfy phone app.
  APP_TOKEN    required in production. Password the web page asks for once.
  DATA_DIR     where alerts.db lives (default ./data; point at a Railway volume to keep alerts across deploys).
  POLL_SECONDS how often prices are checked (default 10).
"""
import json, os, sqlite3, threading, time, secrets
from collections import defaultdict, deque
from contextlib import contextmanager

import httpx
import candles as cdl
import deriv
import deriv_bot
import olymp
import adapt
import playbook
import paper
import trader
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
APP_TOKEN = os.getenv("APP_TOKEN", "")
POLL = max(3, int(os.getenv("POLL_SECONDS", "10")))
PRICE_URL = os.getenv("PRICE_URL", "https://data-api.binance.vision/api/v3/ticker/price")
DB = os.path.join(os.getenv("DATA_DIR", "data"), "alerts.db")
FOREX_POLL = max(15, int(os.getenv("FOREX_POLL_SECONDS", "30")))
FOREX_URL = os.getenv("FOREX_URL", "https://query1.finance.yahoo.com/v8/finance/chart/{sym}=X?interval=1m&range=1d")
CCY = set("USD EUR GBP JPY CHF AUD NZD CAD SEK NOK DKK PLN CZK HUF TRY ZAR MXN BRL CNH CNY HKD SGD INR KRW ILS THB RON XAU XAG".split())
OANDA_TOKEN = os.getenv("OANDA_TOKEN", "")
OANDA_HOST = "https://api-fxtrade.oanda.com" if os.getenv("OANDA_ENV", "practice") == "live" else "https://api-fxpractice.oanda.com"
_oanda_account = {"id": os.getenv("OANDA_ACCOUNT_ID", "")}
KLINES_URL = os.getenv("KLINES_URL", "https://data-api.binance.vision/api/v3/klines")
TIMEFRAMES = {"5m": 300, "10m": 600, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
OANDA_GRAN = {"5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "1d": "D"}
YAHOO_INT = {"5m": ("5m", "5d"), "15m": ("15m", "5d"), "1h": ("60m", "1mo"), "1d": ("1d", "6mo")}
COOLDOWN = 15 * 60  # seconds between repeats of a repeating alert

os.makedirs(os.path.dirname(DB), exist_ok=True)
lock = threading.Lock()
prices: dict[str, float] = {}
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=20000))  # (ts, price)
state = {"last_ok": 0.0, "last_error": ""}


@contextmanager
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, kind TEXT, target REAL,
        window_min INTEGER, repeat INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
        last_fired REAL DEFAULT 0, note TEXT DEFAULT '', created REAL)""")
    have = {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
    for col, typ in (("pattern", "TEXT"), ("timeframe", "TEXT"), ("last_candle", "REAL DEFAULT 0"), ("rule", "TEXT")):
        if col not in have:
            c.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ}")


with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    _t = c.execute("SELECT value FROM settings WHERE key='olymp_token'").fetchone()
    if _t and _t[0]:
        olymp.set_token(_t[0])  # a token saved on the web page wins over the OLYMP_ACCESS_TOKEN variable
    c.execute("CREATE TABLE IF NOT EXISTS olymp_pairs(name TEXT PRIMARY KEY, added REAL)")
    for (n_,) in c.execute("SELECT name FROM olymp_pairs ORDER BY added"):
        if n_ not in olymp.PAIRS:
            olymp.PAIRS.append(n_)  # pairs you added on the web page survive restarts


PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.getenv("PUSHOVER_USER", "")


def notify_pushover(title: str, body: str, loud: bool = True) -> bool:
    """Alarm-style push. Priority 2 (emergency) repeats every 60 s until you tap Acknowledge, and gets through
    silent mode. PUSHOVER_PRIORITY=1 gives one loud alert instead; PUSHOVER_SOUND picks the tone."""
    pri = int(os.getenv("PUSHOVER_PRIORITY", "2"))
    if not loud:
        pri = min(pri, 1)  # housekeeping notices (like an expired token) never use the repeat-until-acknowledged alarm
    data = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": body,
            "priority": pri, "sound": os.getenv("PUSHOVER_SOUND", "siren")}
    if pri == 2:
        data.update(retry=60, expire=1800)  # repeat every minute for up to 30 minutes
    try:
        r = httpx.post("https://api.pushover.net/1/messages.json", data=data, timeout=10)
        if r.status_code >= 300:
            state["last_error"] = f"Pushover rejected the message: {r.text[:150]}"
        return r.status_code < 300
    except Exception as e:  # noqa
        state["last_error"] = f"Pushover failed: {e}"
        return False


def notify(title: str, body: str, tags: str = "chart_with_upwards_trend", loud: bool = True) -> bool:
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        if notify_pushover(title, body, loud):
            return True  # otherwise fall through to ntfy as a backup
    if not NTFY_TOPIC:
        state["last_error"] = "NTFY_TOPIC is not set"
        return False
    try:
        r = httpx.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", content=body.encode(),
                       headers={"Title": title, "Tags": tags, "Priority": os.getenv("NTFY_PRIORITY", "high")}, timeout=10)
        return r.status_code < 300
    except Exception as e:  # noqa
        state["last_error"] = f"notify failed: {e}"
        return False


def no_volume(sym: str) -> bool:
    """Forex and Olymp Trade candles carry no real traded volume."""
    return is_forex(sym) or sym.startswith(olymp.PREFIX)


def fmt(p: float) -> str:
    return f"{p:,.8f}".rstrip("0").rstrip(".") if p < 1 else f"{p:,.4f}".rstrip("0").rstrip(".")


def is_forex(sym: str) -> bool:
    return len(sym) == 6 and sym[:3] in CCY and sym[3:] in CCY and sym[:3] != sym[3:]


def oanda_prices(syms: list[str]) -> dict[str, float]:
    """Mid prices for several forex pairs in ONE request (EURUSD -> EUR_USD)."""
    h = {"Authorization": f"Bearer {OANDA_TOKEN}"}
    if not _oanda_account["id"]:
        r = httpx.get(f"{OANDA_HOST}/v3/accounts", headers=h, timeout=10)
        r.raise_for_status()
        _oanda_account["id"] = r.json()["accounts"][0]["id"]
    out = {}
    for i in range(0, len(syms), 20):  # stay well inside OANDA's per-request instrument limit
        inst = ",".join(f"{x[:3]}_{x[3:]}" for x in syms[i:i + 20])
        r = httpx.get(f"{OANDA_HOST}/v3/accounts/{_oanda_account['id']}/pricing", headers=h,
                      params={"instruments": inst}, timeout=10)
        r.raise_for_status()
        for p in r.json().get("prices", []):
            if p.get("bids") and p.get("asks"):
                out[p["instrument"].replace("_", "")] = (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2
    return out


def yahoo_price(sym: str) -> float:
    r = httpx.get(FOREX_URL.format(sym=sym), timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


def fetch_forex(syms: list[str]) -> dict[str, float]:
    """OANDA when OANDA_TOKEN is set (falls back to Yahoo if OANDA errors), otherwise Yahoo only."""
    if OANDA_TOKEN:
        try:
            return oanda_prices(syms)
        except Exception as e:  # noqa
            state["last_error"] = f"OANDA failed, using Yahoo: {e}"
    out = {}
    for x in syms:
        try:
            out[x] = yahoo_price(x)
        except Exception as e:  # noqa
            state["last_error"] = f"forex price for {x} failed: {e}"
    return out


def pct_move(symbol: str, minutes: int, now: float):
    h = history.get(symbol)
    if not h:
        return None
    cutoff = now - minutes * 60
    if h[0][0] > cutoff + POLL * 2:  # not enough history yet
        return None
    base = next((p for t, p in h if t >= cutoff), None)
    if not base:
        return None
    return (h[-1][1] - base) / base * 100


def check_alerts(now: float):
    with db() as c:
        rows = c.execute("SELECT * FROM alerts WHERE active=1").fetchall()
        for a in rows:
            p = prices.get(a["symbol"])
            if p is None:
                continue
            msg = None
            if a["kind"] == "above" and p >= a["target"]:
                msg = f"{a['symbol']} is {fmt(p)} (at or above {fmt(a['target'])})"
            elif a["kind"] == "below" and p <= a["target"]:
                msg = f"{a['symbol']} is {fmt(p)} (at or below {fmt(a['target'])})"
            elif a["kind"] == "move":
                m = pct_move(a["symbol"], a["window_min"], now)
                if m is not None and abs(m) >= a["target"]:
                    msg = f"{a['symbol']} moved {m:+.2f}% in {a['window_min']} min, now {fmt(p)}"
            if not msg:
                continue
            if a["repeat"] and now - a["last_fired"] < COOLDOWN:
                continue
            if a["note"]:
                msg += f"\n{a['note']}"
            if notify(f"{a['symbol']} alert", msg):
                c.execute("UPDATE alerts SET last_fired=?, active=? WHERE id=?",
                          (now, 1 if a["repeat"] else 0, a["id"]))


_http = httpx.Client(timeout=10)  # keep-alive, so scanning many pairs doesn't open a new connection each time (OANDA limits that)


def oanda_candles(sym: str, tf: str) -> list:
    """Closed OANDA candles (mid price), oldest first: (open_time, o, h, l, c, volume)."""
    r = _http.get(f"{OANDA_HOST}/v3/instruments/{sym[:3]}_{sym[3:]}/candles",
                  headers={"Authorization": f"Bearer {OANDA_TOKEN}"},
                  params={"granularity": OANDA_GRAN[tf], "count": 60, "price": "M"})
    r.raise_for_status()
    return [(_iso(c["time"]), float(c["mid"]["o"]), float(c["mid"]["h"]), float(c["mid"]["l"]),
             float(c["mid"]["c"]), float(c.get("volume", 0))) for c in r.json()["candles"] if c.get("complete")]


def _iso(t: str) -> float:
    import calendar, time as _t
    return float(calendar.timegm(_t.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")))


def fetch_candles(sym: str, tf: str) -> list:
    """Closed candles, oldest first: (open_time, o, h, l, c, volume). Crypto from Binance, forex from OANDA or Yahoo."""
    now = time.time()
    if sym.startswith(olymp.PREFIX):
        return olymp.candles(sym[len(olymp.PREFIX):], TIMEFRAMES[tf])
    if not is_forex(sym):
        r = httpx.get(KLINES_URL, params={"symbol": sym, "interval": tf, "limit": 60}, timeout=10)
        r.raise_for_status()
        return [(k[0] / 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                for k in r.json() if k[6] / 1000 < now]
    if OANDA_TOKEN:
        try:
            return oanda_candles(sym, tf)
        except Exception as e:  # noqa
            state["last_error"] = f"OANDA candles failed, trying Yahoo: {e}"
    if tf not in YAHOO_INT:
        raise ValueError(f"{tf} candles need an OANDA token for forex")
    iv, rng = YAHOO_INT[tf]
    r = httpx.get(FOREX_URL.split("?")[0].format(sym=sym), params={"interval": iv, "range": rng}, timeout=10,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = [(float(t), q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i] or 0.0)
           for i, t in enumerate(res["timestamp"]) if q["close"][i] is not None]
    return out[:-1]  # the last one is still forming


_candle_cache: dict = {}


def get_candles(sym: str, tf: str, now: float) -> list:
    if tf == "10m":  # no exchange gives us this directly, so build it from 5-minute candles
        return cdl.aggregate(get_candles(sym, "5m", now), 600)
    key = (sym, tf)
    hit = _candle_cache.get(key)
    if hit and now - hit[0] < (5 if sym.startswith(olymp.PREFIX) else 30 if TIMEFRAMES[tf] <= 900 else 120):  # Olymp: short, so a signal is seen right after the close
        return hit[1]
    cs = fetch_candles(sym, tf)
    _candle_cache[key] = (now, cs)
    return cs


_res: dict = {}  # alert id -> result of the last candle that was actually judged
_diag: dict = {}  # alert id -> short text about the latest look, shown under the alert on the page


def _stamp(t) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(t)) + " UTC"


def check_candles(now: float):
    """Once per newly CLOSED candle, test it against each candle alert. The first look at an alert only records the
    latest candle, so you never get an alert for something that happened before you created it."""
    with db() as c:
        rows = c.execute("SELECT * FROM alerts WHERE active=1 AND kind='candle' AND symbol NOT IN (?,?)", (ALL_FX, ALL_OLYMP)).fetchall()
        for a in rows:
            try:
                cs = get_candles(a["symbol"], a["timeframe"], now)
            except Exception as e:  # noqa
                state["last_error"] = f"candles for {a['symbol']} failed: {e}"
                _diag[a["id"]] = f"fetch failed: {e}"
                continue
            if not cs:
                _diag[a["id"]] = "got 0 closed candles"
                continue
            _diag[a["id"]] = (f"{len(cs)} closed candles, newest opened {_stamp(cs[-1][0])}"
                              + ("" if len(cs) > 20 else " (need 21+ to judge a signal!)"))
            if cs[-1][0] <= (a["last_candle"] or 0):
                continue
            first = not a["last_candle"]
            c.execute("UPDATE alerts SET last_candle=? WHERE id=?", (cs[-1][0], a["id"]))
            if first:
                _res[a["id"]] = "first look recorded, waiting for the next close"
                continue
            hit = cdl.detect(cs, a["pattern"], use_volume=not no_volume(a["symbol"]), rule=json.loads(a["rule"]) if a["rule"] else None)
            _res[a["id"]] = f"candle {_stamp(cs[-1][0])} judged: " + ("MATCH" if hit else "no match")
            if not hit:
                continue
            paper.open_trade(c, a, a["symbol"], cs[-1], TIMEFRAMES[a["timeframe"]], hit, cs[-3:])  # pretend trade
            trader.queue(c, a, a["symbol"], cs[-1], TIMEFRAMES[a["timeframe"]])  # demo auto-trade, only if this alert has it ticked and the switch is on
            msg = f"{a['symbol']} {a['timeframe']} candle closed at {fmt(cs[-1][4])}: {hit}"
            if a["note"]:
                msg += f"\n{a['note']}"
            if notify(f"{a['symbol']} {a['timeframe']} signal", msg, "bar_chart") and not a["repeat"]:
                c.execute("UPDATE alerts SET active=0, last_fired=? WHERE id=?", (now, a["id"]))


ALL_OLYMP = "ALLOLYMP"  # special "symbol": watch every pair listed in OLYMP_PAIRS
ALL_FX = "ALLFOREX"  # special "symbol": watch every forex/metal pair OANDA offers
ALL_FX_TFS = {"5m", "10m", "15m", "1h"}  # OANDA's 4h and daily candles don't open on clock boundaries
_scan: dict = {}  # alert id -> progress of the scan for the candle that just closed


def scan_candles(sym: str, tf: str) -> list:
    if sym.startswith(olymp.PREFIX):
        asset = sym[len(olymp.PREFIX):]
        return cdl.aggregate(olymp.candles(asset, 300), 600) if tf == "10m" else olymp.candles(asset, TIMEFRAMES[tf])
    if tf == "10m":
        return cdl.aggregate(oanda_candles(sym, "5m"), 600)
    return oanda_candles(sym, tf)


def scan_all(now: float):
    """For each all-pairs alert: when a candle has just closed, check every pair once and send ONE bundled notification.
    Pairs whose candle hasn't appeared yet (or whose market is shut) are retried for up to 75 s, then skipped."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE active=1 AND kind='candle' AND symbol IN (?,?)", (ALL_FX, ALL_OLYMP))]
    for a in rows:
        sec = TIMEFRAMES[a["timeframe"]]
        exp = int(now) // sec * sec - sec  # open time of the candle that closed most recently
        if exp <= (a["last_candle"] or 0) or now < exp + sec + 5:
            continue
        def mark(done_alert=False):
            with db() as c:
                c.execute("UPDATE alerts SET last_candle=? WHERE id=?", (exp, a["id"]))
                if done_alert:
                    c.execute("UPDATE alerts SET active=0, last_fired=? WHERE id=?", (now, a["id"]))
        rule = json.loads(a["rule"]) if a["rule"] else None
        wsec = cdl.window_seconds(a["pattern"], rule)
        if not a["last_candle"] or (wsec and exp % wsec != 0):
            mark()  # first look only records the candle; window patterns only care about the window's first candle
            continue
        st = _scan.get(a["id"])
        if not st or st["exp"] != exp:
            st = _scan[a["id"]] = {"exp": exp, "done": set(), "hits": [], "next": 0.0}
        pairs_ = [olymp.PREFIX + p for p in olymp.PAIRS] if a["symbol"] == ALL_OLYMP else forex_pairs()
        if now >= st["next"]:
            st["next"] = now + 15
            for sym in pairs_:
                if sym in st["done"]:
                    continue
                try:
                    cs = scan_candles(sym, a["timeframe"])
                except Exception as e:  # noqa
                    state["last_error"] = f"scan of {sym} failed: {e}"
                    continue
                if not cs or cs[-1][0] < exp:
                    continue  # this pair's candle isn't out yet
                st["done"].add(sym)
                hit = cdl.detect(cs, a["pattern"], use_volume=False, rule=rule)
                if hit:
                    st["hits"].append(f"{sym} {fmt(cs[-1][4])}: {hit}")
                    with db() as c2:
                        paper.open_trade(c2, a, sym, cs[-1], sec, hit, cs[-3:])  # pretend trade (Olymp assets only)
                        trader.queue(c2, a, sym, cs[-1], sec)  # demo auto-trade (Olymp assets, alert must have it ticked)
        if len(st["done"]) < len(pairs_) and now < exp + sec + 75:
            continue
        hits = st.pop("hits")
        _diag[a["id"]] = f"candle {_stamp(exp)}: checked {len(st['done'])}/{len(pairs_)} pairs, {len(hits)} matched"
        _scan.pop(a["id"], None)
        sent = False
        if hits:
            body = "\n".join(hits[:10]) + (f"\n+{len(hits) - 10} more" if len(hits) > 10 else "")
            if a["note"]:
                body += f"\n{a['note']}"
            mk = "Olymp Trade" if a["symbol"] == ALL_OLYMP else "forex"
            sent = notify(f"{len(hits)} {mk} signal{'s' if len(hits) != 1 else ''} ({a['timeframe']})", body, "bar_chart")
        mark(done_alert=sent and not a["repeat"])


def trader_loop():
    while True:
        try:
            trader.confirm_step(db, notify)
            trader.process(db)
            err = trader.settle_real(db, notify)  # read back how sent demo trades ended, then apply the loss-streak stop
            if err:
                state["last_error"] = err
        except Exception as e:  # noqa
            state["last_error"] = f"auto-trade failed: {e}"
        time.sleep(2)


def deriv_loop():
    while True:
        try:
            deriv_bot.step(db, notify)  # reads back results always; looks for signals and trades only while switched on
        except Exception as e:  # noqa
            state["last_error"] = f"Deriv bot failed: {e}"
        time.sleep(2)


def paper_loop():
    while True:
        try:
            if olymp.enabled() and not olymp.status["expired"]:
                err = paper.settle_due(db, notify)
                if err:
                    state["last_error"] = err
                paper.fill_hist(db)
            playbook.step(db, notify)  # re-learn which setups keep working (only when there are new finished trades)
        except Exception as e:  # noqa
            state["last_error"] = f"paper trading failed: {e}"
        time.sleep(POLL)


def olymp_expiry_watch():
    """Tell the phone ONCE per expiry. A new token (or a successful fetch) re-arms it for the next time."""
    st = olymp.status
    if st["expired"] and not st["notified"] and time.time() >= st.get("retry_at", 0):
        st["retry_at"] = time.time() + 300  # if the phone push fails, try again in 5 minutes rather than every 10 seconds
        if notify("Olymp Trade token expired",
                  "Olymp alerts are paused. Open your Price Alerts page and paste a new access token.", "warning", loud=False):
            st["notified"] = True


def scan_loop():
    while True:
        try:
            olymp_expiry_watch()
        except Exception as e:  # noqa
            state["last_error"] = f"expiry check failed: {e}"
        try:
            if OANDA_TOKEN or olymp.enabled():
                scan_all(time.time())
        except Exception as e:  # noqa
            state["last_error"] = f"all-pairs scan failed: {e}"
        time.sleep(POLL)


def poll_forex(now: float, fresh: dict, last: dict):
    """Fetch only the forex pairs that have an active alert. OANDA: all in one request every POLL seconds.
    Yahoo fallback: one request per pair, throttled to FOREX_POLL."""
    with db() as c:
        wanted = [r_[0] for r_ in c.execute("SELECT DISTINCT symbol FROM alerts WHERE active=1") if is_forex(r_[0])]
    if not OANDA_TOKEN:
        wanted = [x for x in wanted if now - last.get(x, 0) >= FOREX_POLL]
        for x in wanted:
            last[x] = now
    if wanted:
        fresh.update(fetch_forex(wanted))


def poll_olymp(now: float, fresh: dict, last: dict):
    """Latest price of each Olymp Trade asset that has a price or %-move alert (candle alerts fetch their own data)."""
    if not olymp.enabled():
        return
    with db() as c:
        wanted = [r_[0] for r_ in c.execute("SELECT DISTINCT symbol FROM alerts WHERE active=1 AND kind!='candle'")
                  if r_[0].startswith(olymp.PREFIX)]
    for s_ in wanted:
        if now - last.get(s_, 0) < FOREX_POLL:
            continue
        last[s_] = now
        try:
            fresh[s_] = olymp.price(s_[len(olymp.PREFIX):])
        except Exception as e:  # noqa
            state["last_error"] = f"Olymp Trade price of {s_} failed: {e}"


def poll_loop():
    last_forex: dict[str, float] = {}
    last_olymp: dict[str, float] = {}
    while True:
        now = time.time()
        fresh: dict[str, float] = {}
        try:
            r = httpx.get(PRICE_URL, timeout=10)
            r.raise_for_status()
            fresh = {x["symbol"]: float(x["price"]) for x in r.json()}
            state["last_ok"], state["last_error"] = now, ""
        except Exception as e:  # noqa
            state["last_error"] = f"price fetch failed: {e}"
        try:
            poll_forex(now, fresh, last_forex)
            poll_olymp(now, fresh, last_olymp)
            with lock:
                prices.update(fresh)
                with db() as c:
                    watched = {r_[0] for r_ in c.execute(
                        "SELECT symbol FROM alerts WHERE active=1 AND kind='move'")}
                for s_ in watched:
                    if s_ in fresh:
                        history[s_].append((now, fresh[s_]))
                for s_ in [k for k in history if k not in watched]:
                    del history[s_]
                check_alerts(now)
            check_candles(now)
        except Exception as e:  # noqa
            state["last_error"] = f"alert check failed: {e}"
        time.sleep(POLL)


# ---- Data check: how often would your signals differ between OANDA and Olymp Trade? ----
CMP_TF, CMP_KEEP = "5m", 3 * 86400
with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS compare(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, open_time REAL, a TEXT, b TEXT, b_time REAL, ma TEXT, mb TEXT)""")


paper.init(db)
adapt.init(db)
playbook.init(db)
trader.init(db)
deriv_bot.init(db)


def compare_pairs() -> list:
    return [p for p in olymp.PAIRS if is_forex(p)]  # only pairs that exist on both sides (OTC assets have no OANDA twin)


def record_compare(exp: float):
    """Store the 5-minute candle that opened at `exp` from both sources, and which signals each one would have fired on."""
    with db() as c:
        crows = [dict(r) for r in c.execute("SELECT id, rule FROM alerts WHERE active=1 AND kind='candle' AND pattern='custom' AND timeframe=?", (CMP_TF,))]
    tests = [(p, p, None) for p in cdl.PATTERNS if p != "custom" and cdl.WINDOW_PATTERNS.get(p, (CMP_TF,))[0] == CMP_TF]
    for r in crows:
        rule = json.loads(r["rule"]) if r["rule"] else None
        if rule:
            tests.append((f"custom: {rule.get('name') or '#' + str(r['id'])}", "custom", rule))
    for sym in compare_pairs():
        try:
            with db() as c:
                if c.execute("SELECT 1 FROM compare WHERE symbol=? AND open_time=?", (sym, exp)).fetchone():
                    continue
            a, b = oanda_candles(sym, CMP_TF), olymp.candles(sym, 300)
        except Exception as e:  # noqa
            state["last_error"] = f"data check of {sym} failed: {e}"
            continue
        if not a or a[-1][0] != exp:
            continue  # market closed or OANDA has nothing for this candle
        b_ok = bool(b) and b[-1][0] == exp
        ma = [n for n, p, r in tests if cdl.detect(a, p, False, rule=r)]
        mb = [n for n, p, r in tests if cdl.detect(b, p, False, rule=r)] if b_ok else []
        with db() as c:
            c.execute("INSERT INTO compare(ts,symbol,open_time,a,b,b_time,ma,mb) VALUES(?,?,?,?,?,?,?,?)",
                      (time.time(), sym, exp, json.dumps(a[-1][1:5]), json.dumps(b[-1][1:5]) if b_ok else None,
                       b[-1][0] if b else None, json.dumps(ma), json.dumps(mb)))


def compare_loop():
    done = 0.0
    while True:
        try:
            if OANDA_TOKEN and olymp.enabled():
                now = time.time()
                exp = now // 300 * 300 - 300
                if exp > done and now >= exp + 300 + 20:  # give both feeds time to publish the closed candle
                    record_compare(exp)
                    done = exp
                    with db() as c:
                        c.execute("DELETE FROM compare WHERE ts<?", (now - CMP_KEEP,))
        except Exception as e:  # noqa
            state["last_error"] = f"data check failed: {e}"
        time.sleep(POLL)


def compare_stats(hours: float) -> dict:
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM compare WHERE ts>=? ORDER BY ts", (time.time() - hours * 3600,))]
    per, sig = {}, {}
    for r in rows:
        s = per.setdefault(r["symbol"], {"symbol": r["symbol"], "candles": 0, "missing": 0, "compared": 0, "sum": [0.0] * 4, "color_mismatch": 0})
        s["candles"] += 1
        if r["b"] is None:
            s["missing"] += 1  # Olymp had no candle with the same start time: a timing difference, or the asset isn't served
            continue
        a, b = json.loads(r["a"]), json.loads(r["b"])
        pip = 0.01 if "JPY" in r["symbol"] else 0.0001
        s["compared"] += 1
        for i in range(4):
            s["sum"][i] += abs(a[i] - b[i]) / pip
        if (a[3] > a[0]) != (b[3] > b[0]) or (a[3] < a[0]) != (b[3] < b[0]):
            s["color_mismatch"] += 1
        ma, mb = set(json.loads(r["ma"])), set(json.loads(r["mb"]))
        for n in ma | mb:
            d = sig.setdefault(n, {"signal": n, "both": 0, "oanda_only": 0, "olymp_only": 0})
            d["both" if n in ma and n in mb else "oanda_only" if n in ma else "olymp_only"] += 1
    syms = []
    for s in per.values():
        k = max(s["compared"], 1)
        syms.append({"symbol": s["symbol"], "candles": s["candles"], "missing": s["missing"], "compared": s["compared"],
                     "color_mismatch": s["color_mismatch"],
                     "avg_diff_pips": dict(zip(("open", "high", "low", "close"), (round(x / k, 2) for x in s["sum"])))})
    for d in sig.values():
        d["agree_pct"] = round(100 * d["both"] / (d["both"] + d["oanda_only"] + d["olymp_only"]))
    return {"symbols": syms, "signals": sorted(sig.values(), key=lambda d: d["signal"])}


app = FastAPI(title="Price Alerts")


@app.on_event("startup")
def _start():
    if not APP_TOKEN:
        print("WARNING: APP_TOKEN not set, anyone with the URL can manage your alerts")
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=compare_loop, daemon=True).start()
    threading.Thread(target=paper_loop, daemon=True).start()
    threading.Thread(target=trader_loop, daemon=True).start()
    threading.Thread(target=deriv_loop, daemon=True).start()


def auth(x_token: str = Header(default="")):
    if APP_TOKEN and not secrets.compare_digest(x_token, APP_TOKEN):
        raise HTTPException(401, "Wrong password")


_forex_cache: dict = {"list": None}
MAJORS = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"]


def forex_pairs() -> list[str]:
    """Every forex/metal pair OANDA offers (when OANDA_TOKEN is set), else the common majors and crosses."""
    if _forex_cache["list"]:
        return _forex_cache["list"]
    out: list[str] = []
    if OANDA_TOKEN:
        try:
            h = {"Authorization": f"Bearer {OANDA_TOKEN}"}
            if not _oanda_account["id"]:
                r = httpx.get(f"{OANDA_HOST}/v3/accounts", headers=h, timeout=10)
                r.raise_for_status()
                _oanda_account["id"] = r.json()["accounts"][0]["id"]
            r = httpx.get(f"{OANDA_HOST}/v3/accounts/{_oanda_account['id']}/instruments", headers=h, timeout=15)
            r.raise_for_status()
            out = sorted(i["name"].replace("_", "") for i in r.json()["instruments"]
                         if i.get("type") in ("CURRENCY", "METAL") and is_forex(i["name"].replace("_", "")))
        except Exception as e:  # noqa
            state["last_error"] = f"OANDA instrument list failed: {e}"
    if not out:
        out = [a + b for i, a in enumerate(MAJORS) for b in MAJORS[i + 1:]] + ["XAUUSD", "XAGUSD"]
    _forex_cache["list"] = out
    return out


@app.get("/api/pairs", dependencies=[Depends(auth)])
def pairs(q: str = "", kind: str = "all"):
    """Search every crypto and forex pair we can price. Empty q lists the most popular ones."""
    q = q.upper().replace("/", "").replace("-", "").replace("_", "").strip()
    popular = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCAD"]
    rows = []
    if kind in ("all", "crypto"):
        rows += [(k, "crypto") for k in prices if not is_forex(k)]
    if kind in ("all", "forex"):
        rows += [(k, "forex") for k in forex_pairs()]
    if olymp.enabled() and kind in ("all", "olymp"):
        rows += [(olymp.PREFIX + p, "olymp") for p in olymp.PAIRS]
    def rank(r):
        k = r[0]
        return (0 if k in popular and not q else 1, 0 if k.startswith(q) else 1, 0 if k.endswith("USDT") or k.endswith("USD") else 1, len(k), k)
    hits = sorted((r for r in rows if q in r[0].replace("_", "")), key=rank)[:25]
    if OANDA_TOKEN and kind in ("all", "forex") and q in ALL_FX:
        hits.insert(0, (ALL_FX, "every forex pair"))
    if olymp.enabled() and olymp.PAIRS and kind in ("all", "olymp") and q in ALL_OLYMP:
        hits.insert(0, (ALL_OLYMP, "every Olymp Trade pair"))
    fx = [k for k, t in hits if t == "forex" and k not in prices]
    if fx and (OANDA_TOKEN or len(fx) <= 3):
        try:
            prices.update(fetch_forex(fx))
        except Exception:  # noqa
            pass
    return [{"symbol": k, "type": t, "price": prices.get(k)} for k, t in hits]


class NewAlert(BaseModel):
    symbol: str = Field(min_length=3, max_length=40)
    kind: str = Field(pattern="^(above|below|move|candle)$")
    target: float = Field(default=0, ge=0)
    pattern: str = ""
    timeframe: str = ""
    window_min: int = Field(default=60, ge=1, le=1440)
    repeat: bool = False
    note: str = Field(default="", max_length=120)
    rule: dict = {}
    autotrade: int = Field(default=0, ge=0, le=2)  # 0 off, 1 trade at the signal, 2 wait for a confirmation candle


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api/patterns")
def patterns():
    return cdl.PATTERNS


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    return {"ntfy_topic_set": bool(NTFY_TOPIC or (PUSHOVER_TOKEN and PUSHOVER_USER)), "forex_source": "OANDA" if OANDA_TOKEN else "Yahoo (set OANDA_TOKEN for broker prices)", "olymp": f"on, {len(olymp.PAIRS)} pairs listed" if olymp.enabled() else "off",
            "olymp_token": {"set": olymp.enabled(), "expired": olymp.status["expired"], "since": olymp.status["since"], "last_ok": olymp.status["last_ok"]}, "last_ok": state["last_ok"], "error": state["last_error"],
            "pairs_loaded": len(prices)}


class NewToken(BaseModel):
    token: str = Field(min_length=8, max_length=4000)


@app.post("/api/olymp-token", dependencies=[Depends(auth)])
def olymp_set_token(n: NewToken):
    """Save a new Olymp Trade access token from the page. It is tried first; a token Olymp Trade refuses is not saved."""
    new = olymp.clean_token(n.token)
    if len(new) < 8:
        raise HTTPException(400, "That does not look like a token. Paste the access_token value")
    old, prev = olymp.TOKEN, dict(olymp.status)
    olymp.set_token(new)
    note = ""
    try:
        olymp.check()
    except olymp.TokenExpired:
        olymp.set_token(old)
        olymp.status.update(prev)
        raise HTTPException(400, "Olymp Trade did not accept that token. It may be a stale copy; copy it again right after logging in")
    except RuntimeError as e:  # e.g. the websockets package is missing, or no reply: cannot check, so save it and say so
        note = f"Saved, but could not check it: {e}"
    except Exception as e:  # noqa
        olymp.set_token(old)
        olymp.status.update(prev)
        raise HTTPException(400, f"Could not check the token with Olymp Trade: {e}")
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('olymp_token',?)", (new,))
    state["last_error"] = ""
    return {"ok": True, "note": note}


@app.delete("/api/olymp-token", dependencies=[Depends(auth)])
def olymp_clear_token():
    """Forget the token saved on the page (the OLYMP_ACCESS_TOKEN variable, if any, is used again after a restart)."""
    with db() as c:
        c.execute("DELETE FROM settings WHERE key='olymp_token'")
    olymp.set_token(os.getenv("OLYMP_ACCESS_TOKEN", ""))
    return {"ok": True}


# ---------------------------------------------------------------- Deriv demo bot (see deriv.py / deriv_bot.py)
class DerivCreds(BaseModel):
    token: str = Field(default="", max_length=4000)
    app_id: str = Field(min_length=1, max_length=40)
    account_id: str = Field(min_length=3, max_length=60)
    mode: str = Field(default="demo", pattern="^(demo|real)$")
    confirm: str = Field(default="", max_length=40)  # real mode needs the typed phrase


class DerivOn(BaseModel):
    on: bool
    confirm_live: bool = False  # switching ON in real-money mode needs this explicitly


LIVE_PHRASE = "TRADE REAL MONEY"


@app.get("/deriv", include_in_schema=False)
def deriv_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "deriv.html"))


@app.get("/api/deriv", dependencies=[Depends(auth)])
def deriv_overview():
    return deriv_bot.overview(db)


@app.post("/api/deriv/credentials", dependencies=[Depends(auth)])
def deriv_set_credentials(n: DerivCreds):
    """Test the token + app id + account FIRST (the account must match the chosen mode); only a working setup is saved.
    Real-money mode needs the typed phrase. Saving always switches the bot OFF, so it never trades on a changed account unseen."""
    if n.mode == "real" and n.confirm.strip().upper() != LIVE_PHRASE:
        raise HTTPException(400, f"To trade REAL money type exactly: {LIVE_PHRASE}")
    old = (deriv.PAT, deriv.APP_ID, deriv.ACCOUNT_ID, deriv.MODE)
    deriv.set_credentials(n.token.strip() or deriv.PAT, n.app_id, n.account_id, n.mode)
    try:
        note = deriv.check()
    except Exception as e:  # noqa
        deriv.set_credentials(*old)
        raise HTTPException(400, str(e))
    deriv_bot.set_on(db, False)
    with db() as c:
        for k, v in (("deriv_pat", deriv.PAT), ("deriv_app_id", deriv.APP_ID), ("deriv_account_id", deriv.ACCOUNT_ID), ("deriv_mode", deriv.MODE)):
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    return {"ok": True, "note": note + ". The bot is OFF: switch it on when you are ready."}


@app.delete("/api/deriv/credentials", dependencies=[Depends(auth)])
def deriv_clear_credentials():
    deriv_bot.set_on(db, False)
    with db() as c:
        c.execute("DELETE FROM settings WHERE key IN ('deriv_pat','deriv_app_id','deriv_account_id','deriv_mode')")
    deriv.set_credentials(os.getenv("DERIV_PAT", ""), os.getenv("DERIV_APP_ID", ""), os.getenv("DERIV_ACCOUNT_ID", ""), "demo")
    return {"ok": True}


@app.post("/api/deriv/config", dependencies=[Depends(auth)])
def deriv_save_config(raw: dict):
    old = deriv_bot.config(db)
    try:
        cfg = deriv_bot.save_config(db, raw)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    for s in cfg["symbols"]:  # a symbol name Deriv does not know would never trade: say so now
        if s not in old["symbols"]:
            try:
                deriv.candles(s, 60, 3)
            except Exception as e:  # noqa
                deriv_bot.save_config(db, {"symbols": old["symbols"]})
                raise HTTPException(400, f"Deriv does not accept the symbol {s}: {e}")
    return {"ok": True, "config": cfg}


@app.post("/api/deriv/on", dependencies=[Depends(auth)])
def deriv_switch(n: DerivOn):
    if n.on:
        m = deriv_bot.missing(db)
        if m:
            raise HTTPException(400, "Set first: " + ", ".join(m))
        if deriv.MODE == "real" and not n.confirm_live:
            raise HTTPException(400, "This bot is in REAL-MONEY mode: switching it on needs your explicit confirmation")
    deriv_bot.set_on(db, n.on)
    return {"ok": True, "on": n.on}



def clean_asset(raw: str) -> str:
    a = raw.upper().strip()
    if a.startswith(olymp.PREFIX):
        a = a[len(olymp.PREFIX):]
    return a.replace(" ", "_").replace("/", "").replace("-", "_")


class NewOlymp(BaseModel):
    name: str = Field(min_length=2, max_length=40)


@app.get("/api/olymp-pairs", dependencies=[Depends(auth)])
def olymp_list():
    return {"enabled": olymp.enabled(), "pairs": list(olymp.PAIRS)}


@app.post("/api/olymp-pairs", dependencies=[Depends(auth)])
def olymp_add(n: NewOlymp):
    """Type an OTC asset name on the page: it is test-fetched from Olymp Trade first (read-only), then remembered."""
    if not olymp.enabled():
        raise HTTPException(400, "Olymp Trade needs OLYMP_ACCESS_TOKEN to be set on the server")
    name = clean_asset(n.name)
    if len(name) < 2:
        raise HTTPException(400, "Type the asset name, like EURUSD_OTC")
    if name in olymp.PAIRS:
        return {"ok": True, "pairs": list(olymp.PAIRS), "note": "already on your list"}
    try:
        got = olymp.raw_candles(name, 300, 5)
    except Exception as e:  # noqa
        raise HTTPException(400, f"Could not read {name} from Olymp Trade: {e}")
    if not got:
        raise HTTPException(400, f"Olymp Trade returned no candles for {name}. Check the spelling exactly as Olymp Trade names it")
    olymp.PAIRS.append(name)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO olymp_pairs(name, added) VALUES(?,?)", (name, time.time()))
    return {"ok": True, "pairs": list(olymp.PAIRS)}


@app.delete("/api/olymp-pairs/{name}", dependencies=[Depends(auth)])
def olymp_remove(name: str):
    name = clean_asset(name)
    with db() as c:
        c.execute("DELETE FROM olymp_pairs WHERE name=?", (name,))
    if name in olymp.PAIRS:
        olymp.PAIRS.remove(name)  # a pair set in OLYMP_PAIRS comes back after a restart
    return {"ok": True, "pairs": list(olymp.PAIRS)}


@app.get("/api/trader", dependencies=[Depends(auth)])
def trader_status():
    return trader.overview(db)


@app.post("/api/trader/config", dependencies=[Depends(auth)])
def trader_config(v: dict):
    try:
        trader.save_config(db, v)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class Switch(BaseModel):
    on: bool


@app.post("/api/trader/switch", dependencies=[Depends(auth)])
def trader_switch(s: Switch):
    if s.on and trader.missing():
        raise HTTPException(400, "Fill in and save " + ", ".join(trader.missing()) + " first")
    trader.set_on(db, s.on)
    return {"ok": True, "on": s.on}


@app.get("/api/adapt", dependencies=[Depends(auth)])
def adapt_status():
    return adapt.overview(db)


@app.get("/api/adapt/case1", dependencies=[Depends(auth)])
def adapt_case1():
    return adapt.case1_whatif(db)


@app.get("/api/adapt/drivers", dependencies=[Depends(auth)])
def adapt_drivers(family: str = "case 1"):
    return adapt.drivers(db, family)


class PbMode(BaseModel):
    mode: str = ""
    level: str = ""
    signals: int = -1


@app.get("/api/playbook", dependencies=[Depends(auth)])
def playbook_status():
    return playbook.overview(db)


@app.post("/api/playbook/settings", dependencies=[Depends(auth)])
def playbook_settings(s: PbMode):
    try:
        if s.mode:
            playbook.set_mode(db, s.mode)
        if s.level:
            playbook.set_level(db, s.level)
        if s.signals in (0, 1):
            playbook.set_signals(db, bool(s.signals))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/playbook/refresh", dependencies=[Depends(auth)])
def playbook_refresh():
    r = playbook.refresh(db, notify)
    return {"ok": True, "rows": r["rows"], "events": [t for _, t in r["events"]]}


@app.get("/api/playbook/replay", dependencies=[Depends(auth)])
def playbook_replay():
    return playbook.replay(playbook.load(db, 1500), playbook.level(db))


@app.post("/api/adapt/switch", dependencies=[Depends(auth)])
def adapt_switch(s: Switch):
    adapt.set_on(db, s.on)
    return {"ok": True, "on": s.on}


@app.get("/api/paper", dependencies=[Depends(auth)])
def paper_results(hours: float = 168):
    return paper.stats(db, min(max(hours, 1), 720))


@app.get("/api/paper/analysis", dependencies=[Depends(auth)])
def paper_analysis(hours: float = 168, exp: int = 0, signal: str = "ALL SIGNALS"):
    return paper.analysis(db, min(max(hours, 1), 720), exp, signal)


@app.get("/api/trader/real", dependencies=[Depends(auth)])
def trader_real(hours: float = 168):
    return trader.real_stats(db, min(max(hours, 1), 720))


@app.post("/api/paper/notify", dependencies=[Depends(auth)])
def paper_notify(s: Switch):
    paper.set_settle_notify(db, s.on)
    return {"ok": True, "on": s.on}


@app.get("/api/paper/journal", dependencies=[Depends(auth)])
def paper_journal(hours: float = 168):
    h = min(max(hours, 1), 720)
    return {"trades": paper.journal(db, h), "by_pair": paper.by_pair(db, h)}


@app.get("/api/compare", dependencies=[Depends(auth)])
def compare(hours: float = 24):
    return {"enabled": bool(OANDA_TOKEN and olymp.enabled()), "hours": hours, "pairs": compare_pairs(), **compare_stats(min(max(hours, 1), 72))}


@app.get("/api/alerts", dependencies=[Depends(auth)])
def list_alerts():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY id DESC")]
    for r in rows:
        r["price"] = prices.get(r["symbol"])
        r["diag"] = "; ".join(x for x in (_diag.get(r["id"], "not checked yet"), _res.get(r["id"], "")) if x)
    return rows


@app.post("/api/alerts", dependencies=[Depends(auth)])
def add_alert(a: NewAlert):
    sym = a.symbol.upper().replace("/", "").replace("-", "").strip()
    if a.kind == "candle":
        if a.pattern not in cdl.PATTERNS or a.timeframe not in TIMEFRAMES:
            raise HTTPException(400, "Pick a candle signal and a timeframe")
        if a.pattern in cdl.WINDOW_PATTERNS and a.timeframe != cdl.WINDOW_PATTERNS[a.pattern][0]:
            raise HTTPException(400, f"That signal uses {cdl.WINDOW_PATTERNS[a.pattern][0]} candles")
    elif a.target <= 0:
        raise HTTPException(400, "Enter a price (or a % move) above 0")
    if a.autotrade and (a.kind != "candle" or not (sym.startswith(olymp.PREFIX) or sym == ALL_OLYMP)):
        raise HTTPException(400, "Auto-trade only works on candle alerts for Olymp Trade assets (OLYMP:... or ALLOLYMP)")
    rule = None
    if a.kind == "candle" and a.pattern == "custom":
        try:
            rule = cdl.clean_rule(a.rule)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if rule.get("window_min") and rule["window_min"] * 60 % TIMEFRAMES[a.timeframe]:
            raise HTTPException(400, f"The window ({rule['window_min']} min) must be a whole number of {a.timeframe} candles")
    if sym == ALL_OLYMP or sym.startswith(olymp.PREFIX):
        if not olymp.enabled():
            raise HTTPException(400, "Olymp Trade needs OLYMP_ACCESS_TOKEN to be set on the server")
        if sym == ALL_OLYMP:
            if a.kind != "candle":
                raise HTTPException(400, "Watching every pair only works with candle signals")
            if not olymp.PAIRS:
                raise HTTPException(400, "Set OLYMP_PAIRS to your OTC asset names (comma-separated) to watch them all")
            if a.timeframe not in ALL_FX_TFS:
                raise HTTPException(400, "For every pair, use 5m, 10m, 15m or 1h candles")
        elif len(sym) <= len(olymp.PREFIX):
            raise HTTPException(400, "Type the Olymp Trade asset after OLYMP:, like OLYMP:EURUSD")
        else:
            asset = clean_asset(sym)
            sym = olymp.PREFIX + asset
            if asset not in olymp.PAIRS:
                olymp_add(NewOlymp(name=asset))  # tests it against Olymp Trade, raises a clear error if unknown
    elif sym == ALL_FX:
        if a.kind != "candle":
            raise HTTPException(400, "Watching every pair only works with candle signals")
        if not OANDA_TOKEN:
            raise HTTPException(400, "Watching every forex pair needs OANDA_TOKEN to be set")
        if a.timeframe not in ALL_FX_TFS:
            raise HTTPException(400, "For every pair, use 5m, 10m, 15m or 1h candles")
    elif is_forex(sym) and sym not in prices:
        got = fetch_forex([sym])
        if sym in got:
            prices[sym] = got[sym]
        else:
            raise HTTPException(400, f"Could not get a price for {sym}. Try e.g. EURUSD, GBPJPY, XAUUSD")
    elif prices and sym not in prices:
        raise HTTPException(400, f"{sym} not found. Use a crypto pair like BTCUSDT or a forex pair like EURUSD")
    with db() as c:
        c.execute("INSERT INTO alerts(symbol,kind,target,window_min,repeat,note,created,pattern,timeframe,last_candle,rule,autotrade)"
                  " VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
                  (sym, a.kind, a.target, a.window_min, int(a.repeat), a.note, time.time(), a.pattern, a.timeframe,
                   json.dumps(rule) if rule else None, a.autotrade))
    return {"ok": True}


@app.post("/api/alerts/{aid}/toggle", dependencies=[Depends(auth)])
def toggle(aid: int):
    with db() as c:
        c.execute("UPDATE alerts SET active=1-active, last_fired=0 WHERE id=?", (aid,))
    return {"ok": True}


@app.delete("/api/alerts/{aid}", dependencies=[Depends(auth)])
def delete(aid: int):
    with db() as c:
        c.execute("DELETE FROM alerts WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/api/test", dependencies=[Depends(auth)])
def test():
    ok = notify("Test alert", "If you can read this on your phone, alerts work.", "white_check_mark")
    if not ok:
        raise HTTPException(502, state["last_error"] or "ntfy did not accept the message")
    return {"ok": True}
