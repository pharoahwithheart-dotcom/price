"""Deriv (developers.deriv.com, the current API): public candle data, and Rise/Fall trading (demo by default).

Candles: no login. Public websocket, ticks_history with style "candles".
Trading: your Personal Access Token (PAT, scope "trade") + your app id -> REST call for a one-time-password websocket URL for
ONE account -> connect -> proposal -> buy -> follow the contract with proposal_open_contract until Deriv says it is sold.

MODE is "demo" (default) or "real". The bot only ever connects to the gateway that matches MODE: in demo mode a real-money account
is refused, in real mode a demo account is refused, so an account id can never trade on the wrong side. MODE can only be switched
to "real" from the web page, with a typed confirmation. Nothing here sells early, changes or cancels a contract.
"""
import asyncio, itertools, json, threading, time

API = "https://api.derivws.com"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"
GRANULARITIES = [60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400]

PAT = ""         # Personal Access Token (scope: trade). Kept on the server, never sent back to the page.
APP_ID = ""      # the app id of the PAT app you registered at developers.deriv.com
ACCOUNT_ID = ""  # the account id (demo or real, to match MODE), as shown in your Deriv account switcher
MODE = "demo"    # "demo" or "real"
status = {"last_ok": 0.0, "error": ""}


class DerivError(Exception):
    pass


class NotDemo(DerivError):
    pass


def configured() -> bool:
    return bool(PAT and APP_ID and ACCOUNT_ID)


def set_credentials(pat: str, app_id: str, account_id: str, mode: str = None):
    global PAT, APP_ID, ACCOUNT_ID, MODE
    PAT, APP_ID, ACCOUNT_ID = (pat or "").strip(), (app_id or "").strip(), (account_id or "").strip()
    if mode is not None:
        MODE = "real" if mode == "real" else "demo"


# ---------------------------------------------------------------- public market data (candles)
_loop = None
_ws = None
_pend: dict = {}  # the CURRENT connection's waiting requests: req_id -> future
_ids = itertools.count(1)
_lock = threading.Lock()


def _ensure_loop():
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True).start()


async def _reader(ws, pend):
    reason = "connection closed"
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            fut = pend.pop(m.get("req_id"), None) if isinstance(m, dict) else None
            if fut is not None and not fut.done():
                fut.set_result(m)
    except Exception as e:  # noqa
        reason = f"connection lost: {e}"
    finally:
        for k, fut in list(pend.items()):  # only this connection's own requests
            pend.pop(k, None)
            if not fut.done():
                fut.set_exception(ConnectionError(reason))


async def _public():
    global _ws, _pend
    if _ws is not None and getattr(_ws, "close_code", None) is None:
        return _ws
    try:
        import websockets  # in requirements.txt
    except ImportError:
        raise DerivError("the 'websockets' package is not installed (pip install websockets)")
    _ws = await websockets.connect(PUBLIC_WS, open_timeout=15)
    _pend = {}
    asyncio.ensure_future(_reader(_ws, _pend))
    return _ws


async def _call(payload: dict, timeout: float = 15):
    ws = await _public()
    pend, rid = _pend, next(_ids)
    fut = asyncio.get_running_loop().create_future()
    pend[rid] = fut
    try:
        await ws.send(json.dumps(dict(payload, req_id=rid)))
        msg = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        pend.pop(rid, None)
        raise DerivError("Deriv did not answer in time")
    except Exception:
        pend.pop(rid, None)
        raise
    if msg.get("error"):
        raise DerivError(str(msg["error"].get("message") or msg["error"]))
    return msg


def candles(symbol: str, gran: int, count: int = 60) -> list:
    """Closed candles, oldest first: (open_time, open, high, low, close, 0.0). Deriv has no volume for these, so volume is 0."""
    if gran not in GRANULARITIES:
        raise DerivError(f"candle size {gran}s is not one Deriv offers")
    with _lock:
        _ensure_loop()
        try:
            msg = asyncio.run_coroutine_threadsafe(
                _call({"ticks_history": symbol, "end": "latest", "count": count, "style": "candles", "granularity": gran}), _loop).result(30)
        except DerivError as e:
            status["error"] = str(e)
            raise
        except Exception as e:  # noqa
            status["error"] = str(e)
            raise DerivError(str(e))
    now, out = time.time(), []
    for c in msg.get("candles") or []:
        t = float(c.get("epoch", c.get("time")))
        if t + gran <= now + 1:  # the last one is still forming
            out.append((t, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]), 0.0))
    status.update(last_ok=time.time(), error="")
    return sorted(out)


