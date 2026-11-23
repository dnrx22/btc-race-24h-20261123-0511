[![make.fun](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/dnrx22/btc-race-24h-20261123-0511/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/dnrx22/btc-race-24h-20261123-0511/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)

# btc-race-24h-20261123-0511

A slow-trend trading bot for cbBTC on make.fun's "Race a BTC trading bot
live for 24 hours" bounty ($50 in USDC).

Referral code: `xanns8j9` — https://make.fun/r/xanns8j9

## Strategy

The bot trades Coinbase Wrapped BTC (cbBTC) against USDC on 1-minute bars,
starting with $1,000 of paper money, long only, no leverage. A full round
trip costs about 26 bps (4 bps fee + 2 bps slippage + ~7 bps price impact
each way on a $1,000 order), so it stays in cash most of the time and only
trades multi-hour trends:

- **Enter long** when EMA(20) is above EMA(60), the close is above
  EMA(240), the close is more than 1.1% above the close 200 minutes ago,
  and at least 120 bars have passed since the last exit.
- **Exit** when EMA(20) crosses back below EMA(60) and stays there for a
  second consecutive close, or the close falls 1.2% below the entry fill
  (stop-loss), or rises 8% above it (take-profit).
- Full-size positions (up to 100% of cash). At most a few trades per day.

## Validation

- `grade-local` on the published dev window: **net PnL +$1.69** (2 trades,
  max drawdown 0.75%), deterministic across two runs (action logs match).
- Out-of-sample backtest on public Coinbase BTC-USD 1-minute candles with
  the same fee/slippage model: 19 Nov 2026 −$4.36, 20 Nov 2026 −$2.60,
  21 Nov 2026 +$20.52, 22 Nov 2026 +$8.44.

## How to run

Requires Python 3.13 (standard library only, no dependencies, no network).

```sh
python3 main.py < bars.ndjson > replies.ndjson
```

The bot reads `agent-fun/bars-replay/v1` messages (hello, batch, done) as
newline-delimited JSON on stdin and writes exactly one reply per batch on
stdout. It is deterministic: no clock, no randomness, no network, no files
outside the repository — two runs on the same bars produce identical replies.

## License

MIT — see [LICENSE](LICENSE).
