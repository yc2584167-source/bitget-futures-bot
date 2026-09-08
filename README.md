# Bitget Futures Trading Bot — GitHub Actions Edition

## What this is
A single-run version of the multi-signal (RSI + Volume + News sentiment)
Bitget trading bot, designed to be triggered every 5 minutes by GitHub
Actions instead of running as a continuous process. State (position,
entry price, error count) is persisted in `state.json`, committed back
to the repo after each run.

**This version trades USDT-M perpetual futures only.** It has no spot
trading code path, and it never calls any transfer or withdrawal
endpoint — it will not move funds between your Bitget wallets. You must
manually transfer USDT into your Bitget **Futures wallet** yourself,
through Bitget's own app or website, before enabling live trading.
Futures use leverage by nature: losses can exceed your futures margin
balance, and the exchange can force-liquidate the position.

When a position opens, the bot attaches take-profit and stop-loss
directly on the exchange (not just tracked internally), so the exit
still executes even if a scheduled run is delayed or skipped.

## Setup

1. Create a **public** GitHub repo (private repos get only 2,000 free
   Actions minutes/month — a 5-minute schedule uses ~8,640 min/month,
   well over that cap).
2. Add these files to the repo root:
   - `bitget_bot_single_run.py`
   - `state.json.example` — rename/copy to `state.json` for the first run
     (or let the script auto-create it on first execution)
   - `.github/workflows/trading-bot.yml`
3. Go to **Settings → Secrets and variables → Actions** and add:
   - `BITGET_API_KEY`
   - `BITGET_API_SECRET`
   - `BITGET_API_PASSWORD` (Bitget's "passphrase")
   - `CRYPTOPANIC_API_TOKEN` (optional — free tier at cryptopanic.com/developers/api/)
4. Commit and push. The workflow will start running automatically on
   its schedule, or you can trigger it manually from the **Actions** tab
   via "Run workflow" (workflow_dispatch).

## Important notes

- **Public repo = public code.** Anyone can read your strategy and
  thresholds. Your API keys stay safe in Secrets, but don't put anything
  else sensitive in the repo.
- **5-minute granularity**, not continuous. Fine for RSI/volume/news
  signals on a 5m timeframe; not suited to strategies needing tick-level
  reaction time.
- **Defaults to dry-run.** `LIVE_TRADING = False` in the script. Watch
  a few days of `state.json` history and Action logs before considering
  flipping it to `True` — and even then, this is not a recommendation
  to trade real money.
- **Futures trading uses leverage by nature.** It is capped in the
  script (see `MAX_ALLOWED_LEVERAGE`), but losses can still exceed
  your futures wallet balance and positions can be force-liquidated
  by the exchange. This isn't financial advice — treat any live use
  as your own decision and risk.
- **No automatic fund transfers, ever.** The bot only reads and trades
  against your Bitget Futures wallet balance. If that balance is $0,
  it will simply skip trades (or fail min-notional checks) rather than
  pull funds from spot. Move USDT into Futures yourself first.
- If a run fails 5 times in a row, the bot writes a cooldown timestamp
  into `state.json` and skips trading logic on subsequent runs until
  that period passes — this prevents hammering the API during an
  outage.

## Files in this bundle
- `bitget_bot_single_run.py` — the bot itself
- `trading-bot.yml` — move to `.github/workflows/trading-bot.yml`
- `state.json.example` — starting state template
