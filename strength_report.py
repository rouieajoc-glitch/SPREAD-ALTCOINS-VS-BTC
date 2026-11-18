"""
Strength vs BTC - daily Telegram report
=======================================
Same logic as the TradingView "Strength vs BTC table" indicator:
  * ALT/BTC ratio built from ALTUSDT and BTCUSDT daily candles (closed candles only)
  * 1D % and 7D % change of the ratio
  * EMA50 side, slope and fresh breaks
  * Bull Market Support Band (20 SMA + 21 EMA, daily by default)
  * Internal + swing structure (BOS / CHoCH / CHoCH+), ported from CA-SMC+LB v20.1
  * Score = EMA + BMSB + internal trend + swing trend (-4..+4), top 3 bull / bear
Sends the report to Telegram and appends one row per coin to data/strength_log.csv
(the journal used later to test whether the signals have an edge).

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (without them the report is only printed)
"""

import csv
import html
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------- settings
# "KSM" = Binance KSMUSDT. "MEXC:XEL" = MEXC XELUSDT. Add / remove coins here.
COINS = ["ETH", "ADA", "DOT", "ALGO", "SHIB", "RSR", "LINK", "NEAR", "TIA", "SEI",
         "OP", "COMP", "BAT", "CKB", "ID", "ANKR", "AVAX", "INJ", "KSM", "MEXC:XEL","TAO","SOL","XRP","TRX"]

EMA_LEN = 50
SLOPE_N = 5
FRESH_N = 3            # EMA break / CHoCH / BMSB flip counts as NEW for this many days
INT_LEN = 5            # internal structure length (same as CA-SMC+LB)
SW_LEN = 50            # swing structure length (same as CA-SMC+LB)
CONFLUENCE = True      # confluence filter on internal breaks
BMSB_TF = "D"          # "D" = daily band (default), "W" = weekly band
BMSB_SMA = 20
BMSB_EMA = 21
BMSB_IN_SCORE = True
TABLE_STYLE = "emoji"  # "emoji" = colored dots, "text" = + - ~ (narrowest)
LIMIT = 1000           # daily candles downloaded per symbol

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "strength_log.csv")

# Binance's public market-data host works from GitHub's servers (api.binance.com blocks US IPs)
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
MEXC_URL = "https://api.mexc.com/api/v3/klines"


# ---------------------------------------------------------------- data
def get_json(url, params, tries=3):
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network hiccup: retry
            if k == tries - 1:
                raise
            time.sleep(2 + 2 * k)


def klines(exchange, symbol):
    """Closed daily candles as dict: open_time -> (o, h, l, c)."""
    if exchange == "MEXC":
        raw = get_json(MEXC_URL, {"symbol": symbol, "interval": "1d", "limit": LIMIT})
    else:
        raw = get_json(BINANCE_URL, {"symbol": symbol, "interval": "1d", "limit": LIMIT})
    now_ms = int(time.time() * 1000)
    out = {}
    for k in raw:
        open_t, close_t = int(k[0]), int(k[6])
        if close_t >= now_ms:      # skip the candle that is still open
            continue
        out[open_t] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
    return out


def ratio_series(alt, btc):
    """ALT/BTC candles on the days both exist. Open and close are exact; high / low are
    approximated from the two charts' highs / lows (TradingView does the same kind of thing)."""
    days = sorted(set(alt) & set(btc))
    t, o, h, l, c = [], [], [], [], []
    for d in days:
        ao, ah, al, ac = alt[d]
        bo, bh, bl, bc = btc[d]
        ro, rc = ao / bo, ac / bc
        rh, rl = ah / bh, al / bl
        t.append(d)
        o.append(ro)
        c.append(rc)
        h.append(max(rh, ro, rc))
        l.append(min(rl, ro, rc))
    return t, o, h, l, c


