"""Candle signal detection. A candle is (open_time, open, high, low, close, volume), oldest first, closed candles only.
Nothing here predicts price: these flag candles that LOOK like buyers or sellers stepping in."""

import time

LOOKBACK = 20
# Wick rules for the "first candle of the window" signals, as multiples of the candle body.
# A candle passes if EITHER: the wick on the CLOSING side is at most CLOSE_WICK_MAX of the body (opening wick any length),
# OR the wick on the OPENING side is longer than OPEN_WICK_LONG x body, which loosens the closing-side limit to
# CLOSE_WICK_MAX_IF_LONG_OPEN.
CLOSE_WICK_MAX = 1 / 3
OPEN_WICK_LONG = 1.0
CLOSE_WICK_MAX_IF_LONG_OPEN = 0.5

PATTERNS = {
    "surge_up": "Buying surge: big green candle on high volume",
    "surge_down": "Selling surge: big red candle on high volume",
    "bull_engulf": "Bullish engulfing candle",
    "bear_engulf": "Bearish engulfing candle",
    "hammer": "Hammer (rejection of lows after a drop)",
    "shooting_star": "Shooting star (rejection of highs after a rise)",
    "any_bullish": "Any bullish signal above",
    "any_bearish": "Any bearish signal above",
}
PATTERNS["custom"] = "Custom candle: describe it yourself (colour, size, wicks, where and when)"
PATTERNS.update({
    "first5_up": "First 5m candle of each 15m window: strong green, short top wick",
    "first5_down": "First 5m candle of each 15m window: strong red, short bottom wick",
    "first5_any": "First 5m candle of each 15m window: either direction",
})
PATTERNS.update({
    "first10_up": "First 10m candle of each 30m window: strong green, short top wick",
    "first10_down": "First 10m candle of each 30m window: strong red, short bottom wick",
    "first10_any": "First 10m candle of each 30m window: either direction",
})
# pattern -> (candle size label, candle seconds, window seconds)
WINDOW_PATTERNS = {
    "first5_up": ("5m", 300, 900), "first5_down": ("5m", 300, 900), "first5_any": ("5m", 300, 900),
    "first10_up": ("10m", 600, 1800), "first10_down": ("10m", 600, 1800), "first10_any": ("10m", 600, 1800),
}


def aggregate(cs5: list, seconds: int) -> list:
    """Build bigger candles from 5-minute ones (e.g. 10m). Incomplete buckets are dropped."""
    per = seconds // 300
    buckets: dict = {}
    for c in cs5:
        buckets.setdefault(int(c[0]) // seconds * seconds, []).append(c)
    out = []
    for t in sorted(buckets):
        g = buckets[t]
        if len(g) == per:
            out.append((float(t), g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4], sum(x[5] for x in g)))
    return out
BULL = ["surge_up", "bull_engulf", "hammer"]
BEAR = ["surge_down", "bear_engulf", "shooting_star"]


def _body(c):
    return abs(c[4] - c[1])


def _range(c):
    return max(c[2] - c[3], 1e-12)


def _avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _surge(cs, up: bool, use_volume: bool):
    cur, prev = cs[-1], cs[-1 - LOOKBACK:-1]
    avg_body = _avg([_body(c) for c in prev])
    avg_vol = _avg([c[5] for c in prev])
    if avg_body <= 0:
        return None
    big = _body(cur) >= (1.5 if use_volume else 2.0) * avg_body
    direction = cur[4] > cur[1] if up else cur[4] < cur[1]
    pos = (cur[4] - cur[3]) / _range(cur)  # where the close sits in the candle (0 low, 1 high)
    closes_strong = pos >= 0.75 if up else pos <= 0.25
    vol_x = (cur[5] / avg_vol) if avg_vol > 0 else 0.0
    if use_volume and vol_x < 2.0:
        return None
    if big and direction and closes_strong:
        extra = f", volume {vol_x:.1f}x normal" if use_volume else f", body {_body(cur) / avg_body:.1f}x normal"
        return ("Buying surge" if up else "Selling surge") + extra
    return None


def _engulf(cs, up: bool):
    p, c = cs[-2], cs[-1]
    if _body(c) <= _body(p) or _body(p) <= 0:
        return None
    if up and p[4] < p[1] and c[4] > c[1] and c[1] <= p[4] and c[4] >= p[1]:
        return "Bullish engulfing"
    if not up and p[4] > p[1] and c[4] < c[1] and c[1] >= p[4] and c[4] <= p[1]:
        return "Bearish engulfing"
    return None