# ---------------------------------------------------------------- trading
def _otp_url() -> str:
    """Ask Deriv for the one-time-password websocket URL of the configured account. Refuses a gateway that does not match MODE."""
    import httpx
    if not configured():
        raise DerivError("Deriv token, app id and account id are not all set")
    r = httpx.post(f"{API}/trading/v1/options/accounts/{ACCOUNT_ID}/otp", timeout=20,
                   headers={"Authorization": f"Bearer {PAT}", "Deriv-App-ID": APP_ID})
    if r.status_code in (401, 403):
        raise DerivError(f"Deriv refused the token or app id (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise DerivError(f"Deriv could not give a connection for that account (HTTP {r.status_code}): {r.text[:200]}")
    url = (r.json().get("data") or {}).get("url") or ""
    if f"/ws/{MODE}" not in url:
        raise NotDemo("REFUSED: that account is not a DEMO account (the bot is in demo mode). Nothing was traded." if MODE == "demo"
                      else "REFUSED: that is a DEMO account but the bot is in real-money mode. Nothing was traded.")
    return url


async def _rpc(ws, payload: dict, timeout: float = 20) -> dict:
    """Send one request on a private (one trade) connection and wait for the answer carrying the same req_id."""
    rid = next(_ids)
    await ws.send(json.dumps(dict(payload, req_id=rid)))
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            raise DerivError("Deriv did not answer in time")
        m = json.loads(await asyncio.wait_for(ws.recv(), left))
        if isinstance(m, dict) and m.get("req_id") == rid:
            if m.get("error"):
                raise DerivError(str(m["error"].get("message") or m["error"]))
            return m


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _contract(c: dict) -> dict:
    return {"contract_id": c.get("contract_id"), "sold": bool(c.get("is_sold")), "profit": _f(c.get("profit")),
            "status": c.get("status"), "entry": _f(c.get("entry_spot", c.get("entry_tick"))),
            "exit": _f(c.get("exit_tick", c.get("sell_spot"))), "sell_price": _f(c.get("sell_price"))}


async def _trade(symbol, direction, stake, duration_s, currency, on_bought):
    import websockets
    async with websockets.connect(_otp_url(), open_timeout=15) as ws:
        unit, dur = ("m", duration_s // 60) if duration_s % 60 == 0 else ("s", duration_s)
        p = await _rpc(ws, {"proposal": 1, "amount": stake, "basis": "stake", "contract_type": "CALL" if direction == "up" else "PUT",
                            "currency": currency, "duration": dur, "duration_unit": unit, "underlying_symbol": symbol})
        prop = p["proposal"]
        ask = _f(prop.get("ask_price"))
        if ask is None or ask > stake * 1.02:
            raise DerivError(f"price moved: Deriv asks {ask} for a stake of {stake}")
        b = (await _rpc(ws, {"buy": prop["id"], "price": ask}))["buy"]  # price = the most we accept to pay
        info = {"contract_id": b.get("contract_id"), "buy_price": _f(b.get("buy_price")), "payout": _f(b.get("payout")),
                "longcode": b.get("longcode"), "start_time": b.get("start_time"), "sold": False}
        if on_bought:
            on_bought(info)  # saved at once, so a crash can never lose track of a bought contract or buy it twice
        rid = next(_ids)
        await ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": b["contract_id"], "subscribe": 1, "req_id": rid}))
        end = time.time() + duration_s + 90
        while time.time() < end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), max(1, end - time.time())))
            except asyncio.TimeoutError:
                break
            poc = m.get("proposal_open_contract") if isinstance(m, dict) else None
            if poc and str(poc.get("contract_id")) == str(b["contract_id"]) and poc.get("is_sold"):
                return {**info, **_contract(poc)}
        return info  # bought, but the result was not seen yet: lookup() reads it later


def place(symbol: str, direction: str, stake: float, duration_s: int, currency: str = "USD", on_bought=None) -> dict:
    """Buy one Rise (up) / Fall (down) contract and wait for it to end. Blocks for about the duration."""
    if direction not in ("up", "down"):
        raise DerivError("direction must be up or down")
    try:
        r = asyncio.run(_trade(symbol, direction, float(stake), int(duration_s), currency, on_bought))
    except DerivError as e:
        status["error"] = str(e)
        raise
    except Exception as e:  # noqa
        status["error"] = str(e)
        raise DerivError(str(e))
    status.update(last_ok=time.time(), error="")
    return r


async def _lookup(contract_id):
    import websockets
    async with websockets.connect(_otp_url(), open_timeout=15) as ws:
        m = await _rpc(ws, {"proposal_open_contract": 1, "contract_id": int(contract_id)})
        return _contract(m.get("proposal_open_contract") or {})


def lookup(contract_id) -> dict:
    """Read one contract's current state (used to settle a trade whose result was not seen, e.g. after a restart)."""
    try:
        return asyncio.run(_lookup(contract_id))
    except DerivError:
        raise
    except Exception as e:  # noqa
        raise DerivError(str(e))


def check() -> str:
    """Prove the token, app id and account work AND match MODE. Returns a short note. Raises DerivError / NotDemo otherwise."""
    _otp_url()
    return ("token, app id and account accepted, and it is a DEMO account" if MODE == "demo"
            else "token, app id and account accepted, and it is a REAL-MONEY account")
