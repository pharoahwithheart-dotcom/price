# Price Alerts

Pick from a searchable list of every pair (or type one), then set an alert on any Binance crypto pair (BTCUSDT, ETHUSDT, ...) or forex pair (EURUSD, GBPJPY, XAUUSD, ...) and get a push notification on your phone.
Three kinds: price goes above, price drops below, or price moves by X% within N minutes.

## Alarm-style alerts with Pushover (recommended)
ntfy plays a normal notification sound once. Pushover can sound like an alarm and keep repeating until you tap Acknowledge,
and it can get through silent mode.
1. Install the **Pushover** app (iPhone / Android; free trial, then a small one-time fee per platform) and sign up.
   Your **User Key** is shown on the Pushover home page.
2. At pushover.net/apps/build create an application (name it "Price Alerts"); you get an **API Token**.
3. In Railway add `PUSHOVER_USER` = your user key and `PUSHOVER_TOKEN` = the API token. Redeploy.
4. Optional: `PUSHOVER_SOUND` (default `siren`; others: `alien`, `persistent`, `echo`, `climb`, `updown`) and
   `PUSHOVER_PRIORITY` (`2` repeats every minute for 30 min until acknowledged, the default; `1` is a single loud alert).
5. On the phone allow Pushover notifications, and in its settings allow "Critical alerts" so priority-2 messages play
   in silent mode and Do Not Disturb.
If Pushover is set, ntfy is only used as a backup when Pushover fails.

## Phone setup with ntfy (free, normal notification sound)
1. Install the free **ntfy** app (iPhone App Store / Android Play Store).
2. Tap **+**, subscribe to a long random topic name of your choice, e.g. `pa-7h3k9x2m4q8v1z`. Anyone who knows the name can read your alerts, so make it unguessable.

## Deploy on Railway
1. Put this folder in a GitHub repo, then Railway > New Project > Deploy from repo.
2. Variables:
   - `NTFY_TOPIC` = the topic name from step 2
   - `APP_TOKEN`  = a password for the web page
   - `DATA_DIR`   = `/data` and add a **Volume** mounted at `/data` (otherwise alerts are lost when it redeploys)
3. Open the Railway URL on your phone, enter the password, tap **Send a test notification**. Add it to your home screen if you like.

## Run it locally
```
pip install -r requirements.txt
NTFY_TOPIC=your-topic APP_TOKEN=pass uvicorn app:app --port 8000
```

Crypto prices come from Binance's public endpoint (no API key), checked every 10 s.

## Forex and gold prices (OANDA, free)
1. Create a free **OANDA fxTrade Practice** account (no deposit) at oanda.com.
2. In the OANDA hub go to My Services / API (or "Manage API Access"), and generate a **personal access token**.
3. In Railway add `OANDA_TOKEN` = that token. (`OANDA_ENV` defaults to `practice`; the account id is found automatically.)
4. The web page's footer will say "Forex source: OANDA". Prices are OANDA's mid price and are checked every 10 s,
   only for pairs you have an alert on, in one request.

Without `OANDA_TOKEN` it falls back to Yahoo Finance's unofficial feed (less accurate, fetched every 30 s).
Forex markets close at the weekend, so those prices stand still then. Not for split-second trading.

## Candle signals ("money entering")
Choose **a candle forms that looks like money entering**, then a signal and a candle size (5m, 15m, 1h, 4h, 1d).
Checked once each time a candle CLOSES (never on a half-finished candle), and only for candles that close after you
create the alert.
- **Buying / selling surge**: a candle whose body is at least 1.5x the recent average, closing in the top (or bottom)
  quarter of its range, on at least 2x normal volume. For forex, which has no real volume, it needs a body of 2x
  the average instead.