def _pin(cs, up: bool):
    c = cs[-1]
    body, rng = max(_body(c), _range(c) * 0.02), _range(c)
    upper = c[2] - max(c[1], c[4])
    lower = min(c[1], c[4]) - c[3]
    trend = cs[-1][4] - cs[-5][4] if len(cs) >= 5 else 0
    if up and lower >= 2 * body and upper <= body and body / rng <= 0.35 and trend < 0 and c[4] >= c[3] + 0.6 * rng:
        return "Hammer after a drop"
    if not up and upper >= 2 * body and lower <= body and body / rng <= 0.35 and trend > 0 and c[4] <= c[3] + 0.4 * rng:
        return "Shooting star after a rise"
    return None


def _first_in_window(cs, up: bool, use_volume: bool, label: str, window: int):
    """The candle that opens a window (5m candle at :00 :15 :30 :45, or 10m candle at :00 :30). It must move decisively
    in one direction and CLOSE near its extreme: the wick on the closing side may be at most a third of the body (none is
    fine), or at most half the body when the opening-side wick is longer than the body. It must also be big (and, for crypto, on high
    volume) so tiny candles don't count."""
    cur = cs[-1]
    if int(cur[0]) % window != 0:
        return None
    body = _body(cur)
    if body <= 0:
        return None
    if up and cur[4] > cur[1]:
        close_wick, open_wick = cur[2] - cur[4], cur[1] - cur[3]
    elif not up and cur[4] < cur[1]:
        close_wick, open_wick = cur[4] - cur[3], cur[2] - cur[1]
    else:
        return None
    ok = close_wick <= body * CLOSE_WICK_MAX or (open_wick > body * OPEN_WICK_LONG and close_wick <= body * CLOSE_WICK_MAX_IF_LONG_OPEN)
    if not ok:
        return None
    prev = cs[-1 - LOOKBACK:-1]
    avg_body, avg_vol = _avg([_body(c) for c in prev]), _avg([c[5] for c in prev])
    if avg_body <= 0 or body < (1.5 if use_volume else 2.0) * avg_body:
        return None
    vol_x = (cur[5] / avg_vol) if avg_vol > 0 else 0.0
    if use_volume and vol_x < 2.0:
        return None
    extra = f"volume {vol_x:.1f}x normal" if use_volume else f"body {body / avg_body:.1f}x normal"
    return (("Strong green" if up else "Strong red")
            + f" opening {label} candle, opening wick {open_wick / body:.1f}x body, closing wick {close_wick / body:.0%} of body, {extra}")


LABELS = {"body_pct": "body % of height", "body_x": "body x avg", "range_x": "height x avg", "close_pos": "close position %",
          "wup": "upper wick x body", "wlo": "lower wick x body", "wopen": "opening wick x body",
          "wclose": "closing wick x body", "vol_x": "volume x avg",
          "move_pct": "move % (open to close)", "body_prev_x": "body x previous body",
          "range_prev_x": "height x previous height", "vol_prev_x": "volume x previous volume"}
# yes/no conditions against the candle just before it
FLAG_TEXT = {"breaks_prev_high": "closes above previous high", "breaks_prev_low": "closes below previous low",
             "engulfs_prev": "body covers previous body", "inside_prev": "inside previous candle",
             "higher_high": "makes a higher high", "lower_low": "makes a lower low",
             "higher_close": "closes above previous close", "lower_close": "closes below previous close",
             "gap_up": "gaps up (opens above previous high)", "gap_down": "gaps down (opens below previous low)",
             "opens_above_close": "opens above previous close", "opens_below_close": "opens below previous close",
             "opens_in_body": "opens inside previous body", "closes_in_body": "closes inside previous body"}
FLAGS = tuple(FLAG_TEXT)
SHAPES = {"doji": "doji (tiny body)", "marubozu": "marubozu (almost no wicks)", "spinning_top": "spinning top (small body, wicks both sides)",
          "hammer_shape": "hammer shape (long lower wick)", "star_shape": "shooting-star shape (long upper wick)",
          "long_body": "long-bodied candle"}
MAX_STEPS = 3  # a custom pattern can describe up to three candles in a row


