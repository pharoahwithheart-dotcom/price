"""Olymp Trade candle data over Olymp Trade's own (unofficial, undocumented) websocket, with our own small client.

Only two kinds of message can be sent: the candle-history request (e=10) and, from trader.py only, a DEMO trade (e=23 with
group "demo"; send_frame refuses anything else). Nothing here closes or changes a trade, and a live-money trade cannot be sent.
Needs OLYMP_ACCESS_TOKEN (the access_token cookie from a logged-in Olymp Trade browser session) and, to offer your OTC assets in the
pair box and watch them all with "every pair" (no defaults: the app only watches what you list), OLYMP_PAIRS = comma-separated asset names exactly as Olymp Trade names them,
e.g. "EURUSD,GBPUSD,LATAM_X". The websockets package is imported only when first needed.
"""
import asyncio, json, os, random, string, threading, time
from datetime import datetime

PREFIX = "OLYMP:"
TOKEN = os.getenv("OLYMP_ACCESS_TOKEN", "")
PAIRS = [p.strip().upper() for p in os.getenv("OLYMP_PAIRS", "").split(",") if p.strip()]  # your OTC assets, no defaults
_loop = None
_lock = threading.Lock()  # one request at a time keeps the connection simple and the request rate low
RETRY_AFTER = 300  # once the token looks expired, only try Olymp Trade again every 5 minutes (until a new token is saved)
AUTH_WORDS = ("unauthor", "forbidden", "expired", "401", "403", "invalid token", "access_token", "not authenticated", "authentication")
status = {"expired": False, "since": 0.0, "notified": False, "last_try": 0.0, "last_ok": 0.0}


class TokenExpired(RuntimeError):
    """Olymp Trade refused the access token (it expires, so this is expected from time to time)."""


def _mark_expired():
    if not status["expired"]:
        status.update(expired=True, since=time.time(), notified=False)
    status["last_try"] = time.time()


def clean_token(raw: str) -> str:
    """Accepts the bare token, or a whole pasted Cookie header / line containing access_token=..."""
    t = (raw or "").strip().strip('"').strip("'")
    if "access_token=" in t:
        t = t.split("access_token=", 1)[1].split(";", 1)[0].strip()
    return t


def set_token(raw: str):
    """Use a new token from now on: drop the old connection and forget any 'expired' state."""
    global TOKEN
    TOKEN = clean_token(raw)  # the next request notices the changed token and reconnects by itself
    status.update(expired=False, since=0.0, notified=False, last_try=0.0)


def enabled() -> bool:
    return bool(TOKEN)


URL = os.getenv("OLYMP_WS_URL", "wss://ws.olymptrade.com/otp?cid_ver=1&cid_app=web%40OlympTrade%402026.4.3647918%403647918"
                "&cid_device=%40%40desktop&cid_os=windows%4010")
ANSWER_TIMEOUT = 15  # seconds to wait for Olymp Trade to answer one candle request
_ws = None
_ws_token = ""  # the token the open connection was made with
_req = {}  # uuid of a candle request -> its (asset, size) key, so an error reply can be matched to it
_pend = {}  # the CURRENT connection's waiting requests: (asset, size) -> future for that candle reply
_acks = {}  # uuid -> future for the server's reply to a message we sent with that uuid (trades)
recent = {}  # message type "e" -> the last few messages of that type, newest last: for seeing what the server says
_seen = {}


def _uuid() -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(6))


def _note(m: dict):
    """Hand a reply to whoever is waiting for its uuid, and remember a few messages of each type (rate limited, since
    price ticks arrive many times a second) so the page can show what the server answered."""
    u = m.get("uuid")
    if u is not None:
        fut = _acks.pop(u, None)
        if fut is not None and not fut.done():
            fut.set_result(m)
    e, now = m.get("e"), time.time()
    if e is None or "candles" in json.dumps(m.get("d"))[:300]:
        return
    if u is None and now - _seen.get(e, 0) < 2:
        return
    _seen[e] = now
    recent.setdefault(e, []).append((now, json.dumps(m)[:700]))
    del recent[e][:-3]