# ---------------------------------------------------------------- indicators (Pine-identical)
def sma(src, n):
    out = [None] * len(src)
    s = 0.0
    for i, v in enumerate(src):
        s += v
        if i >= n:
            s -= src[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(src, n):
    """Pine ta.ema: seeded with the SMA of the first n values."""
    out = [None] * len(src)
    a = 2.0 / (n + 1)
    first = sma(src, n)
    prev = None
    for i, v in enumerate(src):
        if prev is None:
            prev = first[i]
        else:
            prev = a * v + (1 - a) * prev
        out[i] = prev
    return out


def barssince(cond):
    """Bars since cond was last true (0 = true on this bar), None if never."""
    out, last = [], None
    for i, x in enumerate(cond):
        if x:
            last = i
        out.append(None if last is None else i - last)
    return out


def gt(a, b):
    return a is not None and b is not None and a > b


def lt(a, b):
    return a is not None and b is not None and a < b


# ---------------------------------------------------------------- structure engine (CA-SMC+LB v20.1)
def legs(h, l, size):
    """Current leg per bar: 1 = bullish, 0 = bearish (LuxAlgo leg logic)."""
    n, lg, out = len(h), 0, []
    for i in range(n):
        if i >= size:
            hh = max(h[i - size + 1:i + 1])
            ll = min(l[i - size + 1:i + 1])
            if h[i - size] > hh:
                lg = 0
            elif l[i - size] < ll:
                lg = 1
        out.append(lg)
    return out


def structure(o, h, l, c):
    n = len(c)
    lgS, lgI = legs(h, l, SW_LEN), legs(h, l, INT_LEN)
    S = dict(swHc=None, swHl=None, swHs=False, swLc=None, swLl=None, swLs=False,
             inHc=None, inHl=None, inHs=False, inLc=None, inLl=None, inLs=False,
             inTr=0, inEv=0, inBar=None, swTr=0, swEv=0, swBar=None)
    prev = dict(inHc=None, inLc=None, swHc=None, swLc=None)
    for i in range(n):
        if i > 0:
            chS, chI = lgS[i] - lgS[i - 1], lgI[i] - lgI[i - 1]
            if chS == 1:
                S["swLl"], S["swLc"], S["swLs"] = S["swLc"], l[i - SW_LEN], False
            elif chS == -1:
                S["swHl"], S["swHc"], S["swHs"] = S["swHc"], h[i - SW_LEN], False
            if chI == 1:
                S["inLl"], S["inLc"], S["inLs"] = S["inLc"], l[i - INT_LEN], False
            elif chI == -1:
                S["inHl"], S["inHc"], S["inHs"] = S["inHc"], h[i - INT_LEN], False

        upW = h[i] - max(c[i], o[i])
        loW = min(c[i], o[i] - l[i])           # original LuxAlgo formula (as in CA-SMC+LB)
        bullOK = (not CONFLUENCE) or upW > loW
        bearOK = (not CONFLUENCE) or upW < loW

        def xover(key):   # ta.crossover(close, level)
            return i > 0 and gt(c[i], S[key]) and prev[key] is not None and c[i - 1] <= prev[key]

        def xunder(key):  # ta.crossunder(close, level)
            return i > 0 and lt(c[i], S[key]) and prev[key] is not None and c[i - 1] >= prev[key]

        xIH, xIL, xSH, xSL = xover("inHc"), xunder("inLc"), xover("swHc"), xunder("swLc")
        neH = S["inHc"] is not None and S["swHc"] is not None and S["inHc"] != S["swHc"]
        neL = S["inLc"] is not None and S["swLc"] is not None and S["inLc"] != S["swLc"]

        if xIH and not S["inHs"] and neH and bullOK:
            pU = S["inTr"] == -1 and lt(S["inHc"], S["inHl"]) and lt(S["inLc"], S["inLl"])
            S["inEv"] = (3 if pU else 2) if S["inTr"] == -1 else 1
            S["inTr"], S["inHs"], S["inBar"] = 1, True, i
        if xIL and not S["inLs"] and neL and bearOK:
            pD = S["inTr"] == 1 and gt(S["inHc"], S["inHl"]) and gt(S["inLc"], S["inLl"])
            S["inEv"] = (-3 if pD else -2) if S["inTr"] == 1 else -1
            S["inTr"], S["inLs"], S["inBar"] = -1, True, i
        if xSH and not S["swHs"]:
            pSU = S["swTr"] == -1 and lt(S["swHc"], S["swHl"]) and lt(S["swLc"], S["swLl"])
            S["swEv"] = (3 if pSU else 2) if S["swTr"] == -1 else 1
            S["swTr"], S["swHs"], S["swBar"] = 1, True, i
        if xSL and not S["swLs"]:
            pSD = S["swTr"] == 1 and gt(S["swHc"], S["swHl"]) and gt(S["swLc"], S["swLl"])
            S["swEv"] = (-3 if pSD else -2) if S["swTr"] == 1 else -1
            S["swTr"], S["swLs"], S["swBar"] = -1, True, i

        for k in prev:
            prev[k] = S[k]

    last = n - 1
    return dict(inTr=S["inTr"], inEv=S["inEv"], iA=None if S["inBar"] is None else last - S["inBar"],
                swTr=S["swTr"], swEv=S["swEv"], sA=None if S["swBar"] is None else last - S["swBar"])


# ---------------------------------------------------------------- BMSB
def bmsb_state(t, c):
    if BMSB_TF == "W":
        # weekly closes rebuilt from daily candles (weeks start Monday 00:00 UTC); current week = live close
        wk = [datetime.fromtimestamp(x / 1000, tz=timezone.utc).isocalendar()[:2] for x in t]
        bs, be = [None] * len(c), [None] * len(c)
        done, wema, a = [], None, 2.0 / (BMSB_EMA + 1)
        for i in range(len(c)):
            if i > 0 and wk[i] != wk[i - 1]:
                done.append(c[i - 1])
                if wema is None:
                    if len(done) >= BMSB_EMA:
                        wema = sum(done[-BMSB_EMA:]) / BMSB_EMA
                else:
                    wema = a * c[i - 1] + (1 - a) * wema
            if len(done) >= BMSB_SMA - 1:
                bs[i] = (sum(done[len(done) - (BMSB_SMA - 1):]) + c[i]) / BMSB_SMA
            if wema is not None:
                be[i] = a * c[i] + (1 - a) * wema
    else:
        bs, be = sma(c, BMSB_SMA), ema(c, BMSB_EMA)
    st = []
    for i in range(len(c)):
        if bs[i] is None or be[i] is None:
            st.append(0)
        elif c[i] > max(bs[i], be[i]):
            st.append(1)
        elif c[i] < min(bs[i], be[i]):
            st.append(-1)
        else:
            st.append(2)       # inside the band
    age = barssince([i > 0 and st[i] != st[i - 1] for i in range(len(st))])
    return st[-1], age[-1]


# ---------------------------------------------------------------- one coin
def analyse(t, o, h, l, c):
    if len(c) < 60:
        return None
    e = ema(c, EMA_LEN)
    eS = 1 if gt(c[-1], e[-1]) else -1
    cross = [i > 0 and e[i] is not None and e[i - 1] is not None and
             ((c[i] > e[i] and c[i - 1] <= e[i - 1]) or (c[i] < e[i] and c[i - 1] >= e[i - 1]))
             for i in range(len(c))]
    eA = barssince(cross)[-1]
    eSl = 1 if gt(e[-1], e[-1 - SLOPE_N]) else -1
    bm, bmA = bmsb_state(t, c)
    st = structure(o, h, l, c)
    r = dict(close=c[-1], c1=(c[-1] / c[-2] - 1) * 100, c7=(c[-1] / c[-8] - 1) * 100,
             eS=eS, eA=eA, eSl=eSl, bm=bm, bmA=bmA, day=t[-1])
    r.update(st)
    bmPts = (1 if bm == 1 else -1 if bm == -1 else 0) if BMSB_IN_SCORE else 0
    r["score"] = eS + st["inTr"] + st["swTr"] + bmPts
    return r


# ---------------------------------------------------------------- formatting
def pct(v):
    return "n/a" if v is None else f"{v:+.2f}%"


def arrow(x):
    return "▲" if x == 1 else "▼" if x == -1 else "◆" if x == 2 else "-"


def dot(x):
    """Colored state dot for the table."""
    return "🟢" if x == 1 else "🔴" if x == -1 else "🟡" if x == 2 else "⚪"


def sym(x):
    """1-character state for the monospace table (arrows render double-width on phones)."""
    return "+" if x == 1 else "-" if x == -1 else "~" if x == 2 else "."


def ev_txt(ev):
    return {3: "CHoCH+", 2: "CHoCH", 1: "BOS"}.get(abs(ev), "-")


def age_txt(a):
    return "" if a is None else "new" if a == 0 else f"{a}d"


def fresh(a):
    return a is not None and a <= FRESH_N


def build_message(btc, rows, failed):
    day = datetime.fromtimestamp(btc["day"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    b = btc["score"]
    verdict = ("🟢 BULL → longs on TOP 3" if b >= 2 else
               "🔴 BEAR → shorts on BOTTOM 3" if b <= -2 else "🟡 MIXED → smaller size / wait")
    ok = sorted([r for r in rows if r["res"]], key=lambda r: r["res"]["score"] * 1000 +
                max(-999, min(999, r["res"]["c7"])), reverse=True)
    bull = [r["name"] for r in ok if r["res"]["score"] > 0][:3]
    bear = [r["name"] for r in reversed(ok) if r["res"]["score"] < 0][:3]

    L = []
    L.append(f"<b>📊 STRENGTH vs BTC · 1D · candle {day}</b>")
    L.append(f"BTC 7D {pct(btc['c7'])} · EMA{EMA_LEN} {arrow(btc['eS'])} · BMSB {arrow(btc['bm'])} · "
             f"INT {arrow(btc['inTr'])} · SW {arrow(btc['swTr'])}")
    L.append(f"<b>{verdict}</b>")
    L.append("")
    L.append(f"🟢 <b>TOP 3 BULL:</b> {' · '.join(bull) if bull else 'none'}")
    L.append(f"🔴 <b>TOP 3 BEAR:</b> {' · '.join(bear) if bear else 'none'}")

    news = []
    for r in ok:
        x, n = r["res"], r["name"]
        if fresh(x["eA"]):
            news.append(f"{n}: {'broke ▲' if x['eS'] == 1 else 'lost ▼'} EMA{EMA_LEN} ({age_txt(x['eA'])})")
        if x["bm"] != 0 and fresh(x["bmA"]):
            news.append(f"{n}: BMSB → {'above' if x['bm'] == 1 else 'below' if x['bm'] == -1 else 'in band'} ({age_txt(x['bmA'])})")
        if abs(x["inEv"]) >= 2 and fresh(x["iA"]):
            news.append(f"{n}: INT {arrow(x['inTr'])} {ev_txt(x['inEv'])} ({age_txt(x['iA'])})")
        if abs(x["swEv"]) >= 2 and fresh(x["sA"]):
            news.append(f"{n}: SWING {arrow(x['swTr'])} {ev_txt(x['swEv'])} ({age_txt(x['sA'])})")
    if news:
        L.append("")
        L.append("<b>⚡ New signals:</b>")
        L += news

    L.append("")
    # compact table: fits a phone screen without wrapping
    L.append("<pre>")
    if TABLE_STYLE == "emoji":
        L.append(f"{'COIN':<5}{'1D':>5}{'7D':>6}  E B I S  SC")
        for r in ok:
            x = r["res"]
            L.append(f"{r['name'][:5]:<5}{x['c1']:>+5.1f}{x['c7']:>+6.1f} {dot(x['eS'])}{dot(x['bm'])}"
                     f"{dot(x['inTr'])}{dot(x['swTr'])} {x['score']:>+2d}")
    else:
        L.append(f"{'COIN':<5}{'1D':>5}{'7D':>6} E B I S SC")
        for r in ok:
            x = r["res"]
            L.append(f"{r['name'][:5]:<5}{x['c1']:>+5.1f}{x['c7']:>+6.1f} {sym(x['eS'])} {sym(x['bm'])} "
                     f"{sym(x['inTr'])} {sym(x['swTr'])} {x['score']:>+2d}")
    L.append("</pre>")
    if TABLE_STYLE == "emoji":
        L.append("<i>Dots: EMA50 · BMSB · Internal · Swing\n🟢 bull  🔴 bear  🟡 in BMSB band</i>")
    else:
        L.append("<i>E=EMA50 B=BMSB I=int S=swing · + bull, - bear, ~ in band</i>")
    if failed:
        L.append("⚠️ No data: " + ", ".join(html.escape(f) for f in failed))
    return "\n".join(L), bull, bear


# ---------------------------------------------------------------- journal
LOG_FIELDS = ["candle_date", "coin", "ratio_close", "chg_1d", "chg_7d", "ema_side", "ema_age", "ema_slope",
              "bmsb", "bmsb_age", "int_trend", "int_event", "int_age", "swing_trend", "swing_event", "swing_age",
              "score", "rank", "top3", "btc_score"]


def write_log(rows, btc, bull, bear):
    day = datetime.fromtimestamp(btc["day"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    old = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, newline="") as f:
            old = [r for r in csv.DictReader(f) if r["candle_date"] != day]   # re-run on same day replaces
    ok = sorted([r for r in rows if r["res"]], key=lambda r: r["res"]["score"] * 1000 +
                max(-999, min(999, r["res"]["c7"])), reverse=True)
    new = []
    for k, r in enumerate(ok, 1):
        x = r["res"]
        new.append(dict(candle_date=day, coin=r["name"], ratio_close=f"{x['close']:.10g}",
                        chg_1d=f"{x['c1']:.3f}", chg_7d=f"{x['c7']:.3f}", ema_side=x["eS"], ema_age=x["eA"],
                        ema_slope=x["eSl"], bmsb=x["bm"], bmsb_age=x["bmA"], int_trend=x["inTr"],
                        int_event=x["inEv"], int_age=x["iA"], swing_trend=x["swTr"], swing_event=x["swEv"],
                        swing_age=x["sA"], score=x["score"], rank=k,
                        top3="bull" if r["name"] in bull else "bear" if r["name"] in bear else "",
                        btc_score=btc["score"]))
    with open(LOG_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(old + new)


# ---------------------------------------------------------------- telegram
def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("(Telegram secrets not set: message only printed)")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=20)
    if r.status_code != 200:
        print("Telegram error:", r.status_code, r.text)
        sys.exit(1)


# ---------------------------------------------------------------- main
def parse_coin(s):
    exch, base = (s.split(":", 1) if ":" in s else ("BINANCE", s))
    return exch.upper(), base.upper()


def main():
    btc_k = klines("BINANCE", "BTCUSDT")
    bt = sorted(btc_k)
    btc = analyse(bt, *[[btc_k[d][j] for d in bt] for j in range(4)])

    rows, failed = [], []
    for s in COINS:
        exch, base = parse_coin(s)
        try:
            alt = klines(exch, base + "USDT")
            res = analyse(*ratio_series(alt, btc_k))
        except Exception as e:
            print(f"{s}: {e}")
            res = None
        if res is None:
            failed.append(base)
        rows.append(dict(name=base, res=res))

    text, bull, bear = build_message(btc, rows, failed)
    print(text)
    write_log(rows, btc, bull, bear)
    send_telegram(text)


if __name__ == "__main__":
    main()