def window_seconds(pattern: str, rule=None):
    """If the signal can only match on the first candle of a time window, the window length in seconds.
    A multi-candle custom pattern returns None: its window rule is checked on the pattern's FIRST candle, while the
    alert fires on its last, so every closed candle has to be looked at."""
    if pattern in WINDOW_PATTERNS:
        return WINDOW_PATTERNS[pattern][2]
    if pattern == "custom" and rule and rule.get("window_min") and not rule.get("steps"):
        return int(rule["window_min"]) * 60
    return None


def _num(raw: dict, k: str, lo=0.0, hi=100000.0):
    v = raw.get(k)
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{k.replace('_', ' ')} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{k.replace('_', ' ')} must be between {lo:g} and {hi:g}")
    return v


def _clean_candle(raw: dict) -> dict:
    """The conditions on ONE candle (colour, trend before it, size and shape, comparison with the candle before it)."""
    r: dict = {}
    for k in LABELS:
        for sfx in ("_min", "_max"):
            v = _num(raw, k + sfx, 0, 100 if k in ("body_pct", "close_pos") else 100000)
            if v is not None:
                r[k + sfx] = v
        if k + "_min" in r and k + "_max" in r and r[k + "_min"] > r[k + "_max"]:
            raise ValueError(f"{LABELS[k]}: the minimum is bigger than the maximum")
    for k, opts in (("color", ("green", "red")), ("prev_color", ("green", "red")), ("trend", ("up", "down"))):
        if raw.get(k):
            if raw[k] not in opts:
                raise ValueError(f"{k} must be one of {opts}")
            r[k] = raw[k]
    if r.get("trend"):
        r["trend_n"] = int(_num(raw, "trend_n", 2, 18) or 5)
    for k, opts in (("shape", tuple(SHAPES)), ("break", ("high", "low")), ("ma", ("above", "below"))):
        if raw.get(k):
            if raw[k] not in opts:
                raise ValueError(f"{k} must be one of {opts}")
            r[k] = raw[k]
    if r.get("break"):
        r["break_n"] = int(_num(raw, "break_n", 2, 18) or 5)
    if r.get("ma"):
        r["ma_n"] = int(_num(raw, "ma_n", 2, 20) or 10)
    for k in FLAGS:
        if raw.get(k):
            r[k] = True
    return r


def clean_rule(raw: dict) -> dict:
    """Validate a custom-candle rule from the form. Keeps only known fields; raises ValueError with a readable message.
    Shape: per-candle conditions for 1 to 3 candles in a row (oldest first, the alert fires when the last one closes),
    plus where/when conditions for the whole pattern. A single candle is stored flat (as older alerts are)."""
    raw_steps = raw.get("steps")
    if raw_steps is None:
        steps = [_clean_candle(raw)]
    else:
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_STEPS:
            raise ValueError(f"A pattern is 1 to {MAX_STEPS} candles in a row")
        steps = []
        for i, st in enumerate(raw_steps):
            try:
                steps.append(_clean_candle(st if isinstance(st, dict) else {}))
            except ValueError as e:
                raise ValueError(f"Candle {i + 1}: {e}" if len(raw_steps) > 1 else str(e))
    g: dict = {}
    w = _num(raw, "window_min", 1, 1440)
    if w:
        g["window_min"] = int(w)
    hf, ht = _num(raw, "hour_from", 0, 23), _num(raw, "hour_to", 0, 23)
    if (hf is None) != (ht is None):
        raise ValueError("Set both the 'from' and 'to' hour, or neither")
    if hf is not None:
        g["hour_from"], g["hour_to"] = int(hf), int(ht)
    days = [int(d) for d in (raw.get("days") or []) if str(d) in "0123456"]
    if days:
        g["days"] = sorted(set(days))
    if not any(steps) and not g:
        raise ValueError("Set at least one condition, otherwise every candle would match")
    r: dict = {}
    if len(steps) == 1:
        r.update(steps[0])
    else:
        r["steps"] = steps
    r.update(g)
    r["name"] = str(raw.get("name") or "")[:40]
    return r