- **Bullish / bearish engulfing**: a candle whose body completely covers the previous opposite candle's body.
- **Hammer / shooting star**: a long wick rejecting a recent drop (or rise), with a small body.
- **Any bullish / any bearish**: any of the above on that side.
Crypto candles come from Binance, forex from OANDA (or Yahoo if there's no token; Yahoo has no 4h candles).
These flag candles that LOOK like buyers or sellers stepping in. They do not predict price and give false signals,
so use them as a prompt to look at the chart, and try them on a practice account first.

**First 5m candle of each 15m window** (three choices, always 5-minute candles): looks only at the candle that opens each
15-minute period (:00, :15, :30, :45), once it closes. It must be green (or red). The wick on the CLOSING side (top for green, bottom for red) may be at most a third of
the body (none is fine) and the opening-side wick can be any length. If the opening-side wick (bottom for green, top for
red) is longer than the body, the closing-side wick may be up to half the body instead. It must also have a body at least 1.5x the recent average and, for crypto, volume at
least 2x normal (forex: body at least 2x, since it has no real volume). The wick limits are constants at the top of `candles.py`.

**First 10m candle of each 30m window** (also three choices): the same rule one level up. It checks the 10-minute candle
that opens each 30-minute period (:00 and :30). Exchanges don't offer 10-minute candles, so these are built by joining
two 5-minute candles.

## Watching every forex pair at once
In the pair box choose **ALLFOREX (every forex pair)**, then any candle signal with a 5m, 10m, 15m or 1h candle. Needs
`OANDA_TOKEN`. Each time the candle closes, every forex and metal pair OANDA lists is checked (one request per pair, sent
one after another, well inside OANDA's rate limit) and you get ONE notification listing all the pairs that matched. For
the "first candle of the window" signals the check only runs once per window (every 15 or 30 minutes). Pairs whose market is
closed are skipped. 4h and daily candles aren't offered for this because OANDA's don't start on clock boundaries.

## Custom candle (describe your own)
Choose **Custom candle** as the candle signal and a form appears. Describe **1, 2 or 3 candles in a row** ("How many candles in a
row?"); the alert fires when the LAST one closes and all of them matched, in that order. Fill in only what matters; every box you fill
in must be true of that candle, blank boxes are ignored, and at least one box overall must be filled in. A candle left blank means "any candle".
For each candle:
- **Colour** (green/red) and the **previous candle's colour**; whether the market **fell or rose over the last N candles**.
- **Size and shape**, each with a min and a max: body as % of the candle height; body vs the recent average body; whole candle
  height vs the average; where it closes inside its range (0 = low, 100 = high); upper wick, lower wick, opening-side wick and
  closing-side wick (as multiples of the body); volume vs average (crypto only).
- **Shape** (pick one): doji, marubozu, long body, spinning top, hammer shape, shooting-star shape.
- **Breakout**: closes above the highest high (or below the lowest low) of the last N candles. **Average**: closes above or below the
  simple average of the last N closes (N up to 20).
- More min/max measures: move % from open to close, body vs the previous candle's body, height vs the previous candle's height,
  volume vs the previous candle's volume (crypto only).
- **Against the candle just before it** (in a pattern, that is the previous candle of the pattern): closes above its high, closes
  below its low, makes a higher high, makes a lower low, closes above / below its close, body covers its body, sits inside it, gaps up / down, opens above / below its close, opens or closes inside its body.
For the whole pattern:
- **Where and when**: the "first candle of each N minutes" rule is checked on the pattern's FIRST candle (so 2 candles starting at
  :00 of a 15-minute window); the hours (UTC, may wrap past midnight) and weekdays are checked on the LAST candle.
Works on one pair, on `ALLFOREX` and on `ALLOLYMP`. A 2 or 3 candle pattern with a minutes rule on `ALLFOREX` checks every candle
close (not only the first of each window), because the alert fires on a later candle. Alerts made before this update keep working.

## Olymp Trade (unofficial; candles/prices, plus optional demo auto-trade)
Olymp Trade has no official data API. This uses its own small websocket client (`olymp.py`, needs the `websockets` package, already in requirements.txt)
that talks to the same websocket the Olymp Trade website uses. It is undocumented and can change without warning. **Read before you turn it on:**
- The app asks for candles and prices. The ONLY thing it can ever send besides that is a DEMO trade from the optional Auto-trade feature (below); it never closes or changes a trade, and `olymp.send_frame` refuses any trade whose group is not `demo`.
- But the access token is your browser **session**, which can do anything your login can do, including trade with real money.
  Use a **separate Olymp Trade account that only has demo money** for this, never your main account, and keep the token
  only in the server's environment variables.
- Getting the token: log in at olymptrade.com, press F12, open the Network tab, find the websocket
  connection and copy the `access_token` value from its Cookie header. It expires; when it does, alerts stop and the page's
  status line says so. Paste a new one into `OLYMP_ACCESS_TOKEN`.
- Only one message type is ever sent: the candle-history request (`e:10`). Candle sizes are in seconds; 5, 60 and 300 were
  confirmed to work. Olymp Trade answers 'System error' to other sizes, so 10m, 15m and 1h candles are built from its 5-minute candles
  and 4h and 1d are not available for Olymp assets. If it rejects a request, the status line now says which asset and size. Candles have no volume, so volume rules never apply to Olymp. If Olymp Trade changes its protocol the status line
  shows 'did not answer' and only `olymp.py` needs updating.
- Olymp Trade does not publish a list of assets and the websocket has no call (that we know of) to list them, so the app watches only the OTC
  assets YOU list in `OLYMP_PAIRS` (comma-separated, no defaults), named exactly as Olymp Trade names them. To find the names:
  log in at olymptrade.com, press F12, open the Network tab, open an OTC asset and read the asset name in the websocket
  messages. A name Olymp Trade does not know just shows an error in the status line; the others keep working.

Set `OLYMP_ACCESS_TOKEN` and `OLYMP_PAIRS` (your OTC assets), then in the pair box pick `OLYMP:EURUSD` (price, % move or any candle signal,
including the custom candle form) or `ALLOLYMP` (candle signals on every asset in `OLYMP_PAIRS`, one bundled notification per
candle close). Candle sizes 4h and 1d depend on what Olymp Trade serves; if one fails the status line shows why.
Olymp Trade prices are its own, so wick and body sizes can differ a little from OANDA or Binance.

## Data check: Olymp Trade vs OANDA
With both `OANDA_TOKEN` and `OLYMP_ACCESS_TOKEN` set, every 5 minutes (20 seconds after a 5-minute candle closes) the app saves
that candle from OANDA and from Olymp Trade for each REAL forex pair in your Olymp list (OTC assets have no OANDA twin, so with an OTC-only list this does nothing), plus which candle signals each feed would have fired on it: every built-in 5-minute signal and any custom candle
alerts you have active. The page's **Data check** card shows the last 24 hours:
- the average gap between the two feeds' open, high, low and close, in pips,
- how often the candle's colour differed and how often Olymp had no candle with the same start time (a timing difference),
- for each signal: fired on both, OANDA only, or Olymp only, and the agreement percentage.
Three days of results are kept. Forex is closed at weekends, so nothing is recorded then. If the agreement is high for the pairs
and rules you use, OANDA is a fine stand-in; if not, use the `OLYMP:` pair for that alert.

## Paper trading (pretend trades, no money)
Needs Olymp Trade turned on. Each time a candle alert fires on an Olymp Trade asset (a single `OLYMP:` asset or `ALLOLYMP`), the app
saves one pretend trade: up or down (hammer, engulfing, surge, "first candle" up/down signals have a fixed direction; custom and
"either direction" signals follow the signal candle's own colour), entered at that candle's close. A few minutes later it reads
Olymp Trade's 1-minute candles and records win, loss or draw (same price) for each expiry in `PAPER_EXPIRIES` (default `1,3,5` minutes).
`PAPER_PAYOUT` (default `0.85`) is the profit per 1 staked on a win; the page shows the win rate you need to break even
(about 54% at 85%) and a 95% range for your actual win rate. Nothing here ever places a real trade. The **Paper trading** card
shows the last 7 days; results are kept for 30 days. Trust nothing until you have hundreds of trades.

## Auto-trade on your Olymp Trade DEMO account
Off by default and demo only: every trade is sent with `group: "demo"` and `olymp.send_frame` refuses anything else.
1. In the **Auto-trade** card on the page, fill in **Demo account ID**, **Stake** and **Expiry (seconds)** (copy them from a demo trade
   you place by hand: `account_id`, `amount` and `duration` in the message; for your capture 2947197935, 1500 and 20), then tap
   **Save settings**. They are kept in the database. You can also set the starting values with the environment variables
   `OLYMP_DEMO_ACCOUNT_ID`, `OLYMP_TRADE_AMOUNT`, `OLYMP_TRADE_DURATION`, but what you save on the page wins. The same card
   has the caps (default 6 per hour, 30 per day) and "skip if late" (default 20 s): a signal first seen later than that after
   its candle closed is not traded, because a 20-second trade should start right at the close.
2. When adding a candle alert on `OLYMP:...` or `ALLOLYMP`, tick **Auto-trade**.
3. In the **Auto-trade** card, turn the switch ON. Up signals trade "up", down signals "down" (custom and "either direction"
   signals follow the candle's colour), one trade per alert, asset and candle, only if the signal is less than 90 seconds old.
Every attempt is listed in the card with its status (including the ones not sent, and why), plus the server's reply. The app
does NOT read trade results back yet: the card also shows the last raw messages from Olymp Trade so the result format can be added.
The access token is your login, so keep it on a demo-only Olymp account. Check Olymp Trade's terms: automated trading may not be allowed.

### Wait for a confirmation candle (optional, tick the second box on the alert)
The candle that fires your alert is only the SIGNAL. The very next candle must then confirm it, and only then is a trade placed
for the whole candle after that (entered the moment the confirmation candle closes, expiring when the trading candle closes,
for example about 5 minutes on a 5-minute chart). Take a green signal as the example (a red signal is the mirror image):
- **Case 1, trade the SAME direction as the signal:** the confirmation candle's ENDING wick is long: at least half of (its body + its
  STARTING wick), so anything longer, up to and beyond "longer than the body and starting wick together", counts. Starting and ending
  are by the candle's own colour: a green candle starts at the bottom and ends at the top, a red one the reverse. There is no
  small-body limit any more. `CONFIRM_END_VS_REST` in `candles.py` sets the share (0.5 = half; 1.0 = at least as long as both together).
- **Case 2, trade DOWN (opposite):** a small body in the middle of the candle (its centre within the middle 20% of the range),
  a wick on each side at least as long as the body, and the candle closes BELOW its open. After a red signal it must close
  ABOVE its open and the trade goes UP.
- **Case 3, trade the SAME direction as the signal (rejection candle, e.g. an inverted hammer after an up signal):** small body (at most 35% of the range) at the far end of the candle, a long wick on the SIGNAL's side (top after an up signal, bottom after a down signal) at least 2x the body, at most 1x the body on the other side, and it closes against the signal (red after an up signal, green after a down one). Example: big green signal candle, then a small red candle with a long top wick, then trade UP (the signal's direction) for the next candle.

Case 2 is checked first, then case 3, then case 1. Anything else: no trade, and the card shows the measurements so you can see which test the candle missed. The numbers are constants at the bottom of `candles.py`. Each confirmed trade is also
saved as a pretend trade named "<signal> + confirmation case 1/2/3", with the expiry set to the candle length, so the Paper trading
card shows the two cases separately. The trade's expiry is the number of seconds left until the trading candle closes, minus "Finish before candle close" (default 1 s), so
it ends just BEFORE the next candle starts. It is skipped if fewer than 10 s would be left. Olymp Trade's own page sent exactly 300 for a
5-minute trade; an expiry like 296 has not been seen, so if Olymp Trade rejects it, the card shows its reply (send it to me).

## Trade journal (which signals worked, and what price did instead)
The **Results** card on the page shows, by itself, a table per signal and per pair plus the full list of every pretend trade, newest first: the signal, direction and entry price, the result at each
expiry (WIN / LOSS, with the move in pips), and a plain-words line such as "called up; after 5 min price was down 2.0 pips; best it went your way
3.5 pips, worst against you 2.5 pips". It loads by itself and refreshes every 30 seconds; choose 24 hours, 7 days or 30 days. "+" in pips means price moved your way.
Trades made before this update have no "best/worst" figures. Results are for pretend trades only; real demo trades are not read back.

Each trade in the Results card is also **drawn**: up to 3 candles before entry (the signal candle is boxed, and for a custom pattern the pattern candles
are labelled), the confirmation candle when the trade waited for one, a dashed line at the entry price, and the 1-minute candles after entry with a tick or
cross at each expiry. The All / Wins / Losses buttons filter the list. The candles are saved when the signal fires, so trades made before this update show text only.

## Real demo results, loss stop, time of day, luck check, result notifications
- **Real demo trades vs pretend** (Results card): each demo trade the auto-trader sends is read back about a minute after it ends and marked win / loss /
  draw, next to the pretend win rate for the same period. This is an ESTIMATE from Olymp's 1-minute candles (it assumes the expiry number is in seconds,
  as the Auto-trade card says); your Olymp Trade demo history is the truth. Check one trade by hand to confirm they agree.
- **Loss stop** (Auto-trade card): switches auto-trade OFF after N losses in a row (counted from when you last switched it on) or N losses in 24 hours,
  and tells your phone. Defaults 4 and 8; 0 = never. Environment variables `OLYMP_MAX_LOSS_STREAK` and `OLYMP_MAX_DAY_LOSSES` set the starting values.
- **Luck check**: each signal's results are split into a first and a second half; only a setup above break-even in BOTH halves (20+ decided trades) is
  marked green.
- **Time of day (UTC)**: win rate per hour for any signal and expiry; grey bars have fewer than 10 trades.
- **Notify my phone when results come in** (tick box in the Results card, off by default): one quiet message per batch of results.

## Stake as a percentage of the balance
In the Auto-trade card, **Stake type** can be a fixed amount (the Stake box) or **% of balance** (0.1 to 25 percent). In percent mode enter your demo balance
once, in the SAME units as the Stake (if a stake of 1500 shows as 15.00 on screen, a balance of 10,000.00 is 1000000). The app keeps the balance up to date
from the estimated results of the trades it sends (a win adds the stake times `PAPER_PAYOUT`, default 0.85; a loss takes the stake off), so re-enter it now
and then to correct drift. Optional **min** and **max** stake limits apply. The page shows "Next stake: ..." so you can check the number before switching on.
The Olymp balance itself is not read, because its message format is not known; if you send a captured balance message it can be added.

## Seeing your real (traded) setups in the analysis
Trades sent before send times were recorded are recovered from the reply Olymp saved: when the trade opened, its real entry price and the payout. Their results are read
back from Olymp's 1-minute history (up to about 10 hours back, if Olymp serves that much; otherwise about an hour). Pretend trades saved before charts existed are drawn from
the same history. In the Results card, a setup that the bot really traded shows a **TRADED (demo)** badge with the stake and the (estimated) result, and the **Traded by
the bot** button lists only those. A pretend result that could not be read is retried once with more history.

## Deriv bot (page: /deriv)
Trades Rise/Fall on your Deriv account when the candle patterns you tick show on a closed candle. **Demo by default; real money only if you switch it on yourself.** Uses Deriv's current API (developers.deriv.com): candles come from the public websocket (no login), trades go through your own token.
1. At developers.deriv.com register an app of type **PAT**, then create a Personal Access Token with the **trade** scope. Note the **App ID** and your **account id** (shown in Deriv's account switcher: demo ids start with DOT/VRT, real ones with CR).
2. Open `https://your-app/deriv`, choose **Demo**, paste the three values and tap **Test and save**. It only saves if Deriv accepts them AND the account matches the chosen mode (a real account in demo mode, or a demo account in real mode, is refused).
3. Enter symbols as Deriv names them (`frxEURUSD`, `R_100`, `1HZ100V`; new ones are checked with Deriv when you save), pick the candle size, patterns, stake and contract length, and the safety limits. Then switch it **ON**.
- **Real money:** choose "Real money", type `TRADE REAL MONEY`, and use your real account id. Saving always switches the bot OFF, and switching ON in real mode asks for one more confirmation. A red banner shows whenever live mode is active. Real mode cannot be set with an environment variable, only on the page. Demo and real results, loss streaks and limits are counted separately, so a demo record never mixes with live. Start with a very small stake and a low "stop after losing" amount.
- Bullish patterns buy Rise, bearish ones Fall, one trade per symbol per candle, placed within seconds of the candle closing (later than "max late" it skips that signal).
- Results are Deriv's own profit/loss for each contract. A contract whose result was missed (restart, dropped connection) is read back later.
- Safety limits: hard maximum stake, trades per hour/day, trades open at once, **maximum money lost in 24 hours**, and it switches itself OFF after N losses in a row or in 24 hours (you get a notification). Everything, including skipped signals, is listed on the page.
- Optional environment variables instead of the page: `DERIV_PAT`, `DERIV_APP_ID`, `DERIV_ACCOUNT_ID` (values saved on the page win; these always start in demo mode).
- Deriv candles have no volume, so volume-based rules never apply. Minimum contract lengths depend on the symbol; Deriv's error message is shown in the trade list if a length is not allowed.
- Honest note: nothing here predicts price. Deriv's payout is below 100%, so you need a win rate above the break-even rate shown on the page just to stay level, and synthetic indices (R_100, 1HZ100V...) are random by design. Run on demo until hundreds of trades show a win rate clearly above break-even that also holds on fresh days.

## Adaptive direction
Each confirmation case (1, 2, 3) keeps score of its DEFAULT direction (case 1 same as the signal, case 2 opposite, case 3 same) over the
last `ADAPT_WINDOW` (40) settled confirmed trades, whichever way the bot actually traded: the other direction's result is just the
reverse (win becomes loss, draw stays draw). When there are at least `ADAPT_MIN` (8) decided trades and the 95% range of the default's
win rate is entirely below 50%, that case trades the OTHER way; it goes back when the range is entirely above 50%. Cases adapt separately.
The **Adaptive direction** card on the page shows the score and has an on/off button; a phone message and the trade status say "adapted"
when it changed a direction. Older trades (before this feature) are scored by the rules they were made under: case 1 same, cases 2 and 3 opposite.

## Case 1 reports on the Adaptive direction card
- **Same trades, three direction rules:** every finished Case 1 trade is scored as if it had traded the signal's direction (how it used to),
  with the confirmation candle's colour, or against it.
- **What decides Case 1's direction?** For each saved Case 1 trade it records how the setup looked (signal direction, confirmation colour and
  size, ending wick, where it closed, size and position against the signal candle, time of day, asset, signal pattern) and whether price ROSE
  after entry. Each group's range is widened for the number of groups tested. A group is marked HELD only if that range excludes 50% AND both
  halves of the history lean the same way. In tests on random data this gave no false HELD flags; a strong real pattern needed several hundred
  trades to show. With fewer than 30 trades it says so and shows nothing.

## Deep analysis (what decides the direction)
Every pretend and confirmed trade now also saves the ~40 candles before its signal candle (filled in by the background loop from Olymp's
history; Olymp serves about 50 hours of 5-minute candles, so older trades only get the basic features). The **Find what decides the
direction** button on the Adaptive card, with a group selector (case 1, 2, 3, all confirmed trades, everything), looks at each trade in the
SIGNAL'S frame: did price continue the way the signal pointed, or reverse? It checks the trend over the last 5, 10 and 20 candles, where the
signal closed in the last 20 candles, 20-candle breakouts, volatility (expanding or contracting), the same-colour run, the candle before,
momentum, the close against the 20-candle average, plus the confirmation candle's colour, wicks, size and position and time of day, asset and
pattern. Ranges are widened for the number of groups tested; HELD needs that widened range to exclude 50% AND both halves of history to lean the
same way. Several HELD lines can be one underlying effect shown in different ways (trend, momentum and breakout move together).

## Playbook (learns which setups keep working, trades only by them, drops them when they stop)
Page card **Playbook**. From your finished *confirmed* trades it mines rules: one candle characteristic ("trend into the signal over 10 candles = against it")
or two together ("... AND confirmation case 1"), each with a direction (trade WITH or AGAINST the signal, whichever way the history leans).
A rule is **ACTIVE** only if it (1) matches `PLAYBOOK_MIN_N` (30) trades, (2) is significant on the OLDER 70% of the history after allowing for the
number of groups tested, (3) still wins on the NEWEST 30% (never used to pick it) and beats break-even overall, (4) wins in every third of the history
that has 5+ trades and in its latest matches. An active rule is **retired** when its latest 10+ matches win under 45% or its win rate falls under break-even; it
can return only by passing everything again. The window is the newest `PLAYBOOK_WINDOW` (600) trades, so old behaviour ages out and a new pattern that passes the
same tests is learned automatically (re-learned every 5 minutes when there are new finished trades; one quiet phone message per change).
- **Mode** (off / direction / only): *direction* = a matching rule decides the trade direction (otherwise the usual adaptive direction stands);
  *only* = trade only setups an active rule matches (no match, or rules that disagree = no real trade). Pretend trades are ALWAYS still recorded, so learning continues.
- **How strict**: strict (fewest false rules) / balanced (default, 10% false-discovery control) / relaxed (finds more, mostly luck).
- **Test it on my history**: replays your history in order; before each block of 10 trades it re-learns from the past only, then sees what the playbook would have done.
  Compare the three lines (all trades by default direction / playbook trades / default on those same trades) before switching the mode on.
- New trades now save their 40-candle history immediately; older ones only get it for about 50 hours back.
- Honest limits: in tests on random data it produced almost no false rules, and a real edge of 72% on a small slice of trades needed roughly 900-1500 trades to be
  found. With a few hundred trades expect "none yet": that is the system refusing luck, not a fault. After an edge dies it needs about 10 more matches to retire it.
  Nothing here predicts price; Olymp's payout is under 100%, so a rule must beat the break-even rate (about 54%) to matter.
- **Entry signals for trading by hand**: tick "Entry signals to my phone" in the Playbook card (on by default). Whenever an active rule matches a confirmed setup you get one
  high-priority message such as "ENTER EURUSD_OTC DOWN / PUT now ... expiry 06:23:07 UTC (end of this 5 min candle)", with the setup and the rule behind it. It works with auto-trade
  OFF and in any mode, so you can enter it yourself on any account. It needs an alert with Auto-trade ticked (that is what waits for the confirmation candle) and only fires within
  the usual lateness limit, so a late message means skip it. The playbook keeps learning from every confirmed setup, matched or not, so a second pattern can be found while the first is in use.
- **Heads-up before the entry moment**: the confirmation candle is only known when it closes, so the entry itself cannot be announced earlier. Instead you get (1) **GET READY** right after the
  signal candle closes, when an active rule could still match (only the conditions on the signal side must hold; the confirmation ones stay open), with the exact entry time (the close of the
  confirmation candle) and the possible direction; (2) a **30 seconds** reminder; (3) **ENTER** at the close if the confirmation qualifies. A GET READY that is not followed by ENTER means
  the confirmation candle did not qualify: do not trade. With 1-minute candles the lead time is under a minute; 5-minute and longer candles give several minutes.
