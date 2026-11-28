"""Automatic trading on your Olymp Trade DEMO account, driven by candle alerts you tick "Auto-trade" on.

DEMO ONLY: the frame always says group "demo", olymp.send_frame refuses any trade whose group is not "demo", and nothing
here can be pointed at real money. It is OFF until you switch it on on the page, needs three settings (below), and has
hourly and daily trade caps. Every attempt is saved (including the ones that were not sent, and why).

These are filled in on the page (stored in the database); environment variables below are only the starting values.
No defaults for the first three, so nothing trades by accident:
  OLYMP_DEMO_ACCOUNT_ID  your demo account id (the account_id in the trade message you captured)
  OLYMP_TRADE_AMOUNT     the stake, in the SAME units the website sends (your capture: 1500)
  OLYMP_TRADE_DURATION   the expiry, in the SAME units the website sends (your capture: 20). Place one trade by hand and
                         check the number it sends against the expiry you chose on screen.
  OLYMP_MAX_PER_HOUR (default 6), OLYMP_MAX_PER_DAY (default 30)
  OLYMP_MAX_LATE_SECONDS (default 20): skip a signal seen later than this after its candle closed (the trade should
                         start right at the close, not half a minute later)
"""
import json, os, random, re, string, time

import candles as cdl
import olymp
import adapt
import playbook
import paper

ACCOUNT_ID = int(os.getenv("OLYMP_DEMO_ACCOUNT_ID", "0") or 0)
AMOUNT = int(os.getenv("OLYMP_TRADE_AMOUNT", "0") or 0)
STAKE_MODE = 0  # 0 = the fixed AMOUNT, 1 = STAKE_PCT percent of BALANCE
STAKE_PCT = 1.0  # percent of the balance to stake per trade (mode 1)
BALANCE = 0  # your demo balance in the SAME units as the stake; kept up to date from the estimated results of the trades sent
STAKE_MIN = 0  # mode 1: never stake less than this (0 = no minimum)
STAKE_MAX = 0  # mode 1: never stake more than this (0 = no maximum)
DURATION = int(os.getenv("OLYMP_TRADE_DURATION", "0") or 0)
MAX_HOUR = int(os.getenv("OLYMP_MAX_PER_HOUR", "6"))
MAX_DAY = int(os.getenv("OLYMP_MAX_PER_DAY", "30"))
MARGIN = int(os.getenv("OLYMP_END_MARGIN", "1"))  # a whole-candle trade ends this many seconds BEFORE the candle closes
MIN_HOLD = 10  # seconds: a whole-candle trade with less than this left is skipped
MAX_AGE = 90  # seconds a queued trade may wait to be sent
MAX_STREAK = int(os.getenv("OLYMP_MAX_LOSS_STREAK", "4"))  # switch auto-trade OFF after this many losses in a row (0 = never)
MAX_DAY_LOSSES = int(os.getenv("OLYMP_MAX_DAY_LOSSES", "8"))  # ...or after this many losses in 24 hours (0 = never)
MAX_LATE = int(os.getenv("OLYMP_MAX_LATE_SECONDS", "20"))  # a signal seen later than this after its candle closed is not traded


FIELDS = {  # name -> (label, min, max)
    "account_id": ("Demo account ID", 1, 10**12), "amount": ("Stake", 0, 10**9), "duration": ("Expiry (seconds)", 1, 86400),
    "max_hour": ("Max trades per hour", 1, 1000), "max_day": ("Max trades per day", 1, 10000),
    "max_late": ("Skip a signal seen more than this many seconds after its candle closed", 1, 600),
    "margin": ("Finish this many seconds before the trading candle closes", 0, 30),
    "max_streak": ("Switch OFF after this many losses in a row (0 = never)", 0, 100),
    "max_day_losses": ("Switch OFF after this many losses in 24 hours (0 = never)", 0, 1000),
}


EXTRA = {  # whole-number settings besides FIELDS; the % is handled on its own
    "stake_mode": ("Stake type", 0, 1), "balance": ("Balance", 0, 10**12),
    "stake_min": ("Minimum stake", 0, 10**9), "stake_max": ("Maximum stake", 0, 10**9),
}
PCT_RANGE = (0.1, 25.0)