def _candle_parts(cs: list, st: dict, use_volume: bool):
    """Test the LAST candle of cs against one candle's conditions. Returns a list of descriptions, or None if it fails."""
    cur, prev, hist = cs[-1], cs[-2], cs[-1 - LOOKBACK:-1]
    o, h, l, c = cur[1:5]
    body, rng = abs(c - o), _range(cur)
    color = "green" if c > o else "red" if c < o else ""
    parts = []
    if st.get("color"):
        if color != st["color"]:
            return None
        parts.append(color)
    ab, ar, av = _avg([_body(x) for x in hist]), _avg([_range(x) for x in hist]), _avg([x[5] for x in hist])
    fb = max(body, rng * 0.001)  # avoids dividing by a zero body (doji)
    wo, wc = ((o - l, h - c) if color == "green" else (h - o, c - l) if color == "red" else (None, None))
    m = {"body_pct": body / rng * 100, "body_x": body / ab if ab > 0 else None, "range_x": rng / ar if ar > 0 else None,
         "close_pos": (c - l) / rng * 100, "wup": (h - max(o, c)) / fb, "wlo": (min(o, c) - l) / fb,
         "wopen": None if wo is None else wo / fb, "wclose": None if wc is None else wc / fb,
         "vol_x": cur[5] / av if av > 0 else 0.0,
         "move_pct": body / o * 100 if o else None,
         "body_prev_x": body / _body(prev) if _body(prev) > 0 else None,
         "range_prev_x": rng / _range(prev),
         "vol_prev_x": cur[5] / prev[5] if prev[5] > 0 else None}
    for k, val in m.items():
        lo, hi = st.get(k + "_min"), st.get(k + "_max")
        if (lo is None and hi is None) or (k in ("vol_x", "vol_prev_x") and not use_volume):
            continue
        if val is None or (lo is not None and val < lo) or (hi is not None and val > hi):
            return None
        parts.append(f"{LABELS[k]} {val:.2f}")
    sh = st.get("shape")
    if sh:
        up_w, lo_w, bp = h - max(o, c), min(o, c) - l, body / rng
        shapes = {"doji": bp <= 0.1, "marubozu": bp >= 0.9, "long_body": bp >= 0.7,
                  "spinning_top": bp <= 0.3 and up_w >= body and lo_w >= body and bp > 0.1,
                  "hammer_shape": lo_w >= 2 * fb and up_w <= fb and bp <= 0.35,
                  "star_shape": up_w >= 2 * fb and lo_w <= fb and bp <= 0.35}
        if not shapes[sh]:
            return None
        parts.append(SHAPES[sh].split(" (")[0])
    if st.get("break"):
        n = st.get("break_n", 5)
        if len(cs) < n + 1:
            return None
        win = cs[-1 - n:-1]
        if st["break"] == "high" and not c > max(x[2] for x in win):
            return None
        if st["break"] == "low" and not c < min(x[3] for x in win):
            return None
        parts.append(f"closes {'above' if st['break'] == 'high' else 'below'} the {n}-candle {st['break']}")
    if st.get("ma"):
        n = st.get("ma_n", 10)
        if len(cs) < n:
            return None
        sma = _avg([x[4] for x in cs[-n:]])
        if (st["ma"] == "above" and not c > sma) or (st["ma"] == "below" and not c < sma):
            return None
        parts.append(f"closes {st['ma']} its {n}-candle average")
    pcol = "green" if prev[4] > prev[1] else "red" if prev[4] < prev[1] else ""
    if st.get("prev_color"):
        if pcol != st["prev_color"]:
            return None
        parts.append(f"previous candle {pcol}")
    if st.get("trend"):
        n = st.get("trend_n", 5)
        if len(cs) < n + 2:
            return None
        d = cs[-2][4] - cs[-2 - n][4]
        if (st["trend"] == "down" and d >= 0) or (st["trend"] == "up" and d <= 0):
            return None
        parts.append(f"after a {n}-candle {'drop' if st['trend'] == 'down' else 'rise'}")
    pt, pb = max(prev[1], prev[4]), min(prev[1], prev[4])
    tests = {"breaks_prev_high": c > prev[2], "breaks_prev_low": c < prev[3],
             "engulfs_prev": max(o, c) >= pt and min(o, c) <= pb, "inside_prev": h <= prev[2] and l >= prev[3],
             "higher_high": h > prev[2], "lower_low": l < prev[3], "higher_close": c > prev[4], "lower_close": c < prev[4],
             "gap_up": o > prev[2], "gap_down": o < prev[3], "opens_above_close": o > prev[4], "opens_below_close": o < prev[4],
             "opens_in_body": pb <= o <= pt, "closes_in_body": pb <= c <= pt}
    for k in FLAGS:
        if st.get(k):
            if not tests[k]:
                return None
            parts.append(FLAG_TEXT[k])
    return parts


