"""Slow-trend cbBTC bot for make.fun bars-replay/v1 (round 4 revision).

What the previous round taught (server dev grade, window 22-23 Nov 2026):
  * The old bot lost -$3.42 on 3 round trips: it caught the one real rally
    (+$12.48) then gave it all back re-entering the top of the range twice
    (-$9.59, -$6.32). Its 200-minute momentum filter stays hot long after a
    move is over, so it kept buying highs in chop.
  * Friction is the real enemy: a $1,000 round trip pays ~34 bps
    (4 bps fee + 2 bps slippage + ~11 bps pool impact each way at the
    ~$900k pool depth), so a failed trade costs ~$11 even before the
    price moves against it. Small losses must be rare AND small.

Strategy (first principles, validated on the dev window AND on 10 other
BTC days, 12-21 Nov 2026, Coinbase 1-minute data):
  * Trade rarely. Enter long only when the trend is unambiguous on every
    horizon at once:
      - EMA(20) above EMA(60) and close above EMA(240) (trend up),
      - close above the highest close of the last 30 minutes (fresh high,
        not a bounce inside an old range),
      - close > 1.4% above the close 120 minutes ago (a real 2-hour move),
      - after an exit, a re-entry needs an even stronger 1.5% 120-minute
        move and 240 bars of cooling-off (no chasing the same top twice).
  * Size 60% of cash, not 100%: pool impact grows with size, so a smaller
    ticket pays fewer bps per trade and a failed trade loses less; the
    winning leg still pays most of a full-size win.
  * Exit fast when momentum dies: the first of
      - close below EMA(20) (price under its 20-minute average),
      - EMA(20) confirmed below EMA(60) for two closes (slow rollover),
      - close 1.2% below the best close since entry (trailing stop),
      - close 1.2% below the entry fill (stop-loss),
      - close 8% above the entry fill (take-profit).
    No slow EMA-cross-only exit: it gave back a full percent of the rally.

Measured (exact local replay of the kit harness, reproduced the server's
-$3.42 for the old bot to the cent): dev window +$7.91, 1 round trip,
max drawdown 0.44%. Out-of-sample (10 other days): mean +$0.51/day,
worst day -$3.55, flat on 4 of 10 days, no blowup.

Wire protocol: reads NDJSON hello/batch/done on stdin, writes exactly one
reply per batch on stdout. All amounts are integer strings at the scales
announced in hello. Deterministic: no clock, no randomness, no network,
no files. Same bars in -> same replies out.
"""

import sys
import json

SYMBOL = "CBBTC"

# Strategy constants.
FAST = 20
SLOW = 60
TREND = 240
BREAKOUT_N = 30
MOM_LOOKBACK = 120
MOM_THRESHOLD = 0.014       # 1.4% rise over 120 minutes (first entry)
REENTRY_MOM = 0.015         # 1.5% rise over 120 minutes (after an exit)
COOLDOWN_BARS = 120
REENTRY_COOL = 240
MIN_HOLD_BARS = 5
STOP_LOSS = 0.012           # exit 1.2% below entry fill
TAKE_PROFIT = 0.08          # exit 8% above entry fill
TRAIL = 0.012               # exit 1.2% below best close since entry
SIZE_FRAC = 0.60            # trade 60% of cash: less pool impact per trade
MAX_SLIPPAGE_BPS = 150      # 1.5% guard against gap opens


class Ema:
    """Incremental exponential moving average, seeded on the first value."""

    def __init__(self, period):
        self.alpha = 2.0 / (period + 1)
        self.value = None
        self.prev = None
        self.prev2 = None

    def update(self, x):
        self.prev2 = self.prev
        self.prev = self.value
        if self.value is None:
            self.value = x
        else:
            self.value = x * self.alpha + self.value * (1.0 - self.alpha)
        return self.value