async def _reader(ws, pend):
    """Reads every frame; only candle replies are used, everything else the server sends is ignored."""
    reason = "connection closed"
    try:
        async for raw in ws:
            try:
                msgs = json.loads(raw)
            except ValueError:
                continue
            for m in msgs if isinstance(msgs, list) else [msgs]:
                if isinstance(m, dict):
                    _note(m)
                    if m.get("err") and m.get("uuid") in _req:  # the server REFUSED a candle request: say which one, right away
                        key = _req.pop(m["uuid"])
                        fut = pend.pop(key, None)
                        if fut is not None and not fut.done():
                            why = "; ".join(str(x.get("mess") or x.get("code")) for x in m["err"] if isinstance(x, dict)) or "unknown error"
                            fut.set_exception(RuntimeError(f"Olymp Trade rejected the candle request for {key[0]} ({key[1]}-second candles): {why}"))
                for d in (m.get("d") if isinstance(m, dict) else None) or []:
                    if isinstance(d, dict) and "candles" in d:
                        fut = pend.pop((d.get("p"), d.get("tf")), None)
                        if fut is not None and not fut.done():
                            fut.set_result(d["candles"])
    except Exception as e:  # noqa
        reason = f"connection lost: {e}"
    finally:
        if ws.close_code or ws.close_reason:
            reason = f"connection closed ({ws.close_code} {ws.close_reason})"
        for k, fut in list(pend.items()):  # only this connection's own requests, never a newer connection's
            pend.pop(k, None)
            if not fut.done():
                fut.set_exception(ConnectionError(reason))


async def _drop():
    global _ws
    ws, _ws = _ws, None
    if ws is not None:
        try:
            await ws.close()
        except Exception:  # noqa
            pass


async def _connect():
    global _ws, _ws_token, _pend
    if _ws is not None and getattr(_ws, "close_code", None) is None and _ws_token == TOKEN:
        return _ws
    await _drop()  # closed, or made with an older token
    try:
        import websockets  # in requirements.txt
    except ImportError:
        raise RuntimeError("the 'websockets' package is not installed (pip install websockets)")
    headers = {"Origin": "https://olymptrade.com", "Cookie": f"access_token={TOKEN}",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        try:
            ws = await websockets.connect(URL, additional_headers=headers, open_timeout=15)
        except TypeError:  # older websockets versions
            ws = await websockets.connect(URL, extra_headers=headers, open_timeout=15)
    except Exception as e:  # noqa
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code in (401, 403) or any(w in str(e).lower() for w in AUTH_WORDS):
            raise TokenExpired(f"Olymp Trade refused the access token: {e}")
        raise
    _ws, _ws_token, _pend = ws, TOKEN, {}
    asyncio.ensure_future(_reader(ws, _pend))
    return ws


async def _get(asset: str, size: int, count: int):
    ws = await _connect()
    pend, key = _pend, (asset, size)
    fut = asyncio.get_running_loop().create_future()
    pend[key] = fut
    u = _uuid()
    _req[u] = key
    frame = [{"t": 2, "e": 10, "uuid": u, "d": [{"pair": asset, "size": size, "to": int(time.time()), "limit": min(max(count, 1), 600)}]}]
    try:
        await ws.send(json.dumps(frame))
        return await asyncio.wait_for(fut, ANSWER_TIMEOUT)
    except asyncio.TimeoutError:
        pend.pop(key, None)
        await _drop()  # start from a fresh connection next time
        raise RuntimeError(f"Olymp Trade did not answer for {asset} (check the asset name, or the token may have expired)")
    except Exception as e:  # noqa
        pend.pop(key, None)
        if any(w in str(e).lower() for w in AUTH_WORDS):
            await _drop()
            raise TokenExpired(f"Olymp Trade refused the access token: {e}")
        raise
    finally:
        _req.pop(u, None)


async def _send(frame, wait: float):
    ws = await _connect()
    fut = asyncio.get_running_loop().create_future()
    _acks[frame[0]["uuid"]] = fut
    await ws.send(json.dumps(frame))
    try:
        return await asyncio.wait_for(fut, wait)
    except asyncio.TimeoutError:
        _acks.pop(frame[0]["uuid"], None)
        return None  # sent, but no reply seen


def send_frame(frame: list, wait: float = 6.0):
    """Send ONE message and return the server's reply (None if none arrived in time). Hard limit: only candle requests and
    demo trades pass; anything else, including a trade on any group other than "demo", raises PermissionError."""
    ok = len(frame) == 1 and isinstance(frame[0], dict)
    if ok and frame[0].get("e") == 23:
        d = frame[0].get("d") or []
        ok = len(d) == 1 and d[0].get("group") == "demo"
    elif ok:
        ok = frame[0].get("e") == 10
    if not ok:
        raise PermissionError("blocked: only candle requests and DEMO trades may be sent")
    if status["expired"] and time.time() - status["last_try"] < RETRY_AFTER:
        raise TokenExpired("the Olymp Trade access token has expired, set a new one on the page")
    with _lock:
        _ensure_loop()
        try:
            return asyncio.run_coroutine_threadsafe(_send(frame, wait), _loop).result(wait + 25)
        except TokenExpired:
            _mark_expired()
            raise


def _ensure_loop():
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True).start()