def stake() -> int:
    """The stake for the next trade: the fixed amount, or STAKE_PCT percent of the balance (kept between the min and max)."""
    if not STAKE_MODE:
        return AMOUNT
    amt = int(round(BALANCE * STAKE_PCT / 100))
    if STAKE_MIN:
        amt = max(amt, STAKE_MIN)
    if STAKE_MAX:
        amt = min(amt, STAKE_MAX)
    return max(amt, 0)


def _apply(v: dict):
    global ACCOUNT_ID, AMOUNT, DURATION, MAX_HOUR, MAX_DAY, MAX_LATE, MARGIN, MAX_STREAK, MAX_DAY_LOSSES
    global STAKE_MODE, STAKE_PCT, BALANCE, STAKE_MIN, STAKE_MAX
    STAKE_MODE, BALANCE, STAKE_MIN, STAKE_MAX = int(v["stake_mode"]), int(v["balance"]), int(v["stake_min"]), int(v["stake_max"])
    STAKE_PCT = float(v["stake_pct"])
    ACCOUNT_ID, AMOUNT, DURATION = int(v["account_id"]), int(v["amount"]), int(v["duration"])
    MAX_HOUR, MAX_DAY, MAX_LATE, MARGIN = int(v["max_hour"]), int(v["max_day"]), int(v["max_late"]), int(v["margin"])
    MAX_STREAK, MAX_DAY_LOSSES = int(v["max_streak"]), int(v["max_day_losses"])


def config() -> dict:
    return {"account_id": ACCOUNT_ID, "amount": AMOUNT, "duration": DURATION,
            "max_hour": MAX_HOUR, "max_day": MAX_DAY, "max_late": MAX_LATE, "margin": MARGIN,
            "max_streak": MAX_STREAK, "max_day_losses": MAX_DAY_LOSSES,
            "stake_mode": STAKE_MODE, "stake_pct": STAKE_PCT, "balance": BALANCE, "stake_min": STAKE_MIN, "stake_max": STAKE_MAX}


def save_config(db, raw: dict):
    """Validate the form from the page, keep it in the database (so it survives restarts) and use it from now on."""
    v = {}
    for k, (label, lo, hi) in FIELDS.items():
        try:
            n = int(str(raw.get(k, "")).strip().replace(",", ""))
        except ValueError:
            raise ValueError(f"{label}: enter a whole number")
        if not lo <= n <= hi:
            raise ValueError(f"{label}: must be between {lo} and {hi}")
        v[k] = n
    cur = config()
    for k, (label, lo, hi) in EXTRA.items():
        try:
            n = int(str(raw[k] if k in raw else cur[k]).strip().replace(",", "") or 0)  # absent (an older page): keep the current value
        except ValueError:
            raise ValueError(f"{label}: enter a whole number")
        if not lo <= n <= hi:
            raise ValueError(f"{label}: must be between {lo} and {hi}")
        v[k] = n
    try:
        pct = float(str(raw["stake_pct"] if "stake_pct" in raw else cur["stake_pct"]).strip().replace(",", "."))
    except ValueError:
        raise ValueError("Stake %: enter a number like 1 or 2.5")
    if not PCT_RANGE[0] <= pct <= PCT_RANGE[1]:
        raise ValueError(f"Stake %: must be between {PCT_RANGE[0]} and {PCT_RANGE[1]}")
    v["stake_pct"] = pct
    if v["stake_max"] and v["stake_min"] > v["stake_max"]:
        raise ValueError("Minimum stake cannot be above the maximum stake")
    _store(db, v)
    _apply(v)


def _store(db, v: dict):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('trader_cfg',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(v),))