def custom_match(cs: list, rule: dict, use_volume: bool):
    """Test the pattern against the newest candles. 1 candle: the last one. 2 or 3: those candles in a row, the last
    one being the candle that just closed. Everything filled in must hold. Returns a description or None.
    Where/when: the window rule is checked on the pattern's FIRST candle; hours and weekdays on the LAST (when it fires)."""
    steps = rule.get("steps") or [rule]
    n = len(steps)
    if len(cs) < LOOKBACK + n:
        return None
    g = time.gmtime(cs[-1][0])
    if rule.get("hour_from") is not None:
        a, b = rule["hour_from"], rule["hour_to"]
        if not (a <= g.tm_hour <= b if a <= b else g.tm_hour >= a or g.tm_hour <= b):
            return None
    if rule.get("days") and g.tm_wday not in rule["days"]:
        return None
    if rule.get("window_min") and int(cs[-n][0]) % (rule["window_min"] * 60):
        return None
    descs = []
    for i, st in enumerate(steps):
        sub = cs[:len(cs) - (n - 1 - i)]
        parts = _candle_parts(sub, st, use_volume)
        if parts is None:
            return None
        descs.append(parts)
    win = f"first of {rule['window_min']}m window" if rule.get("window_min") else ""
    if n == 1:
        body = ", ".join(descs[0] + ([win] if win else []))
    else:
        body = (f"({win}) " if win else "") + " then ".join(f"candle {i + 1}: " + (", ".join(p) or "any") for i, p in enumerate(descs))
    return (rule.get("name") or "Custom candle") + ": " + body


def detect(cs: list, pattern: str, use_volume: bool, rule=None):
    """Returns a short description if the LAST candle in cs matches, else None. Needs LOOKBACK+1 candles."""
    if len(cs) < LOOKBACK + 1:
        return None
    if pattern == "custom":
        return custom_match(cs, rule, use_volume) if rule else None
    if pattern in WINDOW_PATTERNS:
        label, _, window = WINDOW_PATTERNS[pattern]
        if pattern.endswith("_any"):
            return (_first_in_window(cs, True, use_volume, label, window)
                    or _first_in_window(cs, False, use_volume, label, window))
        return _first_in_window(cs, pattern.endswith("_up"), use_volume, label, window)
    if pattern in ("any_bullish", "any_bearish"):
        names = BULL if pattern == "any_bullish" else BEAR
        for n in names:
            r = detect(cs, n, use_volume)
            if r:
                return r
        return None
    if pattern == "surge_up":
        return _surge(cs, True, use_volume)
    if pattern == "surge_down":
        return _surge(cs, False, use_volume)
    if pattern == "bull_engulf":
        return _engulf(cs, True)
    if pattern == "bear_engulf":
        return _engulf(cs, False)
    if pattern == "hammer":
        return _pin(cs, True)
    if pattern == "shooting_star":
        return _pin(cs, False)
    return None


# ---- Confirmation candle (the candle right AFTER a signal candle), used by auto-trade's "wait for confirmation" mode.
CONFIRM_END_VS_REST = 0.5     # case 1: the ENDING wick must be at least this share of (body + STARTING wick). 1.0 = at least as long as both together
CONFIRM_SMALL_BODY = 0.35     # case 2: body at most this share of the candle's whole range
CONFIRM_MID = (0.4, 0.6)      # case 2: the middle of the body sits within this part of the range (0 = low, 1 = high)
CONFIRM_REJECT_BODY = 0.35    # case 3 (rejection candle): body at most this share of the candle's range
CONFIRM_REJECT_WICK = 2.0     # case 3: the wick on the SIGNAL's side (top after an up signal) at least this x the body
CONFIRM_REJECT_OTHER = 1.0    # case 3: the wick on the other side at most this x the body


def _wicks(c):
    """(starting wick, ending wick) of a candle by its OWN colour: green starts at the bottom and ends at the top; red the reverse.
    None for a candle with no body."""
    o, h, l, cl = c[1], c[2], c[3], c[4]
    if cl > o:
        return o - l, h - cl
    if cl < o:
        return h - o, cl - l
    return None