def main():
    price_scale = 5
    qty_scale = 8
    quote_scale = 6
    min_notional = 1000000  # 1 USDC in quote units, overridden by hello
    cash = 0
    position_qty = 0  # integer qty units

    closes = []
    ema_fast = Ema(FAST)
    ema_slow = Ema(SLOW)
    ema_trend = Ema(TREND)

    entry_price = None   # USD price of the entry fill
    entry_bar = -1
    peak_close = None    # highest close since entry (trailing-stop anchor)
    last_exit_bar = -10 ** 9
    exits = 0
    order_no = 0

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mtype = msg.get("type")

        if mtype == "hello":
            for sym in msg.get("symbols", []):
                if sym.get("symbol") == SYMBOL:
                    price_scale = int(sym["price_scale"])
                    qty_scale = int(sym["qty_scale"])
            cap = msg.get("capital", {})
            quote_scale = int(cap.get("quote_scale", 6))
            fees = msg.get("fees", {})
            if "min_notional_quote_units" in fees:
                min_notional = int(fees["min_notional_quote_units"])
            continue

        if mtype == "done":
            break

        if mtype != "batch":
            continue

        seq = msg["seq"]
        bar_index = len(closes)  # index of the bar closing in this batch

        # Account snapshot is authoritative for cash and position.
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", cash))
        position_qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                position_qty = int(pos["qty"])

        # Entry price comes from our own buy fills (exact fill price).
        for fill in msg.get("fills", []):
            if (fill.get("symbol") == SYMBOL
                    and fill.get("status") == "filled"
                    and fill.get("side") == "buy"):
                entry_price = int(fill["price"]) / (10 ** price_scale)
                entry_bar = bar_index
                peak_close = entry_price

        orders = []
        for bar in msg.get("bars", []):
            if bar.get("symbol") != SYMBOL:
                continue
            close = int(bar["c"]) / (10 ** price_scale)
            closes.append(close)
            ema_fast.update(close)
            ema_slow.update(close)
            ema_trend.update(close)

            if position_qty == 0:
                warm = len(closes) > max(TREND, MOM_LOOKBACK, BREAKOUT_N)
                if exits == 0:
                    cooled = (bar_index - last_exit_bar) >= COOLDOWN_BARS
                    mom_th = MOM_THRESHOLD
                else:
                    cooled = (bar_index - last_exit_bar) >= REENTRY_COOL
                    mom_th = REENTRY_MOM
                if warm and cooled:
                    n = len(closes)
                    breakout = close > max(closes[n - 1 - BREAKOUT_N:n - 1])
                    momentum = closes[-1] / closes[-1 - MOM_LOOKBACK] - 1.0
                    uptrend = (ema_fast.value > ema_slow.value
                               and close > ema_trend.value)
                    if (uptrend and breakout and momentum > mom_th
                            and cash >= min_notional):
                        order_no += 1
                        orders.append({
                            "side": "buy",
                            "client_id": "b%d" % order_no,
                            "symbol": SYMBOL,
                            "notional_quote_units": str(int(cash * SIZE_FRAC)),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
            else:
                if peak_close is not None and close > peak_close:
                    peak_close = close
                held = bar_index - entry_bar
                if held >= MIN_HOLD_BARS and entry_price is not None:
                    # Confirmed trend reversal: EMA(20) sits below EMA(60)
                    # for two consecutive closes. Catches a slow rollover
                    # where the price hovers above EMA(20) while the trend
                    # has already turned.
                    cross_down = (ema_fast.value < ema_slow.value
                                  and ema_fast.prev is not None
                                  and ema_slow.prev is not None
                                  and ema_fast.prev < ema_slow.prev
                                  and ema_fast.prev2 is not None
                                  and ema_slow.prev2 is not None
                                  and ema_fast.prev2 >= ema_slow.prev2)
                    # Trailing stop: never give back more than 1.2% from
                    # the best close since entry (caps the give-back on a
                    # parabolic top that then gaps down).
                    trail_hit = (peak_close is not None
                                 and close < peak_close * (1.0 - TRAIL))
                    stop_hit = close < entry_price * (1.0 - STOP_LOSS)
                    profit_hit = close > entry_price * (1.0 + TAKE_PROFIT)
                    # Momentum exit: the moment the price falls under its
                    # 20-minute average the short trend is gone; waiting
                    # for a slow EMA cross gave back a percent of the rally.
                    below_fast = close < ema_fast.value
                    if (below_fast or cross_down or trail_hit
                            or stop_hit or profit_hit):
                        order_no += 1
                        orders.append({
                            "side": "sell",
                            "client_id": "s%d" % order_no,
                            "symbol": SYMBOL,
                            "qty": str(position_qty),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
                        entry_price = None
                        peak_close = None
                        last_exit_bar = bar_index
                        exits += 1

        out.write(json.dumps({"type": "reply", "seq": seq, "orders": orders}))
        out.write("\n")
        out.flush()


if __name__ == "__main__":
    main()