def init(db):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='trader_cfg'").fetchone()
        if r and r[0]:  # what you saved on the page wins over the environment variables
            try:
                _apply({**config(), **json.loads(r[0])})
            except (ValueError, KeyError):
                pass
        c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, alert_id INTEGER, asset TEXT, direction TEXT, candle_ts REAL,
            amount INTEGER, duration INTEGER, uuid TEXT, status TEXT, reply TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, alert_id INTEGER, asset TEXT, pattern TEXT, sig_dir TEXT, sec INTEGER,
            conf_open REAL, status TEXT)""")
        if "warned" not in {r[1] for r in c.execute("PRAGMA table_info(pending)")}:
            c.execute("ALTER TABLE pending ADD COLUMN warned INTEGER DEFAULT 0")  # 0 not checked, 1 get-ready sent, 2 30-second reminder sent, -1 nothing to warn about
        have_t = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        if "ends_at" not in have_t:
            c.execute("ALTER TABLE trades ADD COLUMN ends_at REAL")
        for col, typ in (("sent_at", "REAL"), ("dur_sent", "INTEGER"), ("entry_px", "REAL"), ("exit_px", "REAL"),
                         ("result", "TEXT"), ("pips", "REAL"), ("settled", "INTEGER DEFAULT 0"), ("payout", "REAL")):
            if col not in have_t:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")  # filled in when the trade is sent / its result is read back
        have = {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
        if "autotrade" not in have:
            c.execute("ALTER TABLE alerts ADD COLUMN autotrade INTEGER DEFAULT 0")


def missing() -> list:
    if STAKE_MODE:  # percent of balance: needs the balance, and it must work out to a stake above zero
        need = (("Demo account ID", ACCOUNT_ID), ("Balance", BALANCE), ("Stake (the balance times the % rounds to 0: raise the % or the minimum stake)", stake() if BALANCE else 1), ("Expiry", DURATION))
    else:
        need = (("Demo account ID", ACCOUNT_ID), ("Stake", AMOUNT), ("Expiry", DURATION))
    return [n for n, v in need if not v]


def is_on(c) -> bool:
    r = c.execute("SELECT value FROM settings WHERE key='autotrade_on'").fetchone()
    return bool(r and r[0] == "1")


def set_on(db, on: bool):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('autotrade_on',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("1" if on else "0",))
        if on:  # a loss streak is counted from the moment YOU switched it on, so switching on again after a stop starts fresh
            c.execute("INSERT INTO settings(key,value) VALUES('autotrade_since',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (str(time.time()),))


def _log(c, alert_id, asset, direction, candle_ts, status, duration=None, ends_at=None):
    c.execute("INSERT INTO trades(ts,alert_id,asset,direction,candle_ts,amount,duration,status,ends_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (time.time(), alert_id, asset, direction, candle_ts, stake(), duration or DURATION, status, ends_at))


def _enqueue(c, alert_id, asset, d, cts, start_ts, duration=None, ends_at=None):
    """The shared checks, then one 'queued' trade row (or a row saying why not). start_ts is when the trade should have started."""
    if d is None:
        return _log(c, alert_id, asset, None, cts, "not traded: the signal has no clear direction")
    late = time.time() - start_ts
    if late > MAX_LATE:
        return _log(c, alert_id, asset, d, cts, f"not traded: seen {late:.0f}s after the moment to enter (limit {MAX_LATE}s)", duration, ends_at)
    if not is_on(c):
        return _log(c, alert_id, asset, d, cts, "not traded: the auto-trade switch is OFF", duration, ends_at)
    if missing():
        return _log(c, alert_id, asset, d, cts, "not traded: fill in " + ", ".join(missing()) + " in the Auto-trade card", duration, ends_at)
    if c.execute("SELECT 1 FROM trades WHERE alert_id=? AND asset=? AND candle_ts=? AND status NOT LIKE 'not traded%'",
                 (alert_id, asset, cts)).fetchone():
        return
    live = "(status='queued' OR status='sending' OR status LIKE 'sent%')"
    n_h = c.execute(f"SELECT COUNT(*) FROM trades WHERE {live} AND ts>?", (time.time() - 3600,)).fetchone()[0]
    n_d = c.execute(f"SELECT COUNT(*) FROM trades WHERE {live} AND ts>?", (time.time() - 86400,)).fetchone()[0]
    if n_h >= MAX_HOUR or n_d >= MAX_DAY:
        return _log(c, alert_id, asset, d, cts, f"not traded: cap reached ({MAX_HOUR}/hour, {MAX_DAY}/day)", duration, ends_at)
    _log(c, alert_id, asset, d, cts, "queued", duration, ends_at)


def queue(c, a, sym: str, candle, sec: int):
    """Called with the open DB connection when a candle alert fires (the SIGNAL candle just closed).
    autotrade 1: trade straight away with the fixed expiry. autotrade 2: wait for the confirmation candle first."""
    try:
        mode = int(a["autotrade"] or 0)
        if not mode or not sym.startswith(olymp.PREFIX):
            return
    except (KeyError, IndexError, TypeError):
        return
    asset, cts = sym[len(olymp.PREFIX):], candle[0]
    d = paper.direction(a["pattern"], candle)
    if mode == 2:
        if d is None:
            return _log(c, a["id"], asset, None, cts, "not traded: the signal has no clear direction")
        if not c.execute("SELECT 1 FROM pending WHERE alert_id=? AND asset=? AND conf_open=?", (a["id"], asset, cts + sec)).fetchone():
            c.execute("INSERT INTO pending(ts,alert_id,asset,pattern,sig_dir,sec,conf_open,status) VALUES(?,?,?,?,?,?,?,'waiting')",
                      (time.time(), a["id"], asset, a["pattern"], d, sec, cts + sec))
        return
    _enqueue(c, a["id"], asset, d, cts, cts + sec)


def _headsup(db, p, close, now, notify):
    """Before the confirmation candle closes: a GET READY message as soon as the signal candle has closed (if an active playbook rule could still
    match once the confirmation candle is known), and a reminder 30 seconds before the entry moment. The final ENTER message comes at the close."""
    w = p.get("warned") or 0
    if w < 0 or w >= 2 or not playbook.signals_on(db):
        return

    def mark(v):
        with db() as c:
            c.execute("UPDATE pending SET warned=? WHERE id=?", (v, p["id"]))
    at = time.strftime("%H:%M:%S", time.gmtime(close))
    if w == 0:
        try:
            sec = p["sec"]
            cs = olymp.candles(p["asset"], sec) if sec in (60, 300) else cdl.aggregate(olymp.candles(p["asset"], 300), sec)
        except Exception:  # noqa
            return  # try again on the next pass
        full = [x for x in cs if x[0] < p["conf_open"] - 0.5]
        hist = [paper._ohlc(x) for x in full[-41:-1]]
        hist = hist if len(hist) >= 25 and full and abs(hist[-1][0] - (full[-1][0] - p["sec"])) < 1 else []
        if not full or abs(full[-1][0] - (p["conf_open"] - p["sec"])) >= 1:
            return mark(-1)
        feats = adapt._features({"ctx": paper.make_ctx(p["sec"], full[-3:], 1, None), "hist": json.dumps(hist), "sig_dir": p["sig_dir"],
                                 "direction": p["sig_dir"], "entry_time": close, "symbol": olymp.PREFIX + p["asset"], "signal": p["pattern"]}) or {}
        h = playbook.heads_up(db, p["sig_dir"], feats)
        if not h:
            return mark(-1)
        mark(1)
        way = "UP (CALL)" if h["dir"] == "up" else "DOWN (PUT)" if h["dir"] == "down" else "UP or DOWN (decided by the confirmation candle)"
        try:
            notify(f"GET READY {p['asset']}",
                   f"{p['asset']}: a setup is forming. Possible entry {way} at {at} UTC, when the confirmation candle closes (in about {max(int(close - now), 0)} s). "
                   f"Open the asset and set the expiry now. A final ENTER message follows only if the confirmation candle qualifies. Rule: {h['rules'][0]}",
                   "hourglass_flowing_sand", False)
        except Exception:  # noqa
            pass
    elif w == 1 and close - now <= 30:
        mark(2)
        try:
            notify(f"{p['asset']}: 30 seconds", f"{p['asset']}: the confirmation candle closes at {at} UTC. If it qualifies you will get ENTER right then.", "alarm_clock", False)
        except Exception:  # noqa
            pass


def confirm_step(db, notify):
    """For each signal waiting on its confirmation candle: when that candle has closed, judge it. If it matches case 1, 2 or 3,
    queue ONE trade that starts now (the open of the trading candle) and expires when the trading candle closes."""
    now = time.time()
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM pending WHERE status='waiting'")]
        c.execute("DELETE FROM pending WHERE ts<?", (now - 7 * 86400,))
    for p in rows:
        def upd(s):
            with db() as c:
                c.execute("UPDATE pending SET status=? WHERE id=?", (s, p["id"]))
        close = p["conf_open"] + p["sec"]
        if now < close + 1:
            try:
                _headsup(db, p, close, now, notify)
            except Exception:  # noqa
                pass  # a heads-up must never get in the way of the trade logic
            continue
        if now - close > MAX_LATE:
            upd(f"no trade: the confirmation candle was not read within {MAX_LATE}s of closing")
            continue
        try:
            sec = p["sec"]  # sizes Olymp may not serve (10m, 15m...) are built from its 5-minute candles
            cs = olymp.candles(p["asset"], sec) if sec in (60, 300) else cdl.aggregate(olymp.candles(p["asset"], 300), sec)
            cand = next((x for x in cs if abs(x[0] - p["conf_open"]) < 1), None)
        except Exception:  # noqa
            continue  # try again on the next pass until the time limit above
        if cand is None:
            continue
        res = cdl.confirm(cand, p["sig_dir"])
        if res is None:
            upd("no trade: the next candle did not match case 1, 2 or 3: " + cdl.confirm_why(cand, p["sig_dir"]))
            continue
        case, d, why = res
        d, anote = adapt.pick(db, case, p["sig_dir"], d)  # may trade the other way if this case's default direction has been losing
        if anote:
            why = f"{why}; {anote}"
        name = f"{p['pattern']} + confirmation {case}"
        full = [x for x in cs if x[0] < p["conf_open"] - 0.5]  # closed candles up to and including the signal candle
        hist = [paper._ohlc(x) for x in full[-41:-1]]
        hist = hist if len(hist) >= 25 and full and abs(hist[-1][0] - (full[-1][0] - p["sec"])) < 1 else []  # must end right before the signal candle
        ctx = paper.make_ctx(p["sec"], full[-3:], 1, cand)
        feats = adapt._features({"ctx": ctx, "hist": json.dumps(hist), "sig_dir": p["sig_dir"], "direction": d, "entry_time": close,
                                 "symbol": olymp.PREFIX + p["asset"], "signal": name}) or {}
        feats["confirmation case"] = case
        d2, pnote = playbook.apply(db, p["sig_dir"], d, feats)  # the playbook: rules that have been persistent decide, or veto the trade
        if pnote:
            why = f"{why}; {pnote}"
        if d2 is None:
            upd(f"confirmed ({case}) but not traded: {pnote}")
        else:
            d = d2
            upd(f"confirmed ({case}): trade {d}" + (" (adapted)" if anote else "") + (" (playbook)" if pnote else ""))
        with db() as c:
            if d2 is not None:
                _enqueue(c, p["alert_id"], p["asset"], d, p["conf_open"], close, p["sec"], close + p["sec"])
            paper.open_confirmed(c, p["alert_id"], olymp.PREFIX + p["asset"], name, d, close, cand[4], p["sec"],
                                 ctx, sig_dir=p["sig_dir"], hist=json.dumps(hist))  # always kept as a pretend trade: that is what the playbook learns from
        sg = playbook.signal(db, p["sig_dir"], feats)  # entry signal for trading BY HAND on a live account (works with the switch off)
        if sg:
            end = time.strftime("%H:%M:%S", time.gmtime(close + p["sec"]))
            try:
                notify(f"ENTER {p['asset']} {'UP / CALL' if sg['dir'] == 'up' else 'DOWN / PUT'} now",
                       f"{p['asset']}: buy {'UP (CALL)' if sg['dir'] == 'up' else 'DOWN (PUT)'} NOW, expiry {end} UTC (the end of this {p['sec'] // 60} min candle, "
                       f"about {round(max(close + p['sec'] - time.time(), 0) / 60)} min). Setup: {name}. Why: {sg['rules'][0]}. Enter only if you still get the payout; skip if it is late.",
                       "rotating_light", False)
                playbook.log(db, "signal", f"{p['asset']} {sg['dir'].upper()} until {end} UTC ({name})")
            except Exception:  # noqa
                pass
        if d2 is None:
            continue
        if sg:
            continue  # the entry signal above already told you
        try:
            notify(f"{p['asset']} confirmed", f"{name}: {why}. Trading {d} for the whole next {p['sec'] // 60} min candle.", "bar_chart", False)
        except Exception:  # noqa
            pass


def build(asset: str, direction: str, uuid_: str, duration: int, amount: int = 0) -> list:
    return [{"t": 2, "e": 23, "uuid": uuid_, "d": [{
        "account_id": ACCOUNT_ID, "amount": amount or stake(), "cat": "digital", "dir": direction, "duration": duration,
        "group": "demo", "is_flex": False, "mss_id": "", "pair": asset, "pos": 0, "pos_v2": None,
        "risk_free_id": None, "ta": {"ct": "candles", "sc": 300}, "time_close": 0, "timestamp": int(time.time() * 1000)}]}]


def process(db):
    """Send queued trades one at a time. Never holds the database while talking to Olymp Trade."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE status='queued' ORDER BY id")]
    for r in rows:
        def done(status, reply=None, uuid_=None):
            with db() as c:
                c.execute("UPDATE trades SET status=?, reply=?, uuid=COALESCE(?,uuid) WHERE id=?", (status, reply, uuid_, r["id"]))
        with db() as c:
            on = is_on(c)
        if time.time() - r["ts"] > MAX_AGE:
            done("not traded: the signal was too old by the time it could be sent")
        elif not on or missing() or not olymp.enabled():
            done("not traded: switched off (or settings missing) before it was sent")
        else:
            dur = r["duration"]
            if r.get("ends_at"):  # whole-candle trade: expire just BEFORE the trading candle closes, so the next candle never starts
                dur = min(dur, int(r["ends_at"] - time.time()) - MARGIN)
                if dur < MIN_HOLD:
                    done(f"not traded: only {max(dur, 0)}s of the candle were left")
                    continue
            u = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
            done("sending", None, u)  # marked first so a crash can never send it twice
            with db() as c:
                c.execute("UPDATE trades SET sent_at=?, dur_sent=? WHERE id=?", (time.time(), dur, r["id"]))
            try:
                reply = olymp.send_frame(build(r["asset"], r["direction"], u, dur, r["amount"]))  # the stake decided when it was queued
            except Exception as e:  # noqa
                done(f"failed, nothing sent or no confirmation: {e}", None, u)
            else:
                done("sent, Olymp replied" if reply else "sent, but NO reply from Olymp: check the demo account",
                     json.dumps(reply)[:700] if reply else None, u)
                i = reply_info(reply) if reply else {}
                if i:  # Olymp's own numbers: when it opened, the real entry price, the payout
                    with db() as c:
                        c.execute("UPDATE trades SET sent_at=COALESCE(?, sent_at), entry_px=COALESCE(?, entry_px), payout=COALESCE(?, payout) WHERE id=?",
                                  (i.get("time_open"), i.get("curs_open"), i["winperc"] / 100 if "winperc" in i else None, r["id"]))


