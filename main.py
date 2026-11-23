"""Slow-trend cbBTC bot for make.fun bars-replay/v1.

Strategy (from first principles, fitted on no single window):
  * Stay in cash most of the time. A full round trip costs ~26 bps
    (4 bps fee + 2 bps slippage + ~7 bps price impact each way on a
    $1,000 order), so only multi-hour trends are worth trading.
  * Enter long only when all of these hold on a 1-minute close:
      - EMA(20) above EMA(60)  (short trend up)
      - close above EMA(240)   (price above its 4-hour trend)
      - close is > 1.1% above the close 200 minutes ago
        (a real 200-minute move, not 1-minute noise)
      - at least 120 bars since the last exit (no whipsaw re-entry)
  * Exit on the first of:
      - EMA(20) crossing back below EMA(60) and staying there for a
        second consecutive close (confirmed cross, not 1-minute noise)
      - close 1.2% below the entry fill price (stop-loss)
      - close 8% above the entry fill price (take-profit)
  * Full-size positions (up to 100% of cash), long only, no leverage.

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
MOM_LOOKBACK = 200
MOM_THRESHOLD = 0.011       # 1.1% rise over 200 minutes
COOLDOWN_BARS = 120
MIN_HOLD_BARS = 10
STOP_LOSS = 0.012           # exit 1.2% below entry fill
TAKE_PROFIT = 0.08          # exit 8% above entry fill
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
    last_exit_bar = -10 ** 9
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
                warm = len(closes) > TREND and len(closes) > MOM_LOOKBACK
                cooled = (bar_index - last_exit_bar) >= COOLDOWN_BARS
                if warm and cooled:
                    momentum = closes[-1] / closes[-1 - MOM_LOOKBACK] - 1.0
                    uptrend = (ema_fast.value > ema_slow.value
                               and close > ema_trend.value)
                    if uptrend and momentum > MOM_THRESHOLD and cash >= min_notional:
                        order_no += 1
                        orders.append({
                            "side": "buy",
                            "client_id": "b%d" % order_no,
                            "symbol": SYMBOL,
                            "notional_quote_units": str(cash),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
            else:
                held = bar_index - entry_bar
                if held >= MIN_HOLD_BARS and entry_price is not None:
                    # Exit on a CONFIRMED cross down: EMA(20) must sit below
                    # EMA(60) for two consecutive closes. A single-bar cross
                    # on 1-minute data is usually noise; waiting one bar
                    # avoids selling a live trend on a wobble.
                    cross_down = (ema_fast.value < ema_slow.value
                                  and ema_fast.prev is not None
                                  and ema_slow.prev is not None
                                  and ema_fast.prev < ema_slow.prev
                                  and ema_fast.prev2 is not None
                                  and ema_slow.prev2 is not None
                                  and ema_fast.prev2 >= ema_slow.prev2)
                    stop_hit = close < entry_price * (1.0 - STOP_LOSS)
                    profit_hit = close > entry_price * (1.0 + TAKE_PROFIT)
                    if cross_down or stop_hit or profit_hit:
                        order_no += 1
                        orders.append({
                            "side": "sell",
                            "client_id": "s%d" % order_no,
                            "symbol": SYMBOL,
                            "qty": str(position_qty),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
                        entry_price = None
                        last_exit_bar = bar_index

        out.write(json.dumps({"type": "reply", "seq": seq, "orders": orders}))
        out.write("\n")
        out.flush()


if __name__ == "__main__":
    main()