def check():
    """Connect with the current token and ask for 2 candles. Raises TokenExpired if Olymp Trade refuses the token."""
    with _lock:
        _ensure_loop()
        asyncio.run_coroutine_threadsafe(_get(PAIRS[0] if PAIRS else "AUDUSD_OTC", 60, 2), _loop).result(ANSWER_TIMEOUT + 20)


def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)  # milliseconds or seconds
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float(v)


def raw_candles(asset: str, size: int, count: int = 60) -> list:
    """(open_time, o, h, l, c, volume) oldest first, INCLUDING a candle that is still forming."""
    if status["expired"] and time.time() - status["last_try"] < RETRY_AFTER:
        raise TokenExpired("the Olymp Trade access token has expired, set a new one on the page")
    with _lock:
        _ensure_loop()
        try:
            rows = asyncio.run_coroutine_threadsafe(_get(asset, size, count), _loop).result(30)
        except TokenExpired:
            _mark_expired()
            raise
    if status["expired"]:
        status.update(expired=False, since=0.0, notified=False)
    status["last_ok"] = time.time()
    out = [(_ts(r["t"] if "t" in r else r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
           for r in rows or []]
    return sorted(out)


DIRECT_SIZES = (60, 300)  # candle lengths (seconds) Olymp Trade is known to serve; longer ones are built from 5-minute candles


def candles(asset: str, size: int) -> list:
    """Closed candles only. `size` is the candle length in seconds: 1m and 5m come straight from Olymp Trade, 10m, 15m and 1h
    are built from its 5-minute candles (it rejects them with 'System error'); 4h and 1d are not available."""
    now = time.time()
    if size in DIRECT_SIZES:
        rows = raw_candles(asset, size)
    elif size % 300 == 0 and size <= 3600:
        import candles as cdl
        rows = cdl.aggregate(raw_candles(asset, 300, min(600, size // 300 * 22)), size)  # ~22 candles of the wanted size
    else:
        raise ValueError(f"Olymp Trade candles of {size} seconds are not available here; use 5m, 10m, 15m or 1h")
    return [c for c in rows if c[0] + size <= now + 1]


def price(asset: str) -> float:
    return raw_candles(asset, 60, 2)[-1][4]  # close of the newest 1-minute candle