def reply_info(x) -> dict:
    """What Olymp's reply to a trade tells us: time_open, curs_open (the real entry price), duration and winperc (the payout in %).
    `x` is the reply (a dict) or the saved text of it, which may be cut off, so the numbers are picked out one by one."""
    out = {}
    if isinstance(x, dict):
        d = (x.get("d") or [{}])[0]
        d = d if isinstance(d, dict) else {}
        for k in ("time_open", "curs_open", "duration", "winperc"):
            if isinstance(d.get(k), (int, float)):
                out[k] = float(d[k])
        return out
    for k in ("time_open", "curs_open", "duration", "winperc"):
        m = re.search(r'"%s":\s*(-?[0-9]+(?:\.[0-9]+)?)' % k, str(x or ""))
        if m:
            out[k] = float(m.group(1))
    return out


def _backfill(db):
    """Trades sent before send times were recorded: take when they opened, for how long, the real entry price and the payout from Olymp's saved reply."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT id, ts, duration, reply FROM trades WHERE status LIKE 'sent%' AND sent_at IS NULL")]
        for r in rows:
            i = reply_info(r["reply"])
            c.execute("UPDATE trades SET sent_at=?, dur_sent=?, entry_px=COALESCE(?, entry_px), payout=COALESCE(?, payout) WHERE id=?",
                      (i.get("time_open", r["ts"]), int(i.get("duration", r["duration"] or 0)), i.get("curs_open"),
                       i["winperc"] / 100 if "winperc" in i else None, r["id"]))


def _price_at(mins, t: float):
    """Estimated price at time t from 1-minute candles: the open/close of the minute that contains t, blended by how far into it t is."""
    for x in mins:
        if x[0] <= t < x[0] + 60:
            return x[1] + (x[4] - x[1]) * (t - x[0]) / 60
    return None


def _pip(px: float) -> float:
    return 0.01 if px > 20 else 0.0001


def settle_real(db, notify=None):
    """Read back how the demo trades we sent ended. This is an ESTIMATE from Olymp's 1-minute candles (entry and exit are
    taken at the moment inside the minute, so it can be off by a pip or two and a close call can come out the wrong way);
    your Olymp Trade demo history is the truth. Then applies the loss-streak stop. Returns an error text or ''."""
    global BALANCE
    now, err, news, moved = time.time(), "", [], False
    _backfill(db)
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE status LIKE 'sent%' AND settled=0 AND sent_at IS NOT NULL")]
    rows = [r for r in rows if r["sent_at"] + (r["dur_sent"] or r["duration"] or 0) + 65 < now]
    by: dict = {}
    for r in rows:
        by.setdefault(r["asset"], []).append(r)
    for asset, trades in by.items():
        try:
            mins = paper.one_minute(asset, now - min(t["sent_at"] for t in trades))  # as far back as the oldest trade needs
        except Exception as e:  # noqa
            err = f"demo trade results: could not read {asset}: {e}"
            continue
        for r in trades:
            end = r["sent_at"] + (r["dur_sent"] or r["duration"] or 0)
            p0, p1 = r["entry_px"] or _price_at(mins, r["sent_at"]), _price_at(mins, end)  # Olymp's own entry price when its reply gave one
            too_old = (bool(mins) and end + 60 < mins[0][0]) or now - end > 13 * 3600  # the 1-minute history no longer reaches back
            if (p0 is None or p1 is None) and not too_old:
                continue
            if p0 is None or p1 is None:
                res, pips = None, None
            else:
                up = p1 > p0 if r["direction"] == "up" else p1 < p0
                res = "draw" if p1 == p0 else "win" if up else "loss"
                pips = round((p1 - p0) / _pip(p0) * (1 if r["direction"] == "up" else -1), 1)  # + means price moved your way
            with db() as c:
                c.execute("UPDATE trades SET entry_px=?, exit_px=?, result=?, pips=?, settled=1 WHERE id=?", (p0, p1, res, pips, r["id"]))
            if STAKE_MODE and res in ("win", "loss"):  # keep the tracked balance up to date (payout as in the pretend trades)
                BALANCE = max(0, int(round(BALANCE + (r["amount"] * (r["payout"] or paper.PAYOUT) if res == "win" else -r["amount"]))))
                moved = True
            if res and now - end < 3600:
                news.append(f"{r['asset']} {r['direction']}: {res.upper()} ({pips:+} pips, estimated)")
    if moved:
        _store(db, config())  # the tracked balance survives a restart
    if news:
        paper.send_results(db, notify, f"{len(news)} demo trade result{'s' if len(news) != 1 else ''}", news)
    err = err or check_stop(db, notify)
    return err


def check_stop(db, notify=None) -> str:
    """Switch auto-trade OFF after too many losses in a row (counted from when you last switched it on) or in 24 hours.
    Uses the estimated results above, so it reacts about a minute after the last trade ended."""
    with db() as c:
        if not is_on(c):
            return ""
        r = c.execute("SELECT value FROM settings WHERE key='autotrade_since'").fetchone()
        since = float(r[0]) if r and r[0] else 0.0
        res = [x[0] for x in c.execute("SELECT result FROM trades WHERE settled=1 AND result IN ('win','loss') AND sent_at>=? ORDER BY sent_at", (since,))]
        day = c.execute("SELECT COUNT(*) FROM trades WHERE settled=1 AND result='loss' AND sent_at>?", (time.time() - 86400,)).fetchone()[0]
    streak = 0
    for x in reversed(res):
        if x != "loss":
            break
        streak += 1
    why = ""
    if MAX_STREAK and streak >= MAX_STREAK:
        why = f"{streak} losses in a row"
    elif MAX_DAY_LOSSES and day >= MAX_DAY_LOSSES:
        why = f"{day} losses in the last 24 hours"
    if not why:
        return ""
    set_on(db, False)
    with db() as c:
        _log(c, 0, "-", None, time.time(), f"auto-trade switched OFF: {why} (estimated results). Switch it on again when you have looked at it")
    if notify:
        try:
            notify("Auto-trade switched OFF", f"Stopped after {why}. It stays off until you switch it on again on the page.", "warning", False)
        except Exception:  # noqa
            pass
    return ""


def real_stats(db, hours: float) -> dict:
    """The demo trades that were really sent, with the (estimated) result of each, next to the pretend win rate for the same period."""
    since = time.time() - hours * 3600
    _backfill(db)  # trades from before send times were recorded
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT t.*, a.pattern AS pattern, a.rule AS rule FROM trades t LEFT JOIN alerts a ON a.id=t.alert_id "
            "WHERE t.sent_at>=? AND t.status LIKE 'sent%' ORDER BY t.sent_at DESC", (since,))]
    with db() as c:
        pt = [dict(x) for x in c.execute("SELECT alert_id, symbol, signal, entry_time FROM paper WHERE entry_time>=?", (since - 7200,))]
    def name(r):
        for p in sorted(pt, key=lambda p: abs(p["entry_time"] - r["ts"])):  # the pretend trade made at the same moment keeps the full setup name
            if p["alert_id"] == r["alert_id"] and p["symbol"] == olymp.PREFIX + r["asset"] and abs(p["entry_time"] - r["ts"]) < 150:
                return p["signal"]
        if r["pattern"] == "custom":
            try:
                return "custom: " + (json.loads(r["rule"]).get("name") or f"#{r['alert_id']}")
            except Exception:  # noqa
                return f"custom: #{r['alert_id']}"
        return r["pattern"] or f"alert #{r['alert_id']}"
    w = sum(1 for r in rows if r["result"] == "win")
    l = sum(1 for r in rows if r["result"] == "loss")
    d = sum(1 for r in rows if r["result"] == "draw")
    exp = min(paper.EXPIRIES, key=lambda m: abs(m * 60 - (DURATION or 60)))
    st = paper.stats(db, hours)
    pr = next((x for x in st["rows"] if x["signal"] == "ALL SIGNALS" and x["expiry"] == exp), None)
    return {"sent": len(rows), "open": sum(1 for r in rows if not r["settled"]), "wins": w, "losses": l, "draws": d,
            "win_rate": round(100 * w / (w + l), 1) if w + l else None, "breakeven": paper.BREAKEVEN,
            "pretend_rate": pr["win_rate"] if pr else None, "pretend_n": (pr["wins"] + pr["losses"]) if pr else 0, "pretend_expiry": exp,
            "trades": [{"at": r["sent_at"], "asset": r["asset"], "signal": name(r), "direction": r["direction"], "result": r["result"],
                        "pips": r["pips"], "settled": bool(r["settled"]), "amount": r["amount"], "entry_px": r["entry_px"]} for r in rows[:30]]}


def overview(db) -> dict:
    with db() as c:
        on = is_on(c)
        rows = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 25")]
        pend = [dict(r) for r in c.execute("SELECT * FROM pending ORDER BY id DESC LIMIT 10")]
    msgs = [{"e": e, "at": t, "msg": m} for e, v in olymp.recent.items() for t, m in v]
    return {"on": on, "missing": missing(), "config": config(), "stake_now": stake(), "payout": paper.PAYOUT, "olymp_ready": olymp.enabled() and not olymp.status["expired"],
            "trades": rows, "pending": pend, "messages": sorted(msgs, key=lambda x: -x["at"])[:12]}