def confirm(c, signal_dir: str):
    """Judge the confirmation candle c = (time, o, h, l, c, v) after a signal candle that went `signal_dir` ("up"/"down").
    Case 2 (checked first, it is the narrower shape): small body in the middle of the candle with a wick on each side at least as
            long as the body, closing against the signal (below its open after an up signal, above after a down signal)
            -> trade the OPPOSITE direction.
    Case 3 (rejection candle, e.g. an inverted hammer / shooting star after an up signal): small body at the far end of the candle with a
            long wick on the SIGNAL's side (top after an up signal, bottom after a down signal), little wick on the other side, closing against
            the signal -> trade the SAME direction as the signal.
    Case 1: the candle's ENDING wick (the wick on the side where it closed: top if green, bottom if red) is at least
            CONFIRM_END_VS_REST x (body + STARTING wick) -> trade the SAME direction as the signal.
    Returns (case name, trade direction, short description) or None."""
    o, h, l, cl = c[1], c[2], c[3], c[4]
    rng = h - l
    if rng <= 0:
        return None
    body = abs(cl - o)
    b = max(body, rng * 0.02)
    upper, lower = h - max(o, cl), min(o, cl) - l
    centre = ((o + cl) / 2 - l) / rng
    against = cl < o if signal_dir == "up" else cl > o
    if body <= rng * CONFIRM_SMALL_BODY and CONFIRM_MID[0] <= centre <= CONFIRM_MID[1] and upper >= b and lower >= b and against:
        return ("case 2", "down" if signal_dir == "up" else "up", "small body in the middle, wicks both sides, closed against the signal")
    long_wick, short_wick = (upper, lower) if signal_dir == "up" else (lower, upper)
    if (against and body <= rng * CONFIRM_REJECT_BODY and long_wick >= CONFIRM_REJECT_WICK * b and short_wick <= CONFIRM_REJECT_OTHER * b):
        return ("case 3", signal_dir,
                f"rejection candle: {'upper' if signal_dir == 'up' else 'lower'} wick {long_wick / b:.1f}x the body, closed against the signal")
    w = _wicks(c)
    if w and w[1] > 0 and w[1] >= CONFIRM_END_VS_REST * (body + w[0]):
        return ("case 1", signal_dir, f"ending wick {w[1] / (body + w[0]):.1f}x (body + starting wick)")
    return None


def confirm_why(c, signal_dir: str) -> str:
    """The measurements behind a 'no match', in plain words, so you can see which test the candle missed."""
    o, h, l, cl = c[1], c[2], c[3], c[4]
    rng = h - l
    if rng <= 0:
        return "the candle was flat (no range)"
    body = abs(cl - o)
    w = _wicks(c)
    centre = ((o + cl) / 2 - l) / rng
    s = f"candle is {'green' if cl > o else 'red' if cl < o else 'a doji (no body)'}, body {100 * body / rng:.0f}% of the range. "
    if w:
        s += (f"Case 1: ending wick {w[1]:.5g} vs needed {CONFIRM_END_VS_REST:g} x (body {body:.5g} + starting wick {w[0]:.5g}) = "
              f"{CONFIRM_END_VS_REST * (body + w[0]):.5g}. ")
    else:
        s += "Case 1 needs a coloured candle. "
    long_wick, short_wick = (h - max(o, cl), min(o, cl) - l) if signal_dir == "up" else (min(o, cl) - l, h - max(o, cl))
    s += (f"Case 3: signal-side wick {long_wick:.5g} vs needed {CONFIRM_REJECT_WICK:g} x body = {CONFIRM_REJECT_WICK * max(body, rng * 0.02):.5g}, "
          f"other wick {short_wick:.5g} (max {CONFIRM_REJECT_OTHER:g} x body), body {100 * body / rng:.0f}% (max {100 * CONFIRM_REJECT_BODY:.0f}%), must close against the signal. ")
    return s + (f"Case 2: body {100 * body / rng:.0f}% (max {100 * CONFIRM_SMALL_BODY:.0f}%), centre at {100 * centre:.0f}% "
                f"(needs {100 * CONFIRM_MID[0]:.0f}-{100 * CONFIRM_MID[1]:.0f}%), wicks on both sides at least the body, closing against the signal")
